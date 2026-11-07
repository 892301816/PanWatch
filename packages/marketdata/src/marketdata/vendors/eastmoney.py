"""东财 CN 报价 vendor(quote 第二源)。push2 stock/get,单只查询,逐只循环取批量。

字段映射经交叉核对 akshare `stock_ask_bid_em.py`(同一 push2 stock/get 端点,
fltt=2 预格式化模式下的字段含义)+ 本仓 kline.py/capital_flow.py 东财现有惯例。
本 vendor 不传 fltt/invt,取原始未格式化值,价格类字段需 /10^f59 还原,
百分比类字段(涨跌幅/换手率/量比)固定 /100 还原。
"""

from __future__ import annotations

import logging

from marketdata.http import market_get
from marketdata.symbol import Market, Symbol
from marketdata.types import Quote
from marketdata.vendors.base import QuoteVendor

logger = logging.getLogger(__name__)

_URL = "https://push2.eastmoney.com/api/qt/stock/get"
_INDEX_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
_HOST = "push2.eastmoney.com"
_MIN_INTERVAL_S = 0.2
# f43 最新价 / f44 最高 / f45 最低 / f46 今开 / f47 成交量 / f48 成交额 / f50 量比 /
# f55(备用,CN 主用 f168) / f57 代码 / f58 名称 / f59 小数位数 / f60 昨收 /
# f116 总市值 / f117 流通市值 / f168 换手率 / f169 涨跌额 / f170 涨跌幅 / f171 振幅(未映射)
_FIELDS = "f43,f44,f45,f46,f47,f48,f50,f55,f57,f58,f59,f60,f116,f117,f168,f169,f170,f171"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}


def _to_float(value) -> float | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _scaled(value, decimals: int) -> float | None:
    """价格类字段还原:raw / 10^decimals。"""
    v = _to_float(value)
    if v is None:
        return None
    try:
        return v / (10 ** decimals)
    except Exception:
        return None


def _pct(value) -> float | None:
    """百分比类字段还原:raw / 100(涨跌幅/换手率/量比,与小数位数无关)。"""
    v = _to_float(value)
    if v is None:
        return None
    return v / 100


def _parse_one(data: dict | None, market: str, fallback_code: str) -> Quote | None:
    if not data or data.get("f43") is None:
        return None
    dec = int(_to_float(data.get("f59")) or 2)
    price = _scaled(data.get("f43"), dec)
    if price is None or price <= 0:
        return None

    turnover_rate = _pct(data.get("f168"))
    if turnover_rate is None:
        turnover_rate = _pct(data.get("f55"))

    total_mv = _to_float(data.get("f116"))
    circ_mv = _to_float(data.get("f117"))

    return Quote(
        symbol=str(data.get("f57") or fallback_code),
        market=market,
        name=str(data.get("f58") or ""),
        current_price=price,
        prev_close=_scaled(data.get("f60"), dec),
        open_price=_scaled(data.get("f46"), dec),
        high_price=_scaled(data.get("f44"), dec),
        low_price=_scaled(data.get("f45"), dec),
        change_amount=_scaled(data.get("f169"), dec),
        change_pct=_pct(data.get("f170")),
        volume=_to_float(data.get("f47")),
        turnover=_to_float(data.get("f48")),
        turnover_rate=turnover_rate,
        volume_ratio=_pct(data.get("f50")),
        pe_ratio=None,  # 未确认稳定字段(f162 猜测,未经真实响应验证),宁缺毋错
        circulating_market_value=(circ_mv / 1e8) if circ_mv is not None else None,
        total_market_value=(total_mv / 1e8) if total_mv is not None else None,
    )


# f2 最新价 / f3 涨跌幅 / f4 涨跌额 / f12 代码 / f14 名称 / f18 昨收
_INDEX_FIELDS = "f2,f3,f4,f12,f14,f18"
# 网页端公开的固定 ut(非用户凭证)
_UT = "fa5fd1943c7b386f172d6893dbfba10b"


def fetch_index_quotes(secids: list[str], *, timeout: float = 8) -> list[dict]:
    """全球指数行情(东财 ``ulist.np``,一次请求多个 secid)。

    腾讯 ``fetch_raw`` 只覆盖 A 股/港股/美股指数,日经、韩国综合这类亚洲指数
    只能走东财。返回结构与 ``index_quotes`` 保持一致,调用方按 ``symbol`` 匹配。

    指数报价固定两位小数,故价格类字段一律 /100(与个股的 f59 小数位无关)。
    """
    if not secids:
        return []
    payload = market_get(
        _INDEX_URL,
        host_key=_HOST,
        min_interval_s=_MIN_INTERVAL_S,
        params={"secids": ",".join(secids), "fields": _INDEX_FIELDS, "ut": _UT},
        headers=_HEADERS,
        timeout=timeout,
        retries=1,
        parse="json",
        log_label="东财指数",
    )
    rows = ((payload or {}).get("data") or {}).get("diff") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    out: list[dict] = []
    for row in rows:
        code = str(row.get("f12") or "").strip()
        price = _scaled(row.get("f2"), 2)
        if not code or price is None or price <= 0:
            continue
        out.append(
            {
                "symbol": code,
                "name": str(row.get("f14") or "").strip(),
                "current_price": price,
                "change_pct": _pct(row.get("f3")),
                "change_amount": _scaled(row.get("f4"), 2),
                "prev_close": _scaled(row.get("f18"), 2),
            }
        )
    return out


class EastmoneyQuoteVendor(QuoteVendor):
    name = "eastmoney"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Quote]:
        if not symbols:
            return []
        out: list[Quote] = []
        for sym in symbols:
            if sym.market != Market.CN:
                continue
            payload = market_get(
                _URL,
                host_key=_HOST,
                min_interval_s=_MIN_INTERVAL_S,
                params={"secid": sym.to_eastmoney_secid(), "fields": _FIELDS},
                headers=_HEADERS,
                timeout=8,
                retries=2,
                parse="json",
                log_label="东财报价",
                symbol=sym.code,
            )
            if not payload:
                continue
            data = payload.get("data") if isinstance(payload, dict) else None
            q = _parse_one(data, sym.market.value, sym.code)
            if q:
                out.append(q)
        return out
