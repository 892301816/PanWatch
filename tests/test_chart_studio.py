from datetime import date, timedelta

import pytest

from src.modules.strategy.chart_script import series, compile_script
from src.modules.strategy.backtest.single import Bar, run_single_backtest
from src.modules.strategy.template_runtime import normalize_config

SCRIPT = "FAST = MA(2)\nSLOW = EMA(5)\nBUY = CROSSUP(FAST, SLOW)\nSELL = CROSSDOWN(FAST, SLOW)"


def test_script_uses_completed_bars_and_opposite_crosses():
    bars = [Bar(str(date(2025, 1, 1)+timedelta(days=i)), c, c, c, c, 10000)
            for i, c in enumerate([10, 10, 10, 10, 10, 11, 11, 9, 8])]
    points = series(bars, SCRIPT)
    assert points[3]["slow"] is None
    assert points[4]["template_entry"] is None
    assert points[5]["template_entry"] is True
    assert any(row["template_exit"] for row in points[6:])
    assert series(bars[:7], SCRIPT) == points[:7]


@pytest.mark.parametrize("bad", [SCRIPT.replace("MA(2)", "MA(200)"), SCRIPT.replace("EMA", "unknown"),
                                  SCRIPT.replace("CROSSDOWN", "CROSSUP"), SCRIPT+"\nPRINT(FAST)"])
def test_script_validation_rejects_invalid_source(bad):
    with pytest.raises(ValueError):
        compile_script(bad)
    with pytest.raises(ValueError):
        normalize_config({"chart_script": bad})


def test_report_candles_and_orders_share_script_evidence():
    import math
    bars = []
    for i in range(280):
        c = 10 + math.sin(i/4)
        bars.append(Bar(str(date(2025, 1, 1)+timedelta(days=i)), c, c+.1, c-.1, c, 100000))
    result = run_single_backtest("600519", days=90, bars_override=bars,
                                 config={"chart_script": SCRIPT},
                                 buy=[{"field":"template_entry","op":"eq","value":1}],
                                 sell=[{"field":"template_exit","op":"eq","value":1}])
    points = {row["date"]: row for row in result["chart_bars"]}
    assert list(points) == [p["date"] for p in result["curve"]]
    assert result["filled_count"] > 1
    for order in result["orders"]:
        signal = points[order["signal_date"]]
        assert signal["template_entry" if order["side"] == "buy" else "template_exit"]
        if order["status"] == "filled":
            assert order["execution_date"] > signal["date"]

