"""Unified daily limit-up pool: Eastmoney + local daily-bar cross-check.

The old path used Eastmoney and only fell back to local bars when Eastmoney
returned an empty list. This service keeps both results, reconciles them, and
persists one normalized row per symbol for the day:

* Eastmoney rows are primary (they carry seal time / seal fund / break count /
  industry);
* local rows are computed from ``daily_bars`` or the full-market snapshot;
* a local row that confirms an Eastmoney row is recorded as
  ``confirmed_local``; a differing board count is recorded as ``conflict``;
* a local-only row is kept with ``source=local`` and ``fields_complete=False``.

Callers (ladder, sentiment) read the same table, so a degraded Eastmoney
response is visible and auditable instead of silently changing the count.
"""

from __future__ import annotations

import logging
import time
from datetime import date as date_cls

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import LimitUpPool, LimitUpPoolRun

logger = logging.getLogger(__name__)


def _board(code: str) -> str:
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("4", "8", "920")):
        return "北交所"
    return "主板"


def _limit_pct(code: str, name: str) -> float:
    if code.startswith(("4", "8", "920")):
        return 30.0
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if "ST" in (name or "").upper():
        return 5.0
    return 10.0


def _norm_east(item) -> dict:
    code = str(getattr(item, "code", "") or "")
    name = str(getattr(item, "name", "") or "")
    return {
        "symbol": code,
        "name": name,
        "source": "eastmoney",
        "limit_up_days": int(getattr(item, "limit_up_days", 0) or 0),
        "first_seal_time": str(getattr(item, "first_seal_time", "") or ""),
        "seal_fund": getattr(item, "seal_fund", None),
        "break_count": int(getattr(item, "break_count", 0) or 0),
        "industry": str(getattr(item, "industry", "") or "").strip() or None,
        "change_pct": getattr(item, "change_pct", None),
        "amount": getattr(item, "amount", None),
        "turnover_rate": getattr(item, "turnover_rate", None),
        "float_market_cap": getattr(item, "float_market_cap", None),
        "board": _board(code),
        "limit_pct": _limit_pct(code, name),
        "is_st": "ST" in name.upper(),
        "fields_complete": True,
        "confirmed_local": False,
        "conflict": False,
        "local_days": None,
        "raw": {
            "stat_days": int(getattr(item, "stat_days", 0) or 0),
            "stat_count": int(getattr(item, "stat_count", 0) or 0),
        },
    }


def _norm_local(item: dict) -> dict:
    code = str(item.get("symbol") or "")
    name = str(item.get("name") or "")
    return {
        "symbol": code,
        "name": name,
        "source": "local",
        "limit_up_days": int(item.get("limit_up_days", 0) or 0),
        "first_seal_time": "",
        "seal_fund": None,
        "break_count": 0,
        "industry": None,
        "change_pct": item.get("change_pct"),
        "amount": None,
        "turnover_rate": None,
        "float_market_cap": None,
        "board": _board(code),
        "limit_pct": _limit_pct(code, name),
        "is_st": "ST" in name.upper(),
        "fields_complete": False,
        "confirmed_local": False,
        "conflict": False,
        "local_days": None,
        "raw": {"local": True},
    }


def reconcile(trade_date: str, east: list[dict], local: list[dict]) -> tuple[list[dict], dict]:
    """Merge two normalized sources; pure and unit-testable."""
    east_map = {row["symbol"]: row for row in east if row.get("symbol")}
    local_map = {row["symbol"]: row for row in local if row.get("symbol")}
    symbols = sorted(set(east_map) | set(local_map))
    rows: list[dict] = []
    conflicts = 0
    local_only = 0
    for symbol in symbols:
        primary = east_map.get(symbol)
        proxy = local_map.get(symbol)
        if primary:
            row = dict(primary)
            if proxy:
                row["confirmed_local"] = True
                row["local_days"] = int(proxy.get("limit_up_days") or 0)
                if row["local_days"] != row["limit_up_days"]:
                    row["conflict"] = True
                    conflicts += 1
        else:
            row = dict(proxy)
            local_only += 1
        rows.append(row)

    rows.sort(key=lambda r: (-int(r.get("limit_up_days") or 0), r["symbol"]))
    warnings: list[str] = []
    if not east_map and local_map:
        warnings.append(
            "东财涨停池不可用，已用本地日K推算；封板资金/首封时间/炸板次数缺失，"
            "且本地推算不覆盖 ST 5% 涨停和新股无涨跌幅"
        )
    if east_map and local_map:
        if len(east_map) < len(local_map) * 0.8:
            warnings.append(f"东财涨停池 {len(east_map)} 只，本地推算 {len(local_map)} 只，差异较大")
        if conflicts:
            warnings.append(f"{conflicts} 只连板数与本地推算不一致，已保留东财值")
        if local_only:
            warnings.append(f"{local_only} 只仅存在于本地推算，来源标记为 local")
    if not east_map and not local_map:
        warnings.append("东财与本地都没有可用涨停数据")
    quality = "ok"
    if not east_map or conflicts or (east_map and local_map and len(east_map) < len(local_map) * 0.8):
        quality = "partial"
    source = "eastmoney" if east_map and not local_map else "eastmoney+local" if east_map else "local"
    return rows, {
        "trade_date": trade_date,
        "source": source,
        "quality": quality,
        "east_count": len(east_map),
        "local_count": len(local_map),
        "merged_count": len(rows),
        "conflict_count": conflicts,
        "warnings": warnings,
    }


def _east_items(trade_date: str) -> list[dict]:
    try:
        from src.platform.marketdata.marketdata_client import get_market_data

        rows = get_market_data().zt_pool(date=trade_date.replace("-", ""), limit=300)
        return [_norm_east(row) for row in rows if getattr(row, "code", None)]
    except Exception as exc:  # noqa: BLE001
        logger.warning("涨停池(东财)获取失败 %s: %s", trade_date, exc)
        return []


def _local_items(trade_date: str) -> list[dict]:
    try:
        from src.modules.screener import engine

        rows = engine._local_ladder(trade_date)
        candidates = [row for row in rows if row.get("symbol")]
        if not candidates:
            return []
        # The local path uses a 9% main-board threshold and has no ST (5%) or
        # new-listing (no limit) branch. Filter those false positives before
        # merging; legitimate ST limit-ups are not in this local candidate set.
        symbols = sorted({str(row["symbol"]) for row in candidates})
        db = SessionLocal()
        try:
            counts = dict(
                db.execute(
                    text(
                        """
SELECT symbol, COUNT(*) FROM daily_bars
WHERE symbol IN :symbols AND trade_date <= :day
GROUP BY symbol
"""
                    ).bindparams(bindparam("symbols", expanding=True)),
                    {"symbols": symbols, "day": trade_date},
                ).fetchall()
            )
        finally:
            db.close()
        out: list[dict] = []
        for row in candidates:
            name = str(row.get("name") or "")
            symbol = str(row.get("symbol") or "")
            if "ST" in name.upper():
                continue
            if int(counts.get(symbol, 0) or 0) < 6:
                continue
            out.append(_norm_local(row))
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("涨停池(本地)推算失败 %s: %s", trade_date, exc)
        return []


def _row_to_dict(row: LimitUpPool) -> dict:
    return {
        "symbol": row.symbol,
        "name": row.name or "",
        "source": row.source,
        "limit_up_days": int(row.limit_up_days or 0),
        "first_seal_time": row.first_seal_time or "",
        "seal_fund": row.seal_fund,
        "break_count": int(row.break_count or 0),
        "industry": row.industry,
        "change_pct": row.change_pct,
        "amount": row.amount,
        "turnover_rate": row.turnover_rate,
        "float_market_cap": row.float_market_cap,
        "board": row.board or "",
        "limit_pct": row.limit_pct,
        "is_st": bool(row.is_st),
        "fields_complete": bool(row.fields_complete),
        "confirmed_local": bool(row.confirmed_local),
        "conflict": bool(row.conflict),
        "local_days": row.local_days,
    }


def _read_run(db: Session, trade_date: str) -> tuple[LimitUpPoolRun | None, list[dict]]:
    run = db.query(LimitUpPoolRun).filter(LimitUpPoolRun.trade_date == trade_date).first()
    if run is None:
        return None, []
    rows = (
        db.query(LimitUpPool)
        .filter(LimitUpPool.trade_date == trade_date)
        .order_by(LimitUpPool.limit_up_days.desc(), LimitUpPool.symbol.asc())
        .all()
    )
    return run, [_row_to_dict(row) for row in rows]


def _persist(db: Session, trade_date: str, rows: list[dict], meta: dict) -> LimitUpPoolRun:
    db.query(LimitUpPool).filter(LimitUpPool.trade_date == trade_date).delete()
    for item in rows:
        db.add(
            LimitUpPool(
                trade_date=trade_date,
                symbol=item["symbol"],
                name=item.get("name") or "",
                source=item.get("source") or "eastmoney",
                limit_up_days=int(item.get("limit_up_days") or 0),
                first_seal_time=item.get("first_seal_time") or "",
                seal_fund=item.get("seal_fund"),
                break_count=int(item.get("break_count") or 0),
                industry=item.get("industry"),
                change_pct=item.get("change_pct"),
                amount=item.get("amount"),
                turnover_rate=item.get("turnover_rate"),
                float_market_cap=item.get("float_market_cap"),
                board=item.get("board") or "",
                limit_pct=item.get("limit_pct"),
                is_st=bool(item.get("is_st")),
                fields_complete=bool(item.get("fields_complete")),
                confirmed_local=bool(item.get("confirmed_local")),
                conflict=bool(item.get("conflict")),
                local_days=item.get("local_days"),
                raw=item.get("raw") or {},
            )
        )
    run = db.query(LimitUpPoolRun).filter(LimitUpPoolRun.trade_date == trade_date).first()
    if run is None:
        run = LimitUpPoolRun(trade_date=trade_date)
        db.add(run)
    run.source = meta["source"]
    run.quality = meta["quality"]
    run.east_count = int(meta["east_count"])
    run.local_count = int(meta["local_count"])
    run.merged_count = int(meta["merged_count"])
    run.conflict_count = int(meta["conflict_count"])
    run.warnings = list(meta.get("warnings") or [])
    run.meta = {"merged": True, "built_at": time.time()}
    db.commit()
    db.refresh(run)
    return run


def get_pool(
    trade_date: str,
    *,
    db: Session | None = None,
    refresh: bool = False,
) -> dict:
    own = db is None
    session = db or SessionLocal()
    try:
        if not refresh:
            run, rows = _read_run(session, trade_date)
            built_at = float((run.meta or {}).get("built_at") or 0) if run is not None else 0.0
            fresh_today = (
                trade_date == date_cls.today().isoformat()
                and built_at > 0
                and time.time() - built_at < 120
            )
            if run is not None and (trade_date != date_cls.today().isoformat() or fresh_today) and (rows or run.merged_count == 0):
                return {
                    "trade_date": trade_date,
                    "items": rows,
                    "source": run.source,
                    "quality": run.quality,
                    "counts": {
                        "east": run.east_count,
                        "local": run.local_count,
                        "merged": run.merged_count,
                        "conflicts": run.conflict_count,
                    },
                    "warnings": list(run.warnings or []),
                    "cached": True,
                }
        east = _east_items(trade_date)
        local = _local_items(trade_date)
        rows, meta = reconcile(trade_date, east, local)
        run = _persist(session, trade_date, rows, meta)
        if own:
            session.commit()
        return {
            "trade_date": trade_date,
            "items": rows,
            "source": meta["source"],
            "quality": meta["quality"],
            "counts": {
                "east": meta["east_count"],
                "local": meta["local_count"],
                "merged": meta["merged_count"],
                "conflicts": meta["conflict_count"],
            },
            "warnings": list(meta.get("warnings") or []),
            "cached": False,
            "run_id": run.id,
        }
    finally:
        if own:
            session.close()


def _dt_items(trade_date: str) -> list[dict]:
    try:
        from src.platform.marketdata.marketdata_client import get_market_data

        rows = get_market_data().dt_pool(date=trade_date.replace("-", ""), limit=300)
        return [
            {
                "symbol": str(getattr(row, "code", "") or ""),
                "name": str(getattr(row, "name", "") or ""),
                "change_pct": getattr(row, "change_pct", None),
                "industry": str(getattr(row, "industry", "") or "").strip() or None,
                "seal_fund": getattr(row, "seal_fund", None),
                "amount": getattr(row, "amount", None),
                "turnover_rate": getattr(row, "turnover_rate", None),
                "float_market_cap": getattr(row, "float_market_cap", None),
                "source": "eastmoney",
                "fields_complete": True,
            }
            for row in rows
            if getattr(row, "code", None)
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("跌停池(东财)获取失败 %s: %s", trade_date, exc)
        return []


def _down_streaks(db: Session, trade_date: str, rows: list[dict]) -> None:
    """Fill limit_down_days from daily bars, board/ST aware."""
    symbols = sorted({str(row.get("symbol")) for row in rows if row.get("symbol")})
    if not symbols:
        return
    dates = [
        str(r[0])
        for r in db.execute(
            text(
                """
SELECT DISTINCT trade_date FROM daily_bars
WHERE trade_date <= :end ORDER BY trade_date DESC LIMIT 30
"""
            ),
            {"end": trade_date},
        ).fetchall()
    ]
    if not dates:
        return
    start = min(dates)
    bars = db.execute(
        text(
            """
SELECT symbol, trade_date, close FROM daily_bars
WHERE symbol IN :symbols AND trade_date BETWEEN :start AND :end
ORDER BY symbol, trade_date
"""
        ).bindparams(bindparam("symbols", expanding=True)),
        {"symbols": symbols, "start": start, "end": trade_date},
    ).fetchall()
    grouped: dict[str, list[tuple[str, float]]] = {}
    for symbol, day, close in bars:
        try:
            grouped.setdefault(str(symbol), []).append((str(day), float(close)))
        except (TypeError, ValueError):
            continue
    by_symbol = {str(row["symbol"]): row for row in rows}
    for symbol, series in grouped.items():
        series.sort(key=lambda x: x[0])
        if len(series) < 6 or series[-1][0] != trade_date:
            by_symbol[symbol]["limit_down_days"] = 1
            continue
        row = by_symbol[symbol]
        pct = _limit_pct(symbol, str(row.get("name") or "")) / 100.0
        streak = 0
        for index in range(len(series) - 1, 0, -1):
            day, close = series[index]
            prev = series[index - 1][1]
            if prev <= 0:
                break
            if close / prev - 1 <= -pct + 0.005:
                streak += 1
            else:
                break
        row["limit_down_days"] = max(1, streak)
    for row in rows:
        row.setdefault("limit_down_days", 1)


def _local_down_items(trade_date: str, db: Session) -> list[dict]:
    rows = db.execute(
        text(
            """
SELECT b.symbol, b.close, (
  SELECT p.close FROM daily_bars p
  WHERE p.symbol = b.symbol AND p.trade_date < b.trade_date
  ORDER BY p.trade_date DESC LIMIT 1
) AS prev_close
FROM daily_bars b
WHERE b.trade_date = :day AND b.close IS NOT NULL
"""
        ),
        {"day": trade_date},
    ).fetchall()
    names: dict[str, str] = {}
    try:
        from src.modules.screener import snapshot as snapshot_mod

        names = {str(q.get("symbol")): str(q.get("name") or "") for q in snapshot_mod.get_snapshot()}
    except Exception:  # noqa: BLE001
        names = {}
    out: list[dict] = []
    for symbol, close, prev in rows:
        try:
            ratio = float(close) / float(prev) - 1.0
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        code = str(symbol)
        name = names.get(code, "")
        pct = _limit_pct(code, name) / 100.0
        if ratio > -pct + 0.005:
            continue
        out.append(
            {
                "symbol": code,
                "name": name,
                "change_pct": ratio * 100,
                "industry": None,
                "seal_fund": None,
                "amount": None,
                "turnover_rate": None,
                "float_market_cap": None,
                "source": "local",
                "fields_complete": False,
                "limit_down_days": 1,
            }
        )
    return out


def get_limit_down_ladder(trade_date: str, *, db: Session | None = None) -> dict:
    """Limit-down ladder: Eastmoney dt_pool + local consecutive-down days."""
    own = db is None
    session = db or SessionLocal()
    try:
        rows = _dt_items(trade_date)
        source = "eastmoney"
        warnings: list[str] = []
        if not rows:
            rows = _local_down_items(trade_date, session)
            source = "local"
            if rows:
                warnings.append("东财跌停池不可用，已用本地日K推算；封单/行业字段缺失")
        if not rows:
            warnings.append("东财与本地都没有可用跌停数据")
        _down_streaks(session, trade_date, rows)
        for row in rows:
            days = int(row.get("limit_down_days") or 1)
            row["limit_down_days"] = days
            row["stat"] = f"{days} 连跌"
        rows.sort(key=lambda r: (-int(r.get("limit_down_days") or 1), r.get("symbol") or ""))
        buckets: dict[int, list[dict]] = {}
        for row in rows:
            buckets.setdefault(int(row.get("limit_down_days") or 1), []).append(row)
        groups = []
        for days in sorted(buckets, reverse=True):
            groups.append(
                {
                    "level": "首跌" if days <= 1 else f"{days} 连跌",
                    "days": days,
                    "items": [
                        {
                            "symbol": row["symbol"],
                            "name": row.get("name") or "",
                            "time": "",
                            "concept": row.get("industry") or "",
                            "concepts": [row["industry"]] if row.get("industry") else [],
                            "industry": row.get("industry") or "",
                            "stat": row["stat"],
                            "seal_fund": row.get("seal_fund"),
                            "break_count": 0,
                            "source": row.get("source") or "eastmoney",
                            "fields_complete": bool(row.get("fields_complete")),
                            "limit_down_days": int(row.get("limit_down_days") or 1),
                        }
                        for row in buckets[days]
                    ],
                }
            )
        up_pool = get_pool(trade_date, db=session)
        return {
            "trade_date": trade_date,
            "side": "down",
            "stats": {
                "trade_date": trade_date,
                "limit_up": int((up_pool.get("counts") or {}).get("merged") or 0),
                "limit_down": len(rows),
                "max_days": max((g["days"] for g in groups), default=0),
                "limit_down_source": source,
            },
            "groups": groups,
            "filter": {"concept": None, "count": len(rows), "max_days": max((g["days"] for g in groups), default=0)},
            "concept_index_ready": True,
            "source": source,
            "quality": "ok" if source == "eastmoney" else "partial",
            "counts": {"merged": len(rows)},
            "warnings": warnings,
        }
    finally:
        if own:
            session.close()


def local_limit_down_count(trade_date: str) -> int:
    """Fallback count for limit-down when Eastmoney's dt_pool is unavailable."""
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                """
SELECT b.symbol, b.close, (
  SELECT p.close FROM daily_bars p
  WHERE p.symbol = b.symbol AND p.trade_date < b.trade_date
  ORDER BY p.trade_date DESC LIMIT 1
) AS prev_close
FROM daily_bars b
WHERE b.trade_date = :day AND b.close IS NOT NULL
"""
            ),
            {"day": trade_date},
        ).fetchall()
    finally:
        db.close()
    count = 0
    for symbol, close, prev in rows:
        try:
            ratio = float(close) / float(prev) - 1.0
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        pct = _limit_pct(str(symbol), "") / 100.0
        if ratio <= -pct + 0.005:
            count += 1
    return count


__all__ = ["get_pool", "reconcile", "local_limit_down_count"]
