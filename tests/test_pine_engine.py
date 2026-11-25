"""解析 / 求值主干的冒烟测试（不依赖 ta.py，便于先验证管线）。"""
from __future__ import annotations

import pytest

from src.modules.strategy.pine import PineSyntaxError, compile_source, run_script
from src.modules.strategy.pine.engine import PineBar


def make_bars(closes, opens=None, highs=None, lows=None, volumes=None):
    bars = []
    for index, close in enumerate(closes):
        open_price = opens[index] if opens else close
        bars.append(
            PineBar(
                date=f"2024-01-{index + 1:02d}",
                open=open_price,
                high=highs[index] if highs else max(open_price, close) * 1.01,
                low=lows[index] if lows else min(open_price, close) * 0.99,
                close=close,
                volume=volumes[index] if volumes else 1_000_000,
            )
        )
    return bars


def test_parse_accepts_indicator_and_strategy():
    indicator = compile_source('//@version=5\nindicator("测试", overlay=true)\nplot(close)\n')
    assert indicator["ok"] is True
    assert indicator["title"] == "测试"
    assert indicator["kind"] == "indicator"

    strategy = compile_source('//@version=5\nstrategy("策略", overlay=true)\nplot(close)\n')
    assert strategy["ok"] is True
    assert strategy["kind"] == "strategy"


def test_syntax_error_reports_line():
    result = compile_source('//@version=5\nindicator("x"\nplot(close)\n')
    assert result["ok"] is False
    assert result["diagnostics"][0]["line"] >= 1


def test_plot_series_is_produced():
    bars = make_bars([10, 11, 12, 13, 14])
    result = run_script('//@version=5\nindicator("t", overlay=true)\nplot(close, "收盘")\n', bars)
    assert result["kind"] == "indicator"
    names = [plot["name"] for plot in result["plots"]]
    assert "收盘" in names
    values = next(plot for plot in result["plots"] if plot["name"] == "收盘")["values"]
    assert values[-1] == pytest.approx(14.0)


def test_assignment_and_history_reference():
    bars = make_bars([10, 12, 14, 16])
    source = """
//@version=5
indicator("t", overlay=true)
prev = close[1]
plot(prev, "昨收")
plot(bar_index, "序号")
"""
    result = run_script(source, bars)
    previous = next(plot for plot in result["plots"] if plot["name"] == "昨收")["values"]
    assert previous[0] is None
    assert previous[1] == pytest.approx(10.0)
    assert previous[3] == pytest.approx(14.0)
    indexes = next(plot for plot in result["plots"] if plot["name"] == "序号")["values"]
    assert indexes == [0, 1, 2, 3]


def test_if_else_and_ternary():
    bars = make_bars([10, 20, 30])
    source = """
//@version=5
indicator("t", overlay=false)
flag = close > 15 ? 1 : 0
v = 0
if close > 15
    v := 2
else
    v := -2
plot(flag, "flag")
plot(v, "v")
"""
    result = run_script(source, bars)
    flags = next(plot for plot in result["plots"] if plot["name"] == "flag")["values"]
    values = next(plot for plot in result["plots"] if plot["name"] == "v")["values"]
    assert flags == [0, 1, 1]
    assert values == [-2, 2, 2]


def test_for_loop_and_user_function():
    bars = make_bars([1, 2, 3])
    source = """
//@version=5
indicator("t", overlay=false)
double(x) =>
    x * 2

total = 0.0
for i = 1 to 3
    total := total + i
plot(double(close), "双倍")
plot(total, "累加")
"""
    result = run_script(source, bars)
    doubled = next(plot for plot in result["plots"] if plot["name"] == "双倍")["values"]
    total = next(plot for plot in result["plots"] if plot["name"] == "累加")["values"]
    assert doubled[-1] == pytest.approx(6.0)
    assert total == [6.0, 6.0, 6.0]


def test_strategy_entries_fill_next_open():
    # 收盘价突破 15 时入场，成交在下一根开盘
    bars = make_bars(
        [10, 12, 20, 22, 18],
        opens=[10, 12, 12, 20, 22],
        highs=[10.5, 12.5, 20.5, 22.5, 18.5],
        lows=[9.5, 11.5, 11.5, 19.5, 17.5],
        volumes=[1e6] * 5,
    )
    source = """
//@version=5
strategy("突破", overlay=true, initial_capital=100000, default_qty_type=strategy.fixed, default_qty_value=100, pyramiding=1)
longCondition = close > 15
if longCondition and strategy.position_size == 0
    strategy.entry("L", strategy.long)
if close < 19 and strategy.position_size > 0
    strategy.close("L")
plot(close, "收盘")
"""
    result = run_script(source, bars, symbol="000001", initial_capital=100_000)
    assert result["kind"] == "strategy"
    # 信号在第 2 根(0-based)收盘产生 → 第 3 根开盘 20 成交
    entries = [order for order in result["orders"] if order["action"] == "entry"]
    assert entries, result
    assert entries[0]["index"] == 3
    assert entries[0]["price"] == pytest.approx(20.0)


def test_strategy_forced_liquidation_at_end():
    # 开盘价不要正好踩在涨跌停上，否则会被 A 股撮合闸门拦下
    bars = make_bars([10, 11, 12], opens=[10, 10.5, 12], highs=[10.6, 11.6, 12.6], lows=[9.4, 10.4, 11.4])
    source = """
//@version=5
strategy("一直持有", overlay=true, default_qty_type=strategy.fixed, default_qty_value=100)
if bar_index == 0
    strategy.entry("L", strategy.long)
"""
    result = run_script(source, bars, symbol="000001")
    assert result["trades"], result
    assert result["trades"][-1]["exit_price"] == pytest.approx(12.0)


def test_close_by_entry_id_actually_closes():
    """回归：``strategy.close("L")`` 必须能按订单 id 找到持仓。

    曾经成交记录的 id 用的是内部序号（t1/t2），导致 ``close("L")`` 匹配不到，
    整段行情只会产生 1 笔交易（真实数据 620 根只成交 1 次）。
    """
    bars = make_bars(
        [10, 10.2, 10.4, 10.6, 10.8, 11.0, 11.2, 11.4, 11.6],
        opens=[10, 10.1, 10.3, 10.5, 10.7, 10.9, 11.1, 11.3, 11.5],
        highs=[10.5] * 9,
        lows=[9.5] * 9,
    )
    source = """
//@version=5
strategy("id 回归", overlay=true, default_qty_type=strategy.fixed, default_qty_value=100)
if bar_index % 4 == 1
    strategy.entry("L", strategy.long)
if bar_index % 4 == 3
    strategy.close("L")
"""
    result = run_script(source, bars, symbol="000001")
    assert len(result["trades"]) >= 2, result["trades"]
    assert all(trade["entry_id"] == "L" for trade in result["trades"])


def test_exit_order_from_entry_matches_id():
    """``strategy.exit(from_entry="L")`` 也要能找到对应仓位。"""
    bars = make_bars(
        [10, 10.5, 11, 11.5, 12, 12.5, 13, 13.5],
        opens=[10, 10.2, 10.8, 11.2, 11.8, 12.2, 12.8, 13.2],
        highs=[10.6, 11.0, 11.6, 12.0, 12.6, 13.0, 13.6, 14.0],
        lows=[9.8, 10.2, 10.6, 11.0, 11.6, 12.0, 12.6, 13.0],
    )
    source = """
//@version=5
strategy("止盈绑定", overlay=true, default_qty_type=strategy.fixed, default_qty_value=100)
if bar_index == 0
    strategy.entry("L", strategy.long)
    strategy.exit("TP", "L", limit=11.55)
"""
    result = run_script(source, bars, symbol="000001")
    assert result["trades"], result
    assert result["trades"][0]["entry_id"] == "L"


def test_unknown_variable_raises():
    bars = make_bars([10, 11])
    with pytest.raises(Exception):
        run_script('//@version=5\nindicator("t")\nplot(notdefined)\n', bars)
