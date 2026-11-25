"""Pine 内置函数实现（除 ``ta.*`` 之外的部分）。

统一签名::

    def handler(interp, args, kwargs, node) -> Any

``args`` 是位置实参的 :class:`ArgView` 列表，``kwargs`` 是具名实参的
``dict[str, ArgView]``。需要当前值时取 ``arg.now``，需要历史时 ``arg[n]``。
"""

from __future__ import annotations

import math
from typing import Any, Callable

from .errors import PineRuntimeError
from .series import NA, ArgView, is_na, to_number

FUNCTIONS: dict[str, Callable] = {}
ALIASES: dict[str, str] = {}


def register(*names: str):
    def decorator(function: Callable) -> Callable:
        for name in names:
            FUNCTIONS[name] = function
        return function

    return decorator


# ---------------------------------------------------------------------------
# 取值助手
# ---------------------------------------------------------------------------


def _arg(args, kwargs, name: str, index: int | None, default: Any = None) -> Any:
    """按「具名优先、然后位置」取一个平铺值。

    ``index=None`` 表示该参数只接受具名写法（Pine 里大量可选参数如此），
    避免误把别的位置实参当成它。
    """
    view = kwargs.get(name)
    if view is None and index is not None and index < len(args):
        view = args[index]
    if view is None:
        return default
    return view.now


def _num(args, kwargs, name: str, index: int, default: float = 0.0) -> float:
    value = _arg(args, kwargs, name, index, default)
    return to_number(value, default)


def _int(args, kwargs, name: str, index: int, default: int = 0) -> int:
    return int(round(_num(args, kwargs, name, index, default)))


def _bool(args, kwargs, name: str, index: int, default: bool = False) -> bool:
    value = _arg(args, kwargs, name, index, default)
    if is_na(value):
        return default
    return bool(value)


def _text(args, kwargs, name: str, index: int, default: str = "") -> str:
    value = _arg(args, kwargs, name, index, default)
    return default if is_na(value) else str(value)


# ---------------------------------------------------------------------------
# 空值 / 类型
# ---------------------------------------------------------------------------


@register("nz")
def _nz(interp, args, kwargs, node):
    value = _arg(args, kwargs, "source", 0)
    fallback = _arg(args, kwargs, "replacement", 1, 0)
    return fallback if is_na(value) else value


@register("na")
def _na(interp, args, kwargs, node):
    return is_na(_arg(args, kwargs, "value", 0))


@register("fixnan")
def _fixnan(interp, args, kwargs, node):
    view = args[0] if args else kwargs.get("source")
    if view is None:
        return None
    if not is_na(view.now):
        interp.ctx.set_variable("__fixnan__%d" % node.id, view.now)
        return view.now
    return interp.ctx.get_variable("__fixnan__%d" % node.id)


@register("int")
def _int_cast(interp, args, kwargs, node):
    value = _arg(args, kwargs, "value", 0)
    return None if is_na(value) else int(to_number(value))


@register("float")
def _float_cast(interp, args, kwargs, node):
    value = _arg(args, kwargs, "value", 0)
    return None if is_na(value) else float(to_number(value))


@register("bool")
def _bool_cast(interp, args, kwargs, node):
    value = _arg(args, kwargs, "value", 0)
    return None if is_na(value) else bool(value)


@register("string")
def _string_cast(interp, args, kwargs, node):
    value = _arg(args, kwargs, "value", 0)
    return "na" if is_na(value) else str(value)


# ---------------------------------------------------------------------------
# math.*
# ---------------------------------------------------------------------------


def _unary_math(function: Callable[[float], float]):
    def handler(interp, args, kwargs, node):
        value = _arg(args, kwargs, "number", 0)
        if is_na(value):
            return None
        try:
            return function(to_number(value))
        except (ValueError, OverflowError):
            return None

    return handler


def _safe_log(function):
    def handler(interp, args, kwargs, node):
        value = _num(args, kwargs, "number", 0)
        if value <= 0:
            return None
        return function(value)

    return handler


FUNCTIONS["math.abs"] = _unary_math(abs)
FUNCTIONS["math.sqrt"] = _unary_math(math.sqrt)
FUNCTIONS["math.floor"] = lambda i, a, k, n: _floor(_arg(a, k, "number", 0))
FUNCTIONS["math.ceil"] = lambda i, a, k, n: _ceil(_arg(a, k, "number", 0))
FUNCTIONS["math.exp"] = _unary_math(math.exp)
FUNCTIONS["math.log"] = _safe_log(math.log)
FUNCTIONS["math.log10"] = _safe_log(math.log10)
FUNCTIONS["math.sign"] = lambda i, a, k, n: _sign(_arg(a, k, "number", 0))
FUNCTIONS["math.sin"] = _unary_math(math.sin)
FUNCTIONS["math.cos"] = _unary_math(math.cos)
FUNCTIONS["math.tan"] = _unary_math(math.tan)
FUNCTIONS["math.asin"] = _unary_math(math.asin)
FUNCTIONS["math.acos"] = _unary_math(math.acos)
FUNCTIONS["math.atan"] = _unary_math(math.atan)
FUNCTIONS["math.todegrees"] = lambda i, a, k, n: math.degrees(_num(a, k, "radians", 0))
FUNCTIONS["math.toradians"] = lambda i, a, k, n: math.radians(_num(a, k, "degrees", 0))


def _floor(value):
    return None if is_na(value) else math.floor(to_number(value))


def _ceil(value):
    return None if is_na(value) else math.ceil(to_number(value))


def _sign(value):
    if is_na(value):
        return None
    number = to_number(value)
    return 1 if number > 0 else -1 if number < 0 else 0


def _round_half_up(value: float, digits: int = 0) -> float:
    factor = 10 ** digits
    return math.floor(abs(value) * factor + 0.5) / factor * (1 if value >= 0 else -1)


@register("math.round")
def _math_round(interp, args, kwargs, node):
    value = _arg(args, kwargs, "number", 0)
    if is_na(value):
        return None
    digits = _arg(args, kwargs, "precision", 1, 0)
    return _round_half_up(to_number(value), int(to_number(digits)))


@register("math.round_to_mintick")
def _round_to_mintick(interp, args, kwargs, node):
    value = _arg(args, kwargs, "number", 0)
    if is_na(value):
        return None
    tick = interp.ctx.mintick or 0.01
    return round(to_number(value) / tick) * tick


@register("math.max")
def _math_max(interp, args, kwargs, node):
    values = [to_number(view.now, float("-inf")) for view in args]
    values += [to_number(view.now, float("-inf")) for view in kwargs.values()]
    if not values:
        raise PineRuntimeError("math.max 需要至少一个参数")
    return max(values)


@register("math.min")
def _math_min(interp, args, kwargs, node):
    values = [to_number(view.now, float("inf")) for view in args]
    values += [to_number(view.now, float("inf")) for view in kwargs.values()]
    if not values:
        raise PineRuntimeError("math.min 需要至少一个参数")
    return min(values)


@register("math.avg")
def _math_avg(interp, args, kwargs, node):
    values = [to_number(view.now) for view in args] + [to_number(view.now) for view in kwargs.values()]
    return sum(values) / len(values) if values else None


@register("math.pow")
def _math_pow(interp, args, kwargs, node):
    base = _num(args, kwargs, "base", 0)
    exponent = _num(args, kwargs, "exponent", 1)
    try:
        return math.pow(base, exponent)
    except (ValueError, OverflowError):
        return None


@register("math.sum", "sum")
def _math_sum(interp, args, kwargs, node):
    view = args[0] if args else kwargs.get("source")
    length = _int(args, kwargs, "length", 1, 1)
    if view is None or length <= 0:
        return None
    window = view.history(length)
    if any(is_na(value) for value in window):
        return None
    return sum(to_number(value) for value in window)


@register("math.random")
def _math_random(interp, args, kwargs, node):
    import random

    return random.uniform(_num(args, kwargs, "min", 0, 0.0), _num(args, kwargs, "max", 1, 1.0))


for _short in (
    "abs", "sqrt", "floor", "ceil", "exp", "log", "log10", "sign", "sin", "cos", "tan",
    "asin", "acos", "atan", "todegrees", "toradians", "round", "round_to_mintick", "max",
    "min", "avg", "pow", "random",
):
    ALIASES[_short] = f"math.{_short}"


# ---------------------------------------------------------------------------
# 颜色
# ---------------------------------------------------------------------------


def _hex_to_rgb(color: Any) -> tuple[int, int, int, int]:
    if not isinstance(color, str):
        return (0, 0, 0, 0)
    text = color.strip()
    if text.startswith("#") and len(text) == 7:
        return (int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16), 0)
    if text.startswith("#") and len(text) == 9:
        return (int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16), int(text[7:9], 16))
    return (0, 0, 0, 0)


def _rgb_to_hex(red: int, green: int, blue: int, transp: int = 0) -> str:
    red = max(0, min(255, int(red)))
    green = max(0, min(255, int(green)))
    blue = max(0, min(255, int(blue)))
    alpha = max(0, min(255, int(round(255 * (100 - max(0, min(100, transp))) / 100))))
    return f"#{red:02X}{green:02X}{blue:02X}{alpha:02X}"


@register("color.new")
def _color_new(interp, args, kwargs, node):
    color = _arg(args, kwargs, "color", 0)
    transp = _num(args, kwargs, "transp", 1, 0.0)
    if is_na(color):
        return None
    red, green, blue, _ = _hex_to_rgb(color)
    return _rgb_to_hex(red, green, blue, transp)


@register("color.rgb")
def _color_rgb(interp, args, kwargs, node):
    return _rgb_to_hex(
        _num(args, kwargs, "red", 0),
        _num(args, kwargs, "green", 1),
        _num(args, kwargs, "blue", 2),
        _num(args, kwargs, "transp", 3, 0.0),
    )


@register("color.r")
def _color_r(interp, args, kwargs, node):
    return _hex_to_rgb(_arg(args, kwargs, "color", 0))[0]


@register("color.g")
def _color_g(interp, args, kwargs, node):
    return _hex_to_rgb(_arg(args, kwargs, "color", 0))[1]


@register("color.b")
def _color_b(interp, args, kwargs, node):
    return _hex_to_rgb(_arg(args, kwargs, "color", 0))[2]


@register("color.t")
def _color_t(interp, args, kwargs, node):
    return _hex_to_rgb(_arg(args, kwargs, "color", 0))[3]


@register("color.from_gradient")
def _color_from_gradient(interp, args, kwargs, node):
    value = _num(args, kwargs, "value", 0)
    bottom_value = _num(args, kwargs, "bottom_value", 1, 0.0)
    top_value = _num(args, kwargs, "top_value", 2, 100.0)
    bottom_color = _arg(args, kwargs, "bottom_color", 3) or "#000000"
    top_color = _arg(args, kwargs, "top_color", 4) or "#FFFFFF"
    if top_value == bottom_value:
        return bottom_color
    ratio = max(0.0, min(1.0, (value - bottom_value) / (top_value - bottom_value)))
    low = _hex_to_rgb(bottom_color)
    high = _hex_to_rgb(top_color)
    return _rgb_to_hex(
        low[0] + (high[0] - low[0]) * ratio,
        low[1] + (high[1] - low[1]) * ratio,
        low[2] + (high[2] - low[2]) * ratio,
        low[3] + (high[3] - low[3]) * ratio,
    )


# ---------------------------------------------------------------------------
# 字符串
# ---------------------------------------------------------------------------


@register("str.tostring")
def _str_tostring(interp, args, kwargs, node):
    value = _arg(args, kwargs, "value", 0)
    if is_na(value):
        return "na"
    fmt = _arg(args, kwargs, "format", 1)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if fmt == "mintick":
            digits = max(0, int(round(-math.log10(interp.ctx.mintick)))) if interp.ctx.mintick else 2
            return f"{value:.{digits}f}"
        if isinstance(fmt, str) and fmt.startswith("#"):
            digits = fmt.count("#")
            return f"{float(value):.{digits}f}"
        if float(value).is_integer():
            return str(int(value))
        return f"{float(value):g}"
    return str(value)


@register("str.tonumber")
def _str_tonumber(interp, args, kwargs, node):
    text = _text(args, kwargs, "string", 0)
    try:
        return float(text)
    except ValueError:
        return None


@register("str.format", "str.format_time")
def _str_format(interp, args, kwargs, node):
    template = _text(args, kwargs, "formatString", 0, _text(args, kwargs, "format", 0))
    rest = [to_number(view.now) if isinstance(view.now, (int, float)) else view.now for view in args[1:]]
    if "str.format_time" in (getattr(node, "_called", "") or ""):
        import datetime as _dt

        stamp = to_number(_arg(args, kwargs, "time", 0)) / 1000
        try:
            return _dt.datetime.fromtimestamp(stamp, _dt.timezone.utc).strftime(template)
        except Exception:  # pragma: no cover
            return template
    try:
        return template.format(*rest)
    except (IndexError, KeyError, ValueError):
        return template


@register("str.length")
def _str_length(interp, args, kwargs, node):
    return len(_text(args, kwargs, "string", 0))


@register("str.contains")
def _str_contains(interp, args, kwargs, node):
    return _text(args, kwargs, "str", 1) in _text(args, kwargs, "source", 0)


@register("str.startswith")
def _str_startswith(interp, args, kwargs, node):
    return _text(args, kwargs, "source", 0).startswith(_text(args, kwargs, "str", 1))


@register("str.endswith")
def _str_endswith(interp, args, kwargs, node):
    return _text(args, kwargs, "source", 0).endswith(_text(args, kwargs, "str", 1))


@register("str.substring")
def _str_substring(interp, args, kwargs, node):
    source = _text(args, kwargs, "source", 0)
    begin = _int(args, kwargs, "begin_index", 1, 0)
    end = _arg(args, kwargs, "end_index", 2)
    try:
        return source[begin:] if is_na(end) else source[begin:int(to_number(end))]
    except (IndexError, ValueError):
        return source


@register("str.replace", "str.replace_all")
def _str_replace(interp, args, kwargs, node):
    source = _text(args, kwargs, "source", 0)
    target = _text(args, kwargs, "target", 1)
    replacement = _text(args, kwargs, "replacement", 2)
    return source.replace(target, replacement)


@register("str.split")
def _str_split(interp, args, kwargs, node):
    source = _text(args, kwargs, "string", 0)
    separator = _text(args, kwargs, "separator", 1)
    return source.split(separator) if separator else list(source)


@register("str.lower")
def _str_lower(interp, args, kwargs, node):
    return _text(args, kwargs, "source", 0).lower()


@register("str.upper")
def _str_upper(interp, args, kwargs, node):
    return _text(args, kwargs, "source", 0).upper()


@register("str.trim")
def _str_trim(interp, args, kwargs, node):
    return _text(args, kwargs, "source", 0).strip()


@register("str.repeat")
def _str_repeat(interp, args, kwargs, node):
    return _text(args, kwargs, "source", 0) * max(0, _int(args, kwargs, "count", 1, 1))


@register("str.pos")
def _str_pos(interp, args, kwargs, node):
    source = _text(args, kwargs, "source", 0)
    needle = _text(args, kwargs, "str", 1)
    index = source.find(needle)
    return index if index >= 0 else None


@register("str.match")
def _str_match(interp, args, kwargs, node):
    import re

    source = _text(args, kwargs, "source", 0)
    pattern = _text(args, kwargs, "regex", 1)
    try:
        match = re.search(pattern, source)
    except re.error:
        return None
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# input.*
# ---------------------------------------------------------------------------


def _register_input(interp, node, kind: str, default: Any, kwargs: dict, args=()) -> Any:
    title = None
    if "title" in kwargs:
        title = kwargs["title"].now
    elif len(args) > 1:
        title = args[1].now
    name = str(title) if title not in (None, "") else f"参数{node.id}"
    existing = next((meta for meta in interp.ctx.inputs_meta if meta["name"] == name), None)
    if existing is None:
        options = kwargs.get("options")
        option_values = (
            list(options.now) if isinstance(options, ArgView) and isinstance(options.now, list) else None
        )
        existing = {
            "id": name,
            "name": name,
            "kind": kind,
            "default": default,
            "minval": _plain(kwargs.get("minval")),
            "maxval": _plain(kwargs.get("maxval")),
            "step": _plain(kwargs.get("step")),
            "group": _plain(kwargs.get("group")),
            "tooltip": _plain(kwargs.get("tooltip")),
            "options": option_values,
        }
        interp.ctx.inputs_meta.append(existing)
    key = existing["id"]
    if key in interp.ctx.input_overrides:
        return interp.ctx.input_overrides[key]
    if name in interp.ctx.input_overrides:
        return interp.ctx.input_overrides[name]
    return default


def _plain(view):
    if view is None:
        return None
    value = view.now
    return None if is_na(value) else value


def _input_value(interp, node, kind: str, default: Any, args, kwargs) -> Any:
    """统一入口：把位置参数里的 title/options 也交给 _register_input。"""
    return _register_input(interp, node, kind, default, kwargs, args)

@register("input")
def _input(interp, args, kwargs, node):
    default = _arg(args, kwargs, "defval", 0)
    return _register_input(interp, node, "raw", default, kwargs, args)


@register("input.int")
def _input_int(interp, args, kwargs, node):
    default = _arg(args, kwargs, "defval", 0, 1)
    value = _int_of(default)
    return _int_of(_register_input(interp, node, "int", value, kwargs, args))


@register("input.float")
def _input_float(interp, args, kwargs, node):
    default = to_number(_arg(args, kwargs, "defval", 0, 0.0))
    return to_number(_register_input(interp, node, "float", default, kwargs, args))


@register("input.bool")
def _input_bool(interp, args, kwargs, node):
    default = bool(_arg(args, kwargs, "defval", 0, False))
    return bool(_register_input(interp, node, "bool", default, kwargs, args))


@register("input.string")
def _input_string(interp, args, kwargs, node):
    default = _text(args, kwargs, "defval", 0, "")
    value = _register_input(interp, node, "string", default, kwargs, args)
    return "" if is_na(value) else str(value)


@register("input.color")
def _input_color(interp, args, kwargs, node):
    default = _arg(args, kwargs, "defval", 0) or "#2962FF"
    return _register_input(interp, node, "color", default, kwargs, args)


@register("input.source")
def _input_source(interp, args, kwargs, node):
    view = args[0] if args else kwargs.get("defval")
    return view.now if view is not None else None


@register("input.timeframe")
def _input_timeframe(interp, args, kwargs, node):
    default = _text(args, kwargs, "defval", 0, "D")
    return _register_input(interp, node, "timeframe", default, kwargs, args)


@register("input.session")
def _input_session(interp, args, kwargs, node):
    default = _text(args, kwargs, "defval", 0, "0930-1500")
    return _register_input(interp, node, "session", default, kwargs, args)


@register("input.symbol")
def _input_symbol(interp, args, kwargs, node):
    default = _text(args, kwargs, "defval", 0, "")
    return _register_input(interp, node, "symbol", default, kwargs, args)


@register("input.time")
def _input_time(interp, args, kwargs, node):
    default = to_number(_arg(args, kwargs, "defval", 0))
    return _register_input(interp, node, "time", default, kwargs, args)


@register("input.price")
def _input_price(interp, args, kwargs, node):
    default = to_number(_arg(args, kwargs, "defval", 0))
    return _register_input(interp, node, "price", default, kwargs, args)


@register("input.text_area")
def _input_text_area(interp, args, kwargs, node):
    default = _text(args, kwargs, "defval", 0, "")
    return _register_input(interp, node, "text", default, kwargs, args)


def _int_of(value):
    try:
        return int(round(to_number(value)))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 绘图
# ---------------------------------------------------------------------------


# 名字里带这些关键词的曲线默认放到副图（观潮的 companion 指标也是这么判定的）
OSCILLATOR_HINTS = (
    "rsi", "macd", "kdj", "stoch", "kd", "cci", "wr", "willr", "mom", "roc", "obv",
    "adx", "dmi", "atr", "cmo", "mfi", "trix", "psy", "bias", "bbw", "hist", "osc",
    "signal", "柱", "振荡", "强弱", "超买", "超卖", "动量", "能量", "幅度",
)


def _looks_oscillator(name: str) -> bool:
    lower = str(name).lower()
    return any(hint in lower for hint in OSCILLATOR_HINTS)


@register("plot")
def _plot(interp, args, kwargs, node):
    view = args[0] if args else kwargs.get("series")
    title = _arg(args, kwargs, "title", 1)
    if is_na(title) or title == "":
        title = f"plot{len(interp.ctx.plots)}"
    name = str(title)
    overlay = interp.ctx.overlay
    force = _arg(args, kwargs, "force_overlay", None)
    if not is_na(force) and force:
        overlay = True
    elif overlay and _looks_oscillator(name):
        overlay = False
    spec = interp.ctx.plot(name, overlay)
    style = _arg(args, kwargs, "style", 4)
    if not is_na(style):
        spec.style = str(style)
    color = _arg(args, kwargs, "color", 2)
    if not is_na(color):
        spec.color = color
    width = _arg(args, kwargs, "linewidth", 3)
    if not is_na(width):
        spec.linewidth = int(to_number(width, 1))
    base = _arg(args, kwargs, "histbase", 6)
    if not is_na(base):
        spec.histogram_base = to_number(base)
    value = view.now if view is not None else None
    if isinstance(value, (tuple, list)):
        interp.ctx.warn(f"`plot({name})` 拿到的是元组，请先解构再画： [a, b] = ...")
        value = None
    spec.values.set(interp.ctx.index, value)
    return name


@register("plotshape")
def _plotshape(interp, args, kwargs, node):
    view = args[0] if args else kwargs.get("series")
    if view is None or not _truthy(view.now):
        return None
    interp.ctx.shapes.append(
        {
            "index": interp.ctx.index,
            "date": interp.ctx._dates[interp.ctx.index],
            "price": to_number(_arg(args, kwargs, "series", 0), 0.0),
            "style": _arg(args, kwargs, "style", None),
            "location": _arg(args, kwargs, "location", None),
            "color": _arg(args, kwargs, "color", None),
            "text": _arg(args, kwargs, "text", None),
            "size": _arg(args, kwargs, "size", None),
            "title": _arg(args, kwargs, "title", None),
        }
    )
    return None


@register("plotchar")
def _plotchar(interp, args, kwargs, node):
    view = args[0] if args else kwargs.get("series")
    if view is None or not _truthy(view.now):
        return None
    interp.ctx.shapes.append(
        {
            "index": interp.ctx.index,
            "date": interp.ctx._dates[interp.ctx.index],
            "char": _arg(args, kwargs, "char", None),
            "style": "char",
            "location": _arg(args, kwargs, "location", None),
            "color": _arg(args, kwargs, "color", None),
            "text": _arg(args, kwargs, "text", None),
            "title": _arg(args, kwargs, "title", None),
        }
    )
    return None


@register("plotarrow")
def _plotarrow(interp, args, kwargs, node):
    view = args[0] if args else kwargs.get("series")
    if view is None or is_na(view.now):
        return None
    value = to_number(view.now)
    if value == 0:
        return None
    interp.ctx.shapes.append(
        {
            "index": interp.ctx.index,
            "date": interp.ctx._dates[interp.ctx.index],
            "arrow": 1 if value > 0 else -1,
            "style": "arrow",
            "location": "abovebar" if value > 0 else "belowbar",
            "color": _arg(args, kwargs, "colorup" if value > 0 else "colordown", None),
        }
    )
    return None


@register("plotcandle", "plotbar")
def _plotcandle(interp, args, kwargs, node):
    interp.ctx.warn("plotcandle/plotbar 已降级为普通 plot（当前版本只画主图 K 线）")
    return _plot(interp, args, kwargs, node)


@register("hline")
def _hline(interp, args, kwargs, node):
    price = _num(args, kwargs, "price", 0)
    spec = {
        "price": price,
        "title": _arg(args, kwargs, "title", 1),
        "color": _arg(args, kwargs, "color", None),
        "linestyle": _arg(args, kwargs, "linestyle", None),
        "linewidth": _arg(args, kwargs, "linewidth", None),
    }
    key = (price, str(spec["title"]), interp.ctx.index == 0)
    if key not in interp._hline_seen:
        interp._hline_seen.add(key)
        interp.ctx.hlines.append(spec)
    return None


@register("fill")
def _fill(interp, args, kwargs, node):
    first = _arg(args, kwargs, "plot1", 0, _arg(args, kwargs, "hline1", 0))
    second = _arg(args, kwargs, "plot2", 1, _arg(args, kwargs, "hline2", 1))
    if interp.ctx.index == 0 or not interp.ctx.fills:
        interp.ctx.fills.append(
            {
                "from": first,
                "to": second,
                "color": _arg(args, kwargs, "color", 2),
                "title": _arg(args, kwargs, "title", None),
            }
        )
    return None


@register("bgcolor")
def _bgcolor(interp, args, kwargs, node):
    color = _arg(args, kwargs, "color", 0)
    interp.ctx.backgrounds.set(interp.ctx.index, color)
    return None


@register("barcolor")
def _barcolor(interp, args, kwargs, node):
    color = _arg(args, kwargs, "color", 0)
    interp.ctx.bar_colors.set(interp.ctx.index, color)
    return None


@register("fillgaps")
def _fillgaps(interp, args, kwargs, node):
    return None


# ---------------------------------------------------------------------------
# 标签 / 线段 / 矩形（基础能力）
# ---------------------------------------------------------------------------


@register("label.new")
def _label_new(interp, args, kwargs, node):
    identifier = len(interp.ctx.labels) + 1
    interp.ctx.labels.append(
        {
            "id": identifier,
            "index": interp.ctx.index,
            "date": interp.ctx._dates[interp.ctx.index],
            "x": _arg(args, kwargs, "x", 0),
            "y": _arg(args, kwargs, "y", 1),
            "text": _arg(args, kwargs, "text", 2),
            "color": _arg(args, kwargs, "color", None),
            "textcolor": _arg(args, kwargs, "textcolor", None),
            "style": _arg(args, kwargs, "style", None),
            "size": _arg(args, kwargs, "size", None),
            "deleted": False,
        }
    )
    return identifier


@register("label.delete")
def _label_delete(interp, args, kwargs, node):
    identifier = _int(args, kwargs, "id", 0, -1)
    for label in interp.ctx.labels:
        if label["id"] == identifier:
            label["deleted"] = True
    return None


def _label_setter(field: str):
    def handler(interp, args, kwargs, node):
        identifier = _int(args, kwargs, "id", 0, -1)
        value = _arg(args, kwargs, field, 1)
        for label in interp.ctx.labels:
            if label["id"] == identifier:
                label[field] = value
        return None

    return handler


for _field in ("text", "color", "textcolor", "style", "size", "tooltip"):
    FUNCTIONS[f"label.set_{_field}"] = _label_setter(_field)


@register("label.set_xy", "label.set_x", "label.set_y")
def _label_set_xy(interp, args, kwargs, node):
    identifier = _int(args, kwargs, "id", 0, -1)
    for label in interp.ctx.labels:
        if label["id"] == identifier:
            if "x" in kwargs or len(args) > 1:
                label["x"] = _arg(args, kwargs, "x", 1)
            if "y" in kwargs or len(args) > 2:
                label["y"] = _arg(args, kwargs, "y", 2)
    return None


@register("label.get_x", "label.get_y", "label.get_text")
def _label_getter(interp, args, kwargs, node):
    return None


@register("line.new")
def _line_new(interp, args, kwargs, node):
    identifier = len(interp.ctx.lines) + 1
    interp.ctx.lines.append(
        {
            "id": identifier,
            "x1": _arg(args, kwargs, "x1", 0),
            "y1": _arg(args, kwargs, "y1", 1),
            "x2": _arg(args, kwargs, "x2", 2),
            "y2": _arg(args, kwargs, "y2", 3),
            "color": _arg(args, kwargs, "color", None),
            "width": _arg(args, kwargs, "width", None),
            "style": _arg(args, kwargs, "style", None),
            "extend": _arg(args, kwargs, "extend", None),
            "deleted": False,
        }
    )
    return identifier


@register("line.delete")
def _line_delete(interp, args, kwargs, node):
    identifier = _int(args, kwargs, "id", 0, -1)
    for line in interp.ctx.lines:
        if line["id"] == identifier:
            line["deleted"] = True
    return None


@register("line.set_xy1", "line.set_xy2")
def _line_set_xy(interp, args, kwargs, node):
    identifier = _int(args, kwargs, "id", 0, -1)
    suffix = "1" if "1" in (getattr(node, "_called", "") or "") else "2"
    for line in interp.ctx.lines:
        if line["id"] == identifier:
            line[f"x{suffix}"] = _arg(args, kwargs, "x", 1)
            line[f"y{suffix}"] = _arg(args, kwargs, "y", 2)
    return None


@register("box.new")
def _box_new(interp, args, kwargs, node):
    identifier = len(interp.ctx.boxes) + 1
    interp.ctx.boxes.append(
        {
            "id": identifier,
            "left": _arg(args, kwargs, "left", 0),
            "top": _arg(args, kwargs, "top", 1),
            "right": _arg(args, kwargs, "right", 2),
            "bottom": _arg(args, kwargs, "bottom", 3),
            "bgcolor": _arg(args, kwargs, "bgcolor", None),
            "border_color": _arg(args, kwargs, "border_color", None),
            "text": _arg(args, kwargs, "text", None),
            "deleted": False,
        }
    )
    return identifier


@register("box.delete")
def _box_delete(interp, args, kwargs, node):
    identifier = _int(args, kwargs, "id", 0, -1)
    for box in interp.ctx.boxes:
        if box["id"] == identifier:
            box["deleted"] = True
    return None


@register("table.new")
def _table_new(interp, args, kwargs, node):
    interp.ctx.warn("table.* 暂未渲染，脚本其余部分照常运行")
    return 0


@register("table.cell", "table.merge_cells", "table.set_position", "table.set_bgcolor",
          "table.set_frame_color", "table.set_frame_width", "polyline.new", "polyline.delete")
def _noop(interp, args, kwargs, node):
    return None


# ---------------------------------------------------------------------------
# 日志 / 预警
# ---------------------------------------------------------------------------


@register("log.info", "log.warning", "log.error", "runtime.log")
def _log(interp, args, kwargs, node):
    message = _arg(args, kwargs, "message", 0)
    if message not in (None, ""):
        interp.ctx.warn(str(message))
    return None


@register("runtime.error")
def _runtime_error(interp, args, kwargs, node):
    raise PineRuntimeError(str(_arg(args, kwargs, "message", 0) or "runtime.error"))


@register("alert", "alertcondition")
def _alert(interp, args, kwargs, node):
    return None


@register("max_bars_back", "chart.is_standard", "session.ismarket", "session.ispremarket",
          "session.ispostmarket", "time", "dayofweek_time", "ticker.new", "ticker.modify",
          "ticker.standard", "ticker.heikinashi", "ticker.renko", "ticker.kagi",
          "ticker.linebreak", "ticker.pointfigure", "request.financial", "request.dividends",
          "request.earnings", "request.quandl", "request.economic", "request.seed",
          "request.currency_rate", "timeframe.change", "session.regular", "session.extended")
def _na_stub(interp, args, kwargs, node):
    return None


@register("timeframe.change")
def _timeframe_change(interp, args, kwargs, node):
    return False


# ---------------------------------------------------------------------------
# 数组
# ---------------------------------------------------------------------------


@register("array.new_float", "array.new_int", "array.new_bool", "array.new_string", "array.new_color")
def _array_new(interp, args, kwargs, node):
    size = max(0, _int(args, kwargs, "size", 0, 0))
    initial = _arg(args, kwargs, "initial_value", 1, 0 if "float" not in (getattr(node, "_called", "") or "") else 0.0)
    return [initial for _ in range(size)]


@register("array.from")
def _array_from(interp, args, kwargs, node):
    return [view.now for view in args]


def _array_of(kwargs, args):
    holder = kwargs.get("id")
    if holder is None:
        holder = args[0] if args else None
    value = holder.now if holder is not None else None
    if not isinstance(value, list):
        raise PineRuntimeError("array 函数需要传入数组变量")
    return value


@register("array.push")
def _array_push(interp, args, kwargs, node):
    array = _array_of(kwargs, args)
    array.append(_arg(args, kwargs, "value", 1))
    return None


@register("array.get")
def _array_get(interp, args, kwargs, node):
    array = _array_of(kwargs, args)
    index = _int(args, kwargs, "index", 1, 0)
    return array[index] if 0 <= index < len(array) else None


@register("array.set")
def _array_set(interp, args, kwargs, node):
    array = _array_of(kwargs, args)
    index = _int(args, kwargs, "index", 1, 0)
    if 0 <= index < len(array):
        array[index] = _arg(args, kwargs, "value", 2)
    return None


@register("array.size")
def _array_size(interp, args, kwargs, node):
    return len(_array_of(kwargs, args))


@register("array.clear")
def _array_clear(interp, args, kwargs, node):
    _array_of(kwargs, args).clear()
    return None


@register("array.pop")
def _array_pop(interp, args, kwargs, node):
    array = _array_of(kwargs, args)
    return array.pop() if array else None


@register("array.shift")
def _array_shift(interp, args, kwargs, node):
    array = _array_of(kwargs, args)
    return array.pop(0) if array else None


@register("array.unshift")
def _array_unshift(interp, args, kwargs, node):
    _array_of(kwargs, args).insert(0, _arg(args, kwargs, "value", 1))
    return None


@register("array.remove")
def _array_remove(interp, args, kwargs, node):
    array = _array_of(kwargs, args)
    index = _int(args, kwargs, "index", 1, 0)
    return array.pop(index) if 0 <= index < len(array) else None


@register("array.sum", "array.avg", "array.min", "array.max", "array.median", "array.stdev", "array.range")
def _array_stats(interp, args, kwargs, node):
    array = _array_of(kwargs, args)
    values = [to_number(v) for v in array if not is_na(v)]
    if not values:
        return None
    names = getattr(node, "_called", "") or ""
    if names.endswith("sum"):
        return sum(values)
    if names.endswith("avg"):
        return sum(values) / len(values)
    if names.endswith("min"):
        return min(values)
    if names.endswith("max"):
        return max(values)
    if names.endswith("median"):
        ordered = sorted(values)
        middle = len(ordered) // 2
        return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    if names.endswith("stdev"):
        mean = sum(values) / len(values)
        return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
    return max(values) - min(values)


@register("array.join")
def _array_join(interp, args, kwargs, node):
    array = _array_of(kwargs, args)
    separator = _text(args, kwargs, "separator", 1, ",")
    return separator.join("" if is_na(v) else str(v) for v in array)


# ---------------------------------------------------------------------------
# 判断 / 勾稽
# ---------------------------------------------------------------------------


def _truthy(value: Any) -> bool:
    if is_na(value):
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


# ---------------------------------------------------------------------------
# strategy.*：交易指令与持仓查询
# ---------------------------------------------------------------------------


def _broker(interp):
    broker = interp.ctx.broker
    if broker is None:
        raise PineRuntimeError("strategy.* 只能在 strategy(...) 脚本里使用")
    return broker


@register("strategy.entry")
def _strategy_entry(interp, args, kwargs, node):
    broker = _broker(interp)
    from .broker import Order

    order_id = _text(args, kwargs, "id", 0, "entry")
    direction = int(to_number(_arg(args, kwargs, "direction", 1), 1)) or 1
    qty = _arg(args, kwargs, "qty", 2)
    broker.queue(
        Order(
            kind="entry",
            order_id=order_id,
            direction=1 if direction > 0 else -1,
            qty=to_number(qty) if not is_na(qty) else None,
            limit=_opt(args, kwargs, "limit", 3),
            stop=_opt(args, kwargs, "stop", 4),
            comment=_text(args, kwargs, "comment", None, "") or None,
        )
    )
    return order_id


@register("strategy.order")
def _strategy_order(interp, args, kwargs, node):
    broker = _broker(interp)
    from .broker import Order

    order_id = _text(args, kwargs, "id", 0, "order")
    direction = int(to_number(_arg(args, kwargs, "direction", 1), 1)) or 1
    qty = _arg(args, kwargs, "qty", 2)
    broker.queue(
        Order(
            kind="entry",
            order_id=order_id,
            direction=1 if direction > 0 else -1,
            qty=to_number(qty) if not is_na(qty) else None,
            limit=_opt(args, kwargs, "limit", 3),
            stop=_opt(args, kwargs, "stop", 4),
        )
    )
    return order_id


@register("strategy.close")
def _strategy_close(interp, args, kwargs, node):
    broker = _broker(interp)
    order_id = _arg(args, kwargs, "id", 0)
    entry_id = None if is_na(order_id) or order_id == "" else str(order_id)
    broker.close_position(entry_id, _bool(args, kwargs, "immediately", 1, False))
    return None


@register("strategy.close_all")
def _strategy_close_all(interp, args, kwargs, node):
    _broker(interp).close_position(None, _bool(args, kwargs, "immediately", 0, False))
    return None


@register("strategy.exit")
def _strategy_exit(interp, args, kwargs, node):
    broker = _broker(interp)

    from .broker import Order

    order_id = _text(args, kwargs, "id", 0, "exit")
    from_entry = _arg(args, kwargs, "from_entry", 1)
    qty = _arg(args, kwargs, "qty", 2)
    qty_percent = _arg(args, kwargs, "qty_percent", 3)
    reference = broker.last_close or 0.0
    mintick = interp.ctx.mintick or 0.01

    limit = _opt(args, kwargs, "limit", 6)
    stop = _opt(args, kwargs, "stop", 7)
    profit_ticks = _opt(args, kwargs, "profit", 4)
    loss_ticks = _opt(args, kwargs, "loss", 5)
    if limit is None and profit_ticks is not None:
        limit = reference + profit_ticks * mintick
    if stop is None and loss_ticks is not None:
        stop = reference - loss_ticks * mintick

    broker.queue(
        Order(
            kind="exit",
            order_id=order_id,
            direction=0,
            qty=to_number(qty) if not is_na(qty) else None,
            qty_percent=to_number(qty_percent) if not is_na(qty_percent) else None,
            from_entry=str(from_entry) if not is_na(from_entry) and from_entry else None,
            limit=limit,
            stop=stop,
            profit_pct=_opt(args, kwargs, "profit_pct", 13),
            loss_pct=_opt(args, kwargs, "loss_pct", 14),
            trail_points=_opt(args, kwargs, "trail_points", 11),
            trail_price=_opt(args, kwargs, "trail_price", 12),
            trail_offset=_opt(args, kwargs, "trail_offset", 15),
        )
    )
    return None


@register("strategy.cancel")
def _strategy_cancel(interp, args, kwargs, node):
    _broker(interp).cancel(_text(args, kwargs, "id", 0, ""))
    return None


@register("strategy.cancel_all")
def _strategy_cancel_all(interp, args, kwargs, node):
    _broker(interp).cancel_all()
    return None


def _opt(args, kwargs, name: str, index: int | None) -> float | None:
    value = _arg(args, kwargs, name, index)
    if is_na(value):
        return None
    number = to_number(value)
    return number if math.isfinite(number) else None


@register("strategy.opentrades.entry_price", "strategy.opentrades.entry_bar_index",
          "strategy.opentrades.size", "strategy.opentrades.profit",
          "strategy.opentrades.max_runup", "strategy.opentrades.max_drawdown",
          "strategy.opentrades.entry_time")
def _open_trade_field(interp, args, kwargs, node):
    called = str(getattr(node, "_called", "") or "")
    field = called.rsplit(".", 1)[-1]
    number = _arg(args, kwargs, "trade_num", 0)
    broker = _broker(interp)
    return broker.open_trade_field(field, None if is_na(number) else int(to_number(number)))


@register("strategy.closedtrades.entry_price", "strategy.closedtrades.exit_price",
          "strategy.closedtrades.entry_bar_index", "strategy.closedtrades.exit_bar_index",
          "strategy.closedtrades.profit", "strategy.closedtrades.size",
          "strategy.closedtrades.commission", "strategy.closedtrades.entry_time",
          "strategy.closedtrades.exit_time", "strategy.closedtrades.max_runup",
          "strategy.closedtrades.max_drawdown")
def _closed_trade_field(interp, args, kwargs, node):
    called = str(getattr(node, "_called", "") or "")
    field = called.rsplit(".", 1)[-1]
    number = _int(args, kwargs, "trade_num", 0, 0)
    return _broker(interp).closed_trade_field(field, number)


@register("strategy.risk.max_drawdown", "strategy.risk.allow_entry_in",
          "strategy.risk.max_position_size", "strategy.risk.max_intraday_loss",
          "strategy.risk.max_intraday_filled_orders")
def _strategy_risk(interp, args, kwargs, node):
    interp.ctx.warn("strategy.risk.* 暂未生效（不影响其余回测逻辑）")
    return None
