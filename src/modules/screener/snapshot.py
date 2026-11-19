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
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, func, text

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
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    d = now.date()
    if (now.hour, now.minute) < (9, 15):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def is_trading_session(now: datetime | None = None) -> bool:
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
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
        # 少量并发分页，避免全市场 50 多页串行拖过数分钟。
        with ThreadPoolExecutor(max_workers=6) as pool:
            finished = False
            for start in range(1, _MAX_PAGES + 1, 6):
                pages = list(range(start, min(start + 6, _MAX_PAGES + 1)))
                futures = [pool.submit(_fetch_page, client, page) for page in pages]
                for page, future in zip(pages, futures):
                    if finished:
                        continue
                    try:
                        batch = future.result()
                    except Exception as exc:
                        # 不用缺页快照覆盖完整缓存。
                        raise RuntimeError(f"全市场快照第 {page} 页失败，保留旧缓存") from exc
                    for row in batch:
                        if row["symbol"] not in seen:
                            seen.add(row["symbol"])
                            rows.append(row)
                    if len(batch) < _PAGE_SIZE:
                        finished = True
                if finished:
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
        _MEM.update(trade_date=trade_date, rows=rows, ts=time.monotonic(), updated_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat())
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


_REFRESH_STATE = {"running": False, "last_attempt": 0.0, "error": ""}
_REFRESH_LOCK = threading.Lock()


def request_refresh(*, force: bool = False) -> bool:
    """单个后台任务刷新行情，页面请求不等待远程全市场抓取。"""
    with _REFRESH_LOCK:
        if _REFRESH_STATE["running"] or (not force and time.monotonic() - _REFRESH_STATE["last_attempt"] < 60):
            return False
        _REFRESH_STATE.update(running=True, last_attempt=time.monotonic(), error="")
    def run():
        try:
            refresh_snapshot(force=True)
        except Exception as exc:
            _REFRESH_STATE["error"] = str(exc)[:160]
            logger.warning("[选股] 后台快照刷新失败: %s", exc)
        finally:
            _REFRESH_STATE["running"] = False
    threading.Thread(target=run, name="screener-refresh", daemon=True).start()
    return True


def _local_daily_snapshot() -> tuple[str, list[dict]]:
    db = SessionLocal()
    try:
        latest = db.execute(text("SELECT MAX(trade_date) FROM daily_bars")).scalar()
        if not latest:
            return "", []
        previous = db.execute(text("SELECT MAX(trade_date) FROM daily_bars WHERE trade_date < :day"), {"day": latest}).scalar()
        bars = db.execute(text(
            "SELECT b.symbol,b.close,b.open,b.high,b.low,b.volume,b.amount,p.close prev_close "
            "FROM daily_bars b LEFT JOIN daily_bars p ON p.symbol=b.symbol AND p.trade_date=:previous "
            "WHERE b.trade_date=:day"
        ), {"day": latest, "previous": previous}).mappings().all()
        names = {s: n for s, n in db.execute(text("SELECT symbol,name FROM stocks WHERE market='CN'"))}
        rows = []
        for b in bars:
            prev, price = b["prev_close"], b["close"]
            if not price or not prev:
                continue
            name = names.get(b["symbol"], b["symbol"])
            rows.append({"symbol": b["symbol"], "name": name, "market": "CN", "price": price,
                         "prev_close": prev, "open_price": b["open"], "high_price": b["high"], "low_price": b["low"],
                         "volume": (b["volume"] or 0) * 100, "amount": b["amount"],
                         "change_pct": round((price / prev - 1) * 100, 2),
                         "change_amount": round(price - prev, 4), "is_st": "ST" in name.upper(), "is_suspended": False})
        return str(latest), rows
    finally:
        db.close()


def get_snapshot(*, force_refresh: bool = False) -> list[dict]:
    """先返回本地快照/日K，过期行情在后台更新。"""
    target = latest_trade_date().isoformat()
    if force_refresh:
        request_refresh(force=True)
    ttl = _MEM_TTL_TRADING_S if is_trading_session() else _MEM_TTL_CLOSED_S
    if _MEM["rows"] and time.monotonic() - _MEM["ts"] <= ttl:
        if _MEM["trade_date"] != target or (is_trading_session() and time.monotonic() - _MEM["ts"] > 60):
            request_refresh()
        return _MEM["rows"]
    db = SessionLocal()
    try:
        day = db.query(func.max(ScreenerSnapshot.trade_date)).filter(ScreenerSnapshot.trade_date <= target).scalar()
        updated_at = db.query(func.max(ScreenerSnapshot.updated_at)).filter(ScreenerSnapshot.trade_date == day).scalar() if day else None
    finally:
        db.close()
    rows = _load_from_db(str(day)) if day else []
    if not rows:
        day, rows = _local_daily_snapshot()
    _MEM["updated_at"] = updated_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).isoformat() if updated_at else str(day or "")
    _MEM.update(trade_date=str(day or ""), rows=rows, ts=time.monotonic(), source="snapshot" if day and rows and rows[0].get("pe_ttm") is not None else "local")
    if not rows or str(day) != target or is_trading_session():
        request_refresh()
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
        "source": "local-cache",
        "data_date": _MEM["trade_date"] or trade_date,
        "refreshing": _REFRESH_STATE["running"],
        "refresh_error": _REFRESH_STATE["error"],
    }
