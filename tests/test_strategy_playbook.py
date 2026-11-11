"""策略卡:保存/解析/检查;单票回测。"""
import pytest

from src.modules.strategy import playbooks as P
from src.modules.strategy.backtest import single as S


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
def global_playbook():
    row = P.save_playbook(
        {
            "scope": "global",
            "name": "测试策略",
            "buy": [{"field": "runup_3d", "op": "between", "value": [-99, 99]}],
            "sell": [{"field": "profit_ratio", "op": "gte", "value": 101}],
        }
    )
    yield row
    P.delete_playbook(row["id"])


def test_global_playbook_resolves(global_playbook, monkeypatch):
    monkeypatch.setattr(P, "_current_bars", lambda *a, **k: _bars())
    data = P.check_symbol("600000")
    assert data["has_playbook"] is True
    assert data["verdict"] == "符合买入"
    assert data["buy_hits"] == data["buy_total"] == 1


def test_stock_playbook_overrides_global(global_playbook, monkeypatch):
    stock = P.save_playbook(
        {
            "scope": "stock",
            "symbol": "600000",
            "name": "个股覆盖",
            "buy": [{"field": "runup_3d", "op": "gte", "value": 999}],
            "sell": [],
        }
    )
    try:
        monkeypatch.setattr(P, "_current_bars", lambda *a, **k: _bars())
        data = P.check_symbol("600000")
        assert data["name"] == "个股覆盖"
        assert data["verdict"] != "符合买入"
    finally:
        P.delete_playbook(stock["id"])


def test_single_backtest_runs(monkeypatch):
    bars = _bars(120, start=10.0)
    monkeypatch.setattr(S, "load_bars", lambda *a, **k: bars)
    r = S.run_single_backtest(
        "600000",
        buy=[{"field": "runup_3d", "op": "between", "value": [-99, 99]}],
        sell=[{"field": "profit_ratio", "op": "gte", "value": 0}],
        days=120,
    )
    assert r["bars"] == 120
    assert r["trade_count"] >= 1
    assert r["start"] == bars[0].date
