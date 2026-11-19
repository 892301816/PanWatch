"""交易策略卡、买卖前体检与单票回测 API。"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.modules.strategy import gate as gate_service
from src.modules.strategy import playbooks
from src.modules.strategy import workflow
from src.modules.strategy.backtest.single import FEATURES, DEFAULT_BUY, DEFAULT_SELL, run_single_backtest
from src.modules.strategy.backtest.cost_model import CostConfig, CostModel
from src.platform.persistence.database import get_db
from src.platform.persistence.models import BacktestRun, StrategyDraft, StrategyRevision, StrategyDecision

logger = logging.getLogger(__name__)
router = APIRouter()


class ConditionIn(BaseModel):
    field: str
    op: str = "eq"
    value: Any = None
    children: list['ConditionIn'] | None = None
    logic: Literal['all','any'] = 'all'
    offset: int = Field(default=0,ge=0,le=240)
    consecutive: int = Field(default=1,ge=1,le=60)

    def model_dump(self, **kwargs):
        kwargs.setdefault('exclude_none',True)
        return super().model_dump(**kwargs)


class PlaybookIn(BaseModel):
    scope: str = Field("global", description="global / stock")
    symbol: str = ""
    name: str = ""
    buy: list[ConditionIn] = Field(default_factory=list)
    sell: list[ConditionIn] = Field(default_factory=list)
    avoid: list[ConditionIn] = Field(default_factory=list)
    note: str = ""
    enabled: bool = True
    market: Literal["CN", "HK", "US"] = "CN"
    timing: Literal["completed_daily", "intraday_preview"] = "completed_daily"
    config: dict = Field(default_factory=dict)


class DraftIn(PlaybookIn):
    draft_id: int | None = None
    source_text: str = ""
    questions: list[str] = Field(default_factory=list)


class AIDraftIn(BaseModel):
    text: str = Field(min_length=1, max_length=12000)
    scope: Literal["global", "stock"] = "stock"
    symbol: str = ""
    market: Literal["CN", "HK", "US"] = "CN"
    model_id: int | None = None
    draft_id: int | None = None
    expected_hash: str | None = None


class CheckIn(BaseModel):
    symbol: str
    market: Literal["CN", "HK", "US"] = "CN"


class BoardIn(BaseModel):
    symbols: list[str] = Field(default_factory=list)


class BacktestIn(BaseModel):
    symbol: str
    market: str = "CN"
    buy: list[ConditionIn] | None = None
    sell: list[ConditionIn] | None = None
    avoid: list[ConditionIn] | None = None
    days: int = Field(default=500, ge=60, le=5000)
    draft_id: int | None = None
    revision_id: int | None = None
    start_date: str | None = None
    end_date: str | None = None
    start: str | None = None
    end: str | None = None
    initial_capital: float = Field(default=100000, gt=0, allow_inf_nan=False)
    commission_rate: float = Field(default=.00025, ge=0, le=.1, allow_inf_nan=False)
    min_commission: float = Field(default=5, ge=0, allow_inf_nan=False)
    stamp_duty_rate: float = Field(default=.0005, ge=0, le=.1, allow_inf_nan=False)
    transfer_fee_rate: float = Field(default=.00001, ge=0, le=.1, allow_inf_nan=False)
    slippage_bps: float = Field(default=5, ge=0, le=1000, allow_inf_nan=False)


class GateIn(BaseModel):
    symbol: str
    side: Literal["buy", "sell"] = "buy"
    market: Literal["CN", "HK", "US"] = "CN"
    account_id: int | None = None
    price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    quantity: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    use_ai: bool = True
    model_id: int | None = None


class ExceptionIn(BaseModel):
    reason: str = Field(min_length=5, max_length=4000)


class LinkIn(BaseModel):
    trade_id: int


class LibraryApplyIn(BaseModel):
    scope: Literal["stock", "global"] = "stock"
    symbol: str = ""
    expected_hash: str = Field(min_length=1)


class ScanSymbolIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    market: Literal["CN", "HK", "US"] = "CN"
    name: str = ""


class LibraryScanIn(BaseModel):
    symbols: list[ScanSymbolIn] = Field(min_length=1, max_length=30)
    expected_hash: str = Field(min_length=1)
    allow_network: bool = False


class BindingEnabledIn(BaseModel):
    enabled: bool


class LibraryDeleteIn(BaseModel):
    expected_hash: str = Field(min_length=1)
    disable_bindings: bool = False
    binding_ids: list[int] = Field(default_factory=list, max_length=10000)


class PortfolioRunIn(BaseModel):
    draft_id: int
    start_date: str
    end_date: str
    universe: Literal['watchlist','all','custom'] = 'watchlist'
    symbols: list[str] = Field(default_factory=list,max_length=10000)
    initial_capital: float = Field(default=100000,gt=0,allow_inf_nan=False)
    max_positions: int = Field(default=5,ge=1,le=100)
    max_weight: float = Field(default=.2,gt=0,le=1,allow_inf_nan=False)


@router.post('/portfolio-runs')
def start_portfolio_run(payload: PortfolioRunIn, db: Session = Depends(get_db)):
    from src.modules.strategy import portfolio_jobs
    try:return portfolio_jobs.start(db,payload.model_dump())
    except ValueError as exc:raise HTTPException(400,str(exc)) from exc


@router.get('/portfolio-runs/{run_id}')
def read_portfolio_run(run_id: int, db: Session = Depends(get_db)):
    from src.modules.strategy import portfolio_jobs
    try:return portfolio_jobs.read(db,run_id)
    except ValueError as exc:raise HTTPException(404,str(exc)) from exc


@router.post('/portfolio-runs/{run_id}/cancel')
def cancel_portfolio_run(run_id: int, db: Session = Depends(get_db)):
    from src.modules.strategy import portfolio_jobs
    try: portfolio_jobs.read(db,run_id)
    except ValueError as exc: raise HTTPException(404, str(exc)) from exc
    return portfolio_jobs.cancel(run_id)


@router.post("/bindings/{playbook_id}/enabled")
def set_binding_enabled(playbook_id: int, payload: BindingEnabledIn, db: Session = Depends(get_db)):
    from src.platform.persistence.models import StrategyPlaybook
    row = db.get(StrategyPlaybook, playbook_id)
    if row is None:
        raise HTTPException(404, "应用范围不存在")
    revision = db.get(StrategyRevision, row.active_revision_id) if row.active_revision_id else None
    return playbooks.save_playbook({**workflow.policy_payload(row), "enabled": payload.enabled, "source_draft_id": revision.source_draft_id if revision else None}, db)


@router.get("/library")
def library(market: Literal["ALL", "CN", "HK", "US"] = "ALL", include_deleted: bool = False, db: Session = Depends(get_db)):
    from src.modules.strategy import center
    return center.library(db, market, include_deleted)


@router.delete("/library/{draft_id}")
def delete_library(draft_id: int, payload: LibraryDeleteIn, db: Session = Depends(get_db)):
    from src.modules.strategy import center
    try:
        return center.delete_library(db, draft_id, payload.expected_hash, payload.disable_bindings, payload.binding_ids)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc


@router.post("/library/{draft_id}/restore")
def restore_library(draft_id: int, db: Session = Depends(get_db)):
    from src.modules.strategy import center
    try:
        return center.restore_library(db, draft_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/library/{draft_id}/apply")
def apply_library(draft_id: int, payload: LibraryApplyIn, db: Session = Depends(get_db)):
    from src.modules.strategy import center
    try:
        return center.apply_library(db, draft_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/library/{draft_id}/scan")
def scan_library(draft_id: int, payload: LibraryScanIn, db: Session = Depends(get_db)):
    from src.modules.strategy import center
    try:
        return center.scan(db, draft_id, [row.model_dump() for row in payload.symbols], payload.expected_hash, payload.allow_network)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/backtests")
def list_backtests(draft_id: int | None = None, db: Session = Depends(get_db)):
    from src.modules.strategy import center
    rows = db.query(BacktestRun).filter(BacktestRun.strategy_code.in_(("playbook", "strategy_portfolio"))).order_by(BacktestRun.id.desc()).all()
    return {"items": [center.report_dict(row) for row in rows if draft_id is None or (row.input_snapshot or {}).get("draft_id") == draft_id][:50]}


class ExplainIn(BaseModel):
    model_id: int | None = None


@router.get("/playbooks")
def list_playbooks(db: Session = Depends(get_db)):
    return playbooks.list_playbooks(db=db)


@router.get("/presets")
def list_presets():
    return playbooks.presets()


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
    return playbooks.check_symbol(payload.symbol, db=db, market=payload.market)


@router.post("/board")
def board(payload: BoardIn, db: Session = Depends(get_db)):
    return playbooks.board([s for s in payload.symbols if s][:60], db=db)


@router.get("/fields")
def fields():
    return {"fields": FEATURES}


@router.get('/data-audit')
def data_audit(symbol: str = '', db: Session = Depends(get_db)):
    from src.modules.strategy.data_capabilities import audit
    return audit(db,symbol.strip())


@router.post('/reference-data')
def import_reference_data(payload: dict, db: Session = Depends(get_db)):
    from src.modules.strategy.data_capabilities import import_references
    try:return import_references(db,payload.get('records'))
    except (ValueError,KeyError,TypeError) as exc:
        db.rollback()
        raise HTTPException(400,str(exc)) from exc


@router.post("/gate")
async def trade_gate(payload: GateIn, db: Session = Depends(get_db)):
    """买卖前体检:规则 + 形态 + AI(没配 AI 也能出规则结论)。"""
    try:
        return await gate_service.evaluate(
            payload.symbol,
            side=payload.side,
            market=payload.market,
            price=payload.price,
            quantity=payload.quantity,
            use_ai=payload.use_ai,
            model_id=payload.model_id,
            db=db,
        )
    except Exception as exc:  # noqa: BLE001 — 体检失败要给出可读错误
        logger.exception("[策略] 买卖前体检失败")
        raise HTTPException(500, f"体检失败: {exc}")


@router.post("/backtest")
def backtest(payload: BacktestIn, db: Session = Depends(get_db)):
    if payload.draft_id and payload.revision_id:
        raise HTTPException(400, "一次回测只能选择草稿或生效版本")
    revision = db.get(StrategyRevision, payload.revision_id) if payload.revision_id else None
    draft = db.get(StrategyDraft, payload.draft_id) if payload.draft_id else None
    if payload.revision_id and revision is None or payload.draft_id and (draft is None or draft.archived_at):
        raise HTTPException(404, "所选草稿或版本不存在")
    if draft and draft.questions:
        raise HTTPException(400, "请先确认草稿中的问题，回测不能假装已实现这些条件")
    if revision or draft:
        rules = (revision or draft).payload
        if rules.get("market", "CN") != payload.market.upper():
            raise HTTPException(400, "回测市场与所选策略版本不一致")
        if rules.get("scope") == "stock" and rules.get("symbol") != payload.symbol.strip().upper():
            raise HTTPException(400, "个股策略不能用于另一个标的")
    else:
        try:
            rules = workflow.validate_rules({
                "scope": "stock", "symbol": payload.symbol, "market": payload.market,
                "buy": [c.model_dump() for c in payload.buy] if payload.buy is not None else DEFAULT_BUY,
                "sell": [c.model_dump() for c in payload.sell] if payload.sell is not None else DEFAULT_SELL,
                "avoid": [c.model_dump() for c in payload.avoid] if payload.avoid is not None else [],
            })
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if rules.get("timing") == "intraday_preview":
        raise HTTPException(400, "盘中暂态规则不能用日线收盘回测验证")
    costs = {key: getattr(payload, key) for key in ("commission_rate", "min_commission", "stamp_duty_rate", "transfer_fee_rate", "slippage_bps")}
    row = BacktestRun(
        status="running", strategy_code="playbook", strategy_version=str(revision.version) if revision else "draft",
        market=payload.market, start_date=payload.start_date or payload.start or "", end_date=payload.end_date or payload.end or "",
        config={"days": payload.days, "initial_capital": payload.initial_capital, **costs},
        input_snapshot={"rules": rules, "rules_hash": workflow.rules_hash(rules), "draft_id": draft.id if draft else None, "revision_id": revision.id if revision else None, "symbol": payload.symbol},
        started_at=workflow.now(),
    )
    db.add(row)
    # Store the queued/running identity before slow data collection; do not hold
    # SQLite's writer lock throughout a historical run or a network fallback.
    db.commit()
    try:
        result = run_single_backtest(
            payload.symbol,
            buy=rules["buy"], sell=rules["sell"], avoid=rules["avoid"],
            days=payload.days,
            market=payload.market,
            start=payload.start_date or payload.start, end=payload.end_date or payload.end,
            initial_capital=payload.initial_capital, cost_model=CostModel(CostConfig(**costs)),
            config=rules.get('config'),
        )
        result.update(backtest_id=row.id, draft_id=draft.id if draft else None, revision_id=revision.id if revision else None, rules_hash=workflow.rules_hash(rules), rules_snapshot=rules)
        row.status = "failed" if result.get("error") else "succeeded"
        row.error_message = result.get("error", "")
        row.result = result
        row.start_date = result.get("start", row.start_date)
        row.end_date = result.get("end", row.end_date)
        row.input_snapshot = {**row.input_snapshot, "data_info": result.get("data_info", {})}
        row.finished_at = workflow.now()
        db.commit()
        return result
    except Exception as exc:
        row.status, row.error_message, row.finished_at = "failed", str(exc)[:1000], workflow.now()
        db.commit()
        logger.exception("[策略] 回测失败")
        raise HTTPException(400, "回测失败，检查日期范围、可用数据和交易成本参数") from exc


@router.get("/backtests/{run_id}")
def get_backtest(run_id: int, db: Session = Depends(get_db)):
    row = db.get(BacktestRun, run_id)
    if row is None:
        raise HTTPException(404, "回测记录不存在")
    return {"id": row.id, "backtest_id": row.id, "status": row.status, "config": row.config, "input_snapshot": row.input_snapshot, "result": row.result, "error": row.error_message}


@router.get("/drafts")
def list_drafts(symbol: str = "", market: str = "CN", db: Session = Depends(get_db)):
    query = db.query(StrategyDraft).filter(StrategyDraft.market == market.upper(), StrategyDraft.archived_at.is_(None))
    if symbol:
        query = query.filter(StrategyDraft.symbol.in_((symbol.strip().upper(), "")))
    return {"items": [workflow.draft_dict(row) for row in query.order_by(StrategyDraft.updated_at.desc()).limit(100)]}


@router.post("/drafts")
def save_draft(payload: DraftIn, db: Session = Depends(get_db)):
    try:
        return workflow.save_draft(db, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/drafts/ai")
async def ai_draft(payload: AIDraftIn, db: Session = Depends(get_db)):
    try:
        return await workflow.generate_draft(db, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.exception("[策略] AI 草稿生成失败")
        raise HTTPException(502, "AI 草稿生成失败，请重试或手工定义规则") from exc


@router.post("/drafts/{draft_id}/activate")
def activate_draft(draft_id: int, db: Session = Depends(get_db)):
    try:
        return workflow.activate_draft(db, draft_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/revisions")
def list_revisions(symbol: str = "", market: str = "CN", scope: str = "", db: Session = Depends(get_db)):
    query = db.query(StrategyRevision).filter(StrategyRevision.market == market.upper())
    if symbol:
        query = query.filter(StrategyRevision.symbol.in_((symbol.strip().upper(), "")))
    if scope:
        query = query.filter(StrategyRevision.scope == scope)
    return {"items": [workflow.revision_dict(row) for row in query.order_by(StrategyRevision.created_at.desc(), StrategyRevision.id.desc()).limit(100)]}


@router.post("/decisions")
async def create_decision(payload: GateIn, db: Session = Depends(get_db)):
    try:
        return await workflow.create_decision(db, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/decisions")
def list_decisions(symbol: str = "", market: str = "CN", account_id: int | None = None, limit: int = 20, draft_id: int | None = None, db: Session = Depends(get_db)):
    query = db.query(StrategyDecision)
    if market.upper() != "ALL":
        query = query.filter(StrategyDecision.market == market.upper())
    if draft_id is not None:
        query = query.filter(StrategyDecision.revision_id.in_(db.query(StrategyRevision.id).filter(StrategyRevision.source_draft_id == draft_id)))
    if symbol:
        query = query.filter(StrategyDecision.symbol == symbol.strip().upper())
    if account_id is not None:
        query = query.filter(StrategyDecision.account_id == account_id)
    return {"items": [workflow.decision_dict(row) for row in query.order_by(StrategyDecision.created_at.desc(), StrategyDecision.id.desc()).limit(max(1, min(limit, 100)))]}


@router.get("/decisions/{decision_id}")
def get_decision(decision_id: int, db: Session = Depends(get_db)):
    row = db.get(StrategyDecision, decision_id)
    if row is None:
        raise HTTPException(404, "检查记录不存在")
    return workflow.decision_dict(row)


@router.post("/decisions/{decision_id}/exception")
def decision_exception(decision_id: int, payload: ExceptionIn, db: Session = Depends(get_db)):
    try:
        return workflow.add_exception(db, decision_id, payload.reason)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/decisions/{decision_id}/explain")
async def explain_decision(decision_id: int, payload: ExplainIn, db: Session = Depends(get_db)):
    try:
        return await workflow.explain_decision(db, decision_id, payload.model_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/decisions/{decision_id}/link")
def decision_link(decision_id: int, payload: LinkIn, db: Session = Depends(get_db)):
    try:
        return workflow.link_trade(db, decision_id, payload.trade_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
