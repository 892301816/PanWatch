"""Pine 运行时：逐 bar 重放脚本。

核心约定
--------
- 每个变量名对应一条 :class:`Series`，``set(i, v)`` 写入当根 bar；没写到的
  bar 自动沿用上一根的值（等同 Pine 的「保持上次赋值」）。
- 每个内置函数调用点的每个实参也有一条序列，因此 ``ta.sma(close, 20)``
  天然拥有历史，无需指标函数自己再存窗口。
- ``var`` / ``varip`` 只在第一根 bar 初始化，之后靠 ``:=`` 修改。
- na 用 ``None``；算术遇到 na 传染为 na，除零为 na。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from . import nodes as N
from .errors import PineCompileLimit, PineRuntimeError
from .series import NA, ArgView, Series, SiteState, is_na, to_number

MAX_LOOP_ITERATIONS = 100_000
MAX_PLOT_COUNT = 64


# ---------------------------------------------------------------------------
# 常量表
# ---------------------------------------------------------------------------

COLORS = {
    "red": "#F23645", "green": "#089981", "blue": "#2962FF", "white": "#FFFFFF",
    "black": "#000000", "orange": "#FF9800", "yellow": "#FDD835", "purple": "#9C27B0",
    "lime": "#00E676", "aqua": "#00BCD4", "teal": "#00897B", "maroon": "#880E4F",
    "olive": "#808000", "navy": "#000080", "fuchsia": "#E040FB", "silver": "#C0C0C0",
    "gray": "#787B86", "grey": "#787B86",
}

CONSTANTS: dict[str, Any] = {
    "strategy.long": 1,
    "strategy.short": -1,
    "strategy.direction.long": 1,
    "strategy.direction.short": -1,
    "strategy.direction.all": 0,
    "strategy.percent_of_equity": "percent_of_equity",
    "strategy.fixed": "fixed",
    "strategy.cash": "cash",
    "strategy.commission.percent": "percent",
    "strategy.commission.cash_per_contract": "cash_per_contract",
    "strategy.commission.cash_per_order": "cash_per_order",
    "strategy.oca.cancel": "cancel",
    "strategy.oca.reduce": "reduce",
    "strategy.oca.none": "none",
    "strategy.risk.allow_entry_in": "allow_entry_in",
    "plot.style_line": "line",
    "plot.style_stepline": "stepline",
    "plot.style_histogram": "histogram",
    "plot.style_cross": "cross",
    "plot.style_area": "area",
    "plot.style_columns": "columns",
    "plot.style_circles": "circles",
    "plot.style_linebr": "linebr",
    "plot.style_areabr": "areabr",
    "plot.style_steplinebr": "steplinebr",
    "plot.style_stepline_diamond": "stepline_diamond",
    "shape.xcross": "xcross",
    "shape.cross": "cross",
    "shape.triangleup": "triangleup",
    "shape.triangledown": "triangledown",
    "shape.flag": "flag",
    "shape.circle": "circle",
    "shape.arrowup": "arrowup",
    "shape.arrowdown": "arrowdown",
    "shape.labelup": "labelup",
    "shape.labeldown": "labeldown",
    "shape.square": "square",
    "shape.diamond": "diamond",
    "location.abovebar": "abovebar",
    "location.belowbar": "belowbar",
    "location.top": "top",
    "location.bottom": "bottom",
    "location.absolute": "absolute",
    "size.tiny": "tiny",
    "size.small": "small",
    "size.normal": "normal",
    "size.large": "large",
    "size.huge": "huge",
    "size.auto": "auto",
    "extend.none": "none",
    "extend.left": "left",
    "extend.right": "right",
    "extend.both": "both",
    "barmerge.gaps_off": "gaps_off",
    "barmerge.gaps_on": "gaps_on",
    "barmerge.lookahead_off": "lookahead_off",
    "barmerge.lookahead_on": "lookahead_on",
    "alert.freq_all": "all",
    "alert.freq_once_per_bar": "once_per_bar",
    "alert.freq_once_per_bar_close": "once_per_bar_close",
    "format.mintick": "mintick",
    "format.percent": "percent",
    "format.volume": "volume",
    "xloc.bar_index": "bar_index",
    "xloc.bar_time": "bar_time",
    "yloc.price": "price",
    "yloc.abovebar": "abovebar",
    "yloc.belowbar": "belowbar",
    "display.none": "none",
    "display.all": "all",
    "math.pi": math.pi,
    "math.e": math.e,
    "math.phi": 1.618033988749895,
    "math.rphi": 0.6180339887498949,
    "dayofweek.sunday": 1,
    "dayofweek.monday": 2,
    "dayofweek.tuesday": 3,
    "dayofweek.wednesday": 4,
    "dayofweek.thursday": 5,
    "dayofweek.friday": 6,
    "dayofweek.saturday": 7,
    "adjustment.none": "none",
    "adjustment.splits": "splits",
    "adjustment.dividends": "dividends",
}

for _name, _hex in COLORS.items():
    CONSTANTS[f"color.{_name}"] = _hex

# 动态成员：值依赖当前 bar / 标的，按 ctx 求值
DYNAMIC_MEMBERS: dict[str, Callable[["PineContext"], Any]] = {}


def dynamic(name: str):
    def register(function: Callable[["PineContext"], Any]):
        DYNAMIC_MEMBERS[name] = function
        return function

    return register


# ---------------------------------------------------------------------------
# 绘图产物
# ---------------------------------------------------------------------------


@dataclass
class PlotSpec:
    name: str
    overlay: bool
    style: str = "line"
    color: Any = None
    linewidth: int = 1
    histogram_base: float | None = None
    values: Series = field(default_factory=Series)
    visible: bool = True

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "overlay": self.overlay,
            "style": self.style,
            "color": self.color,
            "linewidth": self.linewidth,
            "values": [None if is_na(v) else round(float(v), 6) if isinstance(v, (int, float)) else v for v in self.values.values],
        }


# ---------------------------------------------------------------------------
# 运行上下文
# ---------------------------------------------------------------------------


class PineContext:
    """一次脚本执行的全部状态。"""

    def __init__(
        self,
        bars: list,
        *,
        symbol: str = "",
        market: str = "CN",
        timeframe: str = "D",
        inputs: dict | None = None,
        broker: Any = None,
        mintick: float = 0.01,
        max_requests: int = 12,
    ) -> None:
        self.bars = bars
        self.count = len(bars)
        self.index = -1
        self.symbol = symbol
        self.market = market
        self.timeframe = timeframe
        self.mintick = mintick
        self.input_overrides = inputs or {}
        self.broker = broker

        self.variables: dict[str, Series] = {}
        self.functions: dict[str, N.FuncDef] = {}
        self.imports: dict[str, str] = {}
        self.node_buffers: dict[tuple[int, int], Series] = {}
        self.site_states: dict[int, SiteState] = {}
        self.frames: list[dict[str, Series]] = []

        self.plots: dict[str, PlotSpec] = {}
        self.plot_order: list[str] = []
        self.shapes: list[dict] = []
        self.hlines: list[dict] = []
        self.fills: list[dict] = []
        self.backgrounds: Series = Series()
        self.bar_colors: Series = Series()
        self.labels: list[dict] = []
        self.lines: list[dict] = []
        self.boxes: list[dict] = []
        self.warnings: list[str] = []
        self.inputs_meta: list[dict] = []
        self._input_names: set[str] = set()
        self.overlay = True
        self.is_strategy = False
        self.loop_budget = MAX_LOOP_ITERATIONS

        self._price_series = {
            "open": [bar.open for bar in bars],
            "high": [bar.high for bar in bars],
            "low": [bar.low for bar in bars],
            "close": [bar.close for bar in bars],
            "volume": [bar.volume for bar in bars],
        }
        self._times = [_bar_time(bar) for bar in bars]
        self._dates = [str(getattr(bar, "date", "")) for bar in bars]

    # -- bar 相关 ------------------------------------------------------
    @property
    def bar(self) -> Any:
        return self.bars[self.index]

    def series(self, key: str) -> list:
        return self._price_series[key]

    def value(self, key: str) -> float:
        if self.index < 0:
            return 0.0
        return self._price_series[key][self.index]

    def previous(self, key: str, offset: int = 1) -> float | None:
        position = self.index - offset
        if position < 0:
            return None
        return self._price_series[key][position]

    # -- 变量 ----------------------------------------------------------
    def declare(self, name: str) -> Series:
        series = self.variables.get(name)
        if series is None:
            series = Series()
            self.variables[name] = series
        return series

    def set_variable(self, name: str, value: Any) -> None:
        self.declare(name).set(self.index, value)

    def get_variable(self, name: str) -> Any:
        series = self.variables.get(name)
        return None if series is None else series.current

    def frame(self) -> dict[str, Series]:
        return self.frames[-1] if self.frames else self.variables

    def warn(self, message: str) -> None:
        if message not in self.warnings and len(self.warnings) < 40:
            self.warnings.append(message)

    # -- 内存预算 ------------------------------------------------------
    def budget(self, size: int) -> None:
        self.loop_budget -= size
        if self.loop_budget < 0:
            raise PineCompileLimit("脚本循环/请求次数超出预算，请减少循环或缩短回测区间")

    def plot(self, name: str, overlay: bool) -> PlotSpec:
        spec = self.plots.get(name)
        if spec is None:
            if len(self.plots) >= MAX_PLOT_COUNT:
                raise PineCompileLimit(f"图上最多 {MAX_PLOT_COUNT} 条序列，请精简 plot 数量")
            spec = PlotSpec(name=name, overlay=overlay)
            self.plots[name] = spec
            self.plot_order.append(name)
        return spec


def _bar_time(bar: Any) -> int:
    """把 YYYY-MM-DD / 时间戳统一成毫秒时间戳（Pine 的 time 是毫秒）。"""
    date = str(getattr(bar, "date", "") or "")
    try:
        if len(date) >= 10:
            year, month, day = int(date[0:4]), int(date[5:7]), int(date[8:10])
            import datetime as _dt

            return int(_dt.datetime(year, month, day, tzinfo=_dt.timezone.utc).timestamp() * 1000)
    except Exception:  # pragma: no cover - 日期异常时退化为 0
        pass
    return 0


# ---------------------------------------------------------------------------
# 动态成员
# ---------------------------------------------------------------------------


@dynamic("barstate.islast")
def _(ctx: PineContext):
    return ctx.index == ctx.count - 1


@dynamic("barstate.isfirst")
def _(ctx: PineContext):
    return ctx.index == 0


@dynamic("barstate.ishistory")
def _(ctx: PineContext):
    return True


@dynamic("barstate.isrealtime")
def _(ctx: PineContext):
    return False


@dynamic("barstate.isconfirmed")
def _(ctx: PineContext):
    return True


@dynamic("barstate.isnew")
def _(ctx: PineContext):
    return True


@dynamic("barstate.islastconfirmedhistory")
def _(ctx: PineContext):
    return ctx.index == ctx.count - 1


@dynamic("timeframe.period")
def _(ctx: PineContext):
    return ctx.timeframe


@dynamic("timeframe.multiplier")
def _(ctx: PineContext):
    digits = "".join(character for character in ctx.timeframe if character.isdigit())
    return int(digits) if digits else 1


@dynamic("timeframe.isdaily")
def _(ctx: PineContext):
    return ctx.timeframe in ("D", "1D")


@dynamic("timeframe.isweekly")
def _(ctx: PineContext):
    return ctx.timeframe in ("W", "1W")


@dynamic("timeframe.ismonthly")
def _(ctx: PineContext):
    return ctx.timeframe in ("M", "1M")


@dynamic("timeframe.isintraday")
def _(ctx: PineContext):
    return ctx.timeframe.isdigit()


@dynamic("timeframe.isdwm")
def _(ctx: PineContext):
    return ctx.timeframe in ("D", "W", "M", "1D", "1W", "1M")


@dynamic("timeframe.isseconds")
def _(ctx: PineContext):
    return False


@dynamic("timeframe.isminutes")
def _(ctx: PineContext):
    return ctx.timeframe.isdigit()


@dynamic("syminfo.ticker")
def _(ctx: PineContext):
    return ctx.symbol


@dynamic("syminfo.tickerid")
def _(ctx: PineContext):
    return ctx.symbol


@dynamic("syminfo.prefix")
def _(ctx: PineContext):
    return "SZ" if ctx.symbol.startswith(("0", "1", "3")) else "SH"


@dynamic("syminfo.currency")
def _(ctx: PineContext):
    return "CNY"


@dynamic("syminfo.type")
def _(ctx: PineContext):
    return "stock"


@dynamic("syminfo.description")
def _(ctx: PineContext):
    return ctx.symbol


@dynamic("syminfo.mintick")
def _(ctx: PineContext):
    return ctx.mintick


@dynamic("syminfo.pointvalue")
def _(ctx: PineContext):
    return 1


@dynamic("syminfo.session")
def _(ctx: PineContext):
    return "0930-1130,1300-1500"


@dynamic("syminfo.timezone")
def _(ctx: PineContext):
    return "Asia/Shanghai"


@dynamic("syminfo.root")
def _(ctx: PineContext):
    return ctx.symbol


@dynamic("syminfo.basecurrency")
def _(ctx: PineContext):
    return "CNY"


@dynamic("syminfo.pricescale")
def _(ctx: PineContext):
    return 100


@dynamic("syminfo.minmove")
def _(ctx: PineContext):
    return ctx.mintick


@dynamic("syminfo.minmove2")
def _(ctx: PineContext):
    return ctx.mintick


@dynamic("syminfo.volumetype")
def _(ctx: PineContext):
    return "base"


@dynamic("time")
def _(ctx: PineContext):
    return ctx._times[ctx.index] if ctx.index >= 0 else 0


@dynamic("time_close")
def _(ctx: PineContext):
    return ctx._times[ctx.index] if ctx.index >= 0 else 0


@dynamic("timenow")
def _(ctx: PineContext):
    import time as _time

    return int(_time.time() * 1000)


@dynamic("bar_index")
def _(ctx: PineContext):
    return ctx.index


@dynamic("last_bar_index")
def _(ctx: PineContext):
    return ctx.count - 1


@dynamic("chart.bg_color")
def _(ctx: PineContext):
    return "#131722"


@dynamic("chart.fg_color")
def _(ctx: PineContext):
    return "#D1D4DC"


@dynamic("dayofweek")
def _(ctx: PineContext):
    return _weekday(ctx)


@dynamic("hour")
def _(ctx: PineContext):
    return 9


@dynamic("minute")
def _(ctx: PineContext):
    return 30


@dynamic("year")
def _(ctx: PineContext):
    return _date_part(ctx, 0)


@dynamic("month")
def _(ctx: PineContext):
    return _date_part(ctx, 1)


@dynamic("dayofmonth")
def _(ctx: PineContext):
    return _date_part(ctx, 2)


def _weekday(ctx: PineContext) -> int:
    import datetime as _dt

    date = ctx._dates[ctx.index] if ctx.index >= 0 else ""
    try:
        parsed = _dt.date.fromisoformat(date[:10])
        return (parsed.weekday() + 1) % 7 + 1  # Pine: Sunday=1
    except Exception:  # pragma: no cover
        return 0


def _date_part(ctx: PineContext, position: int) -> int:
    date = ctx._dates[ctx.index] if ctx.index >= 0 else ""
    try:
        return int(date[0:4]) if position == 0 else int(date[5:7]) if position == 1 else int(date[8:10])
    except Exception:  # pragma: no cover
        return 0
