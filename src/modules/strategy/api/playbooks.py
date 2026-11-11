"""交易策略卡与单票回测 API。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.modules.strategy import playbooks
from src.modules.strategy.backtest.single import FEATURES, run_single_backtest
from src.platform.persistence.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


class ConditionIn(BaseModel):
    field: str
    op: str = "eq"
    value: Any = None


class PlaybookIn(BaseModel):
    scope: str = Field("global", description="global / stock")
    symbol: str = ""
    name: str = ""
    buy: list[ConditionIn] = Field(default_factory=list)
    sell: list[ConditionIn] = Field(default_factory=list)
    note: str = ""
    enabled: bool = True


class CheckIn(BaseModel):
    symbol: str


class BoardIn(BaseModel):
    symbols: list[str] = Field(default_factory=list)


class BacktestIn(BaseModel):
    symbol: str
    buy: list[ConditionIn] = Field(default_factory=list)
    sell: list[ConditionIn] = Field(default_factory=list)
    days: int = 500


@router.get("/playbooks")
def list_playbooks(db: Session = Depends(get_db)):
    return playbooks.list_playbooks(db=db)


@router.post("/playbooks")
def save_playbook(payload: PlaybookIn, db: Session = Depends(get_db)):
    try:
        return playbooks.save_playbook(payload.model_dump(), db=db)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/playbooks/{playbook_id}")
def delete_playbook(playbook_id: int, db: Session = Depends(get_db)):
    if not playbooks.delete_playbook(playbook_id, db=db):
        raise HTTPException(404, "策略不存在")
    return {"deleted": playbook_id}


@router.post("/check")
def check(payload: CheckIn, db: Session = Depends(get_db)):
    return playbooks.check_symbol(payload.symbol, db=db)


@router.post("/board")
def board(payload: BoardIn, db: Session = Depends(get_db)):
    return playbooks.board([s for s in payload.symbols if s][:60], db=db)


@router.get("/fields")
def fields():
    return {"fields": FEATURES}


@router.post("/backtest")
def backtest(payload: BacktestIn):
    try:
        return run_single_backtest(
            payload.symbol,
            buy=[c.model_dump() for c in payload.buy] or None,
            sell=[c.model_dump() for c in payload.sell] or None,
            days=payload.days,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[策略] 回测失败")
        raise HTTPException(500, f"回测失败: {exc}")
