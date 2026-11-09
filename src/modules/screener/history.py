"""选股历史:运行快照、回看与后续表现。

一次「运行选股」会在后台落一条 ``screener_runs``:

- header 记录条件、命中数、数据日、耗时、降级提示;
- 前 300 条明细冻结当时的价格与标签(``screener_run_items``);
- 全量命中代码存 JSON,供后续表现统计使用。

同类条件(条件指纹相同)在 10 分钟内重复运行会合并更新,避免翻页/反复点按写爆表。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import text

from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import ScreenerRun, ScreenerRunItem

logger = logging.getLogger(__name__)

DEDUPE_WINDOW_S = 600
PREVIEW_LIMIT = 300
PERFORMANCE_TTL_S = 3600


def conditions_hash(conditions: list[dict], logic: str, exclude: list[str], order_by: str) -> str:
    raw = json.dumps(
        {"c": conditions, "l": logic, "e": sorted(exclude or []), "o": order_by},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def save_run(
    *,
    trade_date: str,
    payload: dict,
    result: dict,
    source: str = "custom",
    rule_id: int | None = None,
    rule_name: str = "",
    created_by: str = "",
    duration_ms: int = 0,
) -> int | None:
    """保存一次运行;翻页请求与关闭保存时返回 None。"""
    if int(payload.get("offset") or 0) > 0 or not payload.get("save", True):
        return None
    extra = result.get("_history") or {}
    symbols = extra.get("symbols") or []
    preview = extra.get("preview") or []
    conditions = payload.get("conditions") or []
    logic = str(payload.get("logic") or "all")
    exclude = payload.get("exclude") or []
    order_by = str(payload.get("order_by") or "change_pct desc")
    digest = conditions_hash(conditions, logic, exclude, order_by)

    db = SessionLocal()
    try:
        cutoff = datetime.now() - timedelta(seconds=DEDUPE_WINDOW_S)
        run = (
            db.query(ScreenerRun)
            .filter(
                ScreenerRun.trade_date == trade_date,
                ScreenerRun.conditions_hash == digest,
                ScreenerRun.created_by == created_by,
                ScreenerRun.created_at >= cutoff,
            )
            .order_by(ScreenerRun.id.desc())
            .first()
        )
        if run is None:
            # 显式用本地时间:SQLite 的 CURRENT_TIMESTAMP 是 UTC,和 datetime.now() 差 8 小时,
            # 会让「10 分钟内去重」的窗口判定失效
            run = ScreenerRun(
                trade_date=trade_date,
                created_by=created_by,
                created_at=datetime.now(),
            )
            db.add(run)
            db.flush()
        else:
            db.query(ScreenerRunItem).filter(ScreenerRunItem.run_id == run.id).delete()

        run.source = source or "custom"
        run.rule_id = rule_id
        run.rule_name = rule_name or ""
        run.conditions = conditions
        run.logic = logic
        run.exclude = exclude
        run.order_by = order_by
        run.conditions_hash = digest
        run.universe = int(result.get("universe") or 0)
        run.matched_count = int(result.get("total") or result.get("count") or 0)
        run.stored_items = len(preview)
        run.symbols_json = json.dumps(symbols, ensure_ascii=False)
        run.warnings = result.get("warnings") or []
        run.duration_ms = int(duration_ms or 0)
        run.performance = None
        run.performance_at = None

        for item in preview:
            db.add(ScreenerRunItem(run_id=run.id, **item))
        db.commit()
        return int(run.id)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("[选股] 历史保存失败")
        return None
    finally:
        db.close()


def list_runs(*, limit: int = 30, offset: int = 0, date_str: str | None = None, q: str = "") -> dict:
    db = SessionLocal()
    try:
        query = db.query(ScreenerRun)
        if date_str:
            query = query.filter(ScreenerRun.trade_date == date_str)
        if q.strip():
            like = f"%{q.strip()}%"
            query = query.filter(
                (ScreenerRun.rule_name.like(like)) | (ScreenerRun.created_by.like(like))
            )
        total = query.count()
        rows = (
            query.order_by(ScreenerRun.id.desc())
            .offset(max(0, offset))
            .limit(max(1, min(limit, 100)))
            .all()
        )
        items = [
            {
                "id": r.id,
                "trade_date": r.trade_date,
                "created_at": r.created_at.isoformat(sep=" ", timespec="seconds")
                if r.created_at
                else "",
                "source": r.source,
                "rule_name": r.rule_name,
                "conditions": r.conditions or [],
                "logic": r.logic,
                "matched_count": r.matched_count,
                "universe": r.universe,
                "stored_items": r.stored_items,
                "duration_ms": r.duration_ms,
            }
            for r in rows
        ]
        return {"total": total, "items": items}
    finally:
        db.close()


def get_run(run_id: int) -> dict | None:
    db = SessionLocal()
    try:
        run = db.query(ScreenerRun).filter(ScreenerRun.id == run_id).first()
        if not run:
            return None
        items = (
            db.query(ScreenerRunItem)
            .filter(ScreenerRunItem.run_id == run_id)
            .order_by(ScreenerRunItem.rank.asc())
            .all()
        )
        return {
            "id": run.id,
            "trade_date": run.trade_date,
            "created_at": run.created_at.isoformat(sep=" ", timespec="seconds")
            if run.created_at
            else "",
            "source": run.source,
            "rule_name": run.rule_name,
            "conditions": run.conditions or [],
            "logic": run.logic,
            "exclude": run.exclude or [],
            "order_by": run.order_by,
            "universe": run.universe,
            "matched_count": run.matched_count,
            "stored_items": run.stored_items,
            "warnings": run.warnings or [],
            "duration_ms": run.duration_ms,
            "performance": run.performance,
            "items": [
                {
                    "rank": i.rank,
                    "symbol": i.symbol,
                    "name": i.name,
                    "price": i.price,
                    "change_pct": i.change_pct,
                    "turnover_rate": i.turnover_rate,
                    "amount": i.amount,
                    "float_mv": i.float_mv,
                    "limit_up_days": i.limit_up_days,
                    "concept_rank": i.concept_rank,
                    "concepts": i.concepts or [],
                }
                for i in items
            ],
        }
    finally:
        db.close()


def delete_run(run_id: int) -> bool:
    db = SessionLocal()
    try:
        deleted = db.query(ScreenerRun).filter(ScreenerRun.id == run_id).delete()
        db.query(ScreenerRunItem).filter(ScreenerRunItem.run_id == run_id).delete()
        db.commit()
        return bool(deleted)
    finally:
        db.close()


def _load_closes(symbols: list[str], since: str, until: str) -> dict[str, list[tuple[str, float]]]:
    """按代码批量取收盘价(优先前复权表,缺失退回原始表)。"""
    out: dict[str, list[tuple[str, float]]] = {}
    if not symbols:
        return out
    db = SessionLocal()
    try:
        for table in ("daily_bars_qfq", "daily_bars"):
            try:
                for start in range(0, len(symbols), 400):
                    chunk = symbols[start: start + 400]
                    placeholders = ",".join(f":s{i}" for i in range(len(chunk)))
                    params = {f"s{i}": s for i, s in enumerate(chunk)}
                    params["since"] = since
                    params["until"] = until
                    rows = db.execute(
                        text(
                            f"SELECT symbol, trade_date, close FROM {table} "
                            f"WHERE symbol IN ({placeholders}) "
                            "AND trade_date BETWEEN :since AND :until "
                            "AND close IS NOT NULL ORDER BY trade_date ASC"
                        ),
                        params,
                    ).fetchall()
                    for symbol, day, close in rows:
                        out.setdefault(str(symbol), []).append((str(day), float(close)))
            except Exception:  # noqa: BLE001
                continue
            if out:
                break
    finally:
        db.close()
    return out


def performance(run_id: int, horizons: tuple[int, ...] = (1, 3, 5, 10)) -> dict:
    """计算历史名单的 T+N 等权收益/胜率;结果缓存 1 小时。"""
    db = SessionLocal()
    try:
        run = db.query(ScreenerRun).filter(ScreenerRun.id == run_id).first()
        if not run:
            return {}
        if (
            run.performance
            and run.performance_at
            and (datetime.now() - run.performance_at).total_seconds() < PERFORMANCE_TTL_S
        ):
            return run.performance
        symbols = json.loads(run.symbols_json or "[]")
        base = run.trade_date
    finally:
        db.close()

    if not symbols or not base:
        return {}

    end = (datetime.fromisoformat(base) + timedelta(days=max(horizons) * 3 + 10)).strftime(
        "%Y-%m-%d"
    )
    closes = _load_closes(symbols, base, end)

    stats: dict[str, dict] = {}
    for h in horizons:
        returns: list[float] = []
        for bars in closes.values():
            if len(bars) <= h:
                continue
            base_close = bars[0][1]
            target_close = bars[h][1]
            if base_close > 0:
                returns.append((target_close / base_close - 1) * 100)
        if not returns:
            stats[str(h)] = {"count": 0}
            continue
        returns.sort()
        n = len(returns)
        stats[str(h)] = {
            "count": n,
            "avg": round(sum(returns) / n, 2),
            "median": round(returns[n // 2], 2),
            "win_rate": round(sum(1 for r in returns if r > 0) / n * 100, 1),
        }

    payload = {"trade_date": base, "horizons": stats, "computed_at": datetime.now().isoformat()}
    db = SessionLocal()
    try:
        run = db.query(ScreenerRun).filter(ScreenerRun.id == run_id).first()
        if run:
            run.performance = payload
            run.performance_at = datetime.now()
            db.commit()
    finally:
        db.close()
    return payload


def cleanup(*, items_days: int = 90, runs_days: int = 365) -> dict:
    """删除过期明细与运行记录(夜间维护调用)。"""
    now = datetime.now()
    item_cut = now - timedelta(days=items_days)
    run_cut = now - timedelta(days=runs_days)
    db = SessionLocal()
    try:
        old_ids = [
            r[0]
            for r in db.query(ScreenerRun.id)
            .filter(ScreenerRun.created_at < item_cut)
            .all()
        ]
        removed_items = 0
        for start in range(0, len(old_ids), 500):
            chunk = old_ids[start: start + 500]
            removed_items += (
                db.query(ScreenerRunItem)
                .filter(ScreenerRunItem.run_id.in_(chunk))
                .delete(synchronize_session=False)
            )
        removed_runs = (
            db.query(ScreenerRun)
            .filter(ScreenerRun.created_at < run_cut)
            .delete(synchronize_session=False)
        )
        db.commit()
        return {"items": int(removed_items), "runs": int(removed_runs)}
    finally:
        db.close()
