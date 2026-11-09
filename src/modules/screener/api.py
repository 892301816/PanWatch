"""选股工作台 HTTP 接口。"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.modules.screener import ai_parse, concepts, engine, history, snapshot
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
    offset: int = 0         # 服务端分页起点
    save: bool = True       # 是否写入选股历史(自动记录可关闭)
    code: str = ""          # 来源标识: custom / preset xxx / rule:123
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


def _source_of(code: str) -> tuple[str, int | None]:
    """把前端的分组标识翻译成历史来源。"""
    code = (code or "").strip()
    if code.startswith("rule:"):
        try:
            return "rule", int(code.split(":", 1)[1])
        except ValueError:
            return "rule", None
    if code and code != "custom":
        return "preset", None
    return "custom", None


@router.post("/run")
def run_screen(payload: ScreenIn, background: BackgroundTasks):
    """执行选股。同步函数,由 FastAPI 放到线程池,不阻塞事件循环。"""
    logger.info(
        "[选股] 执行 logic=%s conditions=%s limit=%s offset=%s",
        payload.logic,
        [f"{c.field}:{c.op}:{c.value}" for c in payload.conditions],
        payload.limit,
        payload.offset,
    )
    started = time.time()
    try:
        result = engine.run_screen(payload.model_dump())
    except Exception as exc:  # noqa: BLE001
        logger.exception("[选股] 执行失败")
        raise HTTPException(500, f"选股失败: {exc}")

    extra = result.pop("_history", None)
    if extra:
        source, rule_id = _source_of(payload.code)
        # 保存放到响应之后,不拖慢选股请求
        background.add_task(
            history.save_run,
            trade_date=result.get("trade_date") or "",
            payload=payload.model_dump(),
            result={**result, "_history": extra},
            source=source,
            rule_id=rule_id,
            rule_name="",
            created_by="",
            duration_ms=int((time.time() - started) * 1000),
        )
    return result


@router.get("/ladder")
def get_ladder(
    date: str | None = Query(default=None, description="YYYY-MM-DD"),
    concept: str | None = Query(default=None, description="只看该概念板块的涨停股"),
):
    """连涨天梯:按连板数分组 + 板块名,可按概念板块过滤。"""
    try:
        return engine.get_ladder(date, concept=concept)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[选股] 天梯失败")
        raise HTTPException(500, f"天梯加载失败: {exc}")


@router.get("/boards")
def get_boards(
    date: str | None = Query(default=None, description="YYYY-MM-DD"),
    limit: int = Query(default=30, ge=1, le=100),
    include_style: bool = Query(default=False, description="是否包含新高/破净/红利等风格板块"),
):
    """板块强弱榜(默认只返回主题/概念板块)。"""
    try:
        return engine.get_boards(date, limit=limit, include_style=include_style)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[选股] 板块榜失败")
        raise HTTPException(500, f"板块榜加载失败: {exc}")


@router.get("/concepts")
def list_concepts():
    """今日索引里的概念名(供条件编辑器做候选,避免手输与索引口径不一致)。"""
    trade_date = snapshot.latest_trade_date().isoformat()
    concepts.ensure_concept_index(trade_date)
    mapping = concepts.get_concept_map(trade_date)
    seen: dict[str, int] = {}
    for cons in mapping.values():
        for c in cons:
            name = str(c.get("name") or "").strip()
            if name and name not in seen:
                seen[name] = int(c.get("rank") or 9999)
    items = [
        {"name": name, "rank": rank}
        for name, rank in sorted(seen.items(), key=lambda x: (x[1], x[0]))
    ]
    return {
        "trade_date": trade_date,
        "items": items,
        "source": concepts.board_source(),
    }


@router.get("/history")
def list_history(
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    date: str | None = Query(default=None, description="YYYY-MM-DD"),
    q: str = Query(default="", max_length=40),
):
    """选股历史列表(按时间倒序)。"""
    return history.list_runs(limit=limit, offset=offset, date_str=date, q=q)


@router.get("/history/{run_id}")
def get_history(run_id: int):
    run = history.get_run(run_id)
    if not run:
        raise HTTPException(404, "记录不存在")
    return run


@router.get("/history/{run_id}/performance")
def get_history_performance(run_id: int):
    """历史名单的 T+1/3/5/10 日表现(按需计算并缓存)。"""
    data = history.performance(run_id)
    if not data:
        raise HTTPException(404, "记录不存在或没有可统计的行情")
    return data


@router.delete("/history/{run_id}")
def delete_history(run_id: int):
    if not history.delete_run(run_id):
        raise HTTPException(404, "记录不存在")
    return {"deleted": run_id}


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
        rows = concepts.build_concept_index(result["trade_date"])
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
