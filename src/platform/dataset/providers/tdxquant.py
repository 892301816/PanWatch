"""TdxQuant(通达信量化数据服务)日K provider。

移植自 Tick Stock Panel v0.3.8 的 ``plugins/tdxquant``(MIT),只保留日K。
原生 TCP 协议由 ``tdxaidata`` 包提供(ctypes 包装,随 wheel 分发 .so/.dll)。

口径: Volume 为股 → /100 转手; Amount 为万元 → ×10000 转元; 原始价。
取数: count 模式单标的(上限 2600 根 ≈ 10 年),节流 0.08 秒。
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import shutil
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

from src.platform.dataset.providers import register
from src.platform.dataset.providers.base import (
    BaseDailyProvider,
    ProgressFn,
    empty_daily,
    finalize_daily,
)
from src.platform.dataset.symbols import to_exchange_code, to_std
from src.platform.dataset.store import data_dir

logger = logging.getLogger(__name__)

_HIST_INTERVAL_S = 0.08
_HIST_MAX_COUNT = 2600
_RETRIES = 3
_RETRY_SLEEP_S = 0.6
LIB_ENV = "TDX_AI_DATA_LIB"
_DLL_BY_PLATFORM = {"win32": "TdxAiData.dll", "darwin": "libTdxAiData.dylib"}
LIB_DLL_NAME = _DLL_BY_PLATFORM.get(sys.platform, "libTdxAiData.so")


def _package_lib_dir() -> Path:
    import tdxaidata

    return Path(tdxaidata.__file__).resolve().parent / "lib"


def _write_token_ini(ini: Path, api_key: str) -> None:
    lines = (
        ini.read_text(encoding="utf-8", errors="replace").splitlines()
        if ini.exists()
        else ["[Token]"]
    )
    out: list[str] = []
    in_token = False
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower() == "[token]":
            in_token = True
            out.append(line)
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_token = False
        if in_token and "=" in line and stripped.lower().split("=", 1)[0].strip() == "token":
            if not replaced:
                out.append(f"token={api_key}")
                replaced = True
            continue
        out.append(line)
    if "[Token]" not in {line.strip() for line in out}:
        out.extend(["[Token]", f"token={api_key}"])
    elif not replaced:
        idx = next(i for i, line in enumerate(out) if line.strip().lower() == "[token]")
        out.insert(idx + 1, f"token={api_key}")
    ini.write_text("\n".join(out) + "\n", encoding="utf-8")


def _materialize_lib(api_key: str, lib_root: Path) -> Path:
    src = _package_lib_dir()
    dll = src / LIB_DLL_NAME
    if not dll.exists():
        raise RuntimeError(f"tdxaidata 缺少 {LIB_DLL_NAME}: {src}")
    lib_root.mkdir(parents=True, exist_ok=True)
    stat = dll.stat()
    want = f"{hashlib.sha256(api_key.encode()).hexdigest()}:{stat.st_mtime_ns}:{stat.st_size}"
    marker = lib_root / ".tdxquant_lib_marker"
    copied = lib_root / LIB_DLL_NAME
    if marker.exists() and marker.read_text(encoding="utf-8") == want and copied.exists():
        return copied
    for item in src.iterdir():
        if item.is_file():
            shutil.copy2(item, lib_root / item.name)
    for name in ("TdxAiData.ini", "user.ini"):
        _write_token_ini(lib_root / name, api_key)
    marker.write_text(want, encoding="utf-8")
    return copied


class TdxQuantError(RuntimeError):
    pass


class TdxQuantClient:
    def __init__(self, api_key: str, lib_root: Path | None = None) -> None:
        if not api_key:
            raise TdxQuantError("未配置 TDXQUANT_API_KEY")
        root = lib_root or (data_dir() / "dataset_cache" / "tdxquant_lib")
        dll = _materialize_lib(api_key, root)
        os.environ[LIB_ENV] = str(dll)
        from tdxaidata import tqs
        from tdxaidata.tdxaidata import _NativeTdxAiData

        self._tqs = tqs
        self._native_cls = _NativeTdxAiData
        self._warmup()

    def _warmup(self, attempts: int = 3) -> None:
        for _ in range(attempts):
            try:
                rows = self._tqs.get_market_snapshot_batch(
                    stock_list=["600519.SH"], field_list=["Now"]
                )
                if isinstance(rows, dict) and rows:
                    return
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.0)
        logger.warning("[数据源] 通达信原生层预热未成功,首批调用可能失败")

    def _raw(self, method: str, *args):
        last: Exception = TdxQuantError(f"{method} 未知失败")
        for _ in range(_RETRIES):
            try:
                out = self._tqs._clean(getattr(self._native_cls.get_instance(), method)(*args))
                if out is not None:
                    return out
                last = TdxQuantError(f"{method} 返回空数据")
            except Exception as exc:  # noqa: BLE001
                last = TdxQuantError(f"{method}: {exc}")
            time.sleep(_RETRY_SLEEP_S)
        raise last

    def his_dats(self, code: str, end_d: date, count: int) -> dict:
        if count <= 0:
            return {}
        end = f"{end_d:%Y-%m-%d} 00:00:00"
        out = self._raw("get_his_dats", code, "", end, "1d", "none", int(count))
        return out if isinstance(out, dict) else {}


def _to_float(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


def _compact_date(value) -> date | None:
    text = str(value or "").strip().replace("-", "")
    if len(text) < 8 or not text[:8].isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


@register
class TdxQuantDailyProvider(BaseDailyProvider):
    name = "tdxquant"
    datasets = {"daily", "adj_factor", "realtime", "depth5", "minute"}
    api_key_env = "TDXQUANT_API_KEY"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self._client: TdxQuantClient | None = None

    def _get_client(self) -> TdxQuantClient:
        if self._client is None:
            lib_root = self.config.get("lib_root")
            self._client = TdxQuantClient(
                self.api_key(), Path(lib_root) if lib_root else None
            )
        return self._client

    def available(self) -> tuple[bool, str]:
        try:
            import tdxaidata  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            return False, f"未安装 tdxaidata: {exc}"
        if not self.api_key():
            return False, f"缺少 API Key(环境变量 {self.api_key_env} 或页面配置)"
        return True, "ok"

    def probe(self) -> tuple[bool, str]:
        ok, detail = self.available()
        if not ok:
            return False, detail
        try:
            raw = self._get_client().his_dats("600000.SH", date.today(), 10)
            return True, f"ok({len(raw.get('Date') or [])} 根)"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:200]

    def fetch_daily(
        self,
        symbols: list[str],
        start: date,
        end: date,
        progress: ProgressFn | None = None,
    ) -> pd.DataFrame:
        syms = [to_std(s) for s in symbols if to_std(s)]
        if not syms or start > end:
            return empty_daily()

        count = max(30, min(_HIST_MAX_COUNT, int((end - start).days * 5 / 7) + 20))
        client = self._get_client()
        frames: list[pd.DataFrame] = []

        for index, code in enumerate(syms, start=1):
            try:
                raw = client.his_dats(to_exchange_code(code), end, count)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[数据源] 通达信日K {code} 失败: {exc}")
                raw = {}
            rows = self._rows(code, raw, start, end)
            if rows:
                frames.append(pd.DataFrame(rows))
            if progress and index % 50 == 0:
                progress(f"通达信单标的日K {index}/{len(syms)}", index, len(syms))
            time.sleep(_HIST_INTERVAL_S)

        return finalize_daily(pd.concat(frames, ignore_index=True)) if frames else empty_daily()

    @staticmethod
    def _rows(symbol: str, raw: dict, start: date, end: date) -> list[dict]:
        dates = raw.get("Date") or []

        def col(name: str, i: int):
            values = raw.get(name)
            return _to_float(values[i]) if values and i < len(values) else None

        out: list[dict] = []
        for i, compact in enumerate(dates):
            d = _compact_date(compact)
            if d is None or d < start or d > end:
                continue
            volume = col("Volume", i)
            amount = col("Amount", i)
            out.append(
                {
                    "symbol": symbol,
                    "trade_date": d.isoformat(),
                    "open": col("Open", i),
                    "high": col("High", i),
                    "low": col("Low", i),
                    "close": col("Close", i),
                    "volume": math.floor(volume / 100.0) if volume is not None else None,
                    "amount": amount * 10_000.0 if amount is not None else None,
                }
            )
        return out
