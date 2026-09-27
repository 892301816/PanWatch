"""ETF 持仓端到端用例：建仓 → 取行情 → 估值盈亏。

回归背景：ETF（如 510300）此前搜不到，自然也无法建仓。搜索放开后需要确认
后续链路对 ETF 同样成立——ETF 在 ``market="CN"`` 下走与股票完全相同的报价
与估值路径，不应被当成需要特殊处理或会被静默跳过的品种。

这里用内存库 + 替身行情层，锁住三件事：
1. ETF 代码原样传给行情层（不被前缀归一化破坏）；
2. 市值/盈亏/当日盈亏按 CN 口径（汇率 1.0）计算；
3. 汇总结果里 ETF 与股票并列出现，不会被过滤掉。

行情数值取自 2026-09-24 收盘的真实腾讯源返回，便于人工核对量级。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.portfolio.api import accounts as accounts_api
from src.platform.persistence.database import Base
from src.platform.persistence.models import Account, Position, Stock  # noqa: F401

# 真实行情形态（腾讯源 510300 / 2026-09-24）
ETF_QUOTE = {
    "symbol": "510300",
    "name": "沪深300ETF华泰柏瑞",
    "market": "CN",
    "current_price": 4.515,
    "change_pct": -1.63,
    "change_amount": -0.075,
    "prev_close": 4.59,
    "open_price": 4.578,
    "high_price": 4.579,
    "low_price": 4.512,
    "volume": 7102519.0,
    "turnover": 3220888135.0,
    "turnover_rate": 2.97,
    "pe_ratio": None,  # ETF 无市盈率，必须能容忍 None
    "total_market_value": 1079.29,
    "circulating_market_value": 1079.29,
}


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture
def etf_position(session):
    """一个 ETF 持仓：成本 4.20 / 1000 份。"""
    account = Account(name="ETF 测试账户", available_funds=10000.0)
    stock = Stock(symbol="510300", name="沪深300ETF华泰柏瑞", market="CN")
    session.add_all([account, stock])
    session.flush()
    position = Position(
        account_id=account.id,
        stock_id=stock.id,
        cost_price=4.20,
        quantity=1000,
    )
    session.add(position)
    session.commit()
    return session, account, stock, position


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """汇率走固定值，避免用例触网。"""
    monkeypatch.setattr(accounts_api, "get_hkd_cny_rate", lambda: 0.92)
    monkeypatch.setattr(accounts_api, "get_usd_cny_rate", lambda: 7.10)


def test_etf_symbol_reaches_quote_layer_unchanged(etf_position, monkeypatch):
    """ETF 代码应原样传给行情层，市场判定为 CN"""
    session, *_ = etf_position
    seen = {}

    def fake_md_quote_rows(symbols, market):
        seen["symbols"] = list(symbols)
        seen["market"] = market
        return [ETF_QUOTE]

    monkeypatch.setattr(accounts_api, "md_quote_rows", fake_md_quote_rows)

    accounts_api.get_portfolio_summary(account_id=None, include_quotes=True, db=session)

    assert seen["symbols"] == ["510300"]
    assert seen["market"] == "CN"


def test_etf_position_is_valued(etf_position, monkeypatch):
    """ETF 持仓应算出市值与盈亏，不被静默跳过"""
    session, account, stock, position = etf_position
    monkeypatch.setattr(accounts_api, "md_quote_rows", lambda symbols, market: [ETF_QUOTE])

    result = accounts_api.get_portfolio_summary(
        account_id=account.id, include_quotes=True, db=session
    )

    positions = result["accounts"][0]["positions"]
    assert len(positions) == 1
    row = positions[0]

    assert row["symbol"] == "510300"
    assert row["market"] == "CN"
    assert row["current_price"] == 4.515
    # 4.515 * 1000
    assert row["market_value"] == 4515.0
    # (4.515 - 4.20) * 1000
    assert row["pnl"] == 315.0
    # 315 / 4200 * 100
    assert row["pnl_pct"] == 7.5
    # (4.515 - 4.59) * 1000
    assert row["daily_pnl"] == -75.0

    total = result["total"]
    assert total["total_market_value"] == 4515.0
    assert total["total_pnl"] == 315.0


def test_etf_without_pe_ratio_does_not_break_summary(etf_position, monkeypatch):
    """ETF 的 pe_ratio 为 None 时汇总仍应正常返回"""
    session, account, *_ = etf_position
    monkeypatch.setattr(accounts_api, "md_quote_rows", lambda symbols, market: [ETF_QUOTE])

    result = accounts_api.get_portfolio_summary(
        account_id=account.id, include_quotes=True, db=session
    )

    assert result["accounts"][0]["positions"][0]["market_value"] == 4515.0


def test_etf_and_stock_coexist_in_summary(session, monkeypatch):
    """ETF 与股票混合持仓应同时出现在汇总中"""
    account = Account(name="混合账户", available_funds=0.0)
    etf = Stock(symbol="510300", name="沪深300ETF华泰柏瑞", market="CN")
    stock = Stock(symbol="600519", name="贵州茅台", market="CN")
    session.add_all([account, etf, stock])
    session.flush()
    session.add_all([
        Position(account_id=account.id, stock_id=etf.id, cost_price=4.20, quantity=1000),
        Position(account_id=account.id, stock_id=stock.id, cost_price=1200.0, quantity=100),
    ])
    session.commit()

    stock_quote = {**ETF_QUOTE, "symbol": "600519", "name": "贵州茅台",
                   "current_price": 1237.0, "prev_close": 1251.24, "change_pct": -1.14}

    def fake_md_quote_rows(symbols, market):
        by_symbol = {q["symbol"]: q for q in (ETF_QUOTE, stock_quote)}
        return [by_symbol[s] for s in symbols if s in by_symbol]

    monkeypatch.setattr(accounts_api, "md_quote_rows", fake_md_quote_rows)

    result = accounts_api.get_portfolio_summary(
        account_id=account.id, include_quotes=True, db=session
    )

    symbols = {p["symbol"] for p in result["accounts"][0]["positions"]}
    assert symbols == {"510300", "600519"}
