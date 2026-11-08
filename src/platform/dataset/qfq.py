"""前复权日K同步(只服务技术指标)。

原始 ``daily_bars`` 是不复权价:除权除息当天会出现"假跳空",均线、20 日新高、
MACD 这类指标会因此失真。这里用腾讯的 ``qfq`` 日线单独落一张表:

- 只存最近若干天(默认 260 根),供选股技术字段使用;
- 不影响行情快照与 K 线展示,也不改 ``daily_bars``;
- 成交量单位与 ``daily_bars`` 一致(手),腾讯不返回成交额,量能比仍用原始价的成交额。

同步是一天一跑的后台任务:全市场约 5,500 只,6 线程 + 节流大约 3 分钟。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor

import httpx
from sqlalchemy import func, text

from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import DailyBarQfq

logger = logging.getLogger(__name__)

_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": "https://gu.qq.com/",
}
_MIN_INTERVAL_S = 0.06
_THROTTLE_LOCK = threading.Lock()
_LAST_CALL = [0.0]
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def tencent_code(symbol: str) -> str:
    """标准代码 → 腾讯符号;不支持的板块返回空串。"""
    code = str(symbol or "").strip()
    if not code:
        return ""
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    if code.startswith(("4", "8", "92")):
        return "bj" + code
    return ""


def parse_qfq_payload(payload: dict, tc: str) -> list[dict]:
    """解析腾讯 ``qfqday``:[日期, 开, 收, 高, 低, 量(手)]。"""
    data = (payload or {}).get("data") or {}
    node = data.get(tc) or {}
    rows = node.get("qfqday") or []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        day = str(row[0])
        if not _DATE_RE.match(day):
            continue
        try:
            out.append(
                {
                    "trade_date": day,
                    "open": float(row[1]),
                    "close": float(row[2]),
                    "high": float(row[3]),
                    "low": float(row[4]),
                    "volume": float(row[5]),
                }
            )
        except (TypeError, ValueError):
            continue
    return out


def fetch_qfq(symbol: str, days: int = 260) -> list[dict]:
    tc = tencent_code(symbol)
    if not tc:
        return []
    with _THROTTLE_LOCK:
        wait = _MIN_INTERVAL_S - (time.monotonic() - _LAST_CALL[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[0] = time.monotonic()
    try:
        resp = httpx.get(
            _URL,
            params={"param": f"{tc},day,,,{max(60, int(days))},qfq"},
            headers=_HEADERS,
            timeout=12,
        )
        if resp.status_code != 200:
            return []
        return parse_qfq_payload(resp.json(), tc)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[前复权] {symbol} 获取失败: {exc}")
        return []


def _store(rows: list[dict]) -> int:
    if not rows:
        return 0
    db = SessionLocal()
    try:
        db.execute(
            text(
                "INSERT OR REPLACE INTO daily_bars_qfq "
                "(symbol, trade_date, open, high, low, close, volume, source) "
                "VALUES (:symbol, :trade_date, :open, :high, :low, :close, :volume, 'tencent_qfq')"
            ),
            rows,
        )
        db.commit()
        return len(rows)
    finally:
        db.close()


def sync_symbols(
    symbols: list[str], *, days: int = 260, workers: int = 6
) -> dict:
    """拉取并写入前复权日K;返回 {symbols, rows, failed}。"""
    symbols = [s for s in dict.fromkeys(symbols) if tencent_code(s)]
    if not symbols:
        return {"symbols": 0, "rows": 0, "failed": 0}

    written = 0
    failed = 0
    batch: list[dict] = []

    def _one(symbol: str) -> list[dict]:
        return [{"symbol": symbol, **bar} for bar in fetch_qfq(symbol, days)]

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for rows in pool.map(_one, symbols):
            if rows:
                batch.extend(rows)
            else:
                failed += 1
            if len(batch) >= 2000:
                written += _store(batch)
                batch = []
    written += _store(batch)
    logger.info(
        f"[前复权] 同步完成: {len(symbols)} 只 / 写入 {written} 行 / 失败 {failed} 只"
    )
    return {"symbols": len(symbols), "rows": written, "failed": failed}


def sync_all(*, days: int = 260, workers: int = 6) -> dict:
    """同步全市场(以 daily_bars 里的代码为准)。"""
    db = SessionLocal()
    try:
        symbols = [
            str(r[0])
            for r in db.execute(text("SELECT DISTINCT symbol FROM daily_bars")).fetchall()
            if r[0]
        ]
    finally:
        db.close()
    return sync_symbols(symbols, days=days, workers=workers)


def load_recent(*, days: int = 21) -> dict[str, list[dict]]:
    """读取每只股票最近 N 根前复权日K(按日期升序)。"""
    since = (date.today() - timedelta(days=max(90, int(days) * 4))).isoformat()
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                "SELECT symbol, trade_date, close, high FROM ("
                "  SELECT symbol, trade_date, close, high,"
                "         ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn"
                "  FROM daily_bars_qfq WHERE trade_date >= :since"
                ") WHERE rn <= :days"
            ),
            {"days": max(1, int(days)), "since": since},
        ).fetchall()
    finally:
        db.close()

    out: dict[str, list[dict]] = {}
    for symbol, day, close, high in rows:
        out.setdefault(str(symbol), []).append(
            {"date": str(day), "close": close, "high": high}
        )
    for bars in out.values():
        bars.sort(key=lambda b: b["date"])
    return out


def status() -> dict:
    db = SessionLocal()
    try:
        rows = int(db.query(func.count(DailyBarQfq.id)).scalar() or 0)
        symbols = int(
            db.query(func.count(func.distinct(DailyBarQfq.symbol))).scalar() or 0
        )
        latest = db.query(func.max(DailyBarQfq.trade_date)).scalar()
    finally:
        db.close()
    return {"rows": rows, "symbols": symbols, "latest": latest}
