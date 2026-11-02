"""理财化账户服务：策略、费用、交易流水、定投与分红入账。

设计原则：
- 全部是记账，不触发真实下单；
- 交易会同时更新持仓数量/成本与账户可用资金；
- 止损/止盈只作为 AI 上下文与手动创建提醒的依据，不自动建提醒；
- 分红按除权日入账，红利账户默认再投资（按除权日收盘价折算份额）。
"""

from __future__ import annotations

import calendar
import json
import logging
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import (
    Account,
    AppSettings,
    DailyBar,
    DcaPlan,
    DividendEvent,
    DividendLedger,
    Position,
    Stock,
    TradeRecord,
)

logger = logging.getLogger(__name__)

# ── 策略默认值 ──────────────────────────────────────────────────────────
STRATEGY_LABELS = {"aggr": "激进", "steady": "稳健", "": "未设置"}

STRATEGY_DEFAULTS = {
    "aggr": {
        "max_single_weight": 0.25,
        "min_positions": 3,
        "stop_loss_pct": 0.08,
        "take_profit_pct": 0.20,
    },
    "steady": {
        "max_single_weight": 0.30,
        "min_positions": 5,
        "stop_loss_pct": 0.12,
        "take_profit_pct": 0.30,
    },
}

DEFAULT_FEE_POLICY = {
    "commission_rate": 0.00025,   # 万 2.5
    "min_commission": 5.0,        # 单笔最低 5 元
    "stamp_duty_rate": 0.0005,    # 印花税 0.05%，仅卖出
    "transfer_fee_rate": 0.00001, # 过户费 0.001%
}

GOAL_DEFAULTS = {
    "annual_passive_income": 12000.0,
    "emergency_months": 6.0,
    "annual_return_target": 8.0,
}

_GOAL_KEYS = {
    "annual_passive_income": "wealth_goal_annual_passive",
    "emergency_months": "wealth_goal_emergency_months",
    "annual_return_target": "wealth_goal_annual_return",
}


def risk_policy_for(account: Account) -> dict:
    base = dict(STRATEGY_DEFAULTS.get(account.strategy_type or "", {}))
    base.update(account.risk_policy or {})
    return base


def fee_policy_for(account: Account) -> dict:
    policy = dict(DEFAULT_FEE_POLICY)
    policy.update(account.fee_policy or {})
    return policy


def calc_fees(policy: dict, side: str, quantity: int, price: float) -> dict:
    """按 A 股常见口径计算费用：佣金（含最低）、印花税（卖出）、过户费。"""
    gross = round(max(0.0, float(quantity) * float(price)), 2)
    if gross <= 0:
        return {
            "gross": 0.0,
            "commission": 0.0,
            "stamp_duty": 0.0,
            "transfer_fee": 0.0,
            "total_fee": 0.0,
        }
    commission = max(
        gross * float(policy.get("commission_rate", 0) or 0),
        float(policy.get("min_commission", 0) or 0),
    )
    stamp = gross * float(policy.get("stamp_duty_rate", 0) or 0) if side == "sell" else 0.0
    transfer = gross * float(policy.get("transfer_fee_rate", 0) or 0)
    return {
        "gross": gross,
        "commission": round(commission, 2),
        "stamp_duty": round(stamp, 2),
        "transfer_fee": round(transfer, 2),
        "total_fee": round(commission + stamp + transfer, 2),
    }


def _today() -> str:
    return date.today().isoformat()


def _apply_buy(position: Position | None, account: Account, stock: Stock,
               quantity: int, price: float, fees: dict, trade_date: str) -> Position:
    gross = fees["gross"]
    total_cost = gross + fees["total_fee"]
    if position is None:
        position = Position(
            account_id=account.id,
            stock_id=stock.id,
            cost_price=round(total_cost / quantity, 4),
            quantity=quantity,
            invested_amount=round(total_cost, 2),
            trading_style="swing",
        )
        # 新仓默认风控线：按账户策略的比例给出建议值，用户可再改
        risk = risk_policy_for(account)
        if risk.get("stop_loss_pct"):
            position.stop_loss_price = round(price * (1 - float(risk["stop_loss_pct"])), 3)
        if risk.get("take_profit_pct"):
            position.take_profit_price = round(price * (1 + float(risk["take_profit_pct"])), 3)
    else:
        old_qty = int(position.quantity or 0)
        old_cost = float(position.cost_price or 0)
        new_qty = old_qty + quantity
        new_cost = (old_qty * old_cost + total_cost) / new_qty if new_qty else price
        position.quantity = new_qty
        position.cost_price = round(new_cost, 4)
        position.invested_amount = round(new_qty * new_cost, 2)
    account.available_funds = round(
        float(account.available_funds or 0) - total_cost, 2
    )
    return position


def _apply_sell(position: Position, account: Account, quantity: int,
                price: float, fees: dict) -> float:
    old_qty = int(position.quantity or 0)
    old_cost = float(position.cost_price or 0)
    realized = round((price - old_cost) * quantity - fees["total_fee"], 2)
    new_qty = old_qty - quantity
    position.quantity = new_qty
    position.invested_amount = round(new_qty * old_cost, 2)
    account.available_funds = round(
        float(account.available_funds or 0) + fees["gross"] - fees["total_fee"], 2
    )
    return realized


def execute_trade(
    db: Session,
    *,
    account: Account,
    stock: Stock,
    action: str,
    quantity: int,
    price: float,
    trade_date: str | None = None,
    note: str = "",
    source: str = "manual",
    fee_override: dict | None = None,
) -> dict:
    """执行一笔记账交易。

    action: buy / sell / dca / dividend / reinvest
    - buy/dca：加仓，更新数量与摊薄成本；
    - sell：减仓，更新数量、计算已实现盈亏；
    - dividend：仅现金入账（quantity=0，price=0，quantity 字段存 0）；
    - reinvest：用分红金额按给定价格增加份额，现金净变化为 0。
    """
    action = (action or "buy").lower()
    trade_date = trade_date or _today()
    quantity = int(quantity or 0)
    price = float(price or 0)

    if action in ("buy", "sell", "dca") and quantity <= 0:
        raise ValueError("数量必须大于 0")
    if action in ("buy", "sell", "dca", "reinvest") and price <= 0:
        raise ValueError("价格必须大于 0")

    policy = fee_policy_for(account)
    side = "sell" if action == "sell" else "buy"
    fees = fee_override or calc_fees(policy, side, quantity, price)

    position = (
        db.query(Position)
        .filter(Position.account_id == account.id, Position.stock_id == stock.id)
        .first()
    )

    realized = 0.0
    cash_change = 0.0
    action_label = action

    if action in ("buy", "dca"):
        if position and int(position.quantity or 0) > 0:
            position = _apply_buy(position, account, stock, quantity, price, fees, trade_date)
        else:
            if position:  # 数量为 0 的残留行，直接复用
                db.delete(position)
                db.flush()
            position = _apply_buy(None, account, stock, quantity, price, fees, trade_date)
            db.add(position)
        cash_change = round(-(fees["gross"] + fees["total_fee"]), 2)
        action_label = "buy" if action == "buy" else "dca"

    elif action == "sell":
        if not position or int(position.quantity or 0) < quantity:
            held = int(position.quantity or 0) if position else 0
            raise ValueError(f"可卖数量不足（当前 {held}）")
        realized = _apply_sell(position, account, quantity, price, fees)
        cash_change = round(fees["gross"] - fees["total_fee"], 2)
        if int(position.quantity or 0) == 0:
            db.delete(position)
            db.flush()
            position = None

    elif action == "dividend":
        cash_change = round(float(fees["gross"] or quantity), 2)
        account.available_funds = round(
            float(account.available_funds or 0) + cash_change, 2
        )

    elif action == "reinvest":
        # 分红再投：金额 = quantity * price，现金不增不减，只增加持仓
        gross = round(quantity * price, 2)
        if not position:
            raise ValueError("分红再投需要已有持仓")
        old_qty = int(position.quantity or 0)
        old_cost = float(position.cost_price or 0)
        new_qty = old_qty + quantity
        new_cost = (old_qty * old_cost + gross) / new_qty if new_qty else price
        position.quantity = new_qty
        position.cost_price = round(new_cost, 4)
        position.invested_amount = round(new_qty * new_cost, 2)
        fees = {"gross": gross, "commission": 0.0, "stamp_duty": 0.0,
                "transfer_fee": 0.0, "total_fee": 0.0}
        cash_change = 0.0
        action_label = "reinvest"

    else:
        raise ValueError(f"不支持的交易类型: {action}")

    record = TradeRecord(
        account_id=account.id,
        stock_id=stock.id,
        stock_symbol=stock.symbol,
        stock_name=stock.name,
        action=action_label,
        quantity=quantity,
        price=round(price, 4),
        amount=round(fees["gross"], 2),
        commission=fees["commission"],
        stamp_duty=fees["stamp_duty"],
        transfer_fee=fees["transfer_fee"],
        total_fee=fees["total_fee"],
        realized_pnl=round(realized, 2),
        cash_change=cash_change,
        trade_date=trade_date,
        note=note,
        source=source,
    )
    db.add(record)
    db.flush()

    return {
        "trade_id": record.id,
        "action": action_label,
        "quantity": quantity,
        "price": round(price, 4),
        "amount": record.amount,
        "fees": {
            "commission": record.commission,
            "stamp_duty": record.stamp_duty,
            "transfer_fee": record.transfer_fee,
            "total": record.total_fee,
        },
        "cash_change": cash_change,
        "realized_pnl": record.realized_pnl,
        "available_funds": round(float(account.available_funds or 0), 2),
        "position": None if position is None else {
            "id": position.id,
            "quantity": int(position.quantity or 0),
            "cost_price": float(position.cost_price or 0),
            "stop_loss_price": position.stop_loss_price,
            "take_profit_price": position.take_profit_price,
        },
        "warning": (
            "可用资金不足，已记为负数"
            if float(account.available_funds or 0) < 0
            else ""
        ),
    }


# ── 定投计划 ────────────────────────────────────────────────────────────
def _last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def compute_first_run(
    frequency: str, day_of_month: int, day_of_week: int, start: date
) -> str:
    frequency = (frequency or "monthly").lower()
    if frequency == "weekly":
        delta = (int(day_of_week) - start.weekday()) % 7
        return (start + timedelta(days=delta)).isoformat()
    if frequency == "biweekly":
        delta = (int(day_of_week) - start.weekday()) % 7
        return (start + timedelta(days=delta)).isoformat()
    # monthly
    day = max(1, min(int(day_of_month or 15), _last_day(start.year, start.month)))
    candidate = date(start.year, start.month, day)
    if candidate < start:
        year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
        day = max(1, min(int(day_of_month or 15), _last_day(year, month)))
        candidate = date(year, month, day)
    return candidate.isoformat()


def advance_run(plan: DcaPlan) -> str:
    frequency = (plan.frequency or "monthly").lower()
    base = date.fromisoformat(plan.next_run_date or _today())
    if frequency == "weekly":
        return (base + timedelta(days=7)).isoformat()
    if frequency == "biweekly":
        return (base + timedelta(days=14)).isoformat()
    year, month = (base.year + 1, 1) if base.month == 12 else (base.year, base.month + 1)
    day = max(1, min(int(plan.day_of_month or 15), _last_day(year, month)))
    return date(year, month, day).isoformat()


def due_dca_plans(db: Session, ahead_days: int = 1) -> list[dict]:
    today = date.today()
    limit = (today + timedelta(days=ahead_days)).isoformat()
    rows = (
        db.query(DcaPlan, Account, Stock)
        .join(Account, Account.id == DcaPlan.account_id)
        .join(Stock, Stock.id == DcaPlan.stock_id)
        .filter(DcaPlan.enabled.is_(True), Account.enabled.is_(True))
        .all()
    )
    out = []
    for plan, account, stock in rows:
        if not plan.next_run_date or plan.next_run_date > limit:
            continue
        out.append(
            {
                "id": plan.id,
                "account_id": account.id,
                "account_name": account.name,
                "stock_id": stock.id,
                "symbol": stock.symbol,
                "name": stock.name,
                "amount": plan.amount,
                "frequency": plan.frequency,
                "next_run_date": plan.next_run_date,
                "overdue": plan.next_run_date < today.isoformat(),
                "executed_count": plan.executed_count,
            }
        )
    out.sort(key=lambda x: x["next_run_date"])
    return out


# ── 分红 ────────────────────────────────────────────────────────────────
_DIV_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
_DIV_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://emweb.securities.eastmoney.com/",
}


def fetch_dividend_events(symbol: str, market: str = "CN") -> list[dict]:
    """东财 F10 分红送配。返回现金分红方案（每股税前金额）。"""
    code = str(symbol or "").strip().split(".")[0]
    if not code or (market or "CN").upper() != "CN":
        return []
    params = {
        "reportName": "RPT_SHAREBONUS_DET",
        "columns": "ALL",
        "filter": f'(SECURITY_CODE="{code}")',
        "pageSize": "50",
        "sortColumns": "REPORT_DATE",
        "sortTypes": "-1",
        "source": "HSF10",
        "client": "PC",
    }
    try:
        resp = httpx.get(_DIV_URL, params=params, headers=_DIV_HEADERS, timeout=12)
        resp.raise_for_status()
        payload = json.loads(resp.content.decode("utf-8", errors="replace"))
        rows = ((payload.get("result") or {}).get("data") or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[分红] 获取失败 {symbol}: {exc}")
        return []

    events = []
    for row in rows:
        bonus = row.get("PRETAX_BONUS_RMB")
        ex_date = str(row.get("EX_DIVIDEND_DATE") or "")[:10]
        if bonus is None or not ex_date:
            continue
        try:
            per_share = round(float(bonus) / 10.0, 6)
        except (TypeError, ValueError):
            continue
        if per_share <= 0:
            continue
        events.append(
            {
                "symbol": code,
                "market": "CN",
                "report_date": str(row.get("REPORT_DATE") or "")[:10],
                "ex_date": ex_date,
                "record_date": str(row.get("EQUITY_RECORD_DATE") or "")[:10],
                "per_share": per_share,
                "plan_profile": str(row.get("IMPL_PLAN_PROFILE") or ""),
            }
        )
    return events


def upsert_dividend_events(db: Session, events: list[dict]) -> None:
    for ev in events:
        exists = (
            db.query(DividendEvent)
            .filter(
                DividendEvent.symbol == ev["symbol"],
                DividendEvent.report_date == ev["report_date"],
                DividendEvent.ex_date == ev["ex_date"],
            )
            .first()
        )
        if exists:
            continue
        db.add(DividendEvent(**ev))
    db.flush()


def _close_on(symbol: str, day: str) -> float | None:
    db = SessionLocal()
    try:
        bar = (
            db.query(DailyBar)
            .filter(DailyBar.symbol == symbol, DailyBar.trade_date == day)
            .first()
        )
        return float(bar.close) if bar and bar.close else None
    finally:
        db.close()


def _quote_price(symbol: str, market: str = "CN") -> float | None:
    """本地日K缺失时的兜底价格（收盘后就是收盘价附近）。"""
    try:
        from src.platform.marketdata.marketdata_client import md_quote_rows

        for row in md_quote_rows([symbol], market):
            if row.get("symbol") == symbol and row.get("current_price"):
                return float(row["current_price"])
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[分红] 兜底行情失败 {symbol}: {exc}")
    return None


def process_dividends(days_back: int = 3) -> dict:
    """把最近几天的除权方案按账户入账；分红再投默认开启。"""
    db = SessionLocal()
    recorded = 0
    skipped = 0
    try:
        today = date.today()
        since = (today - timedelta(days=max(0, days_back))).isoformat()
        accounts = db.query(Account).filter(Account.enabled.is_(True)).all()
        for account in accounts:
            for pos in account.positions or []:
                stock = db.query(Stock).filter(Stock.id == pos.stock_id).first()
                if not stock:
                    continue
                events = fetch_dividend_events(stock.symbol, stock.market)
                upsert_dividend_events(db, events)
                for ev in events:
                    if not (since <= ev["ex_date"] <= today.isoformat()):
                        continue
                    event = (
                        db.query(DividendEvent)
                        .filter(
                            DividendEvent.symbol == ev["symbol"],
                            DividendEvent.report_date == ev["report_date"],
                            DividendEvent.ex_date == ev["ex_date"],
                        )
                        .first()
                    )
                    if not event:
                        continue
                    exists = (
                        db.query(DividendLedger)
                        .filter(
                            DividendLedger.event_id == event.id,
                            DividendLedger.account_id == account.id,
                        )
                        .first()
                    )
                    if exists:
                        continue
                    qty = int(pos.quantity or 0)
                    amount = round(qty * float(event.per_share), 2)
                    if amount <= 0:
                        continue

                    price = _close_on(stock.symbol, event.ex_date) or _quote_price(
                        stock.symbol, stock.market
                    )
                    reinvest = bool(account.dividend_reinvest)
                    shares = 0
                    action = "cash"
                    trade_id = None
                    if reinvest and price and price > 0:
                        shares = int(amount // price)
                    if shares > 0:
                        trade = execute_trade(
                            db,
                            account=account,
                            stock=stock,
                            action="reinvest",
                            quantity=shares,
                            price=price,
                            trade_date=event.ex_date,
                            note=f"分红再投 {event.plan_profile}",
                            source="dividend_auto",
                        )
                        trade_id = trade["trade_id"]
                        action = "reinvest"
                        leftover = round(amount - shares * price, 2)
                        if leftover:
                            account.available_funds = round(
                                float(account.available_funds or 0) + leftover, 2
                            )
                    else:
                        trade = execute_trade(
                            db,
                            account=account,
                            stock=stock,
                            action="dividend",
                            quantity=0,
                            price=0,
                            trade_date=event.ex_date,
                            note=f"现金分红 {event.plan_profile}",
                            source="dividend_auto",
                            fee_override={
                                "gross": amount,
                                "commission": 0.0,
                                "stamp_duty": 0.0,
                                "transfer_fee": 0.0,
                                "total_fee": 0.0,
                            },
                        )
                        trade_id = trade["trade_id"]

                    db.add(
                        DividendLedger(
                            event_id=event.id,
                            account_id=account.id,
                            stock_id=stock.id,
                            quantity=qty,
                            per_share=float(event.per_share),
                            amount=amount,
                            action=action,
                            shares_added=shares,
                            price=price or 0,
                            trade_id=trade_id,
                        )
                    )
                    recorded += 1
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("[分红] 入账失败")
        raise
    finally:
        db.close()
    return {"recorded": recorded, "skipped": skipped}


# ── 理财目标 ────────────────────────────────────────────────────────────
def strategy_context_for_symbol(symbol: str, market: str = "CN") -> list[dict]:
    """给 AI prompt 用：该股票在各个账户里的策略、持仓与风控线。"""
    db = SessionLocal()
    try:
        rows = (
            db.query(Position, Account, Stock)
            .join(Account, Account.id == Position.account_id)
            .join(Stock, Stock.id == Position.stock_id)
            .filter(
                Stock.symbol == str(symbol),
                Stock.market == (market or "CN"),
                Account.enabled.is_(True),
            )
            .all()
        )
        out = []
        for pos, acc, _stock in rows:
            risk = risk_policy_for(acc)
            out.append(
                {
                    "account_name": acc.name,
                    "strategy_type": acc.strategy_type or "",
                    "strategy_label": STRATEGY_LABELS.get(
                        acc.strategy_type or "", "未设置"
                    ),
                    "quantity": int(pos.quantity or 0),
                    "cost_price": float(pos.cost_price or 0),
                    "stop_loss_price": pos.stop_loss_price,
                    "take_profit_price": pos.take_profit_price,
                    "max_single_weight": risk.get("max_single_weight"),
                    "min_positions": risk.get("min_positions"),
                    "dividend_reinvest": bool(acc.dividend_reinvest),
                }
            )
        return out
    finally:
        db.close()


def get_goals(db: Session) -> dict:
    out = dict(GOAL_DEFAULTS)
    rows = (
        db.query(AppSettings)
        .filter(AppSettings.key.in_(list(_GOAL_KEYS.values())))
        .all()
    )
    by_key = {r.key: r.value for r in rows}
    for field, key in _GOAL_KEYS.items():
        if key in by_key:
            try:
                out[field] = float(by_key[key])
            except (TypeError, ValueError):
                pass
    return out


def set_goals(db: Session, data: dict) -> dict:
    for field, key in _GOAL_KEYS.items():
        if field not in data:
            continue
        value = str(float(data[field]))
        row = db.query(AppSettings).filter(AppSettings.key == key).first()
        if row:
            row.value = value
        else:
            db.add(AppSettings(key=key, value=value, description="理财目标"))
    db.commit()
    return get_goals(db)
