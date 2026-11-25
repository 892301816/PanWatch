"""Pine 引擎与 PanWatch 既有链路的集成测试。

覆盖三件事：
1. ``chart_script`` 的引擎路由（旧 4 行 DSL vs Pine）
2. ``series`` / ``chart_bar`` 的字段透传（前端要能拿到自定义 plot 列）
3. ``template_runtime.normalize_config`` 对 ``engine``/``inputs`` 的放行与校验
"""
from __future__ import annotations

import pytest

from src.modules.strategy.chart_script import chart_bar, compile_script, series, series_with_meta
from src.modules.strategy.pine.engine import PineBar

LEGACY = "FAST = MA(5)\nSLOW = MA(20)\nBUY = CROSSUP(FAST,SLOW)\nSELL = CROSSDOWN(FAST,SLOW)"

PINE = """//@version=5
strategy("集成测试", overlay=true, initial_capital=100000,
     default_qty_type=strategy.percent_of_equity, default_qty_value=100)
length = input.int(5, "均线周期")
ma = ta.sma(close, length)
if ta.crossover(close, ma)
    strategy.entry("L", strategy.long)
if ta.crossunder(close, ma)
    strategy.close("L")
plot(ma, "均线", color.blue)
plot(ta.rsi(close, 14), "RSI", color.orange)
"""


def make_bars(count: int = 60):
    bars = []
    for index in range(count):
        close = 10 + (index % 9) * 0.3 + index * 0.02
        bars.append(
            PineBar(
                date=f"2024-{(index // 28) + 1:02d}-{(index % 28) + 1:02d}",
                open=close - 0.05,
                high=close + 0.2,
                low=close - 0.2,
                close=close,
                volume=1_000_000,
            )
        )
    return bars


def test_router_picks_legacy_for_four_line_script():
    compiled = compile_script(LEGACY)
    assert isinstance(compiled, tuple)
    rows = series(make_bars(40), LEGACY)
    assert rows and "template_entry" in rows[0]


def test_router_picks_pine_for_declaration():
    compiled = compile_script(PINE)
    assert isinstance(compiled, dict)
    assert compiled["engine"] == "pine"
    assert compiled["kind"] == "strategy"


def test_pine_rows_carry_plot_columns():
    bars = make_bars()
    rows, meta = series_with_meta(bars, PINE, symbol="000001", interval="1d")
    assert meta["engine"] == "pine"
    names = {plot["name"] for plot in meta["plots"]}
    assert {"均线", "RSI"} <= names
    # 副图判定：RSI 应落在副图，均线在主图
    overlay = {plot["name"]: plot["overlay"] for plot in meta["plots"]}
    assert overlay["均线"] is True
    assert overlay["RSI"] is False
    assert "均线" in rows[-1] and "RSI" in rows[-1]
    assert all(row["template_entry"] in (None, 0, 1) for row in rows)


def test_pine_inputs_are_listed_once():
    _rows, meta = series_with_meta(make_bars(), PINE, symbol="000001")
    names = [item["name"] for item in meta["inputs"]]
    assert names.count("均线周期") == 1


def test_chart_bar_passes_plot_columns_through():
    bars = make_bars(10)
    rows = series(bars, PINE, symbol="000001")
    payload = [chart_bar(bar, row) for bar, row in zip(bars, rows)]
    assert "均线" in payload[-1]
    assert payload[-1]["close"] == bars[-1].close


def test_inputs_override_changes_indicator():
    bars = make_bars()
    rows_a = series(bars, PINE, symbol="000001")
    rows_b = series(bars, PINE, symbol="000001", inputs={"均线周期": 20})
    assert rows_a[-1]["均线"] != pytest.approx(rows_b[-1]["均线"])


def test_normalize_config_accepts_engine_and_inputs():
    from src.modules.strategy.template_runtime import normalize_config

    config = normalize_config({"chart_script": PINE, "engine": "pine", "inputs": {"均线周期": 12}})
    assert config["engine"] == "pine"
    assert config["inputs"] == {"均线周期": 12}


def test_normalize_config_rejects_bad_inputs():
    from src.modules.strategy.template_runtime import normalize_config

    with pytest.raises(ValueError):
        normalize_config({"chart_script": PINE, "inputs": {"x": {"nested": 1}}})
    with pytest.raises(ValueError):
        normalize_config({"chart_script": PINE, "unknown": 1})


def test_workflow_keeps_inputs_for_backtest():
    """回测链路会经 validate_rules → config → attach(series(inputs=...))。"""
    from src.modules.strategy import workflow

    rules = workflow.validate_rules(
        {
            "scope": "global",
            "name": "带参数脚本",
            "market": "CN",
            "config": {"chart_script": PINE, "inputs": {"均线周期": 5}},
            "buy": [{"field": "template_entry", "op": "eq", "value": 1}],
            "sell": [{"field": "template_exit", "op": "eq", "value": 1}],
        }
    )
    assert rules["config"]["inputs"] == {"均线周期": 5}


def test_input_string_renders_and_returns_value():
    bars = make_bars(30)
    source = """//@version=5
indicator("字符串参数", overlay=false)
mode = input.string("A", "模式", options=["A", "B"])
signal = mode == "A" ? 1.0 : 2.0
plot(signal, "信号")
"""
    rows, meta = series_with_meta(bars, source, symbol="000001")
    assert meta["inputs"][0]["kind"] == "string"
    assert rows[-1]["信号"] == 1.0


def test_pine_syntax_error_surfaces_line_message():
    bad = '//@version=5\nindicator("x", overlay=true)\nplot(close\n'
    with pytest.raises(ValueError) as exc:
        compile_script(bad)
    assert "第" in str(exc.value)
