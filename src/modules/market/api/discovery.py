import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.platform.runtime.config import Settings
from src.platform.notifications.notifier import get_global_proxy
from src.platform.marketdata.collectors.discovery_collector import (
    DEFAULT_DISCOVERY_PROVIDER,
    EastMoneyDiscoveryCollector,
)
from src.platform.marketdata.marketdata_client import md_quote_rows
from src.platform.persistence.database import get_db
from src.platform.persistence.models import MarketScanSnapshot, Stock


router = APIRouter()

logger = logging.getLogger(__name__)


_cache: dict[str, tuple[float, object]] = {}


def _resolve_proxy() -> str:
    # Prefer UI-configured proxy, fallback to env settings.
    try:
        return (get_global_proxy() or "").strip() or (
            Settings().http_proxy or ""
        ).strip()
    except Exception:
        return ""


def _cache_get(key: str, ttl_s: int) -> object | None:
    now = time.time()
    hit = _cache.get(key)
    if not hit:
        return None
    ts, obj = hit
    if now - ts > ttl_s:
        return None
    return obj


def _cache_set(key: str, obj: object) -> None:
    _cache[key] = (time.time(), obj)


def _to_number(value) -> float | None:
    if value is None:
        return None
    try:
        n = float(value)
        if n != n:  # NaN
            return None
        return n
    except Exception:
        return None


def _pick_num(mapping: dict, keys: list[str]) -> float | None:
    for key in keys:
        if key in mapping:
            n = _to_number(mapping.get(key))
            if n is not None:
                return n
    return None


def _normalize_market(market: str) -> str:
    m = (market or "CN").strip().upper()
    return m if m in ("CN", "HK", "US") else "CN"


def _discovery_providers(db: Session) -> list[str]:
    """按 priority 取启用中的 discovery provider(DataSource 表),去重保序。

    discovery 不进 Engine(市场级、非 symbol 模型),但仍是可选数据源:这里读表得到
    尝试顺序,失败逐个降级,语义与 Engine 的主备转移一致。表里一条都没有时回落到
    历史默认 provider,保证老实例升级后行为不变。
    """
    from src.platform.persistence.models import DataSource

    rows = (
        db.query(DataSource.provider)
        .filter(DataSource.type == "discovery", DataSource.enabled.is_(True))
        .order_by(DataSource.priority.asc(), DataSource.id.asc())
        .all()
    )
    ordered: dict[str, None] = {}
    for row in rows:
        provider = str(row[0] or "").strip()
        if provider:
            ordered.setdefault(provider, None)
    return list(ordered) or [DEFAULT_DISCOVERY_PROVIDER]


async def _first_non_empty(db: Session, build):
    """按 provider 优先级依次尝试,第一个返回非空结果即采用。

    ``build(collector)`` 返回 awaitable[list]。逐个 provider 隔离异常:某个源超时/
    连接被拒/配置非法都只记为告警并降级到下一个,不影响其余来源。全失败返回 []。
    """
    proxy = _resolve_proxy() or None
    for provider in _discovery_providers(db):
        collector = EastMoneyDiscoveryCollector(proxy=proxy, provider=provider)
        try:
            items = await build(collector)
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.ProxyError) as e:
            logger.warning(f"discovery provider={provider} 连接失败: {e!r}")
            continue
        except ValueError as e:
            logger.warning(f"discovery provider={provider} 非法: {e}")
            continue
        except Exception as e:
            logger.warning(f"discovery provider={provider} 失败: {type(e).__name__}: {e!r}")
            continue
        if items:
            return items
    return []


def _enrich_quotes(rows: list[dict]) -> list[dict]:
    """用既有行情层给个股行补价格/涨跌幅/成交额/成交量。

    bkzj 类来源只返回代码、名称(以及可选的涨跌幅),价格与量额一律留空;而前端卡片
    要显示价格。个股可以走项目已在用的行情链路补全(CN 走腾讯/新浪),板块不行——
    板块的涨跌幅由 vendor 自己取,故这里只处理个股行。

    补全失败只记日志、保留原字段,不让整个板块页因为行情抖动而挂掉。
    """
    symbols = [r["symbol"] for r in rows if r.get("symbol")]
    if not symbols:
        return rows
    try:
        quotes = {q["symbol"]: q for q in md_quote_rows(symbols, "CN")}
    except Exception as e:
        logger.warning(f"板块/榜单行情补全失败,保留原始字段: {e}")
        return rows
    for row in rows:
        quote = quotes.get(row.get("symbol"))
        if not quote:
            continue
        for field, key in (
            ("price", "current_price"),
            ("change_pct", "change_pct"),
            ("turnover", "turnover"),
            ("volume", "volume"),
        ):
            if quote.get(key) is not None:
                row[field] = quote[key]
    return rows


def _latest_snapshot_stocks(db: Session, market: str, limit: int = 120) -> list[dict]:
    mkt = _normalize_market(market)
    latest = (
        db.query(MarketScanSnapshot.snapshot_date)
        .filter(MarketScanSnapshot.stock_market == mkt)
        .order_by(MarketScanSnapshot.snapshot_date.desc())
        .first()
    )
    if not latest:
        return []
    rows = (
        db.query(MarketScanSnapshot)
        .filter(
            MarketScanSnapshot.stock_market == mkt,
            MarketScanSnapshot.snapshot_date == latest[0],
        )
        .order_by(MarketScanSnapshot.score_seed.desc(), MarketScanSnapshot.updated_at.desc())
        .limit(max(20, min(int(limit), 300)))
        .all()
    )
    out: list[dict] = []
    for row in rows:
        quote = row.quote if isinstance(row.quote, dict) else {}
        out.append(
            {
                "symbol": row.stock_symbol,
                "market": row.stock_market,
                "name": row.stock_name or row.stock_symbol,
                "price": _pick_num(quote, ["price", "current_price", "last", "close"]),
                "change_pct": _pick_num(quote, ["change_pct", "pct_change", "chg_pct"]),
                "turnover": _pick_num(quote, ["turnover", "amount", "turnover_value"]),
                "volume": _pick_num(quote, ["volume", "vol"]),
            }
        )
    return out


async def _hot_stocks_live_or_snapshot(
    *,
    db: Session,
    market: str,
    mode: str,
    limit: int,
) -> list[dict]:
    mkt = _normalize_market(market)
    items = await _first_non_empty(
        db, lambda c: c.fetch_hot_stocks(market=mkt, mode=mode, limit=limit)
    )
    data = [
        {
            "symbol": it.symbol,
            "market": it.market,
            "name": it.name,
            "price": it.price,
            "change_pct": it.change_pct,
            "turnover": it.turnover,
            "volume": it.volume,
        }
        for it in items
    ]
    if data:
        # 部分来源(bkzj)只给代码与名称,价格/量额走既有行情层补齐。
        return _enrich_quotes(data)
    # Snapshot fallback: ensures UI is still usable when live source timeout/unavailable.
    return _latest_snapshot_stocks(db, mkt, limit=max(limit, 40))


def _watchlist_symbols(db: Session, market: str) -> set[str]:
    mkt = _normalize_market(market)
    rows = db.query(Stock.symbol).filter(Stock.market == mkt).all()
    return {str(x[0]).strip() for x in rows if x and x[0]}


def _avg(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _sum(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return float(sum(vals))


def _build_synthetic_boards(
    *,
    market: str,
    stocks: list[dict],
    watchlist: set[str],
    limit: int,
) -> list[dict]:
    mkt = _normalize_market(market)
    if not stocks:
        return []
    # Keep a stable high-quality universe for synthetic themes.
    universe = stocks[: max(30, min(len(stocks), 120))]
    gainers = sorted(universe, key=lambda x: _to_number(x.get("change_pct")) or -999.0, reverse=True)
    turnover = sorted(universe, key=lambda x: _to_number(x.get("turnover")) or 0.0, reverse=True)
    volatility = sorted(universe, key=lambda x: abs(_to_number(x.get("change_pct")) or 0.0), reverse=True)
    watch_related = [x for x in universe if str(x.get("symbol") or "") in watchlist]

    def build_bucket(code: str, name: str, items: list[dict]) -> dict | None:
        if not items:
            return None
        top = items[: min(12, len(items))]
        return {
            "code": f"{mkt}_{code}",
            "name": name,
            "change_pct": _avg([_to_number(x.get("change_pct")) for x in top]),
            "change_amount": None,
            "turnover": _sum([_to_number(x.get("turnover")) for x in top]),
        }

    market_name = {"CN": "A股", "HK": "港股", "US": "美股"}.get(mkt, mkt)
    buckets = [
        build_bucket("GAINERS", f"{market_name}涨幅领先", gainers),
        build_bucket("TURNOVER", f"{market_name}成交额领先", turnover),
        build_bucket("VOLATILITY", f"{market_name}波动活跃", volatility),
        build_bucket("WATCHLIST", f"{market_name}自选关联", watch_related),
    ]
    result = [x for x in buckets if x]
    return result[: max(1, min(int(limit), 20))]


def _stocks_by_synthetic_board(
    *,
    code: str,
    market: str,
    stocks: list[dict],
    watchlist: set[str],
    limit: int,
) -> list[dict]:
    mkt = _normalize_market(market)
    suffix = code.replace(f"{mkt}_", "", 1)
    universe = stocks[: max(30, min(len(stocks), 160))]
    if suffix == "GAINERS":
        ranked = sorted(universe, key=lambda x: _to_number(x.get("change_pct")) or -999.0, reverse=True)
    elif suffix == "TURNOVER":
        ranked = sorted(universe, key=lambda x: _to_number(x.get("turnover")) or 0.0, reverse=True)
    elif suffix == "VOLATILITY":
        ranked = sorted(universe, key=lambda x: abs(_to_number(x.get("change_pct")) or 0.0), reverse=True)
    elif suffix == "WATCHLIST":
        ranked = [x for x in universe if str(x.get("symbol") or "") in watchlist]
        ranked = sorted(ranked, key=lambda x: _to_number(x.get("turnover")) or 0.0, reverse=True)
    else:
        ranked = []
    return ranked[: max(1, min(int(limit), 100))]


@router.get("/stocks")
async def get_hot_stocks(
    market: str = "CN",
    mode: str = "turnover",
    limit: int = 20,
    db: Session = Depends(get_db),
):
    """Hot stocks for discovery.

    mode: turnover | gainers
    """

    market = _normalize_market(market)
    mode = (mode or "turnover").lower()
    if mode not in ("turnover", "gainers"):
        raise HTTPException(400, f"不支持的 mode: {mode}")

    key = f"stocks:{market}:{mode}:{int(limit)}"
    cached = _cache_get(key, ttl_s=45)
    if cached is not None:
        return cached

    data = await _hot_stocks_live_or_snapshot(
        db=db,
        market=market,
        mode=mode,
        limit=max(1, min(int(limit), 100)),
    )
    if not data:
        raise HTTPException(
            503, "热门股票数据源不可用（实时源与本地快照均不可用）"
        )
    _cache_set(key, data)
    return data


@router.get("/boards")
async def get_hot_boards(
    market: str = "CN",
    mode: str = "gainers",
    limit: int = 12,
    db: Session = Depends(get_db),
):
    """Hot boards (industry) for discovery.

    mode: gainers | turnover
    """

    market = _normalize_market(market)
    mode = (mode or "gainers").lower()
    if mode not in ("gainers", "turnover", "hot"):
        raise HTTPException(400, f"不支持的 mode: {mode}")

    key = f"boards:{market}:{mode}:{int(limit)}"
    cached = _cache_get(key, ttl_s=60)
    if cached is not None:
        return cached

    data: list[dict] = []
    # CN: prefer real industry boards; HK/US: synthetic themed buckets from market hot pool.
    if market == "CN":
        items = await _first_non_empty(
            db, lambda c: c.fetch_hot_boards(market=market, mode=mode, limit=limit)
        )
        data = [
            {
                "code": it.code,
                "name": it.name,
                "change_pct": it.change_pct,
                "change_amount": it.change_amount,
                "turnover": it.turnover,
            }
            for it in items
        ]

    if not data:
        stocks = await _hot_stocks_live_or_snapshot(
            db=db,
            market=market,
            mode="turnover" if mode == "turnover" else "gainers",
            limit=max(50, int(limit) * 10),
        )
        watchlist = _watchlist_symbols(db, market)
        data = _build_synthetic_boards(
            market=market,
            stocks=stocks,
            watchlist=watchlist,
            limit=limit,
        )
    if not data:
        raise HTTPException(503, "热门板块/主题数据源不可用")
    _cache_set(key, data)
    return data


@router.get("/boards/{board_code}/stocks")
async def get_board_stocks(
    board_code: str,
    mode: str = "gainers",
    limit: int = 20,
    market: str = "CN",
    db: Session = Depends(get_db),
):
    """Top stocks in a board."""

    code = (board_code or "").strip()
    if not code:
        raise HTTPException(400, "缺少板块代码")

    mkt = _normalize_market(market)
    mode = (mode or "gainers").lower()
    if mode not in ("gainers", "turnover", "hot"):
        raise HTTPException(400, f"不支持的 mode: {mode}")

    key = f"board_stocks:{mkt}:{code}:{mode}:{int(limit)}"
    cached = _cache_get(key, ttl_s=60)
    if cached is not None:
        return cached

    if code.startswith(("CN_", "HK_", "US_")):
        market_from_code = code.split("_", 1)[0]
        stocks = await _hot_stocks_live_or_snapshot(
            db=db,
            market=market_from_code,
            mode="turnover" if mode == "turnover" else "gainers",
            limit=max(80, int(limit) * 8),
        )
        watchlist = _watchlist_symbols(db, market_from_code)
        data = _stocks_by_synthetic_board(
            code=code,
            market=market_from_code,
            stocks=stocks,
            watchlist=watchlist,
            limit=limit,
        )
        _cache_set(key, data)
        return data

    items = await _first_non_empty(
        db, lambda c: c.fetch_board_stocks(board_code=code, mode=mode, limit=limit)
    )
    if not items:
        # 真实板块必定有成分股,拿不到即为所有 provider 都失败(逐个原因已记日志)。
        raise HTTPException(503, "板块成分股数据源不可用")
    data = _enrich_quotes([
        {
            "symbol": it.symbol,
            "market": "CN",
            "name": it.name,
            "price": it.price,
            "change_pct": it.change_pct,
            "turnover": it.turnover,
            "volume": it.volume,
        }
        for it in items
    ])
    _cache_set(key, data)
    return data
