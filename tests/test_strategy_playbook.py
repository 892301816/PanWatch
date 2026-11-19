"""策略卡:保存/解析/检查;单票回测。"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.modules.strategy import playbooks as P
from src.modules.strategy.backtest import single as S
from src.platform.persistence.database import Base


def _bars(n=80, start=10.0):
    out = []
    price = start
    for i in range(n):
        price = price * (1.002 if i % 3 else 0.999)
        out.append(
            S.Bar(
                date=f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                open=price * 0.99,
                high=price * 1.01,
                low=price * 0.98,
                close=price,
                volume=1000 + i,
            )
        )
    return out


@pytest.fixture()
def db():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture()
def global_playbook(db):
    row = P.save_playbook(
        {
            "scope": "global",
            "name": "测试策略",
            "buy": [{"field": "runup_3d", "op": "between", "value": [-99, 99]}],
            "sell": [{"field": "profit_ratio", "op": "gte", "value": 101}],
        }, db=db,
    )
    yield row
    P.delete_playbook(row["id"], db=db)


def test_global_playbook_resolves(global_playbook, monkeypatch, db):
    monkeypatch.setattr(P, "_current_bars", lambda *a, **k: _bars())
    data = P.check_symbol("600000", db=db)
    assert data["has_playbook"] is True
    assert data["verdict"] == "符合买入"
    assert data["buy_hits"] == data["buy_total"] == 1


def test_stock_playbook_overrides_global(global_playbook, monkeypatch, db):
    stock = P.save_playbook(
        {
            "scope": "stock",
            "symbol": "600000",
            "name": "个股覆盖",
            "buy": [{"field": "runup_3d", "op": "gte", "value": 999}],
            "sell": [],
        }, db=db,
    )
    try:
        monkeypatch.setattr(P, "_current_bars", lambda *a, **k: _bars())
        data = P.check_symbol("600000", db=db)
        assert data["name"] == "个股覆盖"
        assert data["verdict"] != "符合买入"
    finally:
        P.delete_playbook(stock["id"], db=db)


def test_single_backtest_runs(monkeypatch):
    bars = _bars(120, start=10.0)
    monkeypatch.setattr(S, "load_bars", lambda *a, **k: bars)
    r = S.run_single_backtest(
        "600000",
        buy=[{"field": "runup_3d", "op": "between", "value": [-99, 99]}],
        sell=[{"field": "profit_ratio", "op": "gte", "value": 0}],
        days=120,
    )
    assert r["bars"] == 61  # initial 59 bars initialize indicators only
    assert r["trade_count"] >= 1
    assert r["start"] == bars[59].date
