"""概念板块与个股 → 概念反查索引。

数据源选择:东财 ``push2`` 的 clist 在本机/容器网络会被 reset(见 snapshot.py
文档),但 ``data.eastmoney.com/dataapi/bkzj/getbkzj`` 可用。bkzj 的语义是
「key 决定返回哪个指标、code 决定范围」:

- ``code=m:90+t:3`` → 全部概念板块(约 516 个),``key=f3`` 给涨跌幅、``key=f62`` 给主力净流入;
- ``code=b:BKxxxx`` → 该板块成分股(实测单个概念可返回 200+,上限约 600)。

因此建索引 = 1 次板块榜(f3) + 1 次资金榜(f62) + N 次成分股。
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
from sqlalchemy import delete, func

from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import ScreenerConceptMember

logger = logging.getLogger(__name__)

_BKZJ_URL = "https://data.eastmoney.com/dataapi/bkzj/getbkzj"
_CONCEPT_SCOPE = "m:90+t:3"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://data.eastmoney.com/",
}

_MAX_BOARDS = 240
_DEFAULT_BOARDS = 160
_MEM: dict = {"trade_date": "", "map": None}
_BOARDS_MEM: dict = {"ts": 0.0, "items": []}
_BOARDS_TTL_S = 300.0
_LOCK = threading.Lock()
_BUILDING = threading.Event()


def _num(value) -> float | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bkzj(key: str, code: str, *, timeout: float = 20.0) -> list[dict]:
    resp = httpx.get(
        _BKZJ_URL, params={"key": key, "code": code}, headers=_HEADERS, timeout=timeout
    )
    resp.raise_for_status()
    data = (resp.json() or {}).get("data") or {}
    diff = data.get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    return [x for x in diff if isinstance(x, dict)]


def fetch_concept_boards() -> list[dict]:
    """概念板块榜(涨幅 + 主力净流入),按主力净流入降序。

    bkzj 一次只返回一个指标,所以涨跌幅和资金要各取一次再按代码合并。
    结果在进程内缓存 5 分钟(天梯/板块榜/索引三处共用)。
    """
    import time

    if _BOARDS_MEM["items"] and time.monotonic() - _BOARDS_MEM["ts"] <= _BOARDS_TTL_S:
        return _BOARDS_MEM["items"]

    try:
        pct_rows = _bkzj("f3", _CONCEPT_SCOPE)
        flow_rows = _bkzj("f62", _CONCEPT_SCOPE)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[选股] 概念板块获取失败: {exc}")
        return []

    flow = {str(r.get("f12") or ""): _num(r.get("f62")) for r in flow_rows}
    boards = []
    for r in pct_rows:
        code = str(r.get("f12") or "").strip()
        if not code:
            continue
        boards.append(
            {
                "code": code,
                "name": str(r.get("f14") or "").strip(),
                "change_pct": round((_num(r.get("f3")) or 0.0) / 100, 2),
                "main_net_inflow": flow.get(code),
            }
        )
    boards.sort(key=lambda b: (b["main_net_inflow"] or 0.0), reverse=True)
    _BOARDS_MEM.update(ts=time.monotonic(), items=boards)
    return boards


def _count_rows(trade_date: str) -> int:
    db = SessionLocal()
    try:
        return int(
            db.query(func.count(ScreenerConceptMember.id))
            .filter(ScreenerConceptMember.trade_date == trade_date)
            .scalar()
            or 0
        )
    finally:
        db.close()


def _store(trade_date: str, rows: list[dict]) -> None:
    db = SessionLocal()
    try:
        db.execute(
            delete(ScreenerConceptMember).where(
                ScreenerConceptMember.trade_date == trade_date
            )
        )
        for start in range(0, len(rows), 1000):
            db.bulk_insert_mappings(ScreenerConceptMember, rows[start:start + 1000])
        db.commit()
    finally:
        db.close()


def build_concept_index(trade_date: str, *, board_limit: int = _DEFAULT_BOARDS) -> int:
    """拉概念榜 + 每个概念的成分股,落成个股→概念反查。返回写入条数。"""
    from src.modules.market.theme_service import is_excluded_concept

    boards = [b for b in fetch_concept_boards() if not is_excluded_concept(b["name"])]
    boards = boards[: max(1, min(int(board_limit), _MAX_BOARDS))]
    if not boards:
        return 0

    def _members(board: dict) -> tuple[dict, list[dict]]:
        try:
            rows = _bkzj("f3", f"b:{board['code']}")
            return board, [r for r in rows if str(r.get("f12") or "").strip()]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[选股] 概念成分股失败 {board['code']}: {exc}")
            return board, []

    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for rank, (board, members) in enumerate(pool.map(_members, boards), start=1):
            for m in members:
                out.append(
                    {
                        "trade_date": trade_date,
                        "symbol": str(m.get("f12") or "").strip(),
                        "name": str(m.get("f14") or "").strip(),
                        "concept_code": board["code"],
                        "concept_name": board["name"],
                        "concept_change_pct": board["change_pct"],
                        "concept_rank": rank,
                    }
                )

    if out:
        _store(trade_date, out)
        _MEM.update(trade_date=trade_date, map=None)
        logger.info(f"[选股] 概念索引完成: {len(boards)} 个概念 / {len(out)} 条归属")
    return len(out)


def ensure_concept_index(trade_date: str, *, board_limit: int = _DEFAULT_BOARDS) -> bool:
    """索引不存在时就地构建(首次打开页面会多花十几秒)。"""
    if _count_rows(trade_date) > 0:
        return True
    if _BUILDING.is_set() or not _LOCK.acquire(blocking=False):
        return False
    try:
        if _count_rows(trade_date) > 0:
            return True
        _BUILDING.set()
        build_concept_index(trade_date, board_limit=board_limit)
        return _count_rows(trade_date) > 0
    finally:
        _BUILDING.clear()
        _LOCK.release()


def get_concept_map(trade_date: str) -> dict[str, list[dict]]:
    """返回 {symbol: [{code,name,change_pct,rank}, ...]},按板块强度排序。"""
    if _MEM["map"] is not None and _MEM["trade_date"] == trade_date:
        return _MEM["map"]

    db = SessionLocal()
    try:
        rows = (
            db.query(ScreenerConceptMember)
            .filter(ScreenerConceptMember.trade_date == trade_date)
            .order_by(ScreenerConceptMember.concept_rank.asc())
            .all()
        )
    finally:
        db.close()

    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r.symbol, []).append(
            {
                "code": r.concept_code,
                "name": r.concept_name,
                "change_pct": r.concept_change_pct,
                "rank": r.concept_rank,
            }
        )
    _MEM.update(trade_date=trade_date, map=out)
    return out


def concept_index_status(trade_date: str) -> dict:
    db = SessionLocal()
    try:
        rows = int(
            db.query(func.count(ScreenerConceptMember.id))
            .filter(ScreenerConceptMember.trade_date == trade_date)
            .scalar()
            or 0
        )
        concepts = int(
            db.query(func.count(func.distinct(ScreenerConceptMember.concept_code)))
            .filter(ScreenerConceptMember.trade_date == trade_date)
            .scalar()
            or 0
        )
    finally:
        db.close()
    return {"rows": rows, "concepts": concepts}
