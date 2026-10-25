"""TdxQuant(通达信)vendors:日K + 实时行情。

原生 TCP 由 ``tdxaidata`` 提供(ctypes 包装,随 wheel 分发)。未安装/未配 Key 时
返回空,由 Engine 继续下一个源。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from marketdata.symbol import Market, Symbol
from marketdata.types import Bar, Quote
from marketdata.vendors.base import KlineVendor, QuoteVendor
from marketdata.vendors.kline import INTRADAY_PERIOD, MINUTE_PERIODS, _days, requested_period

_DLL_BY_PLATFORM = {"win32": "TdxAiData.dll", "darwin": "libTdxAiData.dylib"}
_DLL_NAME = _DLL_BY_PLATFORM.get(sys.platform, "libTdxAiData.so")
_LIB_ENV = "TDX_AI_DATA_LIB"
_SNAPSHOT_FIELDS = ("Now", "Open", "Max", "Min", "LastClose", "Volume", "Amount")
_RETRIES = 2


def _api_key(config: dict) -> str:
    return str((config or {}).get("api_key") or os.environ.get("TDXQUANT_API_KEY", "")).strip()


def _lib_root(config: dict) -> Path:
    raw = str((config or {}).get("lib_root") or "").strip()
    if raw:
        return Path(raw)
    base = os.environ.get("DATA_DIR", "").strip()
    root = Path(base) if base else Path.home() / ".cache" / "panwatch"
    return root / "dataset_cache" / "tdxquant_lib"


def _write_token(ini: Path, key: str) -> None:
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
                out.append(f"token={key}")
                replaced = True
            continue
        out.append(line)
    if "[Token]" not in {line.strip() for line in out}:
        out.extend(["[Token]", f"token={key}"])
    elif not replaced:
        idx = next(i for i, line in enumerate(out) if line.strip().lower() == "[token]")
        out.insert(idx + 1, f"token={key}")
    ini.write_text("\n".join(out) + "\n", encoding="utf-8")


def _materialize(key: str, root: Path) -> Path:
    import tdxaidata

    src = Path(tdxaidata.__file__).resolve().parent / "lib"
    dll = src / _DLL_NAME
    if not dll.exists():
        raise RuntimeError(f"tdxaidata 缺少 {_DLL_NAME}")
    root.mkdir(parents=True, exist_ok=True)
    stat = dll.stat()
    want = f"{hashlib.sha256(key.encode()).hexdigest()}:{stat.st_mtime_ns}:{stat.st_size}"
    marker = root / ".tdxquant_lib_marker"
    copied = root / _DLL_NAME
    if marker.exists() and marker.read_text(encoding="utf-8") == want and copied.exists():
        return copied
    for item in src.iterdir():
        if item.is_file():
            shutil.copy2(item, root / item.name)
    for name in ("TdxAiData.ini", "user.ini"):
        _write_token(root / name, key)
    marker.write_text(want, encoding="utf-8")
    return copied


class _Client:
    def __init__(self, config: dict) -> None:
        self.key = _api_key(config)
        if not self.key:
            raise RuntimeError("未配置 TDXQUANT API Key")
        dll = _materialize(self.key, _lib_root(config))
        os.environ[_LIB_ENV] = str(dll)
        from tdxaidata import tqs
        from tdxaidata.tdxaidata import _NativeTdxAiData

        self._tqs = tqs
        self._native = _NativeTdxAiData

    def _raw(self, method: str, *args, **kwargs):
        last: Exception | None = None
        for _ in range(_RETRIES):
            try:
                out = self._tqs._clean(getattr(self._native.get_instance(), method)(*args, **kwargs))
                if out is not None:
                    return out
            except Exception as exc:  # noqa: BLE001
                last = exc
            time.sleep(0.3)
        if last:
            raise last
        return None

    def his_dats(self, code: str, end: date, count: int) -> dict:
        out = self._raw("get_his_dats", code, "", f"{end:%Y-%m-%d} 00:00:00", "1d", "none", int(count))
        return out if isinstance(out, dict) else {}

    def snapshot(self, codes: list[str]) -> dict:
        # 快照走 tqs 包装层(原生层的方法签名不同),与 TSP 的 client 保持一致
        last: Exception | None = None
        for _ in range(_RETRIES):
            try:
                out = self._tqs.get_market_snapshot_batch(
                    stock_list=list(codes), field_list=list(_SNAPSHOT_FIELDS)
                )
                if isinstance(out, dict) and out:
                    return out
            except Exception as exc:  # noqa: BLE001
                last = exc
            time.sleep(0.3)
        if last:
            raise last
        return {}


def _client(config: dict) -> _Client | None:
    try:
        import tdxaidata  # noqa: F401
    except Exception:  # noqa: BLE001
        return None
    try:
        return _Client(config)
    except Exception:  # noqa: BLE001 — 没 Key/原生层不可用 → 交给下一个源
        return None


def _exchange_code(sym: Symbol) -> str:
    tsym = sym.to_tencent()
    return f"{tsym[2:]}.{tsym[:2].upper()}"


def _num(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


class TdxQuantKlineVendor(KlineVendor):
    name = "tdxquant"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Bar]:
        if not symbols:
            return []
        if requested_period(config) in MINUTE_PERIODS or requested_period(config) == INTRADAY_PERIOD:
            return []
        client = _client(config)
        if client is None:
            return []
        days = _days(config, 60)
        count = max(30, min(2600, int(days * 1.6) + 20))
        raw = client.his_dats(_exchange_code(symbols[0]), date.today(), count)
        dates = raw.get("Date") or []
        out: list[Bar] = []
        for i, compact in enumerate(dates):
            d = _compact_date(compact)
            close = _num(_at(raw, "Close", i))
            if d is None or close is None or close <= 0:
                continue
            volume = _num(_at(raw, "Volume", i))
            out.append(
                Bar(
                    date=d,
                    open=_num(_at(raw, "Open", i)) or close,
                    close=close,
                    high=_num(_at(raw, "High", i)) or close,
                    low=_num(_at(raw, "Low", i)) or close,
                    volume=(volume or 0.0) / 100.0,
                )
            )
        return out[-days:] if len(out) > days else out


class TdxQuantQuoteVendor(QuoteVendor):
    name = "tdxquant"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Quote]:
        if not symbols:
            return []
        client = _client(config)
        if client is None:
            return []
        rows = client.snapshot([_exchange_code(s) for s in symbols])
        out: list[Quote] = []
        for code, row in rows.items():
            if not isinstance(row, dict):
                continue
            price = _num(row.get("Now"))
            prev = _num(row.get("LastClose"))
            if price is None or price <= 0:
                continue
            pct = None
            if prev not in (None, 0):
                pct = (price - prev) / prev * 100
            volume = _num(row.get("Volume"))  # 已是手
            amount = _num(row.get("Amount"))  # 万元
            out.append(
                Quote(
                    symbol=str(code).split(".")[0],
                    market=Market.CN.value,
                    current_price=price,
                    prev_close=prev,
                    open_price=_num(row.get("Open")),
                    high_price=_num(row.get("Max")),
                    low_price=_num(row.get("Min")),
                    change_amount=(price - prev) if prev not in (None, 0) else None,
                    change_pct=pct,
                    volume=volume,
                    turnover=amount * 10000.0 if amount is not None else None,
                    timestamp=datetime.now(),
                )
            )
        return out


def _compact_date(value) -> str | None:
    text = str(value or "").strip().replace("-", "")
    if len(text) < 8 or not text[:8].isdigit():
        return None
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


def _at(raw: dict, key: str, index: int):
    values = raw.get(key)
    return values[index] if values and index < len(values) else None
