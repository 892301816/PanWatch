"""TickFlow 日K provider(官方 SDK)。

移植自 Tick Stock Panel v0.3.8 的 ``data_providers/tickflow_provider.py``(MIT)。
SDK 返回列式 dict(``timestamp`` 毫秒 + OHLCV),这里转成 pandas 并统一日期口径。

分档:无 Key / Free → ``TickFlow.free()``(仅单标的历史日K);Starter+ → 批量。
批量失败时自动把批大小减半重试,避免一次性被拒后整轮失败。
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from src.platform.dataset.providers import register
from src.platform.dataset.providers.base import (
    BaseDailyProvider,
    ProgressFn,
    empty_daily,
    finalize_daily,
)
from src.platform.dataset.symbols import to_exchange_code, to_std

logger = logging.getLogger(__name__)

_BATCH = 100
_INTERVAL_S = 0.1
_HARD_LIMIT = 10000


def _sdk_available() -> tuple[bool, str]:
    try:
        import tickflow  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"未安装 tickflow SDK: {exc}"
    return True, "ok"


@register
class TickFlowDailyProvider(BaseDailyProvider):
    name = "tickflow"
    datasets = {"daily", "adj_factor", "minute", "realtime"}
    api_key_env = "TICKFLOW_API_KEY"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self._client = None

    def _get_client(self):
        if self._client is None:
            from tickflow import TickFlow

            key = self.api_key()
            self._client = TickFlow(api_key=key) if key else TickFlow.free()
        return self._client

    def available(self) -> tuple[bool, str]:
        ok, detail = _sdk_available()
        if not ok:
            return False, detail
        return True, "ok（有 Key 走付费端点，无 Key 走 Free 端点）"

    def probe(self) -> tuple[bool, str]:
        ok, detail = _sdk_available()
        if not ok:
            return False, detail
        try:
            df = self._fetch_chunk(["600000"], 10)
            return True, f"ok({len(df)} 行)"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:200]

    # ---- 单批 ----
    def _fetch_chunk(self, symbols: list[str], count: int) -> pd.DataFrame:
        client = self._get_client()
        raw = client.klines.batch(
            [to_exchange_code(s) for s in symbols],
            period="1d",
            adjust="none",
            count=count,
            as_dataframe=False,
            show_progress=False,
        )
        if not isinstance(raw, dict):
            return empty_daily()

        frames: list[pd.DataFrame] = []
        items = [(None, raw)] if "timestamp" in raw else list(raw.items())
        for key, payload in items:
            if not isinstance(payload, dict):
                continue
            ts = payload.get("timestamp") or []
            if not ts:
                continue
            frame = pd.DataFrame(
                {
                    "symbol": [to_std(key or "")] * len(ts),
                    "timestamp": ts,
                }
            )
            for field in ("open", "high", "low", "close", "volume", "amount"):
                values = payload.get(field)
                if values is not None:
                    frame[field] = values
            frames.append(frame)
        if not frames:
            return empty_daily()

        df = pd.concat(frames, ignore_index=True)
        df["trade_date"] = (
            pd.to_datetime(pd.to_numeric(df["timestamp"], errors="coerce"), unit="ms", utc=True)
            .dt.tz_convert("Asia/Shanghai")
            .dt.strftime("%Y-%m-%d")
        )
        return finalize_daily(df)

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

        span = max(1, (end - start).days)
        count = min(_HARD_LIMIT, max(30, int(span * 5 / 7) + 20))

        batch_size = _BATCH
        frames: list[pd.DataFrame] = []
        total = max(1, (len(syms) + batch_size - 1) // batch_size)
        index = 0
        while index < len(syms):
            chunk = syms[index:index + batch_size]
            try:
                seg = self._fetch_chunk(chunk, count)
            except Exception as exc:  # noqa: BLE001
                if batch_size > 10:
                    batch_size = max(10, batch_size // 2)
                    logger.warning(f"[数据源] TickFlow 批量 {len(chunk)} 失败,降到 {batch_size}: {exc}")
                    continue
                logger.warning(f"[数据源] TickFlow 批失败: {exc}")
                seg = empty_daily()
            if len(seg):
                seg = seg[
                    seg["trade_date"].between(start.isoformat(), end.isoformat())
                ]
                if len(seg):
                    frames.append(seg)
            index += len(chunk)
            if progress:
                done = min(total, max(1, index // max(1, batch_size)))
                progress(f"TickFlow 批量 {done}/{total}", done, total)
            time.sleep(_INTERVAL_S)

        return finalize_daily(pd.concat(frames, ignore_index=True)) if frames else empty_daily()
