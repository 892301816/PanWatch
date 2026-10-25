"""选股工作台 HTTP 接口。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.modules.screener import ai_parse, concepts, engine, snapshot
from src.modules.screener.constants import (
    DISCLAIMER,
    FIELD_CATALOG,
    OPS,
    PRESETS,
)
from src.platform.persistence.database import get_db
from src.platform.persistence.models import ScreenerRule

logger = logging.getLogger(__name__)
router = APIRouter()


class ConditionIn(BaseModel):
    field: str
    op: str = "eq"
    value: Any = None


class ScreenIn(BaseModel):
    conditions: list[ConditionIn] = Field(default_factory=list)
    logic: str = "all"
    exclude: list[str] = Field(default_factory=lambda: ["st"])
    order_by: str = "change_pct desc"
    limit: int = 0          # 0 = 全部
    with_spark: bool = False
    refresh: bool = False


class ParseIn(BaseModel):
    text: str = Field(min_length=2, max_length=300)
    model_id: int | None = None


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    conditions: list[ConditionIn] = Field(default_factory=list)
    logic: str = "all"
    exclude: list[str] = Field(default_factory=lambda: ["st"])
    order_by: str = "change_pct desc"
    limit: int = 100
    market: str = "CN"


def _rule_to_dict(row: ScreenerRule) -> dict:
    payload = row.conditions or {}
    return {
        "code": f"rule:{row.id}",
        "id": row.id,
        "name": row.name,
        "description": payload.get("explanation") or "",
        "conditions": payload.get("conditions") or [],
        "logic": payload.get("logic", "all"),
        "exclude": payload.get("exclude", ["st"]),
        "order_by": row.order_by or payload.get("order_by") or "change_pct desc",
        "limit": row.limit_n or payload.get("limit") or 100,
        "is_preset": False,
    }


@router.get("/meta")
def get_meta(db: Session = Depends(get_db)):
    """字段目录、算子、预设策略与用户分组。"""
    rules = (
        db.query(ScreenerRule)
        .filter(ScreenerRule.is_preset.is_(False))
        .order_by(ScreenerRule.sort_order.asc(), ScreenerRule.id.asc())
        .all()
    )
    return {
        "fields": FIELD_CATALOG,
        "ops": OPS,
        "presets": [
            {**p, "is_preset": True, "logic": p.get("logic", "all")} for p in PRESETS
        ],
        "rules": [_rule_to_dict(r) for r in rules],
        "status": {
            **snapshot.snapshot_status(),
            "concepts": concepts.concept_index_status(
                snapshot.latest_trade_date().isoformat()
            ),
        },
        "disclaimer": DISCLAIMER,
    }


@router.post("/run")
def run_screen(payload: ScreenIn):
    """执行选股。同步函数,由 FastAPI 放到线程池,不阻塞事件循环。"""
    logger.info(
        "[选股] 执行 logic=%s conditions=%s limit=%s",
        payload.logic,
        [f"{c.field}:{c.op}:{c.value}" for c in payload.conditions],
        payload.limit,
    )
    try:
        return engine.run_screen(payload.model_dump())
    except Exception as exc:  # noqa: BLE001
        logger.exception("[选股] 执行失败")
        raise HTTPException(500, f"选股失败: {exc}")


@router.get("/ladder")
def get_ladder(date: str | None = Query(default=None, description="YYYY-MM-DD")):
    """连涨天梯:按连板数分组 + 板块名。"""
    try:
        return engine.get_ladder(date)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[选股] 天梯失败")
        raise HTTPException(500, f"天梯加载失败: {exc}")


@router.get("/boards")
def get_boards(
    date: str | None = Query(default=None, description="YYYY-MM-DD"),
    limit: int = Query(default=30, ge=1, le=100),
):
    """板块强弱榜。"""
    try:
        return engine.get_boards(date, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[选股] 板块榜失败")
        raise HTTPException(500, f"板块榜加载失败: {exc}")


@router.post("/parse")
async def parse_text(payload: ParseIn, db: Session = Depends(get_db)):
    """一句话 → 结构化条件(只翻译,不执行)。"""
    try:
        return await ai_parse.parse_text(db, payload.text, payload.model_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[选股] AI 解析失败: {exc}")
        raise HTTPException(503, f"AI 解析失败: {exc}")


@router.post("/refresh")
def refresh_snapshot():
    """强制刷新全市场快照与概念索引(盘后跑一次即可)。"""
    try:
        result = snapshot.refresh_snapshot(force=True)
        rows = concepts.build_concept_index(result["trade_date"], board_limit=160)
        return {**result, "concept_rows": rows}
    except Exception as exc:  # noqa: BLE001
        logger.exception("[选股] 刷新失败")
        raise HTTPException(500, f"刷新失败: {exc}")


@router.get("/status")
def get_status():
    trade_date = snapshot.latest_trade_date().isoformat()
    return {
        "snapshot": snapshot.snapshot_status(),
        "concepts": concepts.concept_index_status(trade_date),
    }


@router.get("/rules")
def list_rules(db: Session = Depends(get_db)):
    rows = (
        db.query(ScreenerRule)
        .filter(ScreenerRule.is_preset.is_(False))
        .order_by(ScreenerRule.sort_order.asc(), ScreenerRule.id.asc())
        .all()
    )
    return [_rule_to_dict(r) for r in rows]


@router.post("/rules")
def create_rule(payload: RuleIn, db: Session = Depends(get_db)):
    conditions = payload.model_dump()
    row = ScreenerRule(
        name=payload.name.strip(),
        market=payload.market,
        conditions={
            "conditions": conditions["conditions"],
            "logic": conditions["logic"],
            "exclude": conditions["exclude"],
            "order_by": conditions["order_by"],
            "limit": conditions["limit"],
        },
        order_by=payload.order_by,
        limit_n=payload.limit,
        is_preset=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _rule_to_dict(row)


@router.put("/rules/{rule_id}")
def update_rule(rule_id: int, payload: RuleIn, db: Session = Depends(get_db)):
    row = db.query(ScreenerRule).filter(ScreenerRule.id == rule_id).first()
    if not row:
        raise HTTPException(404, "分组不存在")
    data = payload.model_dump()
    row.name = payload.name.strip()
    row.market = payload.market
    row.conditions = {
        "conditions": data["conditions"],
        "logic": data["logic"],
        "exclude": data["exclude"],
        "order_by": data["order_by"],
        "limit": data["limit"],
    }
    row.order_by = payload.order_by
    row.limit_n = payload.limit
    db.commit()
    db.refresh(row)
    return _rule_to_dict(row)


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int, db: Session = Depends(get_db)):
    row = db.query(ScreenerRule).filter(ScreenerRule.id == rule_id).first()
    if not row:
        raise HTTPException(404, "分组不存在")
    db.delete(row)
    db.commit()
    return {"deleted": rule_id}
