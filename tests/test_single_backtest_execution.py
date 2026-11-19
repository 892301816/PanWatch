"""Single-stock execution/accounting regressions using only synthetic price data."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.modules.strategy.backtest import single as S
from src.modules.strategy.backtest.cost_model import CostConfig, CostModel


ALWAYS = [{"field": "change_pct", "op": "gte", "value": -100}]


def bars(count=70, price=10.0):
    start = date(2025, 1, 1)
    return [S.Bar((start + timedelta(days=index)).isoformat(), price, price, price, price, 100000, "synthetic", "raw") for index in range(count)]


def run(monkeypatch, data, **kwargs):
    monkeypatch.setattr(S, "load_bars", lambda *a, **k: data)
    return S.run_single_backtest("600000", buy=ALWAYS, sell=[], **kwargs)


def test_cash_conservation_preserves_remainder_after_sales(monkeypatch):
    data = bars(65)
    monkeypatch.setattr(S, "load_bars", lambda *a, **k: data)
    result = S.run_single_backtest("600000", buy=ALWAYS, sell=ALWAYS, initial_capital=10507, days=65)
    running_cash = result["initial_capital"]
    for order in result["orders"]:
        if order["status"] != "filled":
            continue
        assert order["cash_before"] == pytest.approx(running_cash, abs=1e-4)
        running_cash += order["cash_delta"]
        assert order["cash_after"] == pytest.approx(running_cash, abs=1e-4)
        assert order["cash_after"] >= 0
    assert result["final_cash"] == pytest.approx(running_cash, abs=1e-4)
    assert result["final_equity"] == pytest.approx(running_cash + (result["open_position"] or {}).get("quantity", 0) * 10, abs=1e-4)
    assert result["trades"]
    first_trade = result["trades"][0]
    assert first_trade["entry_signal_date"] < first_trade["entry_date"] < first_trade["exit_date"]
    assert first_trade["exit_signal_date"] < first_trade["exit_date"]
    assert first_trade["fees"] > 0 and first_trade["slippage_cost"] > 0
    assert first_trade["trigger_conditions"]["entry"]["buy"][0]["matched"] is True


def test_whole_lots_reserve_slippage_and_minimum_commission(monkeypatch):
    data = bars()
    result = run(monkeypatch, data, initial_capital=1000)
    assert not result["open_position"]
    assert result["final_cash"] == 1000
    assert any(order["reason_code"] == "insufficient_cash_for_lot" for order in result["orders"])
    enough = run(monkeypatch, data, initial_capital=2000)
    buy = next(order for order in enough["orders"] if order["status"] == "filled")
    assert buy["quantity"] == 100
    assert buy["cash_after"] >= 0
    tiny = run(monkeypatch, data, initial_capital=10)
    assert tiny["final_cash"] == 10 and tiny["filled_count"] == 0


def test_explicit_empty_rules_do_not_fall_back_to_defaults(monkeypatch):
    data = bars()
    monkeypatch.setattr(S, "load_bars", lambda *a, **k: data)
    result = S.run_single_backtest("600000", buy=[], sell=[], avoid=[])
    assert result["rules"] == {"buy": [], "sell": [], "avoid": []}
    assert result["orders"] == [] and result["filled_count"] == 0
    hold = S.run_single_backtest("600000", buy=ALWAYS, sell=[])
    assert hold["trade_count"] == 0 and hold["open_position"]
    assert sum(point["buy"] for point in hold["curve"]) == 1
    assert not any(point["sell"] for point in hold["curve"])
    assert hold["current_status"] == "持仓中"
    assert "期末仍持仓" in hold["explain"]


def test_warmup_is_not_part_of_return_period_and_cannot_create_orders(monkeypatch):
    data = bars(160)
    # Extreme pre-period moves must not become measured buy-and-hold returns.
    data[:100] = [replace(bar, open=2, high=2, low=2, close=2) for bar in data[:100]]
    result = run(monkeypatch, data, start=data[130].date, end=data[135].date)
    assert result["start"] == data[130].date and result["end"] == data[135].date
    assert result["bars"] == len(result["curve"]) == 6
    assert result["buy_hold_return"] == 0
    assert result["curve"][0]["equity"] == result["initial_capital"]
    assert result["data_info"]["warmup_bars"] == 120
    assert all(order["signal_date"] >= data[130].date for order in result["orders"])
    assert result["open_position"]["entry_date"] == data[131].date
    assert len(result["data_info"]["data_hash"]) == 64


def test_unavailable_next_open_is_audited_without_fill(monkeypatch):
    data = bars(63)
    data[60] = replace(data[60], volume=0)
    result = run(monkeypatch, data)
    blocked = result["orders"][0]
    assert blocked["signal_date"] == data[59].date and blocked["execution_date"] == data[60].date
    assert blocked["status"] == "unfilled" and blocked["reason_code"] == "suspended_or_zero_volume"
    assert not result["curve"][1]["buy"]
    assert result["open_position"]["entry_date"] == data[62].date


def test_limit_up_cannot_buy_and_limit_down_cannot_sell(monkeypatch):
    data = bars(63)
    data[60] = replace(data[60], open=11, high=11, low=11, close=11)
    result = run(monkeypatch, data)
    assert result["orders"][0]["reason_code"] == "limit_up"
    # Ordinary buy on index60; the next day's lower limit prevents selling.
    data = bars(62)
    data[61] = replace(data[61], open=9, high=9, low=9, close=9)
    monkeypatch.setattr(S, "load_bars", lambda *a, **k: data)
    result = S.run_single_backtest("600000", buy=ALWAYS, sell=ALWAYS)
    assert result["open_position"]
    assert result["trade_count"] == 0
    assert any(order["reason_code"] == "limit_down" for order in result["orders"])


def test_no_next_bar_is_not_reported_as_a_trade(monkeypatch):
    result = run(monkeypatch, bars(60))
    assert result["filled_count"] == 0
    assert result["orders"][0]["reason_code"] == "no_next_bar"
    assert result["orders"][0]["execution_date"] is None


def test_mixed_adjustment_rejected_and_source_changes_change_hash(monkeypatch):
    data = bars()
    first = run(monkeypatch, data)
    second = run(monkeypatch, [replace(bar, source="other_source") for bar in data])
    assert first["data_info"]["data_hash"] != second["data_info"]["data_hash"]
    assert second["data_info"]["source"] == "other_source"
    mixed = run(monkeypatch, [replace(data[0], adjustment="qfq")] + data[1:])
    assert mixed["error_code"] == "mixed_adjustment"


def test_scope_and_parameters_are_explicit(monkeypatch):
    monkeypatch.setattr(S, "load_bars", lambda *a, **k: bars())
    assert S.run_single_backtest("AAPL", market="US")["error_code"] == "unsupported_instrument"
    assert S.run_single_backtest("510300")["error_code"] == "unsupported_instrument"
    assert S.run_single_backtest("600000", initial_capital=0)["error_code"] == "invalid_parameters"
    assert S.run_single_backtest("600000", start="2025-03-01", end="2025-01-01")["error_code"] == "invalid_date_range"
    supported = S.run_single_backtest("300750", buy=ALWAYS, sell=[])
    assert supported["data_info"]["limit_pct"] == 0.2
    assert supported["data_info"]["lot_size"] == 100


def test_fee_free_same_price_roundtrip_conserves_all_capital(monkeypatch):
    data = bars(62)
    monkeypatch.setattr(S, "load_bars", lambda *a, **k: data)
    free = CostModel(CostConfig(0, 0, 0, 0, 0))
    result = S.run_single_backtest("600000", buy=ALWAYS, sell=ALWAYS, initial_capital=10507, cost_model=free)
    assert result["final_cash"] == result["initial_capital"] == 10507
    assert result["final_equity"] == 10507
    assert result["trades"][0]["pnl"] == 0


def test_invalid_prices_fail_instead_of_poisoning_cash_or_json(monkeypatch):
    data = bars()
    data[60] = replace(data[60], close=float("nan"))
    result = run(monkeypatch, data)
    assert result["error_code"] == "invalid_price_data"
    data[60] = replace(data[60], close=10, volume=float("inf"))
    assert run(monkeypatch, data)["error_code"] == "invalid_price_data"


def test_science_board_uses_a_conservative_200_share_buy_lot(monkeypatch):
    monkeypatch.setattr(S, "load_bars", lambda *a, **k: bars())
    result = S.run_single_backtest("688001", buy=ALWAYS, sell=[], initial_capital=1500)
    assert result["data_info"]["lot_size"] == 200
    assert result["filled_count"] == 0
    assert any(order["reason_code"] == "insufficient_cash_for_lot" for order in result["orders"])
