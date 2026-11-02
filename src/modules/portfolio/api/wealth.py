"""理财化持仓接口：策略、交易流水、定投、分红与目标。"""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.modules.portfolio import wealth
from src.platform.persistence.database import get_db
from src.platform.persistence.models import (
    Account,
    DcaPlan,
    DividendEvent,
    DividendLedger,
    Stock,
    TradeRecord,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ── 请求模型 ────────────────────────────────────────────────────────────
class StrategyIn(BaseModel):
    strategy_type: str | None = None  # aggr / steady / ""
    risk_policy: dict | None = None
    capital_plan: dict | None = None
    fee_policy: dict | None = None
    dividend_reinvest: bool | None = None


class PositionRiskIn(BaseModel):
    stop_loss_price: float | None = None
    take_profit_price: float | None = None


class TradeIn(BaseModel):
    account_id: int
    stock_id: int
    action: str = Field("buy", description="buy / sell")
    quantity: int
    price: float
    trade_date: str | None = None
    note: str = ""


class DcaIn(BaseModel):
    account_id: int
    stock_id: int
    amount: float
    frequency: str = "monthly"  # monthly / weekly / biweekly
    day_of_month: int = 15
    day_of_week: int = 0
    start_date: str | None = None
    enabled: bool = True
    note: str = ""


class DcaUpdateIn(BaseModel):
    amount: float | None = None
    frequency: str | None = None
    day_of_month: int | None = None
    day_of_week: int | None = None
    enabled: bool | None = None
    note: str | None = None


class DcaExecuteIn(BaseModel):
    price: float
    quantity: int | None = None
    trade_date: str | None = None
    note: str = ""


class GoalsIn(BaseModel):
    annual_passive_income: float | None = None
    emergency_months: float | None = None
    annual_return_target: float | None = None


# ── 序列化 ──────────────────────────────────────────────────────────────
def _account_payload(account: Account) -> dict:
    return {
        "id": account.id,
        "name": account.name,
        "available_funds": account.available_funds,
        "enabled": account.enabled,
        "strategy_type": account.strategy_type or "",
        "strategy_label": wealth.STRATEGY_LABELS.get(account.strategy_type or "", "未设置"),
        "risk_policy": wealth.risk_policy_for(account),
        "capital_plan": account.capital_plan or {},
        "fee_policy": wealth.fee_policy_for(account),
        "dividend_reinvest": bool(account.dividend_reinvest),
    }


def _trade_payload(t: TradeRecord) -> dict:
    return {
        "id": t.id,
        "account_id": t.account_id,
        "stock_id": t.stock_id,
        "symbol": t.stock_symbol,
        "name": t.stock_name,
        "action": t.action,
        "quantity": t.quantity,
        "price": t.price,
        "amount": t.amount,
        "commission": t.commission,
        "stamp_duty": t.stamp_duty,
        "transfer_fee": t.transfer_fee,
        "total_fee": t.total_fee,
        "realized_pnl": t.realized_pnl,
        "cash_change": t.cash_change,
        "trade_date": t.trade_date,
        "note": t.note,
        "source": t.source,
        "created_at": t.created_at.isoformat() if t.created_at else "",
    }


def _dca_payload(plan: DcaPlan, account: Account | None, stock: Stock | None) -> dict:
    return {
        "id": plan.id,
        "account_id": plan.account_id,
        "account_name": account.name if account else "",
        "stock_id": plan.stock_id,
        "symbol": stock.symbol if stock else "",
        "name": stock.name if stock else "",
        "amount": plan.amount,
        "frequency": plan.frequency,
        "day_of_month": plan.day_of_month,
        "day_of_week": plan.day_of_week,
        "start_date": plan.start_date,
        "next_run_date": plan.next_run_date,
        "enabled": bool(plan.enabled),
        "executed_count": plan.executed_count,
        "total_invested": plan.total_invested,
        "note": plan.note,
    }


# ── 账户策略 ────────────────────────────────────────────────────────────
@router.put("/portfolio/accounts/{account_id}/strategy")
def update_strategy(account_id: int, data: StrategyIn, db: Session = Depends(get_db)):
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account:
        raise HTTPException(404, "账户不存在")

    if data.strategy_type is not None:
        st = (data.strategy_type or "").strip().lower()
        if st not in ("", "aggr", "steady"):
            raise HTTPException(400, "策略只能是 aggr / steady / 空")
        account.strategy_type = st

    if data.risk_policy is not None:
        merged = dict(account.risk_policy or {})
        merged.update(data.risk_policy)
        account.risk_policy = merged
    elif data.strategy_type is not None:
        # 切换策略时，把未显式设置的阈值恢复到该策略默认值
        account.risk_policy = dict(
            wealth.STRATEGY_DEFAULTS.get(account.strategy_type, {})
        )

    if data.capital_plan is not None:
        merged = dict(account.capital_plan or {})
        merged.update(data.capital_plan)
        account.capital_plan = merged
    if data.fee_policy is not None:
        merged = dict(account.fee_policy or {})
        merged.update(data.fee_policy)
        account.fee_policy = merged
    if data.dividend_reinvest is not None:
        account.dividend_reinvest = bool(data.dividend_reinvest)

    db.commit()
    db.refresh(account)
    return _account_payload(account)


@router.put("/portfolio/positions/{position_id}/risk")
def update_position_risk(
    position_id: int, data: PositionRiskIn, db: Session = Depends(get_db)
):
    from src.platform.persistence.models import Position

    pos = db.query(Position).filter(Position.id == position_id).first()
    if not pos:
        raise HTTPException(404, "持仓不存在")
    if data.stop_loss_price is not None:
        pos.stop_loss_price = float(data.stop_loss_price) or None
    if data.take_profit_price is not None:
        pos.take_profit_price = float(data.take_profit_price) or None
    db.commit()
    db.refresh(pos)
    return {
        "id": pos.id,
        "stop_loss_price": pos.stop_loss_price,
        "take_profit_price": pos.take_profit_price,
        "hint": "已保存。系统不会自动创建提醒，需要时请点行内铃铛手动创建。",
    }


# ── 交易流水 ────────────────────────────────────────────────────────────
@router.post("/portfolio/trades")
def create_trade(data: TradeIn, db: Session = Depends(get_db)):
    account = db.query(Account).filter(Account.id == data.account_id).first()
    if not account:
        raise HTTPException(400, "账户不存在")
    stock = db.query(Stock).filter(Stock.id == data.stock_id).first()
    if not stock:
        raise HTTPException(400, "股票不存在")
    if data.action not in ("buy", "sell"):
        raise HTTPException(400, "手动交易只支持 buy / sell")
    try:
        result = wealth.execute_trade(
            db,
            account=account,
            stock=stock,
            action=data.action,
            quantity=data.quantity,
            price=data.price,
            trade_date=data.trade_date,
            note=data.note,
        )
        db.commit()
        return result
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))
    except Exception:
        db.rollback()
        logger.exception("交易记录失败")
        raise HTTPException(500, "交易记录失败")


@router.get("/portfolio/trades")
def list_trades(
    account_id: int | None = None,
    action: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    q = db.query(TradeRecord)
    if account_id:
        q = q.filter(TradeRecord.account_id == account_id)
    if action:
        q = q.filter(TradeRecord.action == action)
    rows = q.order_by(TradeRecord.trade_date.desc(), TradeRecord.id.desc()).limit(
        max(1, min(limit, 500))
    ).all()
    return [_trade_payload(r) for r in rows]


# ── 定投 ────────────────────────────────────────────────────────────────
@router.get("/portfolio/dca")
def list_dca(account_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(DcaPlan)
    if account_id:
        q = q.filter(DcaPlan.account_id == account_id)
    plans = q.order_by(DcaPlan.id.desc()).all()
    out = []
    for p in plans:
        account = db.query(Account).filter(Account.id == p.account_id).first()
        stock = db.query(Stock).filter(Stock.id == p.stock_id).first()
        out.append(_dca_payload(p, account, stock))
    return out


@router.get("/portfolio/dca/due")
def dca_due(days: int = 1, db: Session = Depends(get_db)):
    return wealth.due_dca_plans(db, ahead_days=max(0, min(days, 30)))


@router.post("/portfolio/dca")
def create_dca(data: DcaIn, db: Session = Depends(get_db)):
    if data.amount <= 0:
        raise HTTPException(400, "定投金额必须大于 0")
    if data.frequency not in ("monthly", "weekly", "biweekly"):
        raise HTTPException(400, "频率只能是 monthly / weekly / biweekly")
    account = db.query(Account).filter(Account.id == data.account_id).first()
    if not account:
        raise HTTPException(400, "账户不存在")
    stock = db.query(Stock).filter(Stock.id == data.stock_id).first()
    if not stock:
        raise HTTPException(400, "股票不存在")
    start = date.fromisoformat(data.start_date) if data.start_date else date.today()
    if start < date.today():
        start = date.today()
    plan = DcaPlan(
        account_id=data.account_id,
        stock_id=data.stock_id,
        amount=float(data.amount),
        frequency=data.frequency,
        day_of_month=int(data.day_of_month or 15),
        day_of_week=int(data.day_of_week or 0),
        start_date=start.isoformat(),
        next_run_date=wealth.compute_first_run(
            data.frequency, data.day_of_month, data.day_of_week, start
        ),
        enabled=bool(data.enabled),
        note=data.note,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return _dca_payload(plan, account, stock)


@router.put("/portfolio/dca/{plan_id}")
def update_dca(plan_id: int, data: DcaUpdateIn, db: Session = Depends(get_db)):
    plan = db.query(DcaPlan).filter(DcaPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(404, "定投计划不存在")
    schedule_changed = False
    if data.amount is not None:
        plan.amount = float(data.amount)
    if data.frequency is not None:
        if data.frequency not in ("monthly", "weekly", "biweekly"):
            raise HTTPException(400, "频率不合法")
        plan.frequency = data.frequency
        schedule_changed = True
    if data.day_of_month is not None:
        plan.day_of_month = int(data.day_of_month)
        schedule_changed = True
    if data.day_of_week is not None:
        plan.day_of_week = int(data.day_of_week)
        schedule_changed = True
    if data.enabled is not None:
        plan.enabled = bool(data.enabled)
    if data.note is not None:
        plan.note = data.note
    if schedule_changed:
        plan.next_run_date = wealth.compute_first_run(
            plan.frequency, plan.day_of_month, plan.day_of_week, date.today()
        )
    db.commit()
    db.refresh(plan)
    account = db.query(Account).filter(Account.id == plan.account_id).first()
    stock = db.query(Stock).filter(Stock.id == plan.stock_id).first()
    return _dca_payload(plan, account, stock)


@router.delete("/portfolio/dca/{plan_id}")
def delete_dca(plan_id: int, db: Session = Depends(get_db)):
    plan = db.query(DcaPlan).filter(DcaPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(404, "定投计划不存在")
    db.delete(plan)
    db.commit()
    return {"success": True}


@router.post("/portfolio/dca/{plan_id}/execute")
def execute_dca(plan_id: int, data: DcaExecuteIn, db: Session = Depends(get_db)):
    plan = db.query(DcaPlan).filter(DcaPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(404, "定投计划不存在")
    account = db.query(Account).filter(Account.id == plan.account_id).first()
    stock = db.query(Stock).filter(Stock.id == plan.stock_id).first()
    if not account or not stock:
        raise HTTPException(400, "账户或股票不存在")
    price = float(data.price)
    if price <= 0:
        raise HTTPException(400, "成交价必须大于 0")
    qty = int(data.quantity or 0)
    if qty <= 0:
        qty = int(float(plan.amount) // price)
    if qty <= 0:
        raise HTTPException(400, f"金额 {plan.amount} 不足以按 {price} 买入 1 股")
    try:
        result = wealth.execute_trade(
            db,
            account=account,
            stock=stock,
            action="dca",
            quantity=qty,
            price=price,
            trade_date=data.trade_date,
            note=data.note or f"定投第 {int(plan.executed_count or 0) + 1} 期",
            source="dca",
        )
        plan.executed_count = int(plan.executed_count or 0) + 1
        plan.total_invested = round(float(plan.total_invested or 0) + qty * price, 2)
        plan.next_run_date = wealth.advance_run(plan)
        # 补记逾期期数时，把下一次直接推进到今天之后
        guard = 0
        while plan.next_run_date <= date.today().isoformat() and guard < 24:
            plan.next_run_date = wealth.advance_run(plan)
            guard += 1
        db.commit()
        result["plan"] = _dca_payload(plan, account, stock)
        return result
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))
    except Exception:
        db.rollback()
        logger.exception("定投记账失败")
        raise HTTPException(500, "定投记账失败")


# ── 分红 ────────────────────────────────────────────────────────────────
@router.post("/portfolio/dividends/process")
def run_dividend_process(days: int = 3):
    try:
        return wealth.process_dividends(days_back=max(0, min(days, 30)))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"分红入账失败: {exc}")


@router.get("/portfolio/dividends/records")
def dividend_records(limit: int = 50, db: Session = Depends(get_db)):
    rows = (
        db.query(DividendLedger, DividendEvent, Account, Stock)
        .join(DividendEvent, DividendEvent.id == DividendLedger.event_id)
        .join(Account, Account.id == DividendLedger.account_id)
        .outerjoin(Stock, Stock.id == DividendLedger.stock_id)
        .order_by(DividendLedger.created_at.desc(), DividendLedger.id.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    out = []
    for ledger, event, account, stock in rows:
        out.append(
            {
                "id": ledger.id,
                "account_id": account.id,
                "account_name": account.name,
                "symbol": event.symbol,
                "name": stock.name if stock else event.symbol,
                "ex_date": event.ex_date,
                "quantity": ledger.quantity,
                "per_share": ledger.per_share,
                "amount": ledger.amount,
                "action": ledger.action,
                "shares_added": ledger.shares_added,
                "price": ledger.price,
                "plan_profile": event.plan_profile,
            }
        )
    return out


# ── 理财目标 ────────────────────────────────────────────────────────────
@router.get("/portfolio/wealth/goals")
def get_goals(db: Session = Depends(get_db)):
    return wealth.get_goals(db)


@router.put("/portfolio/wealth/goals")
def put_goals(data: GoalsIn, db: Session = Depends(get_db)):
    payload = {k: v for k, v in data.model_dump().items() if v is not None}
    return wealth.set_goals(db, payload)


# ── 汇总 ────────────────────────────────────────────────────────────────
@router.get("/portfolio/wealth/overview")
def wealth_overview(db: Session = Depends(get_db)):
    accounts = db.query(Account).filter(Account.enabled.is_(True)).all()
    account_rows = []
    for acc in accounts:
        positions = []
        for pos in acc.positions or []:
            stock = db.query(Stock).filter(Stock.id == pos.stock_id).first()
            positions.append(
                {
                    "id": pos.id,
                    "stock_id": pos.stock_id,
                    "symbol": stock.symbol if stock else "",
                    "name": stock.name if stock else "",
                    "market": stock.market if stock else "CN",
                    "quantity": pos.quantity,
                    "cost_price": pos.cost_price,
                    "stop_loss_price": pos.stop_loss_price,
                    "take_profit_price": pos.take_profit_price,
                    "trading_style": pos.trading_style,
                }
            )
        plans = (
            db.query(DcaPlan)
            .filter(DcaPlan.account_id == acc.id, DcaPlan.enabled.is_(True))
            .all()
        )
        account_rows.append(
            {
                **_account_payload(acc),
                "positions": positions,
                "dca_plans": [
                    _dca_payload(
                        p,
                        acc,
                        db.query(Stock).filter(Stock.id == p.stock_id).first(),
                    )
                    for p in plans
                ],
            }
        )

    trades = (
        db.query(TradeRecord)
        .order_by(TradeRecord.trade_date.desc(), TradeRecord.id.desc())
        .limit(50)
        .all()
    )
    return {
        "goals": wealth.get_goals(db),
        "accounts": account_rows,
        "dca_due": wealth.due_dca_plans(db, ahead_days=1),
        "trades": [_trade_payload(t) for t in trades],
    }
