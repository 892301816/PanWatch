"""TickFlow vendors:日K + 实时行情(官方 SDK)。

SDK 未安装或没有权限时返回空,由 Engine 继续下一个源。
Free 档:历史日K 单标的可用、批量需 Starter+;实时行情仅少量标的。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

from marketdata.symbol import Market, Symbol
from marketdata.types import Bar, Quote
from marketdata.vendors.base import KlineVendor, QuoteVendor
from marketdata.vendors.kline import INTRADAY_PERIOD, MINUTE_PERIODS, _days, requested_period

_CN_TZ = timezone(timedelta(hours=8))


def _sdk():
    try:
        from tickflow import TickFlow
    except Exception:  # noqa: BLE001
        return None
    return TickFlow


def _client(config: dict):
    TickFlow = _sdk()
    if TickFlow is None:
        return None
    key = str((config or {}).get("api_key") or os.environ.get("TICKFLOW_API_KEY", "")).strip()
    try:
        return TickFlow(api_key=key) if key else TickFlow.free()
    except Exception:  # noqa: BLE001
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


class TickFlowKlineVendor(KlineVendor):
    name = "tickflow"
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
        code = _exchange_code(symbols[0])
        # Free 档只有单标的 klines.get;批量接口需要 Starter+。单标的走 get 更稳。
        try:
            raw = client.klines.get(
                code,
                period="1d",
                adjust="none",
                count=days,
                as_dataframe=False,
            )
        except Exception:  # noqa: BLE001 — 无权限/网络失败交给下一个源
            try:
                raw = client.klines.batch(
                    [code], period="1d", adjust="none", count=days,
                    as_dataframe=False, show_progress=False,
                )
            except Exception:  # noqa: BLE001
                return []
        payload = None
        if isinstance(raw, dict):
            payload = raw if "timestamp" in raw else next(
                (v for v in raw.values() if isinstance(v, dict)), None
            )
        if not isinstance(payload, dict):
            return []
        ts = payload.get("timestamp") or []
        closes = payload.get("close") or []
        out: list[Bar] = []
        for i, stamp in enumerate(ts):
            try:
                dt = datetime.fromtimestamp(int(stamp) / 1000.0, tz=timezone.utc).astimezone(_CN_TZ)
            except (TypeError, ValueError, OSError):
                continue
            close = _num(closes[i]) if i < len(closes) else None
            if close is None or close <= 0:
                continue
            out.append(
                Bar(
                    date=dt.strftime("%Y-%m-%d"),
                    open=_num(_at(payload, "open", i)) or close,
                    close=close,
                    high=_num(_at(payload, "high", i)) or close,
                    low=_num(_at(payload, "low", i)) or close,
                    volume=_num(_at(payload, "volume", i)) or 0.0,
                )
            )
        return out


class TickFlowQuoteVendor(QuoteVendor):
    name = "tickflow"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Quote]:
        if not symbols:
            return []
        client = _client(config)
        if client is None:
            return []
        try:
            rows = client.quotes.get(symbols=[_exchange_code(s) for s in symbols])
        except Exception:  # noqa: BLE001
            return []
        if isinstance(rows, dict):
            rows = list(rows.values())
        out: list[Quote] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            code = str(row.get("symbol") or row.get("code") or "").split(".")[0]
            price = _num(row.get("last_price") or row.get("price") or row.get("close"))
            if not code or price is None or price <= 0:
                continue
            out.append(
                Quote(
                    symbol=code,
                    market=Market.CN.value,
                    current_price=price,
                    name=str(row.get("name") or ""),
                    prev_close=_num(row.get("prev_close")),
                    open_price=_num(row.get("open")),
                    high_price=_num(row.get("high")),
                    low_price=_num(row.get("low")),
                    change_amount=_num(row.get("change")),
                    change_pct=_num(row.get("change_pct")),
                    volume=_num(row.get("volume")),
                    turnover=_num(row.get("amount") or row.get("turnover")),
                    timestamp=datetime.now(),
                )
            )
        return out


def _at(payload: dict, key: str, index: int):
    values = payload.get(key)
    return values[index] if values and index < len(values) else None
