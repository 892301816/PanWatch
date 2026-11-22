"""Assemble the market sentiment cycle from PanWatch's local daily bars.

Data path:

    daily_bars (all A-shares, <= 25 sessions)
        + marketdata.zt_pool (sealed limit-ups / industry / board count)
        + index quote (HS300 return)
        -> sentiment_cycle.EmotionEngine
        -> market_sentiment_snapshots + market_sentiment_state

The API reads only the snapshot table; the expensive full-market pass happens in
``compute_and_store`` (refresh / first generation). No baostock, no new deps.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.modules.market.sentiment_cycle import (
    Bar,
    Config,
    EmotionEngine,
    Signal,
    Snapshot,
    State,
    classify,
    normalize_symbol,
)
from src.platform.persistence.models import MarketSentimentSnapshot, MarketSentimentState

logger = logging.getLogger(__name__)

_COMPUTE_LOCK = threading.Lock()
_NAME_CACHE: dict[str, str] = {}
_HS300_CACHE: dict[str, float] = {}
_HISTORY_DAYS = 25


def _is_cn_stock(symbol: str) -> bool:
    sym = normalize_symbol(symbol)
    return len(sym) == 6 and sym[0] in ("0", "3", "4", "6", "8")


def _cn_names() -> dict[str, str]:
    global _NAME_CACHE
    if _NAME_CACHE:
        return _NAME_CACHE
    try:
        from src.platform.marketdata.stock_list import get_stock_list

        rows = get_stock_list() or []
        _NAME_CACHE = {
            normalize_symbol(row.get("symbol", "")): str(row.get("name") or "")
            for row in rows
            if str(row.get("market") or "").upper() == "CN"
            and _is_cn_stock(str(row.get("symbol") or ""))
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("加载股票名称失败: %s", exc)
        _NAME_CACHE = {}
    return _NAME_CACHE


def _latest_bar_dates(db: Session, limit: int = 30) -> list[str]:
    rows = db.execute(
        text(
            """
SELECT DISTINCT trade_date
FROM daily_bars
ORDER BY trade_date DESC
LIMIT :limit
"""
        ),
        {"limit": limit},
    ).fetchall()
    return [str(r[0]) for r in rows if r and r[0]]


def _load_bars(db: Session, end_date: str, days: int = _HISTORY_DAYS) -> dict[str, list[Bar]]:
    # Look back from the requested date, not from "today": backfill needs the
    # 25 sessions *before* an older target, not the latest 25 dates overall.
    rows = db.execute(
        text(
            """
SELECT DISTINCT trade_date
FROM daily_bars
WHERE trade_date <= :end
ORDER BY trade_date DESC
LIMIT :limit
"""
        ),
        {"end": end_date, "limit": days},
    ).fetchall()
    dates = [str(r[0]) for r in rows if r and r[0]]
    if not dates:
        return {}
    start = min(dates)
    rows = db.execute(
        text(
            """
SELECT symbol, trade_date, open, high, low, close, volume
FROM daily_bars
WHERE trade_date >= :start AND trade_date <= :end
ORDER BY symbol, trade_date
"""
        ),
        {"start": start, "end": end_date},
    ).fetchall()
    grouped: dict[str, list[Bar]] = {}
    for row in rows:
        symbol = normalize_symbol(str(row[0]))
        if not _is_cn_stock(symbol):
            continue
        close = float(row[5] or 0.0)
        if close <= 0:
            continue
        grouped.setdefault(symbol, []).append(
            Bar(
                symbol=symbol,
                trade_date=str(row[1]),
                open=float(row[2] or 0.0),
                high=float(row[3] or 0.0),
                low=float(row[4] or 0.0),
                close=close,
                preclose=0.0,
                turnover=0.0,
            )
        )
    for bars in grouped.values():
        bars.sort(key=lambda b: b.trade_date)
        for index in range(1, len(bars)):
            bars[index].preclose = bars[index - 1].close
    return grouped


def _zt_pool_map(db: Session, trade_date: str) -> tuple[dict[str, dict], bool, list[str], str]:
    """Read the shared limit-up pool (Eastmoney + local reconciliation).

    Returns (map, eastmoney_available, warnings, source). When only local rows
    exist, industry is missing, so the caller must mark L2/position tier as
    partial instead of pretending the main line is known.
    """
    try:
        from src.modules.screener.limit_up_service import get_pool

        payload = get_pool(trade_date, db=db)
        rows = payload.get("items") or []
        mapping = {
            normalize_symbol(str(row.get("symbol") or "")): {
                "name": str(row.get("name") or ""),
                "industry": row.get("industry"),
                "board_num": int(row.get("limit_up_days") or 0),
                "turnover": float(row.get("turnover_rate") or 0.0),
            }
            for row in rows
            if row.get("symbol")
        }
        return (
            mapping,
            bool((payload.get("counts") or {}).get("east")),
            list(payload.get("warnings") or []),
            str(payload.get("source") or "local"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("共享涨停池读取失败 %s: %s", trade_date, exc)
        return {}, False, ["涨停池读取失败，L2/仓位档未生成"], "local"


def _hs300_returns() -> dict[str, float]:
    """HS300 daily returns keyed by trade date, cached for the process."""
    global _HS300_CACHE
    if _HS300_CACHE:
        return _HS300_CACHE
    try:
        from src.platform.marketdata.marketdata_client import get_market_data

        bars = get_market_data().index_klines("000300", market="CN", days=180) or []
        out: dict[str, float] = {}
        for index in range(1, len(bars)):
            prev = float(getattr(bars[index - 1], "close", 0.0) or 0.0)
            cur = float(getattr(bars[index], "close", 0.0) or 0.0)
            date = str(getattr(bars[index], "date", "") or "")
            if date and prev > 0 and cur > 0:
                out[date] = cur / prev - 1.0
        _HS300_CACHE = out
    except Exception as exc:  # noqa: BLE001
        logger.warning("沪深300历史K线获取失败: %s", exc)
    return _HS300_CACHE


def _hs300_return(trade_date: Optional[str] = None) -> Optional[float]:
    if trade_date:
        return _hs300_returns().get(trade_date)
    try:
        from src.platform.marketdata.marketdata_client import get_market_data

        rows = get_market_data().index_quotes(["sh000300"])
        if not rows:
            return None
        quote = rows[0]
        change_pct = quote.get("change_pct")
        if change_pct is not None:
            return float(change_pct) / 100.0
        current = quote.get("current_price")
        prev = quote.get("prev_close")
        if current and prev:
            return float(current) / float(prev) - 1.0
    except Exception as exc:  # noqa: BLE001
        logger.warning("沪深300行情获取失败: %s", exc)
    return None


def _load_state(db: Session) -> State:
    row = db.query(MarketSentimentState).filter(MarketSentimentState.id == 1).first()
    if row is None:
        return State()
    return State.from_dict(row.state if isinstance(row.state, dict) else {})


def _save_state(db: Session, state: State) -> None:
    row = db.query(MarketSentimentState).filter(MarketSentimentState.id == 1).first()
    if row is None:
        row = MarketSentimentState(id=1)
        db.add(row)
    row.last_date = state.last_date
    row.state = state.to_dict()
    db.commit()


def _upsert_snapshot(db: Session, snapshot: Snapshot) -> MarketSentimentSnapshot:
    row = (
        db.query(MarketSentimentSnapshot)
        .filter(MarketSentimentSnapshot.trade_date == snapshot.date)
        .first()
    )
    if row is None:
        row = MarketSentimentSnapshot(trade_date=snapshot.date)
        db.add(row)
    row.l1 = float(snapshot.l1)
    row.mood = snapshot.mood
    row.systemic_risk = bool(snapshot.systemic_risk)
    row.drop_trigger = bool(snapshot.drop_trigger)
    row.limit_up = int(snapshot.limit_up_count)
    row.limit_down = int(snapshot.limit_down_count)
    row.high_face = int(snapshot.high_face_count)
    row.sky_floor = int(snapshot.sky_floor_count)
    row.main_sector = snapshot.main_sector
    row.main_sector_hl = int(snapshot.main_sector_hl)
    row.sector_sticky = bool(snapshot.sector_sticky)
    row.l2 = snapshot.l2
    row.l2_parts = snapshot.l2_parts or {}
    row.l3 = snapshot.l3
    row.leader = snapshot.leader
    row.leader_break_days = int(snapshot.leader_break_days)
    row.fry_rate = snapshot.fry_rate
    row.promote_rate = snapshot.promote_rate
    row.global_fry_rate = snapshot.global_fry_rate
    row.global_promote_rate = snapshot.global_promote_rate
    row.fry_baseline = snapshot.fry_baseline
    row.promote_baseline = snapshot.promote_baseline
    row.ebb_confirmed = bool(snapshot.ebb_confirmed)
    row.position_tier = snapshot.position_tier
    row.single_position = snapshot.single_position
    row.total_position = snapshot.total_position
    row.data_coverage = float(snapshot.data_coverage)
    row.source = snapshot.source
    row.warnings = list(snapshot.warnings or [])
    db.commit()
    db.refresh(row)
    return row


def compute_and_store(db: Session, trade_date: Optional[str] = None) -> dict:
    """Compute one trade day and persist it. Idempotent per date."""
    with _COMPUTE_LOCK:
        dates = _latest_bar_dates(db, limit=120)
        if not dates:
            return {"ok": False, "error": "daily_bars 为空，无法生成情绪快照"}
        target = trade_date or dates[0]
        if target not in dates:
            return {"ok": False, "error": f"{target} 没有本地日K"}
        existing = (
            db.query(MarketSentimentSnapshot)
            .filter(MarketSentimentSnapshot.trade_date == target)
            .first()
        )
        if existing is not None and existing.position_tier is not None:
            return {"ok": True, "date": target, "existing": True, "snapshot": _row_dict(existing)}
        # A partial snapshot (no tier) did not advance the state; allow retry
        # after the missing bars / industry data arrive.

        bars_by_symbol = _load_bars(db, target)
        if not bars_by_symbol:
            return {"ok": False, "error": "没有可用的日K"}

        today = {sym: bars for sym, bars in bars_by_symbol.items() if bars and bars[-1].trade_date == target}
        previous_date = next((d for d in dates if d < target), None)
        previous_count = sum(
            1 for bars in bars_by_symbol.values() if any(b.trade_date == previous_date for b in bars)
        ) if previous_date else len(today)
        coverage = len(today) / previous_count if previous_count else 0.0
        if len(today) < 500:
            return {"ok": False, "error": f"{target} 日K只有 {len(today)} 只，数据不足"}

        names = _cn_names()
        zt_map, zt_ok, pool_warnings, pool_source = _zt_pool_map(db, target)
        hs_ret = _hs300_return(target)
        missing_industry = 0
        suspect = 0
        no_limit = 0
        signals: list[Signal] = []
        for symbol, bars in today.items():
            today_bar = bars[-1]
            history = bars[:-1][-_HISTORY_DAYS:]
            zt = zt_map.get(symbol)
            name = (zt or {}).get("name") or names.get(symbol, "")
            industry = (zt or {}).get("industry")
            signal = classify(
                today_bar,
                history,
                name=name,
                industry=industry,
                zt_board_num=int((zt or {}).get("board_num") or 0),
            )
            signal.turnover = float((zt or {}).get("turnover") or 0.0)
            if signal.no_limit:
                no_limit += 1
            if signal.suspect:
                suspect += 1
            if (signal.limit_up or signal.break_up) and not industry:
                missing_industry += 1
            signals.append(signal)

        warnings: list[str] = list(pool_warnings)
        if coverage < 0.9:
            warnings.append(f"全市场日K覆盖 {coverage:.1%}，低于 90%，未生成仓位档")
        if hs_ret is None:
            warnings.append("沪深300收益缺失，L1 未计系统性风险")
        if suspect:
            warnings.append(f"{suspect} 只疑似除权/异常跳空，已跳过微观判定")
        if no_limit:
            warnings.append(f"{no_limit} 只上市不足 6 个交易日，未计入涨跌停")
        if missing_industry:
            warnings.append(f"{missing_industry} 只涨停/炸板股缺行业，未参与主线聚合")

        state = _load_state(db)
        previous_last_date = state.last_date
        engine = EmotionEngine(Config(), state)
        snapshot = engine.update(
            target,
            signals,
            hs_ret,
            data_coverage=coverage,
            source=pool_source,
            warnings=warnings,
        )

        partial = coverage < 0.9 or not zt_ok
        if partial:
            snapshot.l2 = None
            snapshot.position_tier = None
            snapshot.single_position = None
            snapshot.total_position = None
            snapshot.l2_parts = {}
            if not zt_map:
                snapshot.main_sector = None
                snapshot.main_sector_hl = 0
                snapshot.l3 = "none"
                snapshot.leader = None

        row = _upsert_snapshot(db, snapshot)
        if not partial and target > (previous_last_date or ""):
            _save_state(db, engine.state)
        return {"ok": True, "date": target, "existing": False, "snapshot": _row_dict(row)}


def _row_dict(row: MarketSentimentSnapshot) -> dict:
    leader = row.leader
    if leader:
        leader = _cn_names().get(normalize_symbol(leader), leader)
    warnings = list(row.warnings or [])
    if row.fry_rate is None and row.main_sector and row.l2 is not None:
        sample_warning = "主线 3 板以上不足 2 只，炸板率取中性 2 分（全市场值仅供对照）"
        if sample_warning not in warnings:
            warnings.append(sample_warning)
    return {
        "date": row.trade_date,
        "l1": row.l1,
        "mood": row.mood,
        "systemic_risk": bool(row.systemic_risk),
        "drop_trigger": bool(row.drop_trigger),
        "limit_up": row.limit_up,
        "limit_down": row.limit_down,
        "high_face": row.high_face,
        "sky_floor": row.sky_floor,
        "main_sector": row.main_sector,
        "main_sector_hl": row.main_sector_hl,
        "sector_sticky": bool(row.sector_sticky),
        "l2": row.l2,
        "l2_parts": row.l2_parts or {},
        "l3": row.l3,
        "leader": leader,
        "leader_break_days": row.leader_break_days,
        "fry_rate": row.fry_rate,
        "promote_rate": row.promote_rate,
        "global_fry_rate": row.global_fry_rate,
        "global_promote_rate": row.global_promote_rate,
        "fry_baseline": row.fry_baseline,
        "promote_baseline": row.promote_baseline,
        "ebb_confirmed": bool(row.ebb_confirmed),
        "tier": row.position_tier,
        "single_position": row.single_position,
        "total_position": row.total_position,
        "coverage": row.data_coverage,
        "source": row.source,
        "warnings": warnings,
        "partial": row.position_tier is None,
    }


def backfill(db: Session, days: int = 60) -> dict:
    """Rebuild the last N sessions in ascending order.

    Only safe on an empty state: the engine is path-dependent. Callers that
    already have a snapshot should skip re-running; the daily job is idempotent.
    """
    dates = _latest_bar_dates(db, limit=max(1, min(int(days), 120)))
    if not dates:
        return {"ok": False, "error": "没有日K"}
    dates = sorted(dates)
    state = _load_state(db)
    if state.last_date:
        return {
            "ok": False,
            "error": f"状态已推进到 {state.last_date}；回填前请先清空 sentiment 快照/状态",
        }
    results = []
    for date in dates:
        result = compute_and_store(db, date)
        results.append({"date": date, "ok": bool(result.get("ok")), "error": result.get("error")})
    return {
        "ok": True,
        "days": len(dates),
        "first": dates[0],
        "last": dates[-1],
        "failed": [r for r in results if not r["ok"]],
    }


def get_payload(db: Session, days: int = 60) -> dict:
    latest_date = None
    try:
        latest_date = _latest_bar_dates(db, limit=1)[0]
    except Exception:  # noqa: BLE001
        latest_date = None
    latest = (
        db.query(MarketSentimentSnapshot)
        .order_by(MarketSentimentSnapshot.trade_date.desc())
        .first()
    )
    history_rows = (
        db.query(MarketSentimentSnapshot)
        .order_by(MarketSentimentSnapshot.trade_date.desc())
        .limit(max(1, min(int(days), 250)))
        .all()
    )
    history = [_row_dict(r) for r in reversed(history_rows)]
    today = _row_dict(latest) if latest else None
    stale = bool(today and latest_date and today["date"] != latest_date)
    return {
        "today": today,
        "history": history,
        "stale": stale,
        "latest_bar_date": latest_date,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
