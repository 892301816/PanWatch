from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, HTTPException
from datetime import datetime

from pydantic import BaseModel, Field

from src.platform.marketdata.collectors.kline_collector import KlineCollector
from src.platform.marketdata.models import MarketCode

router = APIRouter()


class KlineItem(BaseModel):
    symbol: str = Field(..., description="股票代码")
    market: str = Field(..., description="市场: CN/HK/US")
    days: int | None = Field(default=60, description="K线天数")
    interval: str | None = Field(default="1d", description="周期: 1d/1w/1m")


class KlineBatchRequest(BaseModel):
    items: list[KlineItem]


class KlineSummaryItem(BaseModel):
    symbol: str = Field(..., description="股票代码")
    market: str = Field(..., description="市场: CN/HK/US")


class KlineSummaryBatchRequest(BaseModel):
    items: list[KlineSummaryItem]


def _parse_market(market: str) -> MarketCode:
    try:
        return MarketCode(market)
    except ValueError:
        raise HTTPException(400, f"不支持的市场: {market}")


def _serialize_klines(klines) -> list[dict]:
    return [
        {
            "date": k.date,
            "open": k.open,
            "close": k.close,
            "high": k.high,
            "low": k.low,
            "volume": k.volume,
        }
        for k in klines
    ]


def _aggregate_klines(klines, interval: str) -> list:
    """Aggregate daily klines to week/month."""

    iv = (interval or "1d").lower()
    if iv in ("1d", "day", "d"):
        return klines
    if iv not in ("1w", "1m", "week", "month", "w", "m"):
        return klines

    parsed = []
    for k in klines or []:
        try:
            dt = datetime.strptime(k.date, "%Y-%m-%d")
        except Exception:
            continue
        parsed.append((dt, k))

    parsed.sort(key=lambda x: x[0])
    buckets: dict[str, list] = {}
    for dt, k in parsed:
        if iv in ("1w", "week", "w"):
            y, w, _ = dt.isocalendar()
            key = f"{y:04d}-W{w:02d}"
        else:
            key = f"{dt.year:04d}-{dt.month:02d}"
        buckets.setdefault(key, []).append((dt, k))

    out = []
    for _, items in buckets.items():
        items.sort(key=lambda x: x[0])
        first = items[0][1]
        last = items[-1][1]
        high = max(it[1].high for it in items)
        low = min(it[1].low for it in items)
        vol = sum(it[1].volume for it in items)
        out.append(
            type(first)(
                date=items[-1][0].strftime("%Y-%m-%d"),
                open=first.open,
                close=last.close,
                high=high,
                low=low,
                volume=vol,
            )
        )
    out.sort(key=lambda k: k.date)
    return out


@router.get("/{symbol}")
def get_klines(symbol: str, market: str = "CN", days: int = 60, interval: str = "1d"):
    """获取单只股票K线数据"""
    market_code = _parse_market(market)
    collector = KlineCollector(market_code)
    klines = collector.get_klines(symbol, days=days)
    klines = _aggregate_klines(klines, interval)
    return {
        "symbol": symbol,
        "market": market_code.value,
        "days": days,
        "interval": interval,
        "klines": _serialize_klines(klines),
    }


@router.post("/batch")
def get_klines_batch(payload: KlineBatchRequest):
    """批量获取K线数据"""
    if not payload.items:
        return []

    results = []
    for item in payload.items:
        market_code = _parse_market(item.market)
        collector = KlineCollector(market_code)
        days = item.days or 60
        interval = item.interval or "1d"
        klines = collector.get_klines(item.symbol, days=days)
        klines = _aggregate_klines(klines, interval)
        results.append(
            {
                "symbol": item.symbol,
                "market": market_code.value,
                "days": days,
                "interval": interval,
                "klines": _serialize_klines(klines),
            }
        )

    return results


# A 股连续竞价时段。腾讯分时接口在 15:00 之后还会带出 15:06–15:30 的填充点
# (价格与量都不再变化),留着会把分时图的横轴拉长 25/242,走势被压扁。
_SESSION_RANGES: tuple[tuple[str, str], ...] = (("0930", "1130"), ("1300", "1500"))


def _in_session(t: str) -> bool:
    return any(lo <= t <= hi for lo, hi in _SESSION_RANGES)


@router.get("/{symbol}/minute")
def get_minute_klines(
    symbol: str, market: str = "CN", period: str = "m5", count: int = 320
):
    """分钟K线(m1/m5/m15/m30/m60)。

    数据源为腾讯,**不带复权**(该端点无复权参数),与日线的前复权口径不同:
    看当日/近几日形态无影响,跨除权日做对比时需注意。
    """
    from marketdata.vendors.kline import MINUTE_PERIODS

    market_code = _parse_market(market)
    period = (period or "m5").strip().lower()
    if period not in MINUTE_PERIODS:
        raise HTTPException(
            400, f"不支持的周期: {period}（可选 {'/'.join(sorted(MINUTE_PERIODS))}）"
        )

    from src.platform.marketdata.marketdata_client import get_market_data

    bars = get_market_data().minute_klines(
        symbol, market=market_code.value, period=period, count=count
    )
    return {
        "symbol": symbol,
        "market": market_code.value,
        "period": period,
        "count": len(bars),
        "klines": _serialize_klines(bars),
    }


@router.get("/{symbol}/intraday")
def get_intraday(symbol: str, market: str = "CN"):
    """当日分时曲线。返回价/均价/每分钟量额,并已剔除收盘后的填充点。

    源数据给的是**累计**量额;这里换算成图需要的形态:
    - ``avg_price`` = 累计额 / 累计量 / 100(量单位是手)—— 分时图的均价线
    - ``volume``/``amount`` = 相邻两点的差值,即**每分钟**增量 —— 量柱
    """
    market_code = _parse_market(market)

    from src.platform.marketdata.marketdata_client import get_market_data

    raw_points, prev_close = get_market_data().intraday(
        symbol, market=market_code.value
    )

    points: list[dict] = []
    prev_vol = 0.0
    prev_amt = 0.0
    for p in raw_points:
        if not _in_session(p.time):
            continue
        cum_vol = float(p.volume or 0.0)
        cum_amt = float(p.amount or 0.0)
        # 累计量是手(=100 股),换算成每股均价
        avg_price = (cum_amt / (cum_vol * 100)) if cum_vol > 0 else None
        points.append(
            {
                "time": p.time,
                "price": p.price,
                "avg_price": round(avg_price, 4) if avg_price else None,
                "volume": max(0.0, cum_vol - prev_vol),   # 每分钟增量(手)
                "amount": max(0.0, cum_amt - prev_amt),   # 每分钟增量(元)
            }
        )
        prev_vol, prev_amt = cum_vol, cum_amt

    return {
        "symbol": symbol,
        "market": market_code.value,
        "prev_close": prev_close,
        "count": len(points),
        "points": points,
    }


@router.get("/{symbol}/summary")
def get_kline_summary(symbol: str, market: str = "CN"):
    """获取单只股票K线摘要"""
    market_code = _parse_market(market)
    collector = KlineCollector(market_code)
    summary = collector.get_kline_summary(symbol)
    return {
        "symbol": symbol,
        "market": market_code.value,
        "summary": summary,
    }


@router.post("/summary/batch")
def get_kline_summary_batch(payload: KlineSummaryBatchRequest):
    """批量获取K线摘要"""
    if not payload.items:
        return []

    market_codes = [_parse_market(item.market) for item in payload.items]

    def load_one(index: int):
        item = payload.items[index]
        market_code = market_codes[index]
        try:
            summary = KlineCollector(market_code).get_kline_summary(item.symbol)
        except Exception as exc:
            summary = {"error": str(exc)}
        return {
            "symbol": item.symbol,
            "market": market_code.value,
            "summary": summary,
        }

    # 与前端原先的并发上限保持一致，减少批量接口对数据源的瞬时压力。
    with ThreadPoolExecutor(max_workers=min(5, len(payload.items))) as executor:
        futures = [executor.submit(load_one, index) for index in range(len(payload.items))]
        return [future.result() for future in futures]
