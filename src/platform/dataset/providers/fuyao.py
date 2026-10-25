"""扶摇(同花顺金融数据 API)日K provider。

移植自 Tick Stock Panel v0.3.8 的 ``plugins/fuyao``(MIT),只保留日K相关部分,
存储层改成了 pandas + SQLite。

取数三档(逐级降级):
  1. 近端窗口(≤12 天): ``daily-k-10d`` dump(约 1MB),1 次请求覆盖全市场;
  2. 深窗口: ``daily-k`` 十年全量 dump(约 172MB),下载一次后按覆盖起点复用,
     尾部缺口用 10d dump 补;
  3. 兜底: 单标的 ``/api/a-share/prices/historical``,50 只/批,节流 0.12s。

单位口径:扶摇 volume 是股、turnover 是元;本层统一成「手 / 元」。
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pandas as pd

from src.platform.dataset.providers import register
from src.platform.dataset.providers.base import (
    BaseDailyProvider,
    ProgressFn,
    empty_daily,
    finalize_daily,
)
from src.platform.dataset.symbols import to_exchange_code, to_std
from src.platform.dataset.store import cache_dir

logger = logging.getLogger(__name__)

BASE_URL = "https://fuyao.aicubes.cn"
_RECENT_DUMP_DAYS = 12
_HIST_SYMBOL_BATCH = 50
_HIST_INTERVAL_S = 0.12
_DUMP_BATCH_ROWS = 100_000
_SH_MS = 28_800_000  # 北京零点 → UTC 前一日 16:00

_DUMP_KIND_10D = "daily-k-10d"
_DUMP_KIND_BIG = "daily-k"
_DUMP_COLUMNS = [
    "thscode", "adjusted", "date_ms", "open_price", "high_price", "low_price",
    "close_price", "volume", "turnover",
]


class FuyaoError(Exception):
    pass


class FuyaoClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL, timeout: float = 30.0) -> None:
        if not api_key:
            raise FuyaoError("未配置 fuyao API Key")
        self._http = httpx.Client(
            base_url=base_url,
            headers={"X-api-key": api_key},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            resp = self._http.get(path, params=params or {})
        except httpx.HTTPError as exc:
            raise FuyaoError(f"网络请求失败: {exc}") from exc
        if resp.status_code != 200:
            raise FuyaoError(f"HTTP {resp.status_code}: {path}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise FuyaoError(f"响应不是 JSON: {path}") from exc
        code = payload.get("code")
        if code not in (0, "0", None):
            raise FuyaoError(f"扶摇接口错误 code={code}: {payload.get('message', '')}")
        return payload.get("data") or {}

    # ---- 探活 ----
    def snapshot_page(self, limit: int = 1, offset: int = 0) -> list[dict]:
        data = self._get(
            "/api/a-share/prices/snapshot", {"limit": limit, "offset": offset}
        )
        rows = data.get("item")
        if not isinstance(rows, list):
            rows = data.get("data") if isinstance(data.get("data"), list) else []
        return rows

    # ---- 单标的日K ----
    def historical_kline(self, thscode: str, start_ms: int, end_ms: int) -> list[dict]:
        data = self._get(
            "/api/a-share/prices/historical",
            {
                "thscode": thscode,
                "interval": "1d",
                "adjust": "none",
                "start": int(start_ms),
                "end": int(end_ms),
            },
        )
        rows = data.get("item")
        return rows if isinstance(rows, list) else []

    # ---- dump ----
    def dump_download_url(self, kind: str) -> dict:
        return self._get(f"/api/dump/market-dumps/{kind}/download-url", {})

    def download_dump(self, kind: str, dest: Path) -> Path:
        url = str(self.dump_download_url(kind).get("presigned_url") or "")
        if not url:
            raise FuyaoError(f"dump {kind} 未返回预签名 URL")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        try:
            with httpx.stream("GET", url, timeout=900.0, follow_redirects=True) as resp:
                if resp.status_code != 200:
                    raise FuyaoError(f"dump {kind} 下载失败 HTTP {resp.status_code}")
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_bytes(1 << 20):
                        fh.write(chunk)
            tmp.replace(dest)
        except httpx.HTTPError as exc:
            raise FuyaoError(f"dump {kind} 下载网络失败: {exc}") from exc
        finally:
            tmp.unlink(missing_ok=True)
        return dest


def _release_of(url: str) -> str:
    match = re.search(r"releases/(\d{8})", url or "")
    return match.group(1) if match else "latest"


def _date_to_ms(d: date) -> int:
    return int(
        datetime(d.year, d.month, d.day, tzinfo=timezone(timedelta(hours=8))).timestamp()
        * 1000
    )


def _ms_to_date_str(values: pd.Series) -> pd.Series:
    return (
        pd.to_datetime(values.astype("int64"), unit="ms", utc=True)
        .dt.tz_convert("Asia/Shanghai")
        .dt.strftime("%Y-%m-%d")
    )


@register
class FuyaoDailyProvider(BaseDailyProvider):
    name = "fuyao"
    datasets = {"daily", "adj_factor", "realtime"}
    api_key_env = "FUYAO_API_KEY"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self._client: FuyaoClient | None = None
        self._small_dump: dict[str, pd.DataFrame] = {}
        self._path_memo: dict[str, Path] = {}

    # ---- 客户端 ----
    def _get_client(self) -> FuyaoClient:
        if self._client is None:
            self._client = FuyaoClient(self.api_key(), self.base_url(BASE_URL) or BASE_URL)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    def available(self) -> tuple[bool, str]:
        if not self.api_key():
            return False, f"缺少 API Key(环境变量 {self.api_key_env} 或页面配置)"
        return True, "ok"

    def probe(self) -> tuple[bool, str]:
        try:
            rows = self._get_client().snapshot_page(limit=1)
            return True, f"ok({len(rows)} 行快照)"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:200]

    # ---- dump 缓存 ----
    def _ensure_dump_path(self, kind: str, prefix: str) -> Path:
        memo = self._path_memo.get(kind)
        if memo is not None and memo.exists():
            return memo
        client = self._get_client()
        info = client.dump_download_url(kind)
        release = _release_of(str(info.get("presigned_url") or ""))
        dest = cache_dir("fuyao") / f"{prefix}__{release}.parquet"
        if not dest.exists():
            client.download_dump(kind, dest)
            for old in dest.parent.glob(f"{prefix}__*.parquet"):
                if old.name != dest.name:
                    old.unlink(missing_ok=True)
        self._path_memo[kind] = dest
        return dest

    def _ensure_small_dump(self, kind: str, prefix: str) -> pd.DataFrame:
        memo = self._small_dump.get(kind)
        if memo is not None:
            return memo
        df = pd.read_parquet(self._ensure_dump_path(kind, prefix))
        self._small_dump[kind] = df
        return df

    def _big_dump_path(self) -> Path | None:
        cached = sorted(cache_dir("fuyao").glob("daily_k__*.parquet"), reverse=True)
        if cached:
            return cached[0]
        try:
            return self._ensure_dump_path(_DUMP_KIND_BIG, "daily_k")
        except FuyaoError as exc:
            logger.warning(f"[数据源] 扶摇十年 dump 不可用,回退单标的: {exc}")
        return None

    # ---- 映射 ----
    @staticmethod
    def _map_frame(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or len(df) == 0:
            return empty_daily()
        out = df.copy()
        if "adjusted" in out.columns:
            out = out[out["adjusted"].astype(str).str.lower() == "none"]
        if "thscode" in out.columns:
            out["symbol"] = out["thscode"].map(to_std)
        out["trade_date"] = _ms_to_date_str(out["date_ms"])
        vol = pd.to_numeric(out["volume"], errors="coerce") if "volume" in out.columns else pd.Series(float("nan"), index=out.index)
        amt = pd.to_numeric(out["turnover"], errors="coerce") if "turnover" in out.columns else pd.Series(float("nan"), index=out.index)
        out["volume"] = vol / 100.0
        out["amount"] = amt
        out = out.rename(
            columns={
                "open_price": "open",
                "high_price": "high",
                "low_price": "low",
                "close_price": "close",
            }
        )
        out = out[out["volume"].fillna(0) > 0]  # 停牌日
        return finalize_daily(out)

    def _filter(
        self, df: pd.DataFrame, symset: set[str], start: date, end: date
    ) -> pd.DataFrame:
        if df is None or len(df) == 0:
            return empty_daily()
        out = df.copy()
        if "date_ms" in out.columns:
            out["trade_date"] = _ms_to_date_str(out["date_ms"])
        mask = (
            out["trade_date"].between(start.isoformat(), end.isoformat())
            & out["thscode"].map(to_std).isin(symset)
        )
        return self._map_frame(out[mask])

    def _read_big_dump(
        self, path: Path, symset: set[str], start: date, end: date, progress: ProgressFn | None
    ) -> pd.DataFrame:
        import pyarrow.parquet as pq

        parquet = pq.ParquetFile(path)
        columns = [c for c in _DUMP_COLUMNS if c in parquet.schema.names]
        total_batches = max(1, (parquet.metadata.num_rows or 0) // _DUMP_BATCH_ROWS + 1)
        frames: list[pd.DataFrame] = []
        for index, batch in enumerate(
            parquet.iter_batches(batch_size=_DUMP_BATCH_ROWS, columns=columns), start=1
        ):
            raw = batch.to_pandas()
            if len(raw) == 0:
                continue
            raw["trade_date"] = _ms_to_date_str(raw["date_ms"])
            mask = (
                raw["trade_date"].between(start.isoformat(), end.isoformat())
                & raw["thscode"].map(to_std).isin(symset)
            )
            seg = raw[mask]
            if len(seg):
                frames.append(self._map_frame(seg))
            if progress and index % 10 == 0:
                progress(f"读取全量日K dump {index}/{total_batches}", index, total_batches)
        return finalize_daily(pd.concat(frames, ignore_index=True)) if frames else empty_daily()

    def _fetch_api(
        self, symbols: list[str], start: date, end: date, progress: ProgressFn | None
    ) -> pd.DataFrame:
        client = self._get_client()
        start_ms, end_ms = _date_to_ms(start), _date_to_ms(end)
        frames: list[pd.DataFrame] = []
        total = max(1, (len(symbols) + _HIST_SYMBOL_BATCH - 1) // _HIST_SYMBOL_BATCH)
        for index in range(total):
            chunk = symbols[index * _HIST_SYMBOL_BATCH:(index + 1) * _HIST_SYMBOL_BATCH]
            for code in chunk:
                try:
                    rows = client.historical_kline(to_exchange_code(code), start_ms, end_ms)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[数据源] 扶摇单标的 {code} 失败: {exc}")
                    continue
                if not rows:
                    continue
                seg = pd.DataFrame(rows)
                if len(seg):
                    seg["thscode"] = to_exchange_code(code)
                    frames.append(self._map_frame(seg))
                time.sleep(_HIST_INTERVAL_S)
            if progress:
                progress(f"单标的日K {index + 1}/{total} 批", index + 1, total)
        return finalize_daily(pd.concat(frames, ignore_index=True)) if frames else empty_daily()

    # ---- 对外 ----
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
        symset = set(syms)

        # 1) 近端窗口:10d dump 一次覆盖
        if (end - start).days <= _RECENT_DUMP_DAYS:
            try:
                if progress:
                    progress("下载近端 10 日 dump", 0, 1)
                dump = self._ensure_small_dump(_DUMP_KIND_10D, "daily_k_10d")
                df = self._filter(dump, symset, start, end)
                if len(df):
                    return df
            except FuyaoError as exc:
                logger.warning(f"[数据源] 扶摇 10d dump 不可用: {exc}")

        # 2) 深窗口:十年 dump
        big_path = self._big_dump_path()
        parts: list[pd.DataFrame] = []
        if big_path is not None:
            dump_min, dump_max = self._dump_range(big_path)
            if dump_min and dump_max and start <= dump_max:
                hist_end = min(end, dump_max)
                if progress:
                    progress("读取十年全量 dump", 0, 1)
                parts.append(self._read_big_dump(big_path, symset, start, hist_end, progress))
                if end > dump_max:
                    tail_start = dump_max + timedelta(days=1)
                    try:
                        ten = self._ensure_small_dump(_DUMP_KIND_10D, "daily_k_10d")
                        parts.append(self._filter(ten, symset, tail_start, end))
                    except FuyaoError as exc:
                        logger.warning(f"[数据源] 扶摇尾部 10d dump 不可用: {exc}")
            else:
                # dump 还没覆盖到窗口起点(或窗口整体比 dump 新) → 单标的接口
                parts.append(self._fetch_api(syms, start, end, progress))
        else:
            parts.append(self._fetch_api(syms, start, end, progress))

        parts = [p for p in parts if p is not None and len(p)]
        return finalize_daily(pd.concat(parts, ignore_index=True)) if parts else empty_daily()

    @staticmethod
    def _dump_range(path: Path) -> tuple[date | None, date | None]:
        try:
            import pyarrow.parquet as pq

            table = pq.read_table(path, columns=["date_ms"])
            series = table.column("date_ms").to_pandas()
            dates = pd.to_datetime(series.astype("int64"), unit="ms", utc=True).dt.tz_convert(
                "Asia/Shanghai"
            )
            return dates.min().date(), dates.max().date()
        except Exception:  # noqa: BLE001
            return None, None
