"""理财账户核心逻辑：费用、买卖、定投排期。"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.modules.portfolio import wealth
from src.platform.persistence.database import Base
from src.platform.persistence.models import Account, Stock


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


def _seed(db: Session) -> tuple[Account, Stock]:
    account = Account(name="测试", available_funds=100000)
    stock = Stock(symbol="600900", name="长江电力", market="CN")
    db.add_all([account, stock])
    db.commit()
    db.refresh(account)
    db.refresh(stock)
    return account, stock


def test_calc_fees_respects_min_commission_and_sell_stamp():
    policy = dict(wealth.DEFAULT_FEE_POLICY)
    buy = wealth.calc_fees(policy, "buy", 100, 10)
    assert buy["gross"] == 1000
    assert buy["commission"] == 5          # 最低佣金
    assert buy["stamp_duty"] == 0
    assert buy["transfer_fee"] == 0.01

    sell = wealth.calc_fees(policy, "sell", 1000, 10)
    assert sell["commission"] == 5         # 0.25% * 10000 = 2.5 -> 最低 5
    assert sell["stamp_duty"] == 5         # 0.05% * 10000
    assert sell["transfer_fee"] == 0.1


def test_buy_then_sell_updates_cash_cost_and_realized(db):
    account, stock = _seed(db)
    buy = wealth.execute_trade(
        db, account=account, stock=stock, action="buy", quantity=100, price=10
    )
    assert buy["fees"]["total"] == 5.01
    assert round(account.available_funds, 2) == 98994.99
    pos = account.positions[0]
    assert pos.quantity == 100
    assert pos.cost_price == pytest.approx(10.0501, abs=1e-4)

    sell = wealth.execute_trade(
        db, account=account, stock=stock, action="sell", quantity=40, price=11
    )
    assert sell["fees"]["total"] == 5.22
    assert sell["realized_pnl"] == pytest.approx(32.78, abs=0.01)
    assert pos.quantity == 60
    assert round(account.available_funds, 2) == 99429.77


def test_dca_schedule_monthly_and_weekly():
    monthly = wealth.compute_first_run("monthly", 15, 0, date(2026, 11, 2))
    assert monthly == "2026-11-15"
    monthly_next = wealth.compute_first_run("monthly", 15, 0, date(2026, 11, 20))
    assert monthly_next == "2026-12-15"

    weekly = wealth.compute_first_run("weekly", 0, 0, date(2026, 11, 2))  # 周一
    assert weekly == "2026-11-02"

    plan = type("P", (), {"frequency": "weekly", "next_run_date": "2026-11-02", "day_of_month": 0})()
    assert wealth.advance_run(plan) == "2026-11-09"


def test_reinvest_adds_shares_without_cash_change(db):
    account, stock = _seed(db)
    wealth.execute_trade(db, account=account, stock=stock, action="buy", quantity=1000, price=10)
    cash_before = account.available_funds
    pos = account.positions[0]
    wealth.execute_trade(
        db, account=account, stock=stock, action="reinvest", quantity=79, price=10
    )
    assert pos.quantity == 1079
    assert account.available_funds == pytest.approx(cash_before, abs=0.01)
    # 成本摊薄：新增 790 元 / 1079 股
    assert pos.cost_price < 10.0501
