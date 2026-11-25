"""``ta.py`` / ``ta_full.py`` 指标库的单元测试。

不依赖真实行情：每个指标维护若干条 :class:`Series`，逐 bar ``set(i, v)`` 后再构造
:class:`ArgView`，并把 OHLCV 注入 ``site.env``，模拟引擎「逐根重放 + 实参自带历史
序列」的行为。断言以数值性质为主（播种位置、对称性、取值范围、手算值），避免把
实现细节写死成快照。
"""

from __future__ import annotations

import math

import pytest

from src.modules.strategy.pine import ta as ta_entry
from src.modules.strategy.pine import ta_full as ta
from src.modules.strategy.pine.errors import PineRuntimeError
from src.modules.strategy.pine.series import ArgView, Series, SiteState

BAR_NAMES = ("open", "high", "low", "close", "volume")


# ---------------------------------------------------------------------------
# 驱动工具
# ---------------------------------------------------------------------------


def _feed(func, columns, args=(), kwargs=None, *, site=None):
    """逐 bar 推进一个指标，返回每根 bar 的输出。

    - ``columns``：列名 → 逐 bar 取值；名为 open/high/low/close/volume 的列会
      自动写进 ``site.env``（引擎的行情上下文）。
    - ``args``：位置实参；字符串表示列名，其它裸值会被包成「常数序列」的 ArgView。
    - ``kwargs``：具名实参；字符串表示列名（不存在时按裸值），其它裸值同样包成
      常数序列的 ArgView —— 与引擎「每个实参都是一条序列」的行为一致。
    """
    streams = {name: Series() for name in columns}
    constants: dict[tuple, Series] = {}
    site = site if site is not None else SiteState()
    names = list(columns)
    total = len(columns[names[0]]) if names else 0
    outputs = []
    for index in range(total):
        for name in names:
            streams[name].set(index, columns[name][index])
        views = {name: ArgView(stream, stream.at(0)) for name, stream in streams.items()}
        for name in BAR_NAMES:
            if name in views:
                site.env[name] = views[name]

        def as_view(spec):
            if isinstance(spec, str) and spec in views:
                return views[spec]
            key = (type(spec), spec)
            series = constants.get(key)
            if series is None:
                series = Series()
                constants[key] = series
            series.set(index, spec)
            return ArgView(series, spec)

        positional = [as_view(spec) for spec in args]
        named = {key: as_view(spec) for key, spec in (kwargs or {}).items()}
        outputs.append(func(positional, named, site))
    return outputs


def _sample_columns(count=40):
    """一段有涨有跌、有量的合成行情。"""
    close = [10.0 + math.sin(index / 3.0) * 2.0 + index * 0.05 for index in range(count)]
    return {
        "open": [value - 0.2 for value in close],
        "high": [value + 0.5 for value in close],
        "low": [value - 0.5 for value in close],
        "close": close,
        "volume": [1000.0 + index * 10.0 for index in range(count)],
        "cond": [index % 5 == 0 for index in range(count)],
    }


# ---------------------------------------------------------------------------
# 1. 常数序列
# ---------------------------------------------------------------------------


def test_constant_series_moving_averages():
    """常数序列：sma/ema/rma 播种后都等于该常数。"""
    columns = {"close": [5.0] * 12}
    for function in (ta.ta_sma, ta.ta_ema, ta.ta_rma):
        outputs = _feed(function, columns, args=["close", 4])
        assert outputs[:3] == [None, None, None]
        assert outputs[3:] == pytest.approx([5.0] * 9)


# ---------------------------------------------------------------------------
# 2. ema / rma 播种
# ---------------------------------------------------------------------------


def test_ema_seeding_uses_sma():
    """EMA 前 length-1 根为 na，第 length 根等于前 length 根的 SMA。"""
    outputs = _feed(ta.ta_ema, {"close": [1.0, 2.0, 3.0, 4.0, 5.0]}, args=["close", 3])
    assert outputs[:2] == [None, None]
    assert outputs[2] == pytest.approx(2.0)  # (1+2+3)/3
    assert outputs[3] == pytest.approx(3.0)  # 0.5*4 + 0.5*2
    assert outputs[4] == pytest.approx(4.0)  # 0.5*5 + 0.5*3


def test_rma_seeding_uses_sma():
    """RMA（Wilder）同样用 SMA 播种，alpha=1/length。"""
    outputs = _feed(ta.ta_rma, {"close": [2.0, 4.0, 6.0, 8.0]}, args=["close", 2])
    assert outputs[0] is None
    assert outputs[1] == pytest.approx(3.0)  # (2+4)/2
    assert outputs[2] == pytest.approx(4.5)  # 0.5*6 + 0.5*3
    assert outputs[3] == pytest.approx(6.25)  # 0.5*8 + 0.5*4.5


def test_ema_holds_previous_on_na():
    """source 当前值为 na 时，EMA 保持上一根（TideView 口径）。"""
    outputs = _feed(ta.ta_ema, {"close": [1.0, 2.0, None, 3.0]}, args=["close", 2])
    assert outputs[0] is None
    assert outputs[1] == pytest.approx(1.5)
    assert outputs[2] == pytest.approx(1.5)  # 保持上一根，不重新播种
    assert outputs[3] == pytest.approx(2.5)  # 2/3*3 + 1/3*1.5


def test_rma_holds_previous_on_na():
    """source 当前值为 na 时，RMA 保持上一根。"""
    outputs = _feed(ta.ta_rma, {"close": [1.0, 3.0, None, 5.0]}, args=["close", 2])
    assert outputs[1] == pytest.approx(2.0)
    assert outputs[2] == pytest.approx(2.0)
    assert outputs[3] == pytest.approx(3.5)  # 0.5*5 + 0.5*2


# ---------------------------------------------------------------------------
# 3. RSI
# ---------------------------------------------------------------------------


def test_rsi_all_up_is_100():
    """全上涨序列的 RSI 为 100。"""
    outputs = _feed(ta.ta_rsi, {"close": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]}, args=["close", 2])
    assert outputs[:2] == [None, None]
    assert outputs[2:] == pytest.approx([100.0] * 4)


def test_rsi_all_down_is_zero():
    """全下跌序列的 RSI 为 0。"""
    outputs = _feed(ta.ta_rsi, {"close": [6.0, 5.0, 4.0, 3.0]}, args=["close", 2])
    assert outputs[:2] == [None, None]
    assert outputs[2:] == pytest.approx([0.0] * 2)


def test_rsi_stays_in_range():
    """RSI 输出恒在 [0, 100]。"""
    close = [10.0, 11.0, 10.0, 12.0, 9.0, 11.0, 13.0, 12.0, 14.0]
    outputs = _feed(ta.ta_rsi, {"close": close}, args=["close", 3])
    for value in outputs:
        assert value is None or 0.0 <= value <= 100.0
    assert any(value is not None for value in outputs)


# ---------------------------------------------------------------------------
# 4. TR / ATR 手算
# ---------------------------------------------------------------------------


def test_tr_hand_computed():
    """TR 与手算一致：无前收用 high-low，有前收取三项最大。"""
    columns = {
        "high": [10.0, 12.0, 11.0],
        "low": [8.0, 9.0, 10.0],
        "close": [9.0, 11.0, 10.5],
    }
    outputs = _feed(ta.ta_tr, columns)
    assert outputs == pytest.approx([2.0, 3.0, 1.0])


def test_atr_hand_computed():
    """ATR = RMA(TR, 2)，三根样本手算为 [na, 2.5, 1.75]。"""
    columns = {
        "high": [10.0, 12.0, 11.0],
        "low": [8.0, 9.0, 10.0],
        "close": [9.0, 11.0, 10.5],
    }
    outputs = _feed(ta.ta_atr, columns, args=[2])
    assert outputs[0] is None
    assert outputs[1] == pytest.approx(2.5)  # (2+3)/2
    assert outputs[2] == pytest.approx(1.75)  # 0.5*1 + 0.5*2.5


# ---------------------------------------------------------------------------
# 5. 标准差 / 方差 / 平均绝对偏差
# ---------------------------------------------------------------------------


def test_stdev_is_population():
    """总体标准差：[1,2,3,4] → sqrt(1.25)。"""
    outputs = _feed(ta.ta_stdev, {"close": [1.0, 2.0, 3.0, 4.0]}, args=["close", 4])
    assert outputs[:3] == [None, None, None]
    assert outputs[3] == pytest.approx(math.sqrt(1.25))


def test_variance_and_dev():
    """方差用总体公式；dev 为平均绝对偏差。"""
    columns = {"close": [1.0, 2.0, 3.0, 4.0]}
    variance = _feed(ta.ta_variance, columns, args=["close", 4])
    deviation = _feed(ta.ta_dev, columns, args=["close", 4])
    assert variance[3] == pytest.approx(1.25)
    assert deviation[3] == pytest.approx((1.5 + 0.5 + 0.5 + 1.5) / 4)


# ---------------------------------------------------------------------------
# 6. MACD
# ---------------------------------------------------------------------------


def test_macd_has_three_values_and_doubled_histogram():
    """MACD 返回三元组，且柱体是 2*(macd - signal)（TV 差异已写死）。"""
    close = [10.0 + math.sin(index / 2.0) + index * 0.1 for index in range(30)]
    outputs = _feed(ta.ta_macd, {"close": close}, args=["close", 3, 6, 4])
    assert len(outputs) == len(close)
    seen = False
    for macd_line, signal_line, histogram in outputs:
        if macd_line is None or signal_line is None:
            assert histogram is None
            continue
        assert histogram == pytest.approx(2.0 * (macd_line - signal_line))
        if abs(histogram - (macd_line - signal_line)) > 1e-9:
            seen = True
    assert seen, "柱体必须乘 2，不能与 macd-signal 相同"


# ---------------------------------------------------------------------------
# 7. 布林带
# ---------------------------------------------------------------------------


def test_bb_symmetric_around_middle():
    """布林带上下轨相对中轨对称。"""
    close = [1.0, 2.0, 3.0, 4.0, 5.0]
    outputs = _feed(ta.ta_bb, {"close": close}, args=["close", 3, 2.0])
    assert outputs[:2] == [(None, None, None)] * 2
    for middle, upper, lower in outputs[2:]:
        assert middle is not None
        assert upper - middle == pytest.approx(middle - lower)
        assert upper > middle > lower


def test_bbw_matches_band_width():
    """bbw = (upper - lower) / middle。"""
    close = [1.0, 2.0, 3.0, 4.0, 5.0]
    widths = _feed(ta.ta_bbw, {"close": close}, args=["close", 3, 2.0])
    bands = _feed(ta.ta_bb, {"close": close}, args=["close", 3, 2.0])
    for width, (middle, upper, lower) in zip(widths, bands):
        if width is None:
            assert middle is None
            continue
        assert width == pytest.approx((upper - lower) / middle)


# ---------------------------------------------------------------------------
# 8. 交叉
# ---------------------------------------------------------------------------


def test_crossover_and_crossunder():
    """相等时不触发交叉，真正穿越才触发。"""
    first = {"a": [1.0, 1.0, 2.0, 3.0], "b": [2.0, 2.0, 2.0, 2.0]}
    up = _feed(ta.ta_crossover, first, args=["a", "b"])
    assert up == [False, False, False, True]

    second = {"a": [3.0, 3.0, 2.0, 1.0], "b": [2.0, 2.0, 2.0, 2.0]}
    down = _feed(ta.ta_crossunder, second, args=["a", "b"])
    assert down == [False, False, False, True]

    both = _feed(ta.ta_cross, first, args=["a", "b"])
    assert both == [False, False, False, True]


def test_cross_na_returns_false():
    """任一侧为 na 时交叉返回 false（不是 na）。"""
    columns = {"a": [1.0, None, 3.0], "b": [0.0, 0.0, 0.0]}
    outputs = _feed(ta.ta_crossover, columns, args=["a", "b"])
    assert outputs == [False, False, False]


# ---------------------------------------------------------------------------
# 9. 极值
# ---------------------------------------------------------------------------


def test_highest_lowest_skip_na():
    """highest/lowest 跳过 na 继续往前凑窗口。"""
    columns = {"close": [1.0, None, 3.0, 2.0]}
    highs = _feed(ta.ta_highest, columns, args=["close", 2])
    lows = _feed(ta.ta_lowest, columns, args=["close", 2])
    assert highs[:2] == [None, None]
    assert highs[2] == pytest.approx(3.0)  # 跳过 na 后取 [1, 3]
    assert highs[3] == pytest.approx(3.0)
    assert lows[2] == pytest.approx(1.0)
    assert lows[3] == pytest.approx(2.0)


def test_highest_returns_na_when_not_enough_values():
    """有效值不足 length 时返回 na。"""
    outputs = _feed(ta.ta_highest, {"close": [1.0, 2.0]}, args=["close", 3])
    assert outputs == [None, None]


def test_extreme_bars_offsets():
    """highestbars/lowestbars 返回相对当前（负数），同值取最近位置。"""
    columns = {"close": [1.0, 2.0, 3.0, 2.0, 1.0]}
    highs = _feed(ta.ta_highestbars, columns, args=["close", 3])
    lows = _feed(ta.ta_lowestbars, columns, args=["close", 3])
    assert highs[2] == 0
    assert highs[3] == -1
    assert highs[4] == -2
    assert lows[2] == -2
    assert lows[3] == 0  # 窗口 [2,3,2]，最小值并列取最近的当根
    assert lows[4] == 0

    ties = _feed(ta.ta_highestbars, {"close": [5.0, 5.0]}, args=["close", 2])
    assert ties[1] == 0  # 并列取最近出现的位置


# ---------------------------------------------------------------------------
# 10. 枢轴
# ---------------------------------------------------------------------------


def test_pivothigh_returns_center_on_confirmation_bar():
    """pivothigh 在「确认 bar」（中心 bar 之后 right 根）返回中心值。"""
    columns = {"high": [1.0, 2.0, 3.0, 2.0, 1.0]}
    outputs = _feed(ta.ta_pivothigh, columns, args=["high", 1, 1])
    assert outputs[:3] == [None, None, None]
    assert outputs[3] == pytest.approx(3.0)
    assert outputs[4] is None


def test_pivotlow_returns_center_on_confirmation_bar():
    """pivotlow 同确认 bar 口径。"""
    columns = {"low": [5.0, 4.0, 3.0, 4.0, 5.0]}
    outputs = _feed(ta.ta_pivotlow, columns, args=["low", 1, 1])
    assert outputs[3] == pytest.approx(3.0)
    assert outputs[2] is None


# ---------------------------------------------------------------------------
# 11. 参数校验
# ---------------------------------------------------------------------------


def test_invalid_length_raises_pine_runtime_error():
    """length<=0 抛 PineRuntimeError（不是裸 ValueError）。"""
    columns = {"close": [1.0, 2.0, 3.0], "length": [0, 0, 0]}
    for function in (ta.ta_sma, ta.ta_ema, ta.ta_rsi, ta.ta_stdev):
        with pytest.raises(PineRuntimeError):
            _feed(function, columns, args=["close", "length"])
    with pytest.raises(PineRuntimeError):
        _feed(ta.ta_ema, {"close": [1.0, 2.0, 3.0]}, args=["close", -3])


def test_missing_source_returns_na():
    """缺少必填序列时优雅返回 na（不抛异常）。"""
    assert ta.ta_sma([], {}, SiteState()) is None
    assert ta.ta_rsi([], {}, SiteState()) is None


def test_missing_bar_context_returns_na():
    """需要隐式行情却拿不到时返回 na。"""
    assert ta.ta_atr([], {}, SiteState()) is None
    assert ta.ta_willr([], {}, SiteState()) is None


# ---------------------------------------------------------------------------
# 12. 窗口跳过 na
# ---------------------------------------------------------------------------


def test_sma_skips_na_and_looks_further_back():
    """窗口遇 na 跳过并继续往前取，凑满 length 个有效值。"""
    columns = {"close": [1.0, 2.0, None, 4.0, 5.0]}
    outputs = _feed(ta.ta_sma, columns, args=["close", 3])
    assert outputs[0] is None
    assert outputs[1] is None  # 只有 2 个有效值
    assert outputs[2] is None  # 仍只有 2 个有效值
    assert outputs[3] == pytest.approx((1.0 + 2.0 + 4.0) / 3.0)
    assert outputs[4] == pytest.approx((2.0 + 4.0 + 5.0) / 3.0)


def test_bb_and_cci_skip_na():
    """bb / cci 同样跳过窗口内的 na。"""
    columns = {"close": [1.0, 2.0, None, 4.0]}
    bands = _feed(ta.ta_bb, columns, args=["close", 3, 2.0])
    assert bands[2] == (None, None, None)
    middle, upper, lower = bands[3]
    assert middle == pytest.approx((1.0 + 2.0 + 4.0) / 3.0)
    assert upper > middle > lower

    cci = _feed(ta.ta_cci, columns, args=["close", 3])
    assert cci[2] is None
    window = [1.0, 2.0, 4.0]
    mean = sum(window) / 3
    deviation = sum(abs(value - mean) for value in window) / 3
    assert cci[3] == pytest.approx((4.0 - mean) / (0.015 * deviation))


# ---------------------------------------------------------------------------
# 13. 其余指标的数值性质
# ---------------------------------------------------------------------------


def test_stoch_and_willr():
    """stoch 落在 [0,100]，willr 落在 [-100,0]。"""
    columns = {
        "high": [10.0, 11.0, 12.0, 13.0],
        "low": [8.0, 9.0, 10.0, 11.0],
        "close": [9.0, 10.0, 11.0, 12.0],
    }
    stoch = _feed(ta.ta_stoch, columns, args=["close", "high", "low", 3])
    willr = _feed(ta.ta_willr, columns, args=[3])
    assert stoch[2] == pytest.approx(75.0)  # 100*(11-8)/(12-8)
    assert stoch[3] == pytest.approx(75.0)
    assert willr[2] == pytest.approx(-25.0)  # -100*(12-11)/(12-8)
    for value in stoch[2:]:
        assert 0.0 <= value <= 100.0
    for value in willr[2:]:
        assert -100.0 <= value <= 0.0


def test_mfi_cmo_percentrank_ranges():
    """mfi/cmo/percentrank 的取值范围与手算值。"""
    mfi_columns = {"close": [10.0, 11.0, 10.0], "volume": [100.0, 100.0, 100.0]}
    mfi = _feed(ta.ta_mfi, mfi_columns, args=["close", 2])
    assert mfi[1] is None
    assert mfi[2] == pytest.approx(100.0 - 100.0 / (1.0 + 1100.0 / 1000.0))

    cmo = _feed(ta.ta_cmo, {"close": [10.0, 11.0, 10.0, 12.0]}, args=["close", 2])
    assert cmo[2] == pytest.approx(0.0)
    assert cmo[3] == pytest.approx(100.0 * (2.0 - 1.0) / 3.0)

    percentrank = _feed(ta.ta_percentrank, {"close": [1.0, 2.0, 3.0, 4.0]}, args=["close", 4])
    assert percentrank[3] == pytest.approx(100.0)


def test_median_mode_range():
    """median 偶数取均值，mode 并列取最小，range=max-min。"""
    median = _feed(ta.ta_median, {"close": [1.0, 2.0, 3.0, 4.0]}, args=["close", 4])
    assert median[3] == pytest.approx(2.5)

    mode = _feed(ta.ta_mode, {"close": [1.0, 1.0, 2.0, 2.0]}, args=["close", 4])
    assert mode[3] == pytest.approx(1.0)

    spread = _feed(ta.ta_range, {"close": [1.0, 5.0, 3.0]}, args=["close", 3])
    assert spread[2] == pytest.approx(4.0)


def test_correlation_and_covariance():
    """完全线性相关为 1；样本协方差除以 n-1。"""
    columns = {"a": [1.0, 2.0, 3.0, 4.0], "b": [2.0, 4.0, 6.0, 8.0]}
    correlation = _feed(ta.ta_correlation, columns, args=["a", "b", 4])
    covariance = _feed(ta.ta_covariance, columns, args=["a", "b", 4])
    assert correlation[3] == pytest.approx(1.0)
    assert covariance[3] == pytest.approx(10.0 / 3.0)


def test_linreg_on_perfect_line():
    """完美直线的回归拟合值等于原值，offset 往前移动一格。"""
    columns = {"close": [1.0, 2.0, 3.0, 4.0, 5.0]}
    current = _feed(ta.ta_linreg, columns, args=["close", 3, 0])
    previous = _feed(ta.ta_linreg, columns, args=["close", 3, 1])
    assert current[4] == pytest.approx(5.0)
    assert previous[4] == pytest.approx(4.0)


def test_donchian_three_values():
    """donchian 返回 (middle, upper, lower)，比 TV 多中轨。"""
    columns = {"high": [10.0, 11.0, 12.0], "low": [8.0, 9.0, 10.0]}
    outputs = _feed(ta.ta_donchian, columns, args=[2])
    assert outputs[0] == (None, None, None)
    assert outputs[1] == pytest.approx((9.5, 11.0, 8.0))
    assert outputs[2] == pytest.approx((10.5, 12.0, 9.0))


def test_supertrend_direction_and_band():
    """supertrend 返回 (band, direction)，direction ∈ {1, -1}。"""
    columns = {
        "high": [10.0 + index * 0.2 for index in range(20)],
        "low": [9.0 + index * 0.2 for index in range(20)],
        "close": [9.5 + index * 0.2 for index in range(20)],
    }
    outputs = _feed(ta.ta_supertrend, columns, args=[2.0, 5])
    values = [value for value, _ in outputs if value is not None]
    directions = [direction for _, direction in outputs if direction is not None]
    assert values
    assert set(directions) <= {1, -1}
    assert all(direction == 1 for direction in directions[-5:])


def test_dmi_and_adx_ranges():
    """dmi 返回三元组，各分量在 [0, 100]。"""
    columns = _sample_columns(40)
    dmi = _feed(ta.ta_dmi, columns, args=[7, 7])
    adx = _feed(ta.ta_adx, columns, args=[7])
    assert all(len(item) == 3 for item in dmi)
    for plus, minus, value in dmi:
        for component in (plus, minus, value):
            assert component is None or 0.0 <= component <= 100.0
    assert any(value is not None for value in adx)
    for value in adx:
        assert value is None or 0.0 <= value <= 100.0


def test_sar_produces_values_after_first_bar():
    """SAR 首根为 na，之后给出有限值。"""
    columns = _sample_columns(20)
    outputs = _feed(ta.ta_sar, columns)
    assert outputs[0] is None
    assert all(value is not None and math.isfinite(value) for value in outputs[1:])


def test_volume_indicators():
    """obv/accdist/wvad/vwap/vwma 的手算值。"""
    obv = _feed(
        ta.ta_obv,
        {"close": [10.0, 11.0, 10.0, 11.0], "volume": [100.0, 200.0, 300.0, 400.0]},
    )
    assert obv == pytest.approx([100.0, 300.0, 0.0, 400.0])

    accdist_columns = {
        "high": [10.0, 10.0],
        "low": [8.0, 8.0],
        "close": [9.0, 9.5],
        "volume": [1000.0, 1000.0],
    }
    accdist = _feed(ta.ta_accdist, accdist_columns)
    assert accdist[0] == pytest.approx(0.0)
    assert accdist[1] == pytest.approx(500.0)

    wvad_columns = {
        "open": [9.0],
        "high": [10.0],
        "low": [8.0],
        "close": [9.5],
        "volume": [1000.0],
    }
    wvad = _feed(ta.ta_wvad, wvad_columns)
    assert wvad[0] == pytest.approx(250.0)

    vwap = _feed(ta.ta_vwap, {"close": [10.0, 20.0], "volume": [1.0, 3.0]})
    assert vwap[0] == pytest.approx(10.0)
    assert vwap[1] == pytest.approx(17.5)

    vwma = _feed(ta.ta_vwma, {"close": [10.0, 20.0], "volume": [1.0, 3.0]}, args=["close", 2])
    assert vwma[0] is None
    assert vwma[1] == pytest.approx(17.5)


def test_swma_weights():
    """SWMA 权重 1,2,2,1（最老到最新）。"""
    outputs = _feed(ta.ta_swma, {"close": [1.0, 2.0, 3.0, 4.0]}, args=["close"])
    assert outputs[:3] == [None, None, None]
    assert outputs[3] == pytest.approx((1.0 + 2.0 * 2.0 + 2.0 * 3.0 + 4.0) / 6.0)


def test_dema_tema_kama_on_constant():
    """dema/tema/kama 在常数序列上收敛到该常数。"""
    columns = {"close": [7.0] * 20}
    for function in (ta.ta_dema, ta.ta_tema):
        outputs = _feed(function, columns, args=["close", 4])
        assert outputs[-1] == pytest.approx(7.0)
    kama = _feed(ta.ta_kama, columns, args=["close", 4])
    assert kama[-1] == pytest.approx(7.0)


def test_cum_barssince_valuewhen():
    """cum 累计 na 记 0；barssince 从未成立为 na；valuewhen 取第 n 次出现。"""
    cum = _feed(ta.ta_cum, {"close": [1.0, None, 2.0]}, args=["close"])
    assert cum == pytest.approx([1.0, 1.0, 3.0])

    never = _feed(ta.ta_barssince, {"cond": [False, False, False]}, args=["cond"])
    assert never == [None, None, None]
    since = _feed(ta.ta_barssince, {"cond": [False, True, False, False, True]}, args=["cond"])
    assert since == [None, 0, 1, 2, 0]

    valuewhen = _feed(
        ta.ta_valuewhen,
        {"cond": [True, False, True, False], "close": [10.0, 11.0, 12.0, 13.0]},
        args=["cond", "close", 0],
    )
    assert valuewhen == pytest.approx([10.0, 10.0, 12.0, 12.0])
    older = _feed(
        ta.ta_valuewhen,
        {"cond": [True, False, True, False], "close": [10.0, 11.0, 12.0, 13.0]},
        args=["cond", "close", 1],
    )
    assert older == pytest.approx([None, None, 10.0, 10.0])


def test_change_rising_falling():
    """change/rising/falling 的逐根判定。"""
    change = _feed(ta.ta_change, {"close": [1.0, 3.0, 6.0]}, args=["close", 1])
    assert change == pytest.approx([None, 2.0, 3.0])

    rising = _feed(ta.ta_rising, {"close": [1.0, 2.0, 3.0, 3.0]}, args=["close", 2])
    assert rising == [False, False, True, False]
    falling = _feed(ta.ta_falling, {"close": [3.0, 2.0, 1.0]}, args=["close", 2])
    assert falling == [False, False, True]


# ---------------------------------------------------------------------------
# 14. 注册表 / 全量冒烟
# ---------------------------------------------------------------------------

# 每个指标的位置实参：字符串=行情列名，其它=裸值
_SMOKE_ARGS = {
    "ta.sma": ("close", 5),
    "ta.ema": ("close", 5),
    "ta.rma": ("close", 5),
    "ta.wma": ("close", 5),
    "ta.vwma": ("close", 5),
    "ta.hma": ("close", 9),
    "ta.swma": ("close",),
    "ta.dema": ("close", 5),
    "ta.tema": ("close", 5),
    "ta.alma": ("close", 5, 0.85, 6),
    "ta.kama": ("close", 5),
    "ta.vwap": ("close",),
    "ta.bb": ("close", 5, 2.0),
    "ta.bbw": ("close", 5, 2.0),
    "ta.kc": ("close", 5, 1.5, True),
    "ta.donchian": (5,),
    "ta.tr": (),
    "ta.atr": (5,),
    "ta.stdev": ("close", 5),
    "ta.variance": ("close", 5),
    "ta.dev": ("close", 5),
    "ta.rsi": ("close", 5),
    "ta.stoch": ("close", "high", "low", 5),
    "ta.cci": ("close", 5),
    "ta.mfi": ("close", 5),
    "ta.willr": (5,),
    "ta.mom": ("close", 3),
    "ta.roc": ("close", 3),
    "ta.cmo": ("close", 5),
    "ta.cog": ("close", 5),
    "ta.fisher": ("close", 5),
    "ta.tsi": ("close", 3, 5),
    "ta.macd": ("close", 3, 5, 3),
    "ta.adx": (5,),
    "ta.dmi": (5, 5),
    "ta.sar": (),
    "ta.supertrend": (2.0, 5),
    "ta.linreg": ("close", 5, 0),
    "ta.crossover": ("close", "open"),
    "ta.crossunder": ("close", "open"),
    "ta.cross": ("close", "open"),
    "ta.change": ("close", 1),
    "ta.rising": ("close", 2),
    "ta.falling": ("close", 2),
    "ta.highest": ("high", 5),
    "ta.lowest": ("low", 5),
    "ta.highestbars": ("high", 5),
    "ta.lowestbars": ("low", 5),
    "ta.cum": ("volume",),
    "ta.barssince": ("cond",),
    "ta.valuewhen": ("cond", "close", 0),
    "ta.pivothigh": ("high", 1, 1),
    "ta.pivotlow": ("low", 1, 1),
    "ta.correlation": ("close", "open", 5),
    "ta.covariance": ("close", "open", 5),
    "ta.median": ("close", 5),
    "ta.mode": ("close", 5),
    "ta.percentrank": ("close", 5),
    "ta.range": ("close", 5),
    "ta.obv": (),
    "ta.wvad": (),
    "ta.accdist": (),
}

_TUPLE_ARITY = {
    "ta.bb": 3,
    "ta.kc": 3,
    "ta.donchian": 3,
    "ta.macd": 3,
    "ta.dmi": 3,
    "ta.supertrend": 2,
}


def test_registry_complete():
    """注册表恰好 62 个指标，别名齐全，入口 ta.py 透传同一份注册表。"""
    assert len(ta.TA) == 62
    assert set(ta.TA) == set(_SMOKE_ARGS)
    assert all(name.startswith("ta.") for name in ta.TA)
    for short, full in ta.TA_ALIASES.items():
        assert full in ta.TA
    for name in ta.TA:
        short = name.split(".", 1)[1]
        assert ta.TA_ALIASES.get(short) == name
    # ta.py 是入口适配层，导出的就是同一份注册表
    assert ta_entry.TA["ta.sma"] is ta.TA["ta.sma"]
    assert len(ta_entry.TA) == 62


def test_all_indicators_run():
    """62 个指标在合成行情上逐 bar 调用不报错，返回值类型符合约定。"""
    columns = _sample_columns(40)
    count = len(columns["close"])
    for name, specs in _SMOKE_ARGS.items():
        outputs = _feed(ta.TA[name], columns, args=specs)
        assert len(outputs) == count, name
        arity = _TUPLE_ARITY.get(name)
        for value in outputs:
            if arity is not None:
                assert isinstance(value, tuple) and len(value) == arity, name
                continue
            assert value is None or isinstance(value, (int, float, bool)), name


def test_three_argument_convention():
    """指标统一是 (args, kwargs, site) 三参数形式。"""
    assert ta.TA["ta.sma"] is ta.ta_sma
    outputs = _feed(ta.ta_sma, {"close": [1.0, 2.0, 3.0]}, args=["close", 2])
    assert outputs == [None, pytest.approx(1.5), pytest.approx(2.5)]


def test_every_indicator_has_chinese_docstring():
    """62 个指标都有中文 docstring（且注明与 TV 的差异）。"""
    for name, function in ta.TA.items():
        doc = (function.__doc__ or "").strip()
        assert doc, f"{name} 缺少 docstring"
        assert any("\u4e00" <= char <= "\u9fff" for char in doc), f"{name} 的 docstring 不是中文"


def test_kwargs_names_follow_tideview_signatures():
    """具名实参按观潮文档的参数名（fastLength/leftBars/shortLength 等）也能命中。"""
    columns = _sample_columns(40)
    cases = [
        (ta.ta_macd, ("close", 3, 6, 4), {"source": "close", "fastLength": 3, "slowLength": 6, "signalSmoothing": 4}),
        (ta.ta_pivothigh, ("high", 2, 2), {"source": "high", "leftBars": 2, "rightBars": 2}),
        (ta.ta_pivotlow, ("low", 2, 2), {"source": "low", "leftBars": 2, "rightBars": 2}),
        (ta.ta_tsi, ("close", 3, 5), {"source": "close", "shortLength": 3, "longLength": 5}),
        (ta.ta_mfi, ("close", 5), {"series": "close", "length": 5}),
        (ta.ta_adx, (5, 5), {"length": 5, "adxSmoothing": 5}),
        (ta.ta_dmi, (5, 5), {"diLength": 5, "adxSmoothing": 5}),
        (ta.ta_supertrend, (2.0, 5), {"factor": 2.0, "atrPeriod": 5}),
        (ta.ta_kc, ("close", 5, 1.5, True), {"source": "close", "length": 5, "mult": 1.5, "useTrueRange": True}),
        (ta.ta_stoch, ("close", "high", "low", 5), {"close": "close", "high": "high", "low": "low", "length": 5}),
        (ta.ta_roc, ("close", 1), {"source": "close", "length": 1}),
        (ta.ta_mom, ("close", 10), {"source": "close", "length": 10}),
    ]
    for function, positional_args, named_args in cases:
        positional = _feed(function, columns, args=list(positional_args))
        named = _feed(function, columns, kwargs=named_args)
        _assert_outputs_equal(positional, named)


def _assert_outputs_equal(left, right):
    assert len(left) == len(right)
    for first, second in zip(left, right):
        if isinstance(first, tuple) or isinstance(second, tuple):
            assert isinstance(first, tuple) and isinstance(second, tuple)
            _assert_outputs_equal(list(first), list(second))
            continue
        if first is None or second is None:
            assert first is None and second is None
            continue
        assert first == pytest.approx(second)
