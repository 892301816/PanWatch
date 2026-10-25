import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.modules.research.stock_overview import snapshot_metrics
from src.platform.marketdata.marketdata_client import md_quote_rows
from src.platform.marketdata.models import MarketCode

logger = logging.getLogger(__name__)

router = APIRouter()


class QuoteItem(BaseModel):
    symbol: str = Field(..., description="股票代码")
    market: str = Field(..., description="市场: CN/HK/US")


class QuoteBatchRequest(BaseModel):
    items: list[QuoteItem]


def _parse_market(market: str) -> MarketCode:
    try:
        return MarketCode(market)
    except ValueError:
        raise HTTPException(400, f"不支持的市场: {market}")


def _fill_from_snapshot(quote: dict, market: MarketCode) -> dict:
    """行情源(腾讯/同花顺快照)通常不带估值字段,用本地全市场快照补齐。

    只在字段缺失时补,已有实时值优先。A 股快照的市值单位是「亿」,
    与行情接口返回的「元」不同,这里统一换算成元,保持接口口径一致。
    """
    if market != MarketCode.CN:
        return quote
    symbol = quote.get("symbol") or ""
    if not symbol:
        return quote
    try:
        metrics = snapshot_metrics(symbol)
    except Exception as exc:  # noqa: BLE001 — 补数失败不影响行情返回
        logger.debug(f"快照补数失败 {symbol}: {exc}")
        return quote
    if not metrics:
        return quote

    out = dict(quote)
    if out.get("turnover_rate") is None:
        out["turnover_rate"] = metrics.get("turnover_rate")
    if out.get("pe_ratio") is None:
        out["pe_ratio"] = metrics.get("pe_ttm")
    if out.get("total_market_value") is None and metrics.get("total_mv"):
        out["total_market_value"] = metrics["total_mv"] * 1e8
    if out.get("circulating_market_value") is None and metrics.get("float_mv"):
        out["circulating_market_value"] = metrics["float_mv"] * 1e8
    if out.get("amplitude") is None:
        out["amplitude"] = metrics.get("amplitude")
    return out


def _quote_to_response(symbol: str, market: MarketCode, quote: dict | None) -> dict:
    if quote:
        quote = _fill_from_snapshot(quote, market)
    if not quote:
        return {
            "symbol": symbol,
            "market": market.value,
            "name": None,
            "current_price": None,
            "change_pct": None,
            "change_amount": None,
            "prev_close": None,
            "open_price": None,
            "high_price": None,
            "low_price": None,
            "volume": None,
            "turnover": None,
            "turnover_rate": None,
            "pe_ratio": None,
            "total_market_value": None,
            "circulating_market_value": None,
            "amplitude": None,
        }

    return {
        "symbol": symbol,
        "market": market.value,
        "name": quote.get("name"),
        "current_price": quote.get("current_price"),
        "change_pct": quote.get("change_pct"),
        "change_amount": quote.get("change_amount"),
        "prev_close": quote.get("prev_close"),
        "open_price": quote.get("open_price"),
        "high_price": quote.get("high_price"),
        "low_price": quote.get("low_price"),
        "volume": quote.get("volume"),
        "turnover": quote.get("turnover"),
        "turnover_rate": quote.get("turnover_rate"),
        "pe_ratio": quote.get("pe_ratio"),
        "total_market_value": quote.get("total_market_value"),
        "circulating_market_value": quote.get("circulating_market_value"),
        "amplitude": quote.get("amplitude"),
    }


@router.get("/{symbol}")
async def get_quote(symbol: str, market: str = "CN"):
    """获取单只股票实时行情"""
    market_code = _parse_market(market)
    rows = await asyncio.to_thread(md_quote_rows, [symbol], market_code.value)
    if not rows:
        raise HTTPException(404, "行情不存在")
    quote_map = {item.get("symbol"): item for item in rows}
    quote = quote_map.get(symbol)
    if not quote:
        raise HTTPException(404, "行情不存在")
    return _quote_to_response(symbol, market_code, quote)


@router.post("/batch")
async def get_quotes_batch(payload: QuoteBatchRequest):
    """批量获取股票实时行情"""
    if not payload.items:
        return []

    market_items: dict[MarketCode, list[str]] = {}
    for item in payload.items:
        market_code = _parse_market(item.market)
        market_items.setdefault(market_code, []).append(item.symbol)

    quotes_by_market: dict[MarketCode, dict[str, dict]] = {}
    for market_code, symbols in market_items.items():
        rows = await asyncio.to_thread(md_quote_rows, symbols, market_code.value)
        quotes_by_market[market_code] = {item.get("symbol"): item for item in rows}

    results = []
    for item in payload.items:
        market_code = _parse_market(item.market)
        quote = quotes_by_market.get(market_code, {}).get(item.symbol)
        results.append(_quote_to_response(item.symbol, market_code, quote))

    return results
