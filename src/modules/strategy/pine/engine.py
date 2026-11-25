"""Pine 引擎对外入口。

只暴露三个函数：

- :func:`compile_source` —— 语法检查 + 静态信息（标题/类型/参数/绘图）
- :func:`run_script`     —— 在给定 K 线上跑完整脚本，返回绘图 + 成交 + 统计
- :func:`signal_rows`    —— 把回测成交压成 ``template_entry/exit`` 信号列，
                            供现有 A 股回测内核直接复用
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
from dataclasses import dataclass
from typing import Any

from .broker import Broker, BrokerConfig
from .errors import PineCompileLimit, PineError, PineRuntimeError, PineSyntaxError
from .evaluator import Interpreter
from .nodes import Call, Literal, Name, Script
from .parser import parse
from .runtime import PineContext

logger = logging.getLogger(__name__)

ENGINE_NAME = "pine"
MAX_BARS = 6000
MAX_SOURCE = 200_000


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------


@dataclass
class PineBar:
    """引擎内部统一 bar（与 ``backtest.data_adapter.PriceBar`` 同构）。"""

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def coerce_bars(bars: list) -> list[PineBar]:
    """接受 PriceBar / Bar / dict / 任意带属性的对象。"""
    output: list[PineBar] = []
    for bar in bars or []:
        if isinstance(bar, PineBar):
            output.append(bar)
            continue
        if isinstance(bar, dict):
            get = bar.get
        else:
            get = lambda key, default=None, _bar=bar: getattr(_bar, key, default)  # noqa: E731
        try:
            close = float(get("close") or 0)
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        output.append(
            PineBar(
                date=str(get("date") or "")[:10],
                open=float(get("open") or close),
                high=float(get("high") or close),
                low=float(get("low") or close),
                close=close,
                volume=float(get("volume") or 0),
            )
        )
    output.sort(key=lambda item: item.date)
    return output


def _limit_pct(symbol: str) -> float:
    code = str(symbol or "")
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("4", "8", "92")):
        return 0.30
    return 0.10


def _lot_size(symbol: str) -> int:
    return 200 if str(symbol or "").startswith(("688", "689")) else 100


# ---------------------------------------------------------------------------
# 周期重采样（供 request.security 使用）
# ---------------------------------------------------------------------------


def _bucket_key(date: str, timeframe: str) -> str:
    try:
        year, month, day = int(date[0:4]), int(date[5:7]), int(date[8:10])
        parsed = _dt.date(year, month, day)
    except Exception:  # pragma: no cover
        return date
    if timeframe in ("W", "1W"):
        iso = parsed.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if timeframe in ("M", "1M"):
        return f"{year}-{month:02d}"
    if timeframe in ("Q", "1Q"):
        return f"{year}-Q{(month - 1) // 3 + 1}"
    if timeframe in ("Y", "1Y", "12M"):
        return str(year)
    if timeframe.isdigit():
        minutes = int(timeframe)
        slot = (parsed.toordinal() * 1440 + (0)) // max(1, minutes)
        return f"{slot}"
    return date


def resample(bars: list[PineBar], timeframe: str) -> tuple[list[PineBar], list[int]]:
    """把日线聚合到更大周期，返回 ``(htf_bars, bucket_of_main_bar)``。"""
    if timeframe in ("D", "1D", ""):
        return list(bars), list(range(len(bars)))
    buckets: list[str] = []
    groups: list[list[int]] = []
    for index, bar in enumerate(bars):
        key = _bucket_key(bar.date, timeframe)
        if buckets and buckets[-1] == key:
            groups[-1].append(index)
        else:
            buckets.append(key)
            groups.append([index])
    htf: list[PineBar] = []
    for group in groups:
        first, last = bars[group[0]], bars[group[-1]]
        htf.append(
            PineBar(
                date=last.date,
                open=first.open,
                high=max(bars[i].high for i in group),
                low=min(bars[i].low for i in group),
                close=last.close,
                volume=sum(bars[i].volume for i in group),
            )
        )
    mapping = [0] * len(bars)
    for bucket, group in enumerate(groups):
        for index in group:
            mapping[index] = bucket
    return htf, mapping


# ---------------------------------------------------------------------------
# 编译
# ---------------------------------------------------------------------------


def compile_source(source: str) -> dict:
    """语法检查，返回诊断信息与静态概要（不执行）。"""
    try:
        script = parse(str(source or ""))
    except PineError as error:
        return {
            "ok": False,
            "diagnostics": [{"line": error.line, "column": error.column, "message": error.message}],
            "title": None,
            "kind": None,
            "inputs": [],
            "plots": [],
            "warnings": [],
        }
    warnings: list[str] = []
    kind = script.declaration_kind or ""
    title = None
    if script.declaration is not None and script.declaration.args:
        first = script.declaration.args[0]
        if isinstance(first, Literal):
            title = str(first.value)
    if not kind:
        warnings.append("脚本缺少 indicator(...) 或 strategy(...) 声明，将按指标脚本执行")
    if script.declaration is not None:
        for key in ("initial_capital", "commission_value"):
            if key in script.declaration.kwargs and not isinstance(script.declaration.kwargs[key], Literal):
                warnings.append(f"`{key}` 建议使用字面量，否则回测参数可能无法静态读取")
    return {
        "ok": True,
        "diagnostics": [],
        "title": title,
        "kind": kind or "indicator",
        "inputs": [],
        "plots": [],
        "warnings": warnings,
        "statement_count": len(script.statements),
    }


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def _make_gate(symbol: str):
    limit_pct = _limit_pct(symbol)

    def gate(side: str, bar: Any, previous_close: float | None) -> str | None:
        volume = float(getattr(bar, "volume", 0) or 0)
        if volume <= 0:
            return "suspended_or_zero_volume"
        if previous_close is None or previous_close <= 0:
            return None
        upper = round(previous_close * (1 + limit_pct), 2)
        lower = round(previous_close * (1 - limit_pct), 2)
        if side == "buy" and float(bar.open) >= upper - 1e-8:
            return "limit_up"
        if side == "sell" and float(bar.open) <= lower + 1e-8:
            return "limit_down"
        return None

    return gate


def _security_provider(bars: list[PineBar], symbol: str, max_requests: int = 12):
    cache: dict[tuple, list] = {}
    allowed_tf = {"D", "1D", "W", "1W", "M", "1M", "Q", "1Q", "Y", "1Y"}

    def provider(interp, requested_symbol: str, timeframe: str, expression, lookahead_on: bool):
        code = str(requested_symbol or symbol)
        code = code.split(":")[-1] if ":" in code else code
        base = code.split(".")[0]
        if base and base != symbol and not base.startswith(symbol):
            interp.ctx.warn(f"request.security 跨品种 `{requested_symbol}` 暂不可用（当前仅支持本品种），返回 na")
            return None
        if timeframe not in allowed_tf:
            interp.ctx.warn(f"request.security 暂不支持周期 `{timeframe}`，返回 na")
            return None
        key = (timeframe, expression.id, bool(lookahead_on))
        if key in cache:
            values = cache[key]
        else:
            if len(cache) >= max_requests:
                interp.ctx.warn("request.security 调用过多，超出上限后返回 na")
                return None
            htf, mapping = resample(bars, timeframe)
            if len(htf) < 2:
                return None
            sub_ctx = PineContext(
                htf,
                symbol=symbol,
                market=interp.ctx.market,
                timeframe=timeframe,
                mintick=interp.ctx.mintick,
            )
            sub_script = Script()
            sub = Interpreter(sub_ctx, sub_script)
            sub.ctx.input_overrides = interp.ctx.input_overrides
            series: list[Any] = []
            for index in range(len(htf)):
                sub_ctx.index = index
                try:
                    series.append(sub.eval(expression))
                except PineError:
                    series.append(None)
            values = [None] * len(bars)
            for index in range(len(bars)):
                bucket = mapping[index]
                position = bucket if lookahead_on else bucket - 1
                if 0 <= position < len(series):
                    values[index] = series[position]
            cache[key] = values
        return values[interp.ctx.index] if 0 <= interp.ctx.index < len(values) else None

    return provider


def _stats(trades: list[dict], equity: list[float], initial: float, dates: list[str] | None = None) -> dict:
    from .statistics import build_stats

    summary = build_stats(trades, equity, initial_capital=initial, dates=dates)
    return summary


def run_script(
    source: str,
    bars: list,
    *,
    symbol: str = "",
    market: str = "CN",
    timeframe: str = "D",
    inputs: dict | None = None,
    initial_capital: float | None = None,
    commission_value: float | None = None,
    fill_next_open: bool = True,
) -> dict:
    """执行脚本，返回绘图 + 成交 + 统计（失败时抛 :class:`PineError`）。"""
    if not isinstance(source, str):
        raise PineSyntaxError("脚本必须是字符串")
    if len(source) > MAX_SOURCE:
        raise PineSyntaxError(f"脚本过长（上限 {MAX_SOURCE // 1000}KB）")
    script = parse(source)
    price_bars = coerce_bars(bars)
    if len(price_bars) < 2:
        raise PineRuntimeError("K 线不足 2 根，无法运行脚本")
    if len(price_bars) > MAX_BARS:
        price_bars = price_bars[-MAX_BARS:]

    is_strategy = script.declaration_kind == "strategy"
    broker: Broker | None = None
    if is_strategy:
        config = BrokerConfig(
            initial_capital=float(initial_capital or 100_000.0),
            mintick=0.01,
            lot_size=_lot_size(symbol),
            default_qty_type="percent_of_equity",
            default_qty_value=100.0,
            pyramiding=1,
        )
        if commission_value is not None:
            config.commission_type = "percent"
            config.commission_value = float(commission_value)
        broker = Broker(config, gate=_make_gate(symbol))

    ctx = PineContext(
        price_bars,
        symbol=symbol,
        market=market,
        timeframe=timeframe,
        inputs=inputs,
        broker=broker,
        mintick=0.01,
    )
    interpreter = Interpreter(ctx, script)
    interpreter.security_provider = _security_provider(price_bars, symbol)
    result = interpreter.run()

    if broker is not None:
        report = broker.as_report()
        initial = broker.config.initial_capital
        result["trades"] = report["trades"]
        result["orders"] = report["orders"]
        result["equity_curve"] = report["equity_curve"]
        result["rejections"] = report["rejections"]
        result["open_trades"] = report["open_trades"]
        result["stats"] = _stats(
            report["trades"], report["equity_curve"], initial, [bar.date for bar in price_bars]
        )
        result["broker"] = {
            "initial_capital": initial,
            "commission_type": broker.config.commission_type,
            "commission_value": broker.config.commission_value,
            "final_equity": round(report["equity_curve"][-1], 2) if report["equity_curve"] else initial,
        }
    else:
        result["trades"] = []
        result["orders"] = []
        result["equity_curve"] = []
        result["stats"] = {}
    result["engine"] = ENGINE_NAME
    result["bars"] = [bar.date for bar in price_bars]
    return result


def rows_from_result(result: dict, bars: list) -> list[dict]:
    """把执行结果压成图表行（行情 + ``template_entry/exit`` + plot 列）。"""
    price_bars = coerce_bars(bars)
    count = len(price_bars)
    entry = [0] * count
    exit_signal = [0] * count
    for order in result.get("orders", []):
        index = int(order.get("index", 0))
        target = index - 1
        if not (0 <= target < count):
            continue
        if order.get("action") == "entry":
            entry[target] = 1
        else:
            exit_signal[target] = 1
    plot_columns = {plot["name"]: plot.get("values") or [] for plot in result.get("plots", [])}
    rows: list[dict] = []
    for index in range(count):
        bar = price_bars[index]
        row: dict[str, Any] = {
            "date": bar.date,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "template_entry": entry[index] or None,
            "template_exit": exit_signal[index] or None,
        }
        for name, values in plot_columns.items():
            row[name] = values[index] if index < len(values) else None
        rows.append(row)
    return rows


def signal_rows(source: str, bars: list, **kwargs) -> list[dict]:
    """把 Pine 执行结果压成图表行。

    与现有回测内核的约定保持一致：**信号在产生它的那根 bar 上打点**，
    成交由内核在下一根开盘完成。Pine 撮合器在 i+1 开盘成交，所以信号 bar = i。
    """
    result = run_script(source, bars, **kwargs)
    return rows_from_result(result, bars)


def is_pine_source(source: str) -> bool:
    """判断脚本是否应按 Pine 引擎执行（否则走旧的 4 行 DSL）。

    真脚本一定有 ``//@version`` 或 ``indicator(...)`` / ``strategy(...)`` /
    ``library(...)`` 声明；只有片段（如单独一行 ``ta.rsi(close,14)``）不算，
    否则旧 DSL 的错误提示会被 Pine 吞掉。
    """
    if not isinstance(source, str):
        return False
    text = source.strip()
    if not text:
        return False
    head = text.splitlines()[0].strip().lower()
    if head.startswith("//@version"):
        return True
    lowered = text.lower()
    return any(marker in lowered for marker in ("indicator(", "strategy(", "library("))
