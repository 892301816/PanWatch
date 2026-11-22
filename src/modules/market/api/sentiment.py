"""Market sentiment cycle API (read-only snapshot + manual refresh)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.modules.market import sentiment_service
from src.platform.persistence.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/sentiment")
def get_sentiment(
    days: int = Query(60, ge=1, le=250),
    db: Session = Depends(get_db),
):
    """Latest sentiment snapshot plus recent history. Reads the DB only."""
    return sentiment_service.get_payload(db, days=days)


@router.post("/sentiment/refresh")
def refresh_sentiment(db: Session = Depends(get_db)):
    """Compute today's snapshot from local daily bars + the limit-up pool."""
    try:
        result = sentiment_service.compute_and_store(db)
    except Exception as exc:  # noqa: BLE001
        logger.exception("市场情绪刷新失败")
        return {"ok": False, "error": f"刷新失败: {exc}"}
    if not result.get("ok"):
        return result
    return {
        "ok": True,
        "date": result.get("date"),
        "existing": result.get("existing", False),
        "snapshot": result.get("snapshot"),
    }
