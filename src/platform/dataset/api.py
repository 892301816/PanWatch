"""行情数据集 HTTP 接口。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.platform.dataset import router as dataset_router
from src.platform.dataset import scheduler as dataset_scheduler
from src.platform.dataset import store, sync
from src.platform.dataset.providers.base import empty_daily
from src.platform.persistence.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


class SyncIn(BaseModel):
    mode: str = Field(default="incremental", pattern="^(incremental|full)$")
    days: int | None = None
    provider: str | None = None
    allow_fallback: bool = True


class ProviderIn(BaseModel):
    priority: int | None = None
    enabled: bool | None = None
    config: dict | None = None


class ScheduleIn(BaseModel):
    enabled: bool | None = None
    hour: int | None = None
    minute: int | None = None


def _mask(config: dict) -> dict:
    out = dict(config or {})
    key = str(out.get("api_key") or "")
    if key:
        out["api_key"] = f"{key[:3]}****{key[-4:]}" if len(key) > 8 else "****"
    return out


def _provider_view(row: dict) -> dict:
    return {
        "id": row["id"],
        "provider": row["provider"],
        "priority": row["priority"],
        "enabled": row["enabled"],
        "configured": bool((row.get("config") or {}).get("api_key")),
        "config": _mask(row.get("config") or {}),
        "last_ok_at": row.get("last_ok_at", ""),
        "last_error": row.get("last_error", ""),
        "fail_count": row.get("fail_count", 0),
    }


@router.get("/status")
def get_status(db: Session = Depends(get_db)):
    store.ensure_defaults()
    sync.maybe_auto_sync()
    schedule = dataset_scheduler.get_schedule()
    return {
        "providers": [_provider_view(r) for r in store.list_providers("daily")],
        "sync": store.get_sync_state("daily"),
        "stats": store.bar_stats(),
        "progress": sync.get_progress(),
        "schedule": {**schedule, "next_run_at": dataset_scheduler.next_run_at()},
        "stale": sync.is_stale(),
    }


@router.post("/sync")
def start_sync(payload: SyncIn):
    started = sync.start_sync_async(
        mode=payload.mode,
        days=payload.days,
        provider=payload.provider,
        allow_fallback=payload.allow_fallback,
    )
    if not started:
        raise HTTPException(409, "已有同步任务在运行")
    return {"started": True, "mode": payload.mode}


@router.get("/progress")
def get_progress():
    return sync.get_progress()


@router.put("/providers/{provider_id}")
def update_provider(provider_id: int, payload: ProviderIn):
    existing = [r for r in store.list_providers("daily") if r["id"] == provider_id]
    current = existing[0] if existing else None
    if current is None:
        raise HTTPException(404, "数据源不存在")

    config = payload.config
    if config is not None:
        # 掩码占位符表示「保持原 Key 不变」
        masked = str(config.get("api_key") or "")
        if "****" in masked:
            config = {**config}
            config.pop("api_key", None)
            old = (current.get("config") or {}).get("api_key")
            if old:
                config["api_key"] = old
        merged = {**(current.get("config") or {}), **config}
        config = merged

    updated = store.update_provider(
        provider_id,
        priority=payload.priority,
        enabled=payload.enabled,
        config=config,
    )
    dataset_router.reset_cooldowns()
    return updated


@router.post("/providers/{provider_id}/test")
def test_provider(provider_id: int):
    rows = [r for r in store.list_providers("daily") if r["id"] == provider_id]
    if not rows:
        raise HTTPException(404, "数据源不存在")
    row = rows[0]
    ok, detail = dataset_router.probe(row["provider"], row.get("config") or {})
    return {"provider": row["provider"], "ok": ok, "detail": detail}


@router.put("/schedule")
def update_schedule(payload: ScheduleIn):
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    cfg = dataset_scheduler.set_schedule(data)
    return {**cfg, "next_run_at": dataset_scheduler.next_run_at()}


@router.get("/bars/{symbol}")
def get_bars(symbol: str, days: int = Query(default=60, ge=5, le=1000)):
    rows = store.bars_for_symbol(symbol, days)
    if not rows:
        from src.platform.dataset.symbols import to_std

        rows = store.bars_for_symbol(to_std(symbol), days)
    return {"symbol": symbol, "count": len(rows), "bars": rows}


@router.get("/spark")
def get_spark(
    symbols: str = Query(default=""),
    days: int = Query(default=20, ge=5, le=60),
    fallback: bool = Query(default=True, description="本地缺数据时是否回退 K 线接口"),
):
    """批量迷你走势。优先本地 daily_bars;新股等本地没有的回退逐只拉一次。"""
    codes = [s.strip() for s in symbols.split(",") if s.strip()][:200]
    data = store.recent_closes(codes, days)

    missing = [c for c in codes if len(data.get(c) or []) < 2]
    if fallback and missing:
        from concurrent.futures import ThreadPoolExecutor

        from src.platform.marketdata.collectors.kline_collector import KlineCollector
        from src.platform.marketdata.models import MarketCode

        def _fetch(code: str) -> tuple[str, list[float]]:
            try:
                bars = KlineCollector(MarketCode.CN).get_klines(code, days=max(20, days))
                closes = [
                    round(float(b.close), 2) for b in bars if b.close is not None
                ][-days:]
                return code, closes
            except Exception:  # noqa: BLE001
                return code, []

        with ThreadPoolExecutor(max_workers=6) as pool:
            for code, closes in pool.map(_fetch, missing[:40]):
                if closes:
                    data[code] = closes

    return {"data": data}
