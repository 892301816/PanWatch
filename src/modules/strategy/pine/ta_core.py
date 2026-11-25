"""Pine ``ta.*`` 指标核心集（兜底实现）。

口径对齐观潮（TideView）引擎的实测行为，与 TradingView 的已知差异写在各
函数 docstring 里。只覆盖真正高频的指标；``ta.py`` 如果提供了更完整的
实现，引擎会优先用 ``ta.py``，这里的版本仅在缺失时兜底。
"""

from __future__ import annotations

import math
from typing import Any

from .errors import PineRuntimeError
from .series import ArgView, Series, SiteState, is_na, to_number

TA: dict[str, Any] = {}
TA_ALIASES: dict[str, str] = {}


def _register(dotted: str, *aliases: str):
    def decorator(function):
        TA[dotted] = function
        for alias in aliases or (dotted.split(".", 1)[-1],):
            TA_ALIASES[alias] = dotted
        return function

    return decorator


# ---------------------------------------------------------------------------
# 取值
# ---------------------------------------------------------------------------


def _view(args, kwargs, name: str, index: int | None = 0) -> ArgView | None:
    view = kwargs.get(name)
    if view is None and index is not None and index < len(args):
        view = args[index]
    return view


def _scalar(args, kwargs, name: str, index: int | None, default: float) -> float:
    view = _view(args, kwargs, name, index)
    if view is None:
        return default
    return to_number(view.now, default)


def _length(value: float, name: str = "length") -> int:
    length = int(round(value))
    if length <= 0:
        raise PineRuntimeError(f"{name} 必须是正整数")
    return length


def _col(values: list[Any]) -> list[float]:
    """把契约里的 None 过滤成「最近 N 个有效值」。"""
    return [to_number(value) for value in values if not is_na(value)]


def _window(view: ArgView, length: int) -> list[float] | None:
    """窗口内的有效值；跳过 na 并继续往前取，凑不满则 None。"""
    raw = view.history(length)
    values = _col(raw)
    if len(values) < length:
        return None
    return values


def _window_any(view: ArgView, length: int) -> list[Any] | None:
    raw = view.history(length)
    if any(is_na(value) for value in raw):
        return None
    return [to_number(value) for value in raw]


# ---------------------------------------------------------------------------
# 均线
# ---------------------------------------------------------------------------


@_register("ta.sma", "sma")
def ta_sma(args, kwargs, site: SiteState):
    """简单移动平均。窗口内 na 会被跳过并往前补取（观潮口径，非 TV）。"""
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 14))
    if view is None:
        return None
    values = _window(view, length)
    return None if values is None else sum(values) / length


def _recursive_average(view: ArgView, length: int, site: SiteState, alpha: float):
    previous = site.values.get("value")
    current = view.now
    if is_na(previous):
        # 用前 length 个有效值播种
        history = view.history(length)
        values = _col(history)
        if len(values) < length:
            site.values["value"] = None
            return None
        seed = sum(values[:length]) / length
        site.values["value"] = seed
        return seed
    if is_na(current):
        return previous  # 源为 na 时输出保持
    value = alpha * to_number(current) + (1 - alpha) * to_number(previous)
    site.values["value"] = value
    return value


@_register("ta.ema", "ema")
def ta_ema(args, kwargs, site: SiteState):
    """指数移动平均：alpha=2/(n+1)，前 n 个值用 SMA 播种。"""
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 14))
    if view is None:
        return None
    return _recursive_average(view, length, site, 2.0 / (length + 1))


@_register("ta.rma", "rma")
def ta_rma(args, kwargs, site: SiteState):
    """Wilder 平滑：alpha=1/n，前 n 个值用 SMA 播种。"""
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 14))
    if view is None:
        return None
    return _recursive_average(view, length, site, 1.0 / length)


@_register("ta.wma", "wma")
def ta_wma(args, kwargs, site: SiteState):
    """加权移动平均（最近值权重最大）。"""
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 14))
    if view is None:
        return None
    values = _window(view, length)
    if values is None:
        return None
    weights = sum(range(1, length + 1))
    return sum(value * (index + 1) for index, value in enumerate(values)) / weights


@_register("ta.vwma", "vwma")
def ta_vwma(args, kwargs, site: SiteState):
    """成交量加权均线：sma(source*volume)/sma(volume)。"""
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 20))
    volume = _view(args, kwargs, "volume", None)
    if view is None:
        return None
    volumes = volume.history(length) if volume is not None else _volume_history(site, length)
    values = view.history(length)
    if sum(1 for value in values if not is_na(value)) < length:
        return None
    total_weight = 0.0
    total = 0.0
    for price, size in zip(values, volumes):
        if is_na(price) or is_na(size):
            continue
        total += to_number(price) * to_number(size)
        total_weight += to_number(size)
    return total / total_weight if total_weight > 0 else None


def _volume_history(site: SiteState, length: int):
    buffer = site.values.setdefault("volume", [])
    return buffer[-length:]


@_register("ta.swma", "swma")
def ta_swma(args, kwargs, site: SiteState):
    """对称加权均线，权重 1/2/2/1，需要 4 个有效值。"""
    view = _view(args, kwargs, "source")
    if view is None:
        return None
    raw = view.history(4)
    if any(is_na(value) for value in raw):
        return None
    first, second, third, fourth = (to_number(value) for value in raw)
    return (first + 2 * second + 2 * third + fourth) / 6


@_register("ta.hma", "hma")
def ta_hma(args, kwargs, site: SiteState):
    """Hull 均线：wma(2*wma(n/2) - wma(n), round(sqrt(n)))。"""
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 9))
    if view is None:
        return None
    half = max(1, length // 2)
    root = max(1, int(round(math.sqrt(length))))
    short = _simple_wma(view, half)
    full = _simple_wma(view, length)
    if short is None or full is None:
        return None
    derived = _series_view(site, "derived", 2 * short - full)
    return _simple_wma(derived, root)


def _simple_wma(view: ArgView, length: int):
    values = _window(view, length)
    if values is None:
        return None
    weights = sum(range(1, length + 1))
    return sum(value * (index + 1) for index, value in enumerate(values)) / weights


def _sub_site(site: SiteState, key: str) -> SiteState:
    nested = site.values.get(key)
    if not isinstance(nested, SiteState):
        nested = SiteState()
        nested.env = site.env
        site.values[key] = nested
    return nested


@_register("ta.dema", "dema")
def ta_dema(args, kwargs, site: SiteState):
    """双重指数均线：2*EMA - EMA(EMA)。"""
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 14))
    if view is None:
        return None
    alpha = 2.0 / (length + 1)
    first = _recursive_average(view, length, _sub_site(site, "e1"), alpha)
    if first is None:
        return None
    second = _recursive_average(_series_view(site, "derived_e1", first), length, _sub_site(site, "e2"), alpha)
    return None if second is None else 2 * first - second


@_register("ta.tema", "tema")
def ta_tema(args, kwargs, site: SiteState):
    """三重指数均线：3*e1 - 3*e2 + e3。"""
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 14))
    if view is None:
        return None
    alpha = 2.0 / (length + 1)
    first = _recursive_average(view, length, _sub_site(site, "e1"), alpha)
    if first is None:
        return None
    second = _recursive_average(_series_view(site, "derived_e1", first), length, _sub_site(site, "e2"), alpha)
    if second is None:
        return None
    third = _recursive_average(_series_view(site, "derived_e2", second), length, _sub_site(site, "e3"), alpha)
    return None if third is None else 3 * first - 3 * second + third


@_register("ta.vwap", "vwap")
def ta_vwap(args, kwargs, site: SiteState):
    """累计成交均价。

    本引擎只保证日线口径，因此按「全样本累计」近似锚定 VWAP；分钟级
    日内重置需要分钟 K（当前管线不提供），这是与本家的已知差异。
    """
    price = _view(args, kwargs, "source", None) or site.env.get("close")
    volume = _view(args, kwargs, "volume", None) or site.env.get("volume")
    if price is None:
        return None
    total_price = site.values.get("sum_price", 0.0)
    total_volume = site.values.get("sum_volume", 0.0)
    size = to_number(volume.now, 0.0) if volume is not None else 0.0
    total_price += to_number(price.now) * size
    total_volume += size
    site.values["sum_price"] = total_price
    site.values["sum_volume"] = total_volume
    return total_price / total_volume if total_volume > 0 else None


# ---------------------------------------------------------------------------
# 波动率
# ---------------------------------------------------------------------------


@_register("ta.stdev", "stdev")
def ta_stdev(args, kwargs, site: SiteState):
    """总体标准差（除以 n）。"""
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 20))
    if view is None:
        return None
    values = _window(view, length)
    if values is None:
        return None
    mean = sum(values) / length
    return math.sqrt(sum((value - mean) ** 2 for value in values) / length)


@_register("ta.variance", "variance")
def ta_variance(args, kwargs, site: SiteState):
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 20))
    if view is None:
        return None
    values = _window(view, length)
    if values is None:
        return None
    mean = sum(values) / length
    return sum((value - mean) ** 2 for value in values) / length


@_register("ta.dev", "dev")
def ta_dev(args, kwargs, site: SiteState):
    """平均绝对偏差。"""
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 20))
    if view is None:
        return None
    values = _window(view, length)
    if values is None:
        return None
    mean = sum(values) / length
    return sum(abs(value - mean) for value in values) / length


@_register("ta.tr", "tr")
def ta_tr(args, kwargs, site: SiteState):
    """真实波幅：max(H-L, |H-prevC|, |L-prevC|)。"""
    high = _view(args, kwargs, "high", None) or site.env.get("high")
    low = _view(args, kwargs, "low", None) or site.env.get("low")
    close = _view(args, kwargs, "close", None) or site.env.get("close")
    if high is None or low is None:
        return None
    previous_close = close[1] if close is not None else None
    top = to_number(high.now)
    bottom = to_number(low.now)
    if is_na(previous_close):
        return top - bottom
    return max(top - bottom, abs(top - to_number(previous_close)), abs(bottom - to_number(previous_close)))


def _tr_view(site: SiteState) -> ArgView | None:
    """把 TR 值喂进一条「伪 series」，供 ATR 的 RMA 使用。"""
    high = site.env.get("high")
    low = site.env.get("low")
    close = site.env.get("close")
    if high is None or low is None:
        return None
    previous_close = close[1] if close is not None else None
    top = to_number(high.now)
    bottom = to_number(low.now)
    if is_na(previous_close):
        value = top - bottom
    else:
        value = max(top - bottom, abs(top - to_number(previous_close)), abs(bottom - to_number(previous_close)))
    return _series_view(site, "tr_series", value)


def _series_view(site: SiteState, key: str, value: Any) -> ArgView:
    """把一个逐 bar 派生值包装成 ArgView（用于嵌套平滑）。"""
    holder = site.values.get(key)
    if holder is None:
        holder = {"series": Series(), "count": 0}
        site.values[key] = holder
    series = holder["series"]
    series.set(holder["count"], value)
    holder["count"] += 1
    return ArgView(series, value)


@_register("ta.atr", "atr")
def ta_atr(args, kwargs, site: SiteState):
    """ATR = RMA(TR, n)；TR 从注入的行情上下文取。"""
    length = _length(_scalar(args, kwargs, "length", 0, 14))
    view = _view(args, kwargs, "source", None)
    if view is None:
        view = _tr_view(site)
    if view is None:
        return None
    return _recursive_average(view, length, _sub_site(site, "rma"), 1.0 / length)


# ---------------------------------------------------------------------------
# 通道
# ---------------------------------------------------------------------------


@_register("ta.bb", "bb", "boll", "ta.bbands")
def ta_bb(args, kwargs, site: SiteState):
    """布林带 → (中轨, 上轨, 下轨)。"""
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 20))
    mult = _scalar(args, kwargs, "mult", 2, 2.0)
    if view is None:
        return (None, None, None)
    middle = ta_sma([view], {"length": ArgView(view.series, length)}, site)
    deviation = ta_stdev([view], {"length": ArgView(view.series, length)}, site)
    if middle is None or deviation is None:
        return (None, None, None)
    return (middle, middle + mult * deviation, middle - mult * deviation)


@_register("ta.bbw", "bbw")
def ta_bbw(args, kwargs, site: SiteState):
    middle, upper, lower = ta_bb(args, kwargs, site)
    if middle in (None, 0) or upper is None:
        return None
    return (upper - lower) / middle


@_register("ta.donchian", "donchian")
def ta_donchian(args, kwargs, site: SiteState):
    """唐奇安通道 → (中轨, 上轨, 下轨)（本引擎比 TV 多一个中轨）。"""
    length = _length(_scalar(args, kwargs, "length", 0, 20))
    high = site.env.get("high")
    low = site.env.get("low")
    if high is None or low is None:
        return (None, None, None)
    tops = _window(high, length)
    bottoms = _window(low, length)
    if tops is None or bottoms is None:
        return (None, None, None)
    upper = max(tops)
    lower = min(bottoms)
    return ((upper + lower) / 2, upper, lower)


@_register("ta.kc", "kc")
def ta_kc(args, kwargs, site: SiteState):
    """肯特纳通道 → (中轨, 上轨, 下轨)。第 4 参数是「是否用 TR」，不是 ATR 周期。"""
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 20))
    mult = _scalar(args, kwargs, "mult", 2, 1.5)
    use_tr = bool(_scalar(args, kwargs, "useTrueRange", 3, 1))
    if view is None:
        return (None, None, None)
    middle = ta_sma([view], {"length": ArgView(view.series, length)}, site)
    deviation = ta_atr([], {}, site) if use_tr else ta_stdev([view], {"length": ArgView(view.series, length)}, site)
    if middle is None or deviation is None:
        return (None, None, None)
    return (middle, middle + mult * deviation, middle - mult * deviation)


# ---------------------------------------------------------------------------
# 动量
# ---------------------------------------------------------------------------


@_register("ta.rsi", "rsi")
def ta_rsi(args, kwargs, site: SiteState):
    """RSI（Wilder）。gain=loss=0 时返回 50（观潮口径）。"""
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 14))
    if view is None:
        return None
    current = view.now
    previous = view[1]
    if is_na(current) or is_na(previous):
        return None
    change = to_number(current) - to_number(previous)
    gain = max(change, 0.0)
    loss = max(-change, 0.0)
    up_site = _sub_site(site, "up")
    down_site = _sub_site(site, "down")
    upper = _recursive_value(gain, length, up_site, 1.0 / length, site)
    lower = _recursive_value(loss, length, down_site, 1.0 / length, site)
    if upper is None or lower is None:
        return None
    if upper == 0 and lower == 0:
        return 50.0
    if lower == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + upper / lower)


def _recursive_value(value: float, length: int, site: SiteState, alpha: float, parent: SiteState):
    previous = site.values.get("value")
    buffer = site.values.get("buffer")
    if buffer is None:
        buffer = []
        site.values["buffer"] = buffer
    buffer.append(value)
    if len(buffer) < length:
        return None
    if is_na(previous):
        seed = sum(buffer[-length:]) / length
        site.values["value"] = seed
        return seed
    current = alpha * value + (1 - alpha) * to_number(previous)
    site.values["value"] = current
    return current


@_register("ta.macd", "macd")
def ta_macd(args, kwargs, site: SiteState):
    """MACD → (macd, signal, hist)。

    **本引擎 hist = 2 * (macd - signal)**（观潮/通达信口径，与 TV 不同）。
    """
    view = _view(args, kwargs, "source")
    fast = _length(_scalar(args, kwargs, "fastLength", 1, 12), "fastLength")
    slow = _length(_scalar(args, kwargs, "slowLength", 2, 26), "slowLength")
    signal = _length(_scalar(args, kwargs, "signalSmoothing", 3, 9), "signalSmoothing")
    if view is None:
        return (None, None, None)
    fast_value = _recursive_average(view, fast, _sub_site(site, "fast"), 2.0 / (fast + 1))
    slow_value = _recursive_average(view, slow, _sub_site(site, "slow"), 2.0 / (slow + 1))
    if fast_value is None or slow_value is None:
        return (None, None, None)
    macd = fast_value - slow_value
    signal_view = ArgView(view.series, macd)
    signal_value = _recursive_average(signal_view, signal, _sub_site(site, "signal"), 2.0 / (signal + 1))
    if signal_value is None:
        return (macd, None, None)
    return (macd, signal_value, 2 * (macd - signal_value))


@_register("ta.stoch", "stoch")
def ta_stoch(args, kwargs, site: SiteState):
    """KD 随机指标（0-100）。参数顺序 (close, high, low, length)。"""
    close = _view(args, kwargs, "close", 0)
    high = _view(args, kwargs, "high", 1)
    low = _view(args, kwargs, "low", 2)
    length = _length(_scalar(args, kwargs, "length", 3, 9))
    if close is None or high is None or low is None:
        return None
    tops = _window(high, length)
    bottoms = _window(low, length)
    if tops is None or bottoms is None:
        return None
    highest = max(tops)
    lowest = min(bottoms)
    if highest == lowest:
        return None
    return 100.0 * (to_number(close.now) - lowest) / (highest - lowest)


@_register("ta.cci", "cci")
def ta_cci(args, kwargs, site: SiteState):
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 20))
    if view is None:
        return None
    values = _window(view, length)
    if values is None:
        return None
    mean = sum(values) / length
    deviation = sum(abs(value - mean) for value in values) / length
    if deviation == 0:
        return None
    return (to_number(view.now) - mean) / (0.015 * deviation)


@_register("ta.mom", "mom")
def ta_mom(args, kwargs, site: SiteState):
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 10))
    if view is None:
        return None
    previous = view[length]
    if is_na(previous):
        return None
    return to_number(view.now) - to_number(previous)


@_register("ta.roc", "roc")
def ta_roc(args, kwargs, site: SiteState):
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 1))
    if view is None:
        return None
    previous = view[length]
    if is_na(previous) or to_number(previous) == 0:
        return None
    return 100.0 * (to_number(view.now) - to_number(previous)) / to_number(previous)


@_register("ta.willr", "willr", "ta.wpr", "wpr", "ta.williamsR")
def ta_willr(args, kwargs, site: SiteState):
    """威廉指标（只有 length 一个参数，close 固定）。"""
    length = _length(_scalar(args, kwargs, "length", 0, 14))
    high = site.env.get("high")
    low = site.env.get("low")
    close = site.env.get("close")
    if high is None or low is None or close is None:
        return None
    tops = _window(high, length)
    bottoms = _window(low, length)
    if tops is None or bottoms is None:
        return None
    highest = max(tops)
    lowest = min(bottoms)
    if highest == lowest:
        return None
    return -100.0 * (highest - to_number(close.now)) / (highest - lowest)


@_register("ta.cmo", "cmo")
def ta_cmo(args, kwargs, site: SiteState):
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 14))
    if view is None:
        return None
    raw = view.history(length + 1)
    if len(raw) < length + 1 or any(is_na(value) for value in raw):
        return None
    numbers = [to_number(value) for value in raw]
    gains = sum(max(numbers[i] - numbers[i - 1], 0.0) for i in range(1, len(numbers)))
    losses = sum(max(numbers[i - 1] - numbers[i], 0.0) for i in range(1, len(numbers)))
    if gains + losses == 0:
        return 0.0
    return 100.0 * (gains - losses) / (gains + losses)


# ---------------------------------------------------------------------------
# 趋势 / 交叉 / 极值
# ---------------------------------------------------------------------------


@_register("ta.crossover", "crossover")
def ta_crossover(args, kwargs, site: SiteState):
    first = _view(args, kwargs, "source1", 0)
    second = _view(args, kwargs, "source2", 1)
    if first is None or second is None:
        return False
    a0, b0, a1, b1 = first.now, second.now, first[1], second[1]
    if any(is_na(value) for value in (a0, b0, a1, b1)):
        return False
    return to_number(a1) <= to_number(b1) and to_number(a0) > to_number(b0)


@_register("ta.crossunder", "crossunder")
def ta_crossunder(args, kwargs, site: SiteState):
    first = _view(args, kwargs, "source1", 0)
    second = _view(args, kwargs, "source2", 1)
    if first is None or second is None:
        return False
    a0, b0, a1, b1 = first.now, second.now, first[1], second[1]
    if any(is_na(value) for value in (a0, b0, a1, b1)):
        return False
    return to_number(a1) >= to_number(b1) and to_number(a0) < to_number(b0)


@_register("ta.cross", "cross")
def ta_cross(args, kwargs, site: SiteState):
    return ta_crossover(args, kwargs, site) or ta_crossunder(args, kwargs, site)


@_register("ta.change", "change")
def ta_change(args, kwargs, site: SiteState):
    view = _view(args, kwargs, "source")
    length = max(1, int(round(_scalar(args, kwargs, "length", 1, 1))))
    if view is None:
        return None
    previous = view[length]
    if is_na(previous):
        return None
    return to_number(view.now) - to_number(previous)


@_register("ta.rising", "rising")
def ta_rising(args, kwargs, site: SiteState):
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 1))
    if view is None:
        return False
    raw = view.history(length + 1)
    if len(raw) < length + 1 or any(is_na(value) for value in raw):
        return False
    numbers = [to_number(value) for value in raw]
    return all(numbers[index] > numbers[index - 1] for index in range(1, len(numbers)))


@_register("ta.falling", "falling")
def ta_falling(args, kwargs, site: SiteState):
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 1))
    if view is None:
        return False
    raw = view.history(length + 1)
    if len(raw) < length + 1 or any(is_na(value) for value in raw):
        return False
    numbers = [to_number(value) for value in raw]
    return all(numbers[index] < numbers[index - 1] for index in range(1, len(numbers)))


@_register("ta.highest", "highest")
def ta_highest(args, kwargs, site: SiteState):
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 14))
    if view is None:
        return None
    values = _window(view, length)
    return None if values is None else max(values)


@_register("ta.lowest", "lowest")
def ta_lowest(args, kwargs, site: SiteState):
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 14))
    if view is None:
        return None
    values = _window(view, length)
    return None if values is None else min(values)


@_register("ta.highestbars", "highestbars")
def ta_highestbars(args, kwargs, site: SiteState):
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 14))
    if view is None:
        return None
    raw = view.history(length)
    if len(raw) < length or any(is_na(value) for value in raw):
        return None
    numbers = [to_number(value) for value in raw]
    peak = max(numbers)
    position = len(numbers) - 1 - numbers[::-1].index(peak)
    return -(len(numbers) - 1 - position)


@_register("ta.lowestbars", "lowestbars")
def ta_lowestbars(args, kwargs, site: SiteState):
    view = _view(args, kwargs, "source")
    length = _length(_scalar(args, kwargs, "length", 1, 14))
    if view is None:
        return None
    raw = view.history(length)
    if len(raw) < length or any(is_na(value) for value in raw):
        return None
    numbers = [to_number(value) for value in raw]
    trough = min(numbers)
    position = len(numbers) - 1 - numbers[::-1].index(trough)
    return -(len(numbers) - 1 - position)


@_register("ta.cum", "cum")
def ta_cum(args, kwargs, site: SiteState):
    view = _view(args, kwargs, "source")
    if view is None:
        return None
    total = site.values.get("total", 0.0)
    total += 0.0 if is_na(view.now) else to_number(view.now)
    site.values["total"] = total
    return total


@_register("ta.barssince", "barssince")
def ta_barssince(args, kwargs, site: SiteState):
    condition = _view(args, kwargs, "condition", 0)
    if condition is None:
        return None
    last = site.values.get("last")
    if bool(condition.now):
        site.values["last"] = 0
        return 0
    if last is None:
        return None
    site.values["last"] = to_number(last) + 1
    return site.values["last"]
