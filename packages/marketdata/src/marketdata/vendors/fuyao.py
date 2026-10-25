"""扶摇(同花顺)vendors:日K + 实时行情。

移植自 Tick Stock Panel v0.3.8(MIT)。这里只实现市场数据包需要的两个面:
- ``FuyaoKlineVendor``:单标的日K(原始价),分钟/分时不支持 → 返回空,由 Engine 转移;
- ``FuyaoQuoteVendor``:按 thscodes 批量快照(单次上限 100)。
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone

from marketdata.http import market_get
from marketdata.symbol import Market, Symbol
from marketdata.types import Bar, Quote
from marketdata.vendors.base import KlineVendor, QuoteVendor
from marketdata.vendors.kline import INTRADAY_PERIOD, MINUTE_PERIODS, _days, requested_period

BASE_URL = "https://fuyao.aicubes.cn"
_QUOTE_BATCH = 100
_MARKET_CODE = {"SH": "SH", "SZ": "SZ", "BJ": "BJ"}


def _api_key(config: dict) -> str:
    return str((config or {}).get("api_key") or os.environ.get("FUYAO_API_KEY", "")).strip()


def _exchange_code(sym: Symbol) -> str:
    tsym = sym.to_tencent()  # sh600000 / sz000001 / bj920285
    return f"{tsym[2:]}.{tsym[:2].upper()}"


def _date_to_ms(d: date) -> int:
    return int(
        datetime(d.year, d.month, d.day, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000
    )


def _ms_to_date(value) -> str:
    try:
        ts = int(value) / 1000.0
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(
        timezone(timedelta(hours=8))
    ).strftime("%Y-%m-%d")


def _get(api_key: str, path: str, params: dict):
    data = market_get(
        BASE_URL + path,
        host_key="fuyao.aicubes.cn",
        params=params,
        headers={"X-api-key": api_key},
        parse="json",
        timeout=20,
        retries=1,
        min_interval_s=0.1,
        log_label="扶摇",
    )
    if not isinstance(data, dict):
        return {}
    code = data.get("code")
    if code not in (0, "0", None):
        raise RuntimeError(f"扶摇 code={code}: {data.get('message', '')}")
    return data.get("data") or {}


class FuyaoKlineVendor(KlineVendor):
    name = "fuyao"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Bar]:
        if not symbols:
            return []
        if requested_period(config) in MINUTE_PERIODS or requested_period(config) == INTRADAY_PERIOD:
            return []  # 扶摇未接入分钟,交给下一个源
        key = _api_key(config)
        if not key:
            return []
        # 上层常为缓存/回测放大到几千天;扶摇单次窗口上限 10 年,这里必须钳住,
        # 否则 1.6 倍后超过 3650 天会直接 code=1003 报错。
        days = min(max(1, _days(config, 60)), 2400)
        end = date.today()
        start = end - timedelta(days=min(3500, max(30, int(days * 1.6) + 20)))
        rows = _get(
            key,
            "/api/a-share/prices/historical",
            {
                "thscode": _exchange_code(symbols[0]),
                "interval": "1d",
                "adjust": "none",
                "start": _date_to_ms(start),
                "end": _date_to_ms(end),
            },
        ).get("item") or []
        out: list[Bar] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            d = _ms_to_date(row.get("date_ms"))
            close = _num(row.get("close_price"))
            if not d or close is None or close <= 0:
                continue
            vol = _num(row.get("volume"))
            out.append(
                Bar(
                    date=d,
                    open=_num(row.get("open_price")) or close,
                    close=close,
                    high=_num(row.get("high_price")) or close,
                    low=_num(row.get("low_price")) or close,
                    volume=(vol or 0.0) / 100.0,
                )
            )
        return out[-days:] if len(out) > days else out


class FuyaoQuoteVendor(QuoteVendor):
    name = "fuyao"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Quote]:
        if not symbols:
            return []
        key = _api_key(config)
        if not key:
            return []
        out: list[Quote] = []
        for start in range(0, len(symbols), _QUOTE_BATCH):
            chunk = symbols[start:start + _QUOTE_BATCH]
            data = _get(
                key,
                "/api/a-share/prices/snapshot",
                {"thscodes": ",".join(_exchange_code(s) for s in chunk)},
            )
            for row in data.get("item") or []:
                q = _to_quote(row)
                if q is not None:
                    out.append(q)
            time.sleep(0.05)
        return out


def _num(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


def _to_quote(row: dict) -> Quote | None:
    if not isinstance(row, dict):
        return None
    thscode = str(row.get("thscode") or "")
    if not thscode:
        return None
    code = thscode.split(".")[0]
    price = _num(row.get("last_price"))
    if price is None or price <= 0:
        return None
    pct = _num(row.get("price_change_ratio_pct"))
    volume = _num(row.get("volume"))
    return Quote(
        symbol=code,
        market=Market.CN.value,
        current_price=price,
        name=str(row.get("name") or ""),
        prev_close=_num(row.get("prev_price")),
        open_price=_num(row.get("open_price")),
        high_price=_num(row.get("high_price")),
        low_price=_num(row.get("low_price")),
        change_amount=_num(row.get("price_change")),
        change_pct=pct,
        volume=(volume or 0.0) / 100.0 if volume is not None else None,
        turnover=_num(row.get("turnover")),
        timestamp=datetime.now(),
    )
