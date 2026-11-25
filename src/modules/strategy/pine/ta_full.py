"""Pine ``ta.*`` 完整指标库（观潮/TideView 口径，逐 bar 重放）。

本模块是 ``ta.py`` 的完整实现：它先继承 :mod:`ta_core` 的核心集，再补齐其余
指标，并修正核心集里「窗口跳过 na 后继续往前凑满 length」不彻底、``vwma`` 拿不到
成交量等细节。同名键会覆盖 ``ta_core`` 的版本，未覆盖的仍用核心集实现。

调用约定
--------
每个指标都是::

    def ta_xxx(args, kwargs, site) -> float | bool | tuple | None

- ``args``：位置实参的 :class:`ArgView` 列表；
- ``kwargs``：具名实参的 ``dict[str, ArgView]``；
- ``site``：该调用点的 :class:`SiteState`，跨 bar 持久。递归指标的状态一律写在
  ``site.values``；隐式行情（open/high/low/close/volume）由引擎注入 ``site.env``，
  值是 ArgView，可以 ``[n]`` 回看。

数值口径
--------
- na 用 ``None``；算术前统一 :func:`to_number`。
- **窗口类指标**（sma/wma/vwma/stdev/variance/dev/bb/bbw/cci/highest/lowest/
  median/mode/percentrank/range/cog/fisher）遇到窗口内 na 时跳过该值继续往前取，
  凑满 ``length`` 个有效值；凑不满才返回 na（观潮口径，TV 直接返回 na）。
- **ema/rma 家族**：当前 source 为 na 时输出保持上一根，不重新播种。
- MACD 柱体 = ``2 * (macd - signal)``（观潮/通达信口径，与 TV 不同）。

继承自 :mod:`ta_core` 的指标（ema/rma/swma/hma/dema/tema/vwap/tr/atr/donchian/
kc/rsi/macd/stoch/willr）与 TV 的已知差异：ema/rma 在 source 为 na 时保持上一根
（TV 会传染 na）；rsi 在 up==down==0 时返回 50 而不是 na；macd 柱体乘 2；
vwap 只按全样本累计、不做日内重置；kc 的中轨用 SMA（TV 用 EMA）。
"""

from __future__ import annotations

import math
from typing import Callable, Sequence

from .errors import PineRuntimeError
from .series import ArgView, SiteState, is_na, to_number
from .ta_core import (  # 复用核心集的取值/递归/派生序列助手
    TA as _CORE_TA,
    TA_ALIASES as _CORE_ALIASES,
    _recursive_average,
    _series_view,
    _sub_site,
    _tr_view,
    _window_any,
)
from .ta_core import (  # 未覆盖的核心实现原样透出，保持「完整库」的可导入性
    ta_atr,
    ta_dema,
    ta_donchian,
    ta_ema,
    ta_hma,
    ta_kc,
    ta_macd,
    ta_rsi,
    ta_rma,
    ta_stoch,
    ta_swma,
    ta_tema,
    ta_tr,
    ta_vwap,
    ta_willr,
)

TA: dict[str, Callable] = dict(_CORE_TA)
TA_ALIASES: dict[str, str] = dict(_CORE_ALIASES)


def _register(dotted: str, *aliases: str):
    """注册指标：完整名 + 短名（及额外别名）。"""

    def decorator(function):
        TA[dotted] = function
        for alias in (dotted.split(".", 1)[-1], *aliases):
            TA_ALIASES.setdefault(alias, dotted)
        return function

    return decorator


# ---------------------------------------------------------------------------
# 取值助手（支持同一参数的多种具名写法）
# ---------------------------------------------------------------------------


def _first_view(args, kwargs, names: Sequence[str], index: int | None = None) -> ArgView | None:
    """按顺序尝试多个具名参数，取不到再退回位置实参。"""
    for name in names:
        view = kwargs.get(name)
        if view is not None:
            return view
    if index is not None and index < len(args):
        return args[index]
    return None


def _first_number(args, kwargs, names: Sequence[str], index: int | None, default: float) -> float:
    view = _first_view(args, kwargs, names, index)
    if view is None:
        return default
    return to_number(getattr(view, "now", view), default)


def _first_length(
    args,
    kwargs,
    names: Sequence[str],
    index: int | None,
    default: int,
    label: str = "length",
    minimum: int = 1,
) -> int:
    view = _first_view(args, kwargs, names, index)
    if view is None:
        return default
    value = int(round(to_number(getattr(view, "now", view), default)))
    if value < minimum:
        raise PineRuntimeError(f"`{label}` 必须 >= {minimum}")
    return value


def _env(site: SiteState, name: str) -> ArgView | None:
    """取引擎注入的隐式行情（``site.env``，键为 open/high/low/close/volume）。"""
    return site.env.get(name) if site is not None else None


# ---------------------------------------------------------------------------
# 窗口助手（跳过 na 并继续往前凑满）
# ---------------------------------------------------------------------------


def _skip_na_window(view: ArgView, length: int) -> list[float] | None:
    """最近 ``length`` 个有效值：跳过 na 继续往前取，凑不满返回 None。"""
    available = len(view.series.values)
    picked: list[float] = []
    offset = 0
    while len(picked) < length and offset < available:
        raw = view[offset]
        if not is_na(raw):
            picked.append(to_number(raw))
        offset += 1
    if len(picked) < length:
        return None
    picked.reverse()
    return picked


def _strict_window(view: ArgView, length: int) -> list[float] | None:
    """最近 ``length`` 根相邻值；任一 na 即返回 None。"""
    if len(view.series.values) < length:
        return None
    picked: list[float] = []
    for offset in range(length - 1, -1, -1):
        raw = view[offset]
        if is_na(raw):
            return None
        picked.append(to_number(raw))
    return picked


def _pair_window(first: ArgView, second: ArgView, length: int) -> list[tuple[float, float]] | None:
    """最近 ``length`` 对同时有效的值（跳过任一为 na 的 bar）。"""
    bound = min(len(first.series.values), len(second.series.values))
    picked: list[tuple[float, float]] = []
    offset = 0
    while len(picked) < length and offset < bound:
        left = first[offset]
        right = second[offset]
        if not is_na(left) and not is_na(right):
            picked.append((to_number(left), to_number(right)))
        offset += 1
    if len(picked) < length:
        return None
    picked.reverse()
    return picked


# ---------------------------------------------------------------------------
# 数值助手
# ---------------------------------------------------------------------------


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _population_stdev(values: Sequence[float]) -> float:
    mean = _mean(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def _alma(values: Sequence[float], offset: float, sigma: float) -> float:
    """ALMA 高斯权重：``exp(-((i - m)^2) / (2 * s^2))``，m=floor(offset*(n-1))。"""
    count = len(values)
    peak = math.floor(offset * (count - 1))
    spread = count / sigma
    weights = [math.exp(-((position - peak) ** 2) / (2.0 * spread * spread)) for position in range(count)]
    total = sum(weights)
    return sum(weight * value for weight, value in zip(weights, values)) / total


# ---------------------------------------------------------------------------
# 均线的窗口修正（覆盖 ta_core）
# ---------------------------------------------------------------------------


@_register("ta.sma", "sma")
def ta_sma(args, kwargs, site: SiteState):
    """简单移动平均：最近 ``length`` 个**有效值**的均值。

    跳过窗口内的 na 并继续往前取，凑满 length 个才计算（观潮口径；TV 遇到
    窗口内 na 直接返回 na）。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 14, "length")
    if view is None:
        return None
    values = _skip_na_window(view, length)
    return None if values is None else sum(values) / length


@_register("ta.wma", "wma")
def ta_wma(args, kwargs, site: SiteState):
    """加权移动平均：最老一根权重 1，最新一根权重 length。

    与 TV 差异：窗口内 na 会被跳过（TV 返回 na）。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 14, "length")
    if view is None:
        return None
    values = _skip_na_window(view, length)
    if values is None:
        return None
    total_weight = length * (length + 1) / 2.0
    return sum((position + 1) * value for position, value in enumerate(values)) / total_weight


@_register("ta.vwma", "vwma")
def ta_vwma(args, kwargs, site: SiteState):
    """成交量加权均线：``sma(source*volume, length) / sma(volume, length)``。

    volume 取自 ``site.env["volume"]``（也接受显式 ``volume`` 实参）。与 TV 差异：
    分子分母共用同一批成对有效值（source 或 volume 为 na 的 bar 跳过）。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 20, "length")
    volume = _first_view(args, kwargs, ("volume",), None) or _env(site, "volume")
    if view is None or volume is None:
        return None
    pairs = _pair_window(view, volume, length)
    if pairs is None:
        return None
    total_volume = sum(size for _, size in pairs)
    if total_volume == 0:
        return None
    return sum(price * size for price, size in pairs) / total_volume


@_register("ta.stdev", "stdev")
def ta_stdev(args, kwargs, site: SiteState):
    """总体标准差（除以 n），窗口跳过 na 继续往前取。

    与 TV 差异：TV 在窗口含 na 时返回 na，本引擎跳过 na 继续凑满 length 个有效值。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 20, "length")
    if view is None:
        return None
    values = _skip_na_window(view, length)
    return None if values is None else _population_stdev(values)


@_register("ta.variance", "variance")
def ta_variance(args, kwargs, site: SiteState):
    """总体方差（除以 n），窗口跳过 na。

    与 TV 差异：TV 在窗口含 na 时返回 na，本引擎跳过 na 继续凑满窗口。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 20, "length")
    if view is None:
        return None
    values = _skip_na_window(view, length)
    if values is None:
        return None
    mean = _mean(values)
    return sum((value - mean) ** 2 for value in values) / length


@_register("ta.dev", "dev")
def ta_dev(args, kwargs, site: SiteState):
    """平均绝对偏差 ``mean(|x - mean|)``，窗口跳过 na。

    与 TV 差异：TV 在窗口含 na 时返回 na，本引擎跳过 na 继续凑满窗口。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 20, "length")
    if view is None:
        return None
    values = _skip_na_window(view, length)
    if values is None:
        return None
    mean = _mean(values)
    return sum(abs(value - mean) for value in values) / length


@_register("ta.bb", "bb", "boll", "ta.bbands")
def ta_bb(args, kwargs, site: SiteState):
    """布林带 ``(middle, upper, lower)``：中轨 SMA，带宽用总体标准差。

    ``mult`` 默认 2.0。与 TV 差异：窗口内 na 会被跳过（TV 返回 na）。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 20, "length")
    mult = _first_number(args, kwargs, ("mult",), 2, 2.0)
    if view is None:
        return None, None, None
    values = _skip_na_window(view, length)
    if values is None:
        return None, None, None
    middle = _mean(values)
    deviation = _population_stdev(values) * mult
    return middle, middle + deviation, middle - deviation


@_register("ta.bbw", "bbw")
def ta_bbw(args, kwargs, site: SiteState):
    """布林带宽度 ``(upper - lower) / middle``；middle 为 0 返回 na。

    与 TV 差异：窗口内 na 会被跳过（TV 返回 na）。
    """
    middle, upper, lower = ta_bb(args, kwargs, site)
    if middle is None or upper is None or middle == 0:
        return None
    return (upper - lower) / middle


@_register("ta.cci", "cci")
def ta_cci(args, kwargs, site: SiteState):
    """顺势指标 ``(src - sma) / (0.015 * dev)``；dev 为 0 返回 na。

    与 TV 差异：窗口内 na 会被跳过（TV 返回 na）。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 20, "length")
    if view is None or is_na(view.now):
        return None
    values = _skip_na_window(view, length)
    if values is None:
        return None
    mean = _mean(values)
    deviation = sum(abs(value - mean) for value in values) / length
    if deviation == 0:
        return None
    return (to_number(view.now) - mean) / (0.015 * deviation)


@_register("ta.highest", "highest")
def ta_highest(args, kwargs, site: SiteState):
    """窗口最大值，跳过 na 继续往前取；凑不满 length 个有效值返回 na。

    与 TV 差异：TV 在窗口含 na 时返回 na，本引擎跳过 na 继续凑满窗口。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 14, "length")
    if view is None:
        return None
    values = _skip_na_window(view, length)
    return None if values is None else max(values)


@_register("ta.lowest", "lowest")
def ta_lowest(args, kwargs, site: SiteState):
    """窗口最小值，跳过 na 继续往前取；凑不满 length 个有效值返回 na。

    与 TV 差异：TV 在窗口含 na 时返回 na，本引擎跳过 na 继续凑满窗口。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 14, "length")
    if view is None:
        return None
    values = _skip_na_window(view, length)
    return None if values is None else min(values)


# ---------------------------------------------------------------------------
# 均线（新增）
# ---------------------------------------------------------------------------


@_register("ta.alma", "alma")
def ta_alma(args, kwargs, site: SiteState):
    """ALMA（Arnaud Legoux 均线）：高斯权重，``offset`` 控制重心、``sigma`` 控制宽度。

    与 TV 差异：窗口位置敏感，窗口内出现 na 直接返回 na（TV 一致）。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 9, "length")
    offset = _first_number(args, kwargs, ("offset",), 2, 0.85)
    sigma = _first_number(args, kwargs, ("sigma",), 3, 6.0)
    if sigma <= 0:
        raise PineRuntimeError("`sigma` 必须大于 0")
    if view is None:
        return None
    values = _window_any(view, length)
    return None if values is None else _alma(values, offset, sigma)


@_register("ta.kama", "kama")
def ta_kama(args, kwargs, site: SiteState):
    """Kaufman 自适应均线：ER=|change|/volatility，``sc=(er*(fast-slow)+slow)^2``。

    其中 fast=2/3、slow=2/31。与 TV 差异：第一个可计算值直接取当根 source
    （TV 的 ``na(result[1]) ? src`` 行为），source 为 na 时保持上一根。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 10, "length")
    if view is None:
        return None
    previous = site.values.get("kama")
    current = view.now
    if is_na(current) or len(view.series.values) < length + 1:
        return previous
    past = view[length]
    if is_na(past):
        return previous
    volatility = 0.0
    for offset in range(length):
        newer = view[offset]
        older = view[offset + 1]
        if is_na(newer) or is_na(older):
            return previous
        volatility += abs(to_number(newer) - to_number(older))
    change = abs(to_number(current) - to_number(past))
    efficiency = 0.0 if volatility == 0 else change / volatility
    fastest = 2.0 / 3.0
    slowest = 2.0 / 31.0
    smoothing = (efficiency * (fastest - slowest) + slowest) ** 2
    if previous is None:
        site.values["kama"] = to_number(current)
        return site.values["kama"]
    value = previous + smoothing * (to_number(current) - previous)
    site.values["kama"] = value
    return value


# ---------------------------------------------------------------------------
# 动量（新增）
# ---------------------------------------------------------------------------


@_register("ta.mfi", "mfi")
def ta_mfi(args, kwargs, site: SiteState):
    """资金流量指标：正/负资金流按 ``prev <= cur`` 分流，``100-100/(1+ratio)``。

    资金流 = ``source * volume``，volume 取自 ``site.env``。与 TV 差异：相等变化
    同时计入正负两侧；两侧都为 0 返回 na。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 14, "length")
    volume = _first_view(args, kwargs, ("volume",), None) or _env(site, "volume")
    if view is None or volume is None:
        return None
    bound = min(len(view.series.values), len(volume.series.values))

    def flows(offset: int) -> tuple[float, float] | None:
        current = view[offset]
        previous = view[offset + 1]
        size = volume[offset]
        if is_na(current) or is_na(previous) or is_na(size):
            return None
        money = to_number(size) * to_number(current)
        delta = to_number(current) - to_number(previous)
        return (money if delta >= 0 else 0.0, money if delta <= 0 else 0.0)

    positive: list[float] = []
    negative: list[float] = []
    offset = 0
    while (len(positive) < length or len(negative) < length) and offset < bound:
        pair = flows(offset)
        if pair is not None:
            positive.append(pair[0])
            negative.append(pair[1])
        offset += 1
    if len(positive) < length or len(negative) < length:
        return None
    upper = sum(positive[-length:])
    lower = sum(negative[-length:])
    if lower == 0:
        return None if upper == 0 else 100.0
    return 100.0 - 100.0 / (1.0 + upper / lower)


@_register("ta.cog", "cog")
def ta_cog(args, kwargs, site: SiteState):
    """重心指标：``-sum(src[k]*(k+1)) / sum(src[k])``，k=0 为当前 bar。

    与 TV 差异：TV 的 ``ta.cog`` 权重方向相同（越老的 bar 权重越大），但窗口内
    na 的处理不同；本实现在窗口内跳过 na。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 10, "length")
    if view is None:
        return None
    values = _skip_na_window(view, length)
    if values is None:
        return None
    total = sum(values)
    if total == 0:
        return None
    weighted = sum((position + 1) * value for position, value in enumerate(reversed(values)))
    return -weighted / total


@_register("ta.fisher", "fisher")
def ta_fisher(args, kwargs, site: SiteState):
    """Ehlers Fisher 变换（单值）：``fisher = 0.5*ln((1+v)/(1-v)) + 0.5*fisher[1]``。

    与 TV 差异：只返回 Fisher 线，触发线是它的上一根值（``fisher[1]``）；TV v6
    的 ``ta.fisher`` 直接返回 ``[fisher, trigger]`` 二元组。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 9, "length")
    if view is None or is_na(view.now):
        return None
    values = _skip_na_window(view, length)
    if values is None:
        return None
    highest = max(values)
    lowest = min(values)
    span = highest - lowest
    normalized = 0.5 if span == 0 else (to_number(view.now) - lowest) / span
    previous_value = float(site.values.get("fisher_value", 0.0))
    previous_line = float(site.values.get("fisher_line", 0.0))
    value = 0.66 * (normalized - 0.5) + 0.67 * previous_value
    value = max(-0.999, min(0.999, value))
    line = 0.5 * math.log((1.0 + value) / (1.0 - value)) + 0.5 * previous_line
    site.values["fisher_value"] = value
    site.values["fisher_line"] = line
    return line


@_register("ta.tsi", "tsi")
def ta_tsi(args, kwargs, site: SiteState):
    """真实强弱指标：``100*ema(ema(change,short),long) / ema(ema(|change|,short),long)``。

    默认 short=13、long=25（TV 双平滑口径）。与 TV 差异：分母为 0 时返回 0。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    short = _first_length(args, kwargs, ("short", "shortLength", "short_length"), 1, 13, "short")
    long = _first_length(args, kwargs, ("long", "longLength", "long_length"), 2, 25, "long")
    if view is None:
        return None
    current = view.now
    previous = view[1]
    change = None if is_na(current) or is_na(previous) else to_number(current) - to_number(previous)
    magnitude = None if change is None else abs(change)
    smoothed = _recursive_average(
        _series_view(site, "tsi_change", change), short, _sub_site(site, "tsi_short"), 2.0 / (short + 1)
    )
    numerator = _recursive_average(
        _series_view(site, "tsi_smoothed", smoothed), long, _sub_site(site, "tsi_long"), 2.0 / (long + 1)
    )
    absolute = _recursive_average(
        _series_view(site, "tsi_abs", magnitude), short, _sub_site(site, "tsi_abs_short"), 2.0 / (short + 1)
    )
    denominator = _recursive_average(
        _series_view(site, "tsi_abs_smoothed", absolute), long, _sub_site(site, "tsi_abs_long"), 2.0 / (long + 1)
    )
    if numerator is None or denominator is None:
        return None
    if denominator == 0:
        return 0.0
    return 100.0 * numerator / denominator


# ---------------------------------------------------------------------------
# 趋势（新增）
# ---------------------------------------------------------------------------


def _dmi_values(site: SiteState, high: ArgView, low: ArgView, close: ArgView, di_length: int, smoothing: int):
    """Wilder DMI 公共实现，返回 ``(diPlus, diMinus, adx)``。"""

    def direction(offset: int, positive: bool) -> float | None:
        high_value = high[offset]
        low_value = low[offset]
        previous_high = high[offset + 1]
        previous_low = low[offset + 1]
        if is_na(high_value) or is_na(low_value) or is_na(previous_high) or is_na(previous_low):
            return None
        upward = to_number(high_value) - to_number(previous_high)
        downward = to_number(previous_low) - to_number(low_value)
        plus = upward if upward > 0 else 0.0
        minus = downward if downward > 0 else 0.0
        if plus <= minus:
            plus = 0.0
        if minus <= plus:
            minus = 0.0
        return plus if positive else minus

    alpha = 1.0 / di_length
    plus_view = _series_view(site, "dmi_plus", direction(0, True))
    minus_view = _series_view(site, "dmi_minus", direction(0, False))
    tr_view = _tr_view(site)
    if tr_view is None:
        return None, None, None
    plus_sum = _recursive_average(plus_view, di_length, _sub_site(site, "dmi_plus_rma"), alpha)
    minus_sum = _recursive_average(minus_view, di_length, _sub_site(site, "dmi_minus_rma"), alpha)
    tr_sum = _recursive_average(tr_view, di_length, _sub_site(site, "dmi_tr_rma"), alpha)
    plus_di = None if plus_sum is None or tr_sum in (None, 0) else 100.0 * plus_sum / tr_sum
    minus_di = None if minus_sum is None or tr_sum in (None, 0) else 100.0 * minus_sum / tr_sum
    dx = None
    if plus_di is not None and minus_di is not None:
        total = plus_di + minus_di
        if total != 0:
            dx = 100.0 * abs(plus_di - minus_di) / total
    dx_view = _series_view(site, "dmi_dx", dx)
    adx = _recursive_average(dx_view, smoothing, _sub_site(site, "dmi_adx_rma"), 1.0 / smoothing)
    return plus_di, minus_di, adx


def _dmi_arguments(args, kwargs):
    di_length = _first_length(args, kwargs, ("length", "diLength", "di_length"), 0, 14, "diLength")
    smoothing = _first_length(
        args, kwargs, ("adxSmoothing", "adx_smoothing", "smoothing"), 1, 14, "adxSmoothing"
    )
    return di_length, smoothing


@_register("ta.adx", "adx")
def ta_adx(args, kwargs, site: SiteState):
    """平均趋向指数：标准 Wilder DMI 的 ADX 单值。

    与 TV 差异：``adxSmoothing`` 省略时默认 14（TV 要求显式传入两个参数）。
    """
    di_length, smoothing = _dmi_arguments(args, kwargs)
    high = _env(site, "high")
    low = _env(site, "low")
    close = _env(site, "close")
    if high is None or low is None or close is None:
        return None
    return _dmi_values(site, high, low, close, di_length, smoothing)[2]


@_register("ta.dmi", "dmi")
def ta_dmi(args, kwargs, site: SiteState):
    """动向指标三元组 ``(diPlus, diMinus, adx)``：+DM/-DM/TR 各做 RMA，ADX 再平滑 DX。

    与 TV 差异：``+DM=max(high-high[1],0)`` 当且仅当它大于 ``low[1]-low`` 时保留，
    否则归零（``-DM`` 对称）；na 时保持上一根（TV 会传染 na）。
    """
    di_length, smoothing = _dmi_arguments(args, kwargs)
    high = _env(site, "high")
    low = _env(site, "low")
    close = _env(site, "close")
    if high is None or low is None or close is None:
        return None, None, None
    return _dmi_values(site, high, low, close, di_length, smoothing)


@_register("ta.sar", "sar", "psar", "ta.psar")
def ta_sar(args, kwargs, site: SiteState):
    """抛物线 SAR（Wilder）：``sar = sar[1] + af*(ep - sar[1])``，并受前低/前高压制。

    别名 ``ta.psar`` / ``psar``。与 TV 差异：首根（bar 0）返回 na，从第 2 根起给值
    （TV 也是从 ``bar_index == 1`` 起算）。
    """
    start = _first_number(args, kwargs, ("start",), 0, 0.02)
    increment = _first_number(args, kwargs, ("increment",), 1, 0.02)
    maximum = _first_number(args, kwargs, ("maximum",), 2, 0.2)
    if start <= 0 or increment <= 0 or maximum <= 0:
        raise PineRuntimeError("`start`/`increment`/`maximum` 必须大于 0")
    high = _env(site, "high")
    low = _env(site, "low")
    close = _env(site, "close")
    if high is None or low is None or close is None:
        return None
    if len(high.series.values) < 2:
        return None
    current_high, current_low, current_close = high.now, low.now, close.now
    previous_close = close[1]
    if is_na(current_high) or is_na(current_low) or is_na(current_close) or is_na(previous_close):
        return site.values.get("sar")
    if "sar" not in site.values:
        if to_number(current_close) > to_number(previous_close):
            site.values["trend"] = 1
            site.values["ep"] = to_number(current_high)
            site.values["sar"] = to_number(current_low)
        else:
            site.values["trend"] = -1
            site.values["ep"] = to_number(current_low)
            site.values["sar"] = to_number(current_high)
        site.values["af"] = start
        return site.values["sar"]

    trend = site.values["trend"]
    acceleration = site.values["af"]
    extreme = site.values["ep"]
    previous_sar = site.values["sar"]
    candidate = previous_sar + acceleration * (extreme - previous_sar)
    if trend > 0:
        for offset in (1, 2):
            value = low[offset]
            if not is_na(value):
                candidate = min(candidate, to_number(value))
        if to_number(current_low) < candidate:
            trend = -1
            candidate = extreme
            extreme = to_number(current_low)
            acceleration = start
        elif to_number(current_high) > extreme:
            extreme = to_number(current_high)
            acceleration = min(acceleration + increment, maximum)
    else:
        for offset in (1, 2):
            value = high[offset]
            if not is_na(value):
                candidate = max(candidate, to_number(value))
        if to_number(current_high) > candidate:
            trend = 1
            candidate = extreme
            extreme = to_number(current_high)
            acceleration = start
        elif to_number(current_low) < extreme:
            extreme = to_number(current_low)
            acceleration = min(acceleration + increment, maximum)
    site.values["trend"] = trend
    site.values["af"] = acceleration
    site.values["ep"] = extreme
    site.values["sar"] = candidate
    return candidate


@_register("ta.supertrend", "supertrend")
def ta_supertrend(args, kwargs, site: SiteState):
    """超级趋势：返回 ``(supertrend, direction)``，direction=1 多头 / -1 空头。

    ``hl2 ± factor*atr(atrPeriod)``，与上一根的带值比较决定是否换轨。与 TV 差异：
    TV 的 ``ta.supertrend`` 用 -1 表示上涨、1 表示下跌，本引擎按约定反过来。
    """
    factor = _first_number(args, kwargs, ("factor",), 0, 3.0)
    period = _first_length(args, kwargs, ("atrPeriod", "atr_period", "length"), 1, 10, "atrPeriod")
    high = _env(site, "high")
    low = _env(site, "low")
    close = _env(site, "close")
    if high is None or low is None or close is None:
        return None, None
    tr_view = _tr_view(site)
    if tr_view is None:
        return None, None
    atr = _recursive_average(tr_view, period, _sub_site(site, "st_atr"), 1.0 / period)
    current_high, current_low, current_close = high.now, low.now, close.now
    if atr is None or is_na(current_high) or is_na(current_low) or is_na(current_close):
        return None, site.values.get("st_direction")
    midpoint = (to_number(current_high) + to_number(current_low)) / 2.0
    upper_basic = midpoint + factor * atr
    lower_basic = midpoint - factor * atr
    previous_upper = site.values.get("st_upper")
    previous_lower = site.values.get("st_lower")
    previous_direction = site.values.get("st_direction")
    if previous_upper is None or previous_lower is None or previous_direction is None:
        upper, lower, direction = upper_basic, lower_basic, 1
    else:
        previous_close = close[1]
        if is_na(previous_close):
            upper, lower = upper_basic, lower_basic
        else:
            close_before = to_number(previous_close)
            upper = upper_basic if (upper_basic < previous_upper or close_before > previous_upper) else previous_upper
            lower = lower_basic if (lower_basic > previous_lower or close_before < previous_lower) else previous_lower
        direction = previous_direction
        close_now = to_number(current_close)
        if previous_direction == -1 and close_now > previous_upper:
            direction = 1
        elif previous_direction == 1 and close_now < previous_lower:
            direction = -1
    site.values["st_upper"] = upper
    site.values["st_lower"] = lower
    site.values["st_direction"] = direction
    return (lower if direction == 1 else upper), direction


@_register("ta.linreg", "linreg")
def ta_linreg(args, kwargs, site: SiteState):
    """线性回归拟合值：最近 ``length`` 个值最小二乘，x 轴 0..length-1（当前为 length-1）。

    ``offset`` 表示取多少根之前的拟合值（0=当前）。与 TV 差异：窗口内 na 返回 na。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 14, "length")
    offset = _first_number(args, kwargs, ("offset",), 2, 0.0)
    if view is None:
        return None
    values = _strict_window(view, length)
    if values is None:
        return None
    if length < 2:
        return values[-1] if offset == 0 else None
    count = length
    sum_x = count * (count - 1) / 2.0
    sum_xx = (count - 1) * count * (2 * count - 1) / 6.0
    sum_y = sum(values)
    sum_xy = sum(position * value for position, value in enumerate(values))
    denominator = count * sum_xx - sum_x * sum_x
    if denominator == 0:  # pragma: no cover - length>=2 时不可达
        return None
    slope = (count * sum_xy - sum_x * sum_y) / denominator
    intercept = (sum_y - slope * sum_x) / count
    target = (count - 1) - int(round(offset))
    return intercept + slope * target


# ---------------------------------------------------------------------------
# 条件取值 / 枢轴（新增）
# ---------------------------------------------------------------------------


@_register("ta.valuewhen", "valuewhen")
def ta_valuewhen(args, kwargs, site: SiteState):
    """条件为真那根的 ``source`` 值；``occurrence=0`` 表示最近一次。

    与 TV 差异：无（条件为 na 视为不成立，出现次数不够返回 na）。
    """
    condition = _first_view(args, kwargs, ("condition",), 0)
    source = _first_view(args, kwargs, ("source", "series"), 1)
    occurrence = _first_length(args, kwargs, ("occurrence",), 2, 0, "occurrence", minimum=0)
    if condition is None or source is None:
        return None
    hits = site.values.get("hits")
    if hits is None:
        hits = []
        site.values["hits"] = hits
    if not is_na(condition.now) and bool(condition.now):
        hits.append(source.now)
    target = len(hits) - 1 - occurrence
    if target < 0:
        return None
    return hits[target]


def _pivot(view: ArgView, left: int, right: int, high: bool) -> float | None:
    window = view.history(left + right + 1)
    if any(is_na(value) for value in window):
        return None
    values = [to_number(value) for value in window]
    center = left
    value = values[center]
    for position, item in enumerate(values):
        if position == center:
            continue
        if (item >= value) if high else (item <= value):
            return None
    return value


@_register("ta.pivothigh", "pivothigh")
def ta_pivothigh(args, kwargs, site: SiteState):
    """枢轴高点：当前 bar 回看 ``rightBars`` 根得到中心 bar，左右各严格更低才返回值。

    「确认 bar」口径（与 TV 一致）：值在中心 bar 之后第 ``rightBars`` 根才出现。
    与 TV 差异：无。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    left = _first_length(args, kwargs, ("leftBars", "left"), 1, 1, "leftBars", minimum=1)
    right = _first_length(args, kwargs, ("rightBars", "right"), 2, 1, "rightBars", minimum=1)
    if view is None:
        return None
    return _pivot(view, left, right, True)


@_register("ta.pivotlow", "pivotlow")
def ta_pivotlow(args, kwargs, site: SiteState):
    """枢轴低点：当前 bar 回看 ``rightBars`` 根得到中心 bar，左右各严格更高才返回值。

    「确认 bar」口径（与 TV 一致）。与 TV 差异：无。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    left = _first_length(args, kwargs, ("leftBars", "left"), 1, 1, "leftBars", minimum=1)
    right = _first_length(args, kwargs, ("rightBars", "right"), 2, 1, "rightBars", minimum=1)
    if view is None:
        return None
    return _pivot(view, left, right, False)


# ---------------------------------------------------------------------------
# 统计（新增）
# ---------------------------------------------------------------------------


@_register("ta.correlation", "correlation")
def ta_correlation(args, kwargs, site: SiteState):
    """滚动皮尔逊相关系数：``cov / (stdev_x * stdev_y)``（总体口径）。

    与 TV 差异：任一序列在窗口内出现 na 直接返回 na；标准差为 0 返回 na。
    """
    first = _first_view(args, kwargs, ("source1", "series1", "a"), 0)
    second = _first_view(args, kwargs, ("source2", "series2", "b"), 1)
    length = _first_length(args, kwargs, ("length",), 2, 20, "length")
    if first is None or second is None:
        return None
    pairs = _strict_pairs(first, second, length)
    if pairs is None:
        return None
    mean_left = _mean([pair[0] for pair in pairs])
    mean_right = _mean([pair[1] for pair in pairs])
    covariance = sum((left - mean_left) * (right - mean_right) for left, right in pairs) / length
    deviation_left = math.sqrt(sum((left - mean_left) ** 2 for left, _ in pairs) / length)
    deviation_right = math.sqrt(sum((right - mean_right) ** 2 for _, right in pairs) / length)
    if deviation_left == 0 or deviation_right == 0:
        return None
    return covariance / (deviation_left * deviation_right)


def _strict_pairs(first: ArgView, second: ArgView, length: int) -> list[tuple[float, float]] | None:
    """最近 ``length`` 对相邻值（位置一一对应），任一 na 即 None。"""
    if len(first.series.values) < length or len(second.series.values) < length:
        return None
    picked: list[tuple[float, float]] = []
    for offset in range(length - 1, -1, -1):
        left = first[offset]
        right = second[offset]
        if is_na(left) or is_na(right):
            return None
        picked.append((to_number(left), to_number(right)))
    return picked


@_register("ta.covariance", "covariance")
def ta_covariance(args, kwargs, site: SiteState):
    """样本协方差：``sum((x-mean_x)*(y-mean_y))/(n-1)``。

    与 TV 差异：窗口内任一序列出现 na 即返回 na；n<2 返回 na。
    """
    first = _first_view(args, kwargs, ("source1", "series1", "a"), 0)
    second = _first_view(args, kwargs, ("source2", "series2", "b"), 1)
    length = _first_length(args, kwargs, ("length",), 2, 20, "length")
    if first is None or second is None or length < 2:
        return None
    pairs = _strict_pairs(first, second, length)
    if pairs is None:
        return None
    mean_left = _mean([pair[0] for pair in pairs])
    mean_right = _mean([pair[1] for pair in pairs])
    total = sum((left - mean_left) * (right - mean_right) for left, right in pairs)
    return total / (length - 1)


@_register("ta.median", "median")
def ta_median(args, kwargs, site: SiteState):
    """窗口中位数：奇数取中间，偶数取中间两个的均值。

    与 TV 差异：TV 的 ``ta.median`` 偶数窗口取中间**较小**那个值，这里取均值；
    窗口内 na 会被跳过（TV 返回 na）。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 14, "length")
    if view is None:
        return None
    values = _skip_na_window(view, length)
    if values is None:
        return None
    ordered = sorted(values)
    middle = length // 2
    if length % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


@_register("ta.mode", "mode")
def ta_mode(args, kwargs, site: SiteState):
    """窗口众数；并列时取最小值（与 Pine ``array.mode`` 一致）。

    与 TV 差异：窗口内 na 会被跳过。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 14, "length")
    if view is None:
        return None
    values = _skip_na_window(view, length)
    if values is None:
        return None
    counts: dict[float, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    top = max(counts.values())
    return min(value for value, count in counts.items() if count == top)


@_register("ta.percentrank", "percentrank")
def ta_percentrank(args, kwargs, site: SiteState):
    """百分位排名：``(窗口中小于当前值的个数) / (length-1) * 100``。

    与 TV 差异：``length==1`` 返回 0；窗口内 na 会被跳过。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 14, "length")
    if view is None or is_na(view.now):
        return None
    values = _skip_na_window(view, length)
    if values is None or length <= 1:
        return 0.0
    current = to_number(view.now)
    below = sum(1 for value in values if value < current)
    return 100.0 * below / (length - 1)


@_register("ta.range", "range")
def ta_range(args, kwargs, site: SiteState):
    """窗口极差 ``max - min``，窗口跳过 na。

    与 TV 差异：TV 在窗口含 na 时返回 na，本引擎跳过 na 继续凑满窗口。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 14, "length")
    if view is None:
        return None
    values = _skip_na_window(view, length)
    if values is None:
        return None
    return max(values) - min(values)


# ---------------------------------------------------------------------------
# 量能（新增）
# ---------------------------------------------------------------------------


@_register("ta.obv", "obv")
def ta_obv(args, kwargs, site: SiteState):
    """能量潮：``prev + sign(close-prev_close)*volume``，首根取 volume。

    close/volume 取自 ``site.env``。与 TV 差异：close/volume 为 na 时保持上一根。
    """
    close = _env(site, "close")
    volume = _env(site, "volume")
    if close is None or volume is None:
        return None
    current_close = close.now
    current_volume = volume.now
    previous = site.values.get("obv")
    if previous is None:
        value = None if is_na(current_volume) else to_number(current_volume)
        site.values["obv"] = value
        return value
    if is_na(current_close) or is_na(current_volume):
        return previous
    previous_close = close[1]
    if not is_na(previous_close):
        if to_number(current_close) > to_number(previous_close):
            previous += to_number(current_volume)
        elif to_number(current_close) < to_number(previous_close):
            previous -= to_number(current_volume)
    site.values["obv"] = previous
    return previous


@_register("ta.accdist", "accdist")
def ta_accdist(args, kwargs, site: SiteState):
    """累积/派发线：``((close-low)-(high-close))/(high-low)*volume`` 的累计。

    high==low 时该 bar 记 0。与 TV 差异：TV 里 ``ta.accdist`` 是内置变量而非函数，
    本引擎按函数暴露；na 时保持上一根。
    """
    high = _env(site, "high")
    low = _env(site, "low")
    close = _env(site, "close")
    volume = _env(site, "volume")
    if high is None or low is None or close is None or volume is None:
        return None
    total = float(site.values.get("accdist", 0.0))
    high_value, low_value = high.now, low.now
    close_value, volume_value = close.now, volume.now
    if is_na(high_value) or is_na(low_value) or is_na(close_value) or is_na(volume_value):
        return total
    span = to_number(high_value) - to_number(low_value)
    if span != 0:
        total += (
            (to_number(close_value) - to_number(low_value))
            - (to_number(high_value) - to_number(close_value))
        ) / span * to_number(volume_value)
    site.values["accdist"] = total
    return total


@_register("ta.wvad", "wvad")
def ta_wvad(args, kwargs, site: SiteState):
    """Williams 可变累积/派发：``((close-open)/(high-low))*volume`` 的累计。

    high==low 时该 bar 记 0。与 TV 差异：TV 里 ``ta.wvad`` 是内置变量，本引擎按
    函数暴露；na 时保持上一根。
    """
    open_view = _env(site, "open")
    high = _env(site, "high")
    low = _env(site, "low")
    close = _env(site, "close")
    volume = _env(site, "volume")
    if open_view is None or high is None or low is None or close is None or volume is None:
        return None
    total = float(site.values.get("wvad", 0.0))
    open_value = open_view.now
    high_value, low_value = high.now, low.now
    close_value, volume_value = close.now, volume.now
    if any(is_na(value) for value in (open_value, high_value, low_value, close_value, volume_value)):
        return total
    span = to_number(high_value) - to_number(low_value)
    if span != 0:
        total += (to_number(close_value) - to_number(open_value)) / span * to_number(volume_value)
    site.values["wvad"] = total
    return total


# ---------------------------------------------------------------------------
# 动量（补充 docstring 并规范化默认值）
# ---------------------------------------------------------------------------


@_register("ta.mom", "mom")
def ta_mom(args, kwargs, site: SiteState):
    """动量：``source - source[length]``，默认 length=10（观潮口径）。

    与 TV 差异：TV 的 ``ta.mom`` 必须显式给 length；任一值为 na 时返回 na。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 10, "length")
    if view is None:
        return None
    current = view.now
    past = view[length]
    if is_na(current) or is_na(past):
        return None
    return to_number(current) - to_number(past)


@_register("ta.roc", "roc")
def ta_roc(args, kwargs, site: SiteState):
    """变动率：``100*(source - source[length]) / source[length]``，默认 length=1。

    与 TV 差异：TV 的 ``ta.roc`` 必须显式给 length；基准值为 0 时返回 na。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 1, "length")
    if view is None:
        return None
    current = view.now
    past = view[length]
    if is_na(current) or is_na(past):
        return None
    base = to_number(past)
    if base == 0:
        return None
    return 100.0 * (to_number(current) - base) / base


@_register("ta.cmo", "cmo")
def ta_cmo(args, kwargs, site: SiteState):
    """钱德动量摆动指标：``100*(sumUp - sumDown) / (sumUp + sumDown)``，窗口 length。

    与 TV 差异：完全无波动（sumUp+sumDown==0）时返回 0（观潮口径）。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 14, "length")
    if view is None:
        return None
    if len(view.series.values) < length + 1:
        return None
    upward = 0.0
    downward = 0.0
    for offset in range(length):
        current = view[offset]
        previous = view[offset + 1]
        if is_na(current) or is_na(previous):
            return None
        delta = to_number(current) - to_number(previous)
        if delta > 0:
            upward += delta
        else:
            downward -= delta
    if upward + downward == 0:
        return 0.0
    return 100.0 * (upward - downward) / (upward + downward)


# ---------------------------------------------------------------------------
# 交叉 / 变化（补充 docstring 并统一 na 口径）
# ---------------------------------------------------------------------------


def _cross_flags(first: ArgView, second: ArgView):
    current_first = first.now
    current_second = second.now
    previous_first = first[1]
    previous_second = second[1]
    if any(is_na(value) for value in (current_first, current_second, previous_first, previous_second)):
        return None
    return (
        to_number(current_first),
        to_number(current_second),
        to_number(previous_first),
        to_number(previous_second),
    )


@_register("ta.crossover", "crossover")
def ta_crossover(args, kwargs, site: SiteState):
    """向上交叉：``a[1] <= b[1] and a > b``；任一值为 na 返回 false。

    与 TV 差异：TV 在 na 时返回 na，本引擎按观潮口径返回 false。
    """
    first = _first_view(args, kwargs, ("source1", "series1", "a"), 0)
    second = _first_view(args, kwargs, ("source2", "series2", "b"), 1)
    if first is None or second is None:
        return False
    values = _cross_flags(first, second)
    if values is None:
        return False
    current_first, current_second, previous_first, previous_second = values
    return previous_first <= previous_second and current_first > current_second


@_register("ta.crossunder", "crossunder")
def ta_crossunder(args, kwargs, site: SiteState):
    """向下交叉：``a[1] >= b[1] and a < b``；任一值为 na 返回 false。

    与 TV 差异：TV 在 na 时返回 na，本引擎按观潮口径返回 false。
    """
    first = _first_view(args, kwargs, ("source1", "series1", "a"), 0)
    second = _first_view(args, kwargs, ("source2", "series2", "b"), 1)
    if first is None or second is None:
        return False
    values = _cross_flags(first, second)
    if values is None:
        return False
    current_first, current_second, previous_first, previous_second = values
    return previous_first >= previous_second and current_first < current_second


@_register("ta.cross", "cross")
def ta_cross(args, kwargs, site: SiteState):
    """交叉（任一方向）：crossover 或 crossunder；na 返回 false。

    与 TV 差异：na 时返回 false（TV 为 na）。
    """
    return ta_crossover(args, kwargs, site) or ta_crossunder(args, kwargs, site)


@_register("ta.change", "change")
def ta_change(args, kwargs, site: SiteState):
    """变化量：``source - source[length]``，length 默认 1。

    与 TV 差异：无（任一值为 na 返回 na）。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 1, "length")
    if view is None:
        return None
    current = view.now
    past = view[length]
    if is_na(current) or is_na(past):
        return None
    return to_number(current) - to_number(past)


@_register("ta.rising", "rising")
def ta_rising(args, kwargs, site: SiteState):
    """严格递增：最近 ``length+1`` 根逐根变大；历史不足或含 na 返回 false。

    与 TV 差异：TV 返回 na，本引擎返回 false（与 crossover 口径一致）。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 1, "length")
    if view is None:
        return False
    values = _strict_window(view, length + 1)
    if values is None:
        return False
    return all(values[index] > values[index - 1] for index in range(1, len(values)))


@_register("ta.falling", "falling")
def ta_falling(args, kwargs, site: SiteState):
    """严格递减：最近 ``length+1`` 根逐根变小；历史不足或含 na 返回 false。

    与 TV 差异：TV 返回 na，本引擎返回 false（与 crossunder 口径一致）。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 1, "length")
    if view is None:
        return False
    values = _strict_window(view, length + 1)
    if values is None:
        return False
    return all(values[index] < values[index - 1] for index in range(1, len(values)))


# ---------------------------------------------------------------------------
# 极值偏移 / 累计（补充 docstring 并统一 na 口径）
# ---------------------------------------------------------------------------


def _extreme_offset(view: ArgView, length: int, want_max: bool) -> int | None:
    """极值相对当前的偏移（负数，0=当根）；同值取最近出现的位置。"""
    bound = len(view.series.values)
    best: float | None = None
    best_offset: int | None = None
    seen = 0
    offset = 0
    while seen < length and offset < bound:
        raw = view[offset]
        if is_na(raw):
            offset += 1
            continue
        value = to_number(raw)
        if best is None or (value > best if want_max else value < best):
            best = value
            best_offset = -offset
        seen += 1
        offset += 1
    if seen < length or best is None:
        return None
    return best_offset


@_register("ta.highestbars", "highestbars")
def ta_highestbars(args, kwargs, site: SiteState):
    """窗口最高值相对当前的偏移（负数，0=当根）；并列取最近出现的位置。

    与 TV 差异：窗口内 na 会被跳过并继续往前取（TV 返回 na）。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 14, "length")
    if view is None:
        return None
    return _extreme_offset(view, length, True)


@_register("ta.lowestbars", "lowestbars")
def ta_lowestbars(args, kwargs, site: SiteState):
    """窗口最低值相对当前的偏移（负数，0=当根）；并列取最近出现的位置。

    与 TV 差异：窗口内 na 会被跳过并继续往前取（TV 返回 na）。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    length = _first_length(args, kwargs, ("length",), 1, 14, "length")
    if view is None:
        return None
    return _extreme_offset(view, length, False)


@_register("ta.cum", "cum")
def ta_cum(args, kwargs, site: SiteState):
    """累计和：自第一根起累加，na 视为 0。

    与 TV 差异：无。
    """
    view = _first_view(args, kwargs, ("source", "series"), 0)
    if view is None:
        return None
    total = float(site.values.get("cum", 0.0))
    total += to_number(view.now, 0.0)
    site.values["cum"] = total
    return total


@_register("ta.barssince", "barssince")
def ta_barssince(args, kwargs, site: SiteState):
    """条件最近一次成立的 bar 数；从未成立返回 na（首根之前也返回 na）。

    与 TV 差异：无（条件为 na 视为不成立）。
    """
    condition = _first_view(args, kwargs, ("condition",), 0)
    if condition is None:
        return None
    bar = len(condition.series.values) - 1
    if not is_na(condition.now) and bool(condition.now):
        site.values["barssince_last"] = bar
        return 0
    last = site.values.get("barssince_last")
    if last is None:
        return None
    return bar - last


__all__ = [
    "TA",
    "TA_ALIASES",
    "ta_sma",
    "ta_wma",
    "ta_vwma",
    "ta_stdev",
    "ta_variance",
    "ta_dev",
    "ta_bb",
    "ta_bbw",
    "ta_cci",
    "ta_highest",
    "ta_lowest",
    "ta_alma",
    "ta_kama",
    "ta_mfi",
    "ta_cog",
    "ta_fisher",
    "ta_tsi",
    "ta_adx",
    "ta_dmi",
    "ta_sar",
    "ta_supertrend",
    "ta_linreg",
    "ta_valuewhen",
    "ta_pivothigh",
    "ta_pivotlow",
    "ta_correlation",
    "ta_covariance",
    "ta_median",
    "ta_mode",
    "ta_percentrank",
    "ta_range",
    "ta_obv",
    "ta_accdist",
    "ta_wvad",
    "ta_mom",
    "ta_roc",
    "ta_cmo",
    "ta_crossover",
    "ta_crossunder",
    "ta_cross",
    "ta_change",
    "ta_rising",
    "ta_falling",
    "ta_highestbars",
    "ta_lowestbars",
    "ta_cum",
    "ta_barssince",
    # 以下来自 ta_core（未覆盖的核心实现）
    "ta_ema",
    "ta_rma",
    "ta_swma",
    "ta_hma",
    "ta_dema",
    "ta_tema",
    "ta_vwap",
    "ta_tr",
    "ta_atr",
    "ta_donchian",
    "ta_kc",
    "ta_rsi",
    "ta_macd",
    "ta_stoch",
    "ta_willr",
]
