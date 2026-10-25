"""全市场快照:新浪行情中心分页抓取 + SQLite 落库 + 内存热缓存。

为什么不用东财 clist:本机/容器的网络出口会 **reset** `push2.eastmoney.com`
与 `push2delay.eastmoney.com` 的 `/api/qt/clist/get`(TCP 建连后直接断开),
而 `data.eastmoney.com`(bkzj)与 `push2ex`(涨停池)可用。快照因此改走新浪的
`Market_Center.getHQNodeData`,它同样支持全市场分页且字段够选股用。

字段口径:
- 新浪 ``mktcap`` / ``nmc`` 单位是「万元」,这里换算成「元」存库;
- ``volume`` 是股数(不是手);
- 新浪不提供量比、主力净流入、行业,这三项置空,对应条件在字段目录里标记为不可用。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import delete, func

from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import ScreenerSnapshot

logger = logging.getLogger(__name__)

_SINA_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
_PAGE_SIZE = 100
_MAX_PAGES = 90
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.sina.com.cn/",
}

_MEM: dict = {"trade_date": "", "rows": None, "ts": 0.0}
_MEM_TTL_TRADING_S = 60.0
_MEM_TTL_CLOSED_S = 900.0
_LOCK = threading.Lock()


def latest_trade_date() -> date:
    """最近一个工作日。节假日不处理——数据源返回的是它自己的最新快照。"""
    d = date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def is_trading_session(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = (now.hour, now.minute)
    return (9, 15) <= hm <= (15, 5)


def _num(value) -> float | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out  # NaN


def _row(item: dict) -> dict | None:
    symbol = str(item.get("code") or "").strip()
    if not symbol:
        raw = str(item.get("symbol") or "")
        symbol = raw[2:] if len(raw) > 2 else raw
    if not symbol:
        return None

    name = str(item.get("name") or "").strip()
    price = _num(item.get("trade"))
    prev_close = _num(item.get("settlement"))
    high = _num(item.get("high"))
    low = _num(item.get("low"))
    amplitude = None
    if high is not None and low is not None and prev_close:
        amplitude = round((high - low) / prev_close * 100, 2)

    mktcap = _num(item.get("mktcap"))   # 万元
    nmc = _num(item.get("nmc"))         # 万元

    return {
        "symbol": symbol,
        "name": name,
        "market": "CN",
        "price": price,
        "change_pct": _num(item.get("changepercent")),
        "change_amount": _num(item.get("pricechange")),
        "volume": _num(item.get("volume")),
        "amount": _num(item.get("amount")),
        "amplitude": amplitude,
        "turnover_rate": _num(item.get("turnoverratio")),
        "volume_ratio": None,          # 新浪不提供
        "pe_ttm": _num(item.get("per")),
        "pb": _num(item.get("pb")),
        "high_price": high,
        "low_price": low,
        "open_price": _num(item.get("open")),
        "prev_close": prev_close,
        "total_mv": mktcap * 1e4 if mktcap is not None else None,
        "float_mv": nmc * 1e4 if nmc is not None else None,
        "main_net_inflow": None,       # 新浪不提供
        "speed": None,
        "industry": "",
        "is_st": ("ST" in name.upper()) or ("退" in name),
        "is_suspended": price is None or price == 0,
    }


def _fetch_page(client: httpx.Client, page: int) -> list[dict]:
    params = {
        "page": str(page),
        "num": str(_PAGE_SIZE),
        "sort": "symbol",
        "asc": "1",
        "node": "hs_a",
    }
    resp = client.get(_SINA_URL, params=params, timeout=20)
    resp.raise_for_status()
    text = resp.text.strip()
    if not text or text in ("null", "[]"):
        return []
    try:
        items = resp.json()
    except Exception:  # noqa: BLE001 — 新浪偶尔返回 JS 字面量
        import json

        items = json.loads(text)
    if not isinstance(items, list):
        return []
    return [r for r in (_row(it) for it in items if isinstance(it, dict)) if r]


def fetch_full_market() -> list[dict]:
    """抓全市场快照。失败页只记日志,不整批失败。"""
    rows: list[dict] = []
    seen: set[str] = set()
    with httpx.Client(headers=_HEADERS, follow_redirects=True, timeout=25) as client:
        for page in range(1, _MAX_PAGES + 1):
            try:
                batch = _fetch_page(client, page)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[选股] 快照第 {page} 页失败: {exc}")
                continue
            if not batch:
                break
            for r in batch:
                if r["symbol"] not in seen:
                    seen.add(r["symbol"])
                    rows.append(r)
            if len(batch) < _PAGE_SIZE:
                break

    if not rows:
        raise RuntimeError("全市场快照抓取失败(新浪无返回)")
    logger.info(f"[选股] 快照抓取完成: {len(rows)} 只")
    return rows


def _store(trade_date: str, rows: list[dict]) -> None:
    db = SessionLocal()
    try:
        db.execute(delete(ScreenerSnapshot).where(ScreenerSnapshot.trade_date == trade_date))
        payload = [{**r, "trade_date": trade_date} for r in rows]
        for start in range(0, len(payload), 1000):
            db.bulk_insert_mappings(ScreenerSnapshot, payload[start:start + 1000])
        db.commit()
    finally:
        db.close()


def refresh_snapshot(*, force: bool = False) -> dict:
    trade_date = latest_trade_date().isoformat()
    with _LOCK:
        if not force:
            db = SessionLocal()
            try:
                count = (
                    db.query(func.count(ScreenerSnapshot.id))
                    .filter(ScreenerSnapshot.trade_date == trade_date)
                    .scalar()
                ) or 0
            finally:
                db.close()
            if count > 0 and not is_trading_session():
                return {"trade_date": trade_date, "count": int(count), "refreshed": False}

        rows = fetch_full_market()
        _store(trade_date, rows)
        _MEM.update(trade_date=trade_date, rows=rows, ts=time.monotonic())
        return {"trade_date": trade_date, "count": len(rows), "refreshed": True}


def _load_from_db(trade_date: str) -> list[dict]:
    db = SessionLocal()
    try:
        records = (
            db.query(ScreenerSnapshot)
            .filter(ScreenerSnapshot.trade_date == trade_date)
            .all()
        )
        return [
            {
                "symbol": r.symbol,
                "name": r.name,
                "market": r.market or "CN",
                "price": r.price,
                "change_pct": r.change_pct,
                "change_amount": r.change_amount,
                "volume": r.volume,
                "amount": r.amount,
                "amplitude": r.amplitude,
                "turnover_rate": r.turnover_rate,
                "pe_ttm": r.pe_ttm,
                "volume_ratio": r.volume_ratio,
                "high_price": r.high_price,
                "low_price": r.low_price,
                "open_price": r.open_price,
                "prev_close": r.prev_close,
                "total_mv": r.total_mv,
                "float_mv": r.float_mv,
                "speed": r.speed,
                "pb": r.pb,
                "main_net_inflow": r.main_net_inflow,
                "industry": r.industry or "",
                "is_st": bool(r.is_st),
                "is_suspended": bool(r.is_suspended),
            }
            for r in records
        ]
    finally:
        db.close()


def get_snapshot(*, force_refresh: bool = False) -> list[dict]:
    """取当日快照。优先内存 → 库 → 现抓。"""
    trade_date = latest_trade_date().isoformat()

    if not force_refresh and _MEM["rows"] and _MEM["trade_date"] == trade_date:
        ttl = _MEM_TTL_TRADING_S if is_trading_session() else _MEM_TTL_CLOSED_S
        if time.monotonic() - _MEM["ts"] <= ttl:
            return _MEM["rows"]

    rows = _load_from_db(trade_date)
    if rows and not force_refresh:
        _MEM.update(trade_date=trade_date, rows=rows, ts=time.monotonic())
        return rows

    refresh_snapshot(force=force_refresh)
    rows = _load_from_db(trade_date)
    if not rows:
        raise RuntimeError("全市场快照为空")
    _MEM.update(trade_date=trade_date, rows=rows, ts=time.monotonic())
    return rows


_WARMED = False


def warmup_async() -> None:
    """启动时后台预热一次快照,避免第一个打开选股页的人等十几秒。"""
    global _WARMED
    if _WARMED:
        return
    _WARMED = True

    def _run() -> None:
        try:
            if snapshot_status()["count"] == 0:
                result = refresh_snapshot(force=True)
                logger.info(f"[选股] 快照预热完成: {result}")
        except Exception:  # noqa: BLE001 — 预热失败不影响服务
            logger.exception("[选股] 快照预热失败")

    threading.Thread(target=_run, name="screener-warmup", daemon=True).start()


def snapshot_status() -> dict:
    trade_date = latest_trade_date().isoformat()
    db = SessionLocal()
    try:
        count = (
            db.query(func.count(ScreenerSnapshot.id))
            .filter(ScreenerSnapshot.trade_date == trade_date)
            .scalar()
        ) or 0
    finally:
        db.close()
    return {
        "trade_date": trade_date,
        "count": int(count),
        "trading_session": is_trading_session(),
        "cached": bool(_MEM["rows"]) and _MEM["trade_date"] == trade_date,
        "source": "sina",
    }
