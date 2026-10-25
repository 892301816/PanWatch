"""数据集持久化:日K upsert、同步游标、数据源配置。"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import DailyBar, DatasetSyncState

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]

# 日K同步支持的 provider(与 marketdata 包内 kline vendors 同名)
SUPPORTED_DAILY = ("fuyao", "tickflow", "tdxquant")

# 运行期健康状态(DataSource 表没有 last_ok 字段,放内存即可)
_HEALTH: dict[str, dict] = {}


def data_dir() -> Path:
    raw = os.environ.get("DATA_DIR", "").strip()
    root = Path(raw) if raw else (_PROJECT_ROOT / "data")
    root.mkdir(parents=True, exist_ok=True)
    return root


def cache_dir(provider: str) -> Path:
    path = data_dir() / "dataset_cache" / provider
    path.mkdir(parents=True, exist_ok=True)
    return path


# ── 数据源配置 ────────────────────────────────────────────────────────
def ensure_defaults() -> None:
    """兼容旧版本:清理冗余索引。数据源本身由 DataSource 表管理。"""
    db = SessionLocal()
    try:
        # symbol 单列索引与唯一索引前缀重复,占 60MB+ 且拖慢写入
        db.execute(text("DROP INDEX IF EXISTS ix_daily_bars_symbol"))
        db.commit()
    finally:
        db.close()


def _provider_name(db, provider_id: int) -> str:
    from src.platform.persistence.models import DataSource

    row = db.query(DataSource).filter(DataSource.id == provider_id).first()
    return row.provider if row else ""


def list_providers(dataset: str = "daily") -> list[dict]:
    """日K数据源 = DataSource 表里 type=kline 的同花顺/TickFlow/通达信,按优先级排序。"""
    from src.platform.persistence.models import DataSource

    ensure_defaults()
    db = SessionLocal()
    try:
        rows = (
            db.query(DataSource)
            .filter(
                DataSource.type == "kline",
                DataSource.provider.in_(SUPPORTED_DAILY),
            )
            .order_by(DataSource.priority.asc(), DataSource.id.asc())
            .all()
        )
        out = []
        for r in rows:
            health = _HEALTH.get(r.provider) or {}
            out.append(
                {
                    "id": r.id,
                    "dataset": dataset,
                    "provider": r.provider,
                    "priority": r.priority if r.priority is not None else 100,
                    "enabled": bool(r.enabled),
                    "config": r.config or {},
                    "last_ok_at": health.get("last_ok_at", ""),
                    "last_error": health.get("last_error", ""),
                    "fail_count": health.get("fail_count", 0),
                }
            )
        return out
    finally:
        db.close()


def update_provider(provider_id: int, **fields) -> dict | None:
    from src.platform.persistence.models import DataSource

    db = SessionLocal()
    try:
        row = db.query(DataSource).filter(DataSource.id == provider_id).first()
        if row is None:
            return None
        for key in ("priority", "enabled", "config"):
            if key in fields and fields[key] is not None:
                setattr(row, key, fields[key])
        db.commit()
        db.refresh(row)
        return {
            "id": row.id,
            "provider": row.provider,
            "priority": row.priority,
            "enabled": bool(row.enabled),
            "config": row.config or {},
        }
    finally:
        db.close()


def record_success(provider_id: int) -> None:
    db = SessionLocal()
    try:
        name = _provider_name(db, provider_id)
        if name:
            _HEALTH[name] = {
                "last_ok_at": datetime.now().isoformat(),
                "last_error": "",
                "fail_count": 0,
            }
    finally:
        db.close()


def record_failure(provider_id: int, error: str) -> None:
    db = SessionLocal()
    try:
        name = _provider_name(db, provider_id)
        if name:
            current = _HEALTH.setdefault(
                name, {"last_ok_at": "", "last_error": "", "fail_count": 0}
            )
            current["last_error"] = str(error)[:500]
            current["fail_count"] = int(current.get("fail_count") or 0) + 1
    finally:
        db.close()


# ── 日K ───────────────────────────────────────────────────────────────
_UPSERT_SQL = text(
    """
INSERT INTO daily_bars (symbol, trade_date, open, high, low, close, volume, amount, source)
VALUES (:symbol, :trade_date, :open, :high, :low, :close, :volume, :amount, :source)
ON CONFLICT(symbol, trade_date) DO UPDATE SET
    open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
    volume=excluded.volume, amount=excluded.amount, source=excluded.source,
    updated_at=CURRENT_TIMESTAMP
"""
)


def upsert_bars(df: pd.DataFrame, source: str, chunk_size: int = 5000) -> int:
    """批量 upsert。返回写入行数。"""
    if df is None or len(df) == 0:
        return 0
    records = []
    for row in df.itertuples(index=False):
        records.append(
            {
                "symbol": str(getattr(row, "symbol", "") or ""),
                "trade_date": str(getattr(row, "trade_date", "") or "")[:10],
                "open": _f(getattr(row, "open", None)),
                "high": _f(getattr(row, "high", None)),
                "low": _f(getattr(row, "low", None)),
                "close": _f(getattr(row, "close", None)),
                "volume": _f(getattr(row, "volume", None)),
                "amount": _f(getattr(row, "amount", None)),
                "source": source,
            }
        )
    db = SessionLocal()
    written = 0
    try:
        for start in range(0, len(records), chunk_size):
            chunk = records[start:start + chunk_size]
            db.execute(_UPSERT_SQL, chunk)
            db.commit()
            written += len(chunk)
    finally:
        db.close()
    return written


def _f(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


def latest_bar_date() -> str:
    db = SessionLocal()
    try:
        value = db.execute(text("SELECT MAX(trade_date) FROM daily_bars")).scalar()
        return str(value or "")
    finally:
        db.close()


def bar_stats() -> dict:
    db = SessionLocal()
    try:
        total = db.execute(text("SELECT COUNT(*) FROM daily_bars")).scalar() or 0
        days = db.execute(text("SELECT COUNT(DISTINCT trade_date) FROM daily_bars")).scalar() or 0
        symbols = db.execute(text("SELECT COUNT(DISTINCT symbol) FROM daily_bars")).scalar() or 0
        return {"rows": int(total), "days": int(days), "symbols": int(symbols)}
    finally:
        db.close()


def recent_closes(symbols: list[str], days: int = 20) -> dict[str, list[float]]:
    """取每只股票最近 N 个收盘价(升序),供迷你走势。

    结果集通常 ≤40 只,走 (symbol) 索引逐只查询比 IN 展开更省事也够快。
    """
    if not symbols:
        return {}
    out: dict[str, list[float]] = {}
    db = SessionLocal()
    try:
        for symbol in symbols:
            rows = db.execute(
                text(
                    "SELECT close FROM daily_bars WHERE symbol = :symbol "
                    "ORDER BY trade_date DESC LIMIT :limit"
                ),
                {"symbol": symbol, "limit": max(1, int(days))},
            ).fetchall()
            closes = [float(r[0]) for r in rows if r[0] is not None]
            if closes:
                out[str(symbol)] = list(reversed(closes))
    finally:
        db.close()
    return out


def bars_for_symbol(symbol: str, days: int = 60) -> list[dict]:
    db = SessionLocal()
    try:
        rows = (
            db.query(DailyBar)
            .filter(DailyBar.symbol == symbol)
            .order_by(DailyBar.trade_date.desc())
            .limit(max(1, int(days)))
            .all()
        )
        return [
            {
                "date": r.trade_date,
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "volume": r.volume,
                "amount": r.amount,
            }
            for r in reversed(rows)
        ]
    finally:
        db.close()


# ── 同步状态 ──────────────────────────────────────────────────────────
def get_sync_state(dataset: str = "daily") -> dict:
    db = SessionLocal()
    try:
        row = (
            db.query(DatasetSyncState)
            .filter(DatasetSyncState.dataset == dataset)
            .first()
        )
        if row is None:
            return {
                "dataset": dataset,
                "last_date": "",
                "last_mode": "",
                "rows": 0,
                "status": "idle",
                "error": "",
                "started_at": "",
                "finished_at": "",
            }
        return {
            "dataset": row.dataset,
            "last_date": row.last_date or "",
            "last_mode": row.last_mode or "",
            "rows": row.rows or 0,
            "status": row.status or "idle",
            "error": row.error or "",
            "started_at": row.started_at.isoformat() if row.started_at else "",
            "finished_at": row.finished_at.isoformat() if row.finished_at else "",
        }
    finally:
        db.close()


def update_sync_state(dataset: str = "daily", **fields) -> None:
    db = SessionLocal()
    try:
        row = (
            db.query(DatasetSyncState)
            .filter(DatasetSyncState.dataset == dataset)
            .first()
        )
        if row is None:
            row = DatasetSyncState(dataset=dataset)
            db.add(row)
        for key in (
            "last_date", "last_mode", "rows", "status", "error",
            "started_at", "finished_at",
        ):
            if key in fields:
                setattr(row, key, fields[key])
        db.commit()
    finally:
        db.close()
