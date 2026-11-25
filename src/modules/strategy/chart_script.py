"""图表工作台脚本运行时。

同一个文件现在承载**两套引擎**，由 :func:`compile_script` 自动路由：

1. ``v1`` 四语句 DSL —— ``FAST/SLOW/BUY/SELL``，纯正则、无代码执行面，
   历史草稿与既有报告继续按它解释。
2. ``pine`` —— Pine Script v5 子集（见 :mod:`src.modules.strategy.pine`），
   只要脚本里出现 ``//@version`` / ``indicator(...)`` / ``strategy(...)`` /
   ``ta.*`` / ``plot(...)`` 就走它。

对外四个函数名保持不变，避免动 ``playbooks`` / ``single`` / ``chart_writer``
的调用链。
"""

from __future__ import annotations

import re

from src.modules.strategy.pine import (
    PineError,
    compile_source as pine_compile_source,
    is_pine_source,
)

LEGACY_MAX_LENGTH = 2000
PINE_MAX_LENGTH = 200_000
INTERVAL_TIMEFRAME = {"1d": "D", "1w": "W", "1m": "M"}


def compile_script(source):
    """校验脚本；Pine 返回 ``{"engine": "pine", ...}``，旧 DSL 返回原元组。"""
    if is_pine_source(source):
        if len(source) > PINE_MAX_LENGTH:
            raise ValueError(f"Pine 脚本过长（上限 {PINE_MAX_LENGTH // 1000}KB）")
        result = pine_compile_source(source)
        if not result["ok"]:
            diagnostic = result["diagnostics"][0]
            raise ValueError(f"第 {diagnostic['line']} 行第 {diagnostic['column']} 列：{diagnostic['message']}")
        return {"engine": "pine", **result}

    if not isinstance(source, str) or len(source) > LEGACY_MAX_LENGTH:
        raise ValueError("脚本必须是 2000 字以内的文本")
    lines = source.strip().upper().splitlines()
    if len(lines) != 4:
        lowered = source.lower()
        if any(marker in lowered for marker in ("ta.", "plot(", "input.", "//@version")):
            raise ValueError(
                "脚本需要 indicator(...) 或 strategy(...) 声明开头（Pine 脚本），"
                "或者写成 FAST/SLOW/BUY/SELL 四行"
            )
        raise ValueError("脚本需要 FAST、SLOW、BUY、SELL 四行")
    averages = []
    for index, key in enumerate(("FAST", "SLOW")):
        match = re.fullmatch(rf"{key}\s*=\s*(MA|EMA)\s*\(\s*([0-9]{{1,2}})\s*\)", lines[index].strip())
        if not match or not 2 <= int(match[2]) <= 60:
            raise ValueError(f"第 {index+1} 行：{key} 需要 MA(n) 或 EMA(n)，周期为 2–60")
        averages.append((match[1], int(match[2])))
    crosses = []
    for index, key in enumerate(("BUY", "SELL"), 2):
        match = re.fullmatch(rf"{key}\s*=\s*(CROSSUP|CROSSDOWN)\s*\(\s*FAST\s*,\s*SLOW\s*\)", lines[index].strip())
        if not match:
            raise ValueError(f"第 {index+1} 行：使用 {key} = CROSSUP(FAST, SLOW) 或 CROSSDOWN(FAST, SLOW)")
        crosses.append(match[1])
    if crosses[0] == crosses[1]:
        raise ValueError("第 4 行：卖出和买入的交叉方向必须不同")
    return averages, crosses[0]


def series(bars, source, **context):
    """按脚本产出逐 bar 的行（含 ``template_entry/exit`` 与 Pine 的 plot 列）。"""
    rows, _meta = series_with_meta(bars, source, **context)
    return rows


def series_with_meta(bars, source, **context):
    """同 :func:`series`，但额外返回绘图/参数/告警等元数据。"""
    compiled = compile_script(source)
    if isinstance(compiled, dict) and compiled.get("engine") == "pine":
        return _pine_rows(bars, source, **context)
    return _legacy_rows(bars, source), {"engine": "v1", "plots": [], "inputs": [], "warnings": []}


def _pine_rows(bars, source, *, symbol="", market="CN", interval="1d", inputs=None):
    from src.modules.strategy.pine import rows_from_result, run_script

    try:
        result = run_script(
            source,
            bars,
            symbol=str(symbol or ""),
            market=str(market or "CN"),
            timeframe=INTERVAL_TIMEFRAME.get(str(interval), "D"),
            inputs=inputs,
        )
    except PineError as error:
        raise ValueError(error.render()) from error
    rows = rows_from_result(result, bars)
    meta = {
        "engine": "pine",
        "title": result.get("title"),
        "kind": result.get("kind"),
        "overlay": result.get("overlay"),
        "plots": [
            {
                "name": plot["name"],
                "overlay": plot["overlay"],
                "style": plot["style"],
                "color": plot["color"],
                "linewidth": plot["linewidth"],
            }
            for plot in result.get("plots", [])
        ],
        "inputs": result.get("inputs", []),
        "warnings": result.get("warnings", []),
        "hlines": result.get("hlines", []),
        "shapes": len(result.get("shapes", [])),
        "stats": result.get("stats") or {},
        "trades": result.get("trades") or [],
        "equity_curve": result.get("equity_curve") or [],
    }
    return rows, meta


def _legacy_rows(bars, source):
    averages, buy_cross = compile_script(source)
    columns = []
    for kind, n in averages:
        values = []
        rolling = 0.0
        for i, bar in enumerate(bars):
            rolling += bar.close
            if i >= n:
                rolling -= bars[i - n].close
            value = None
            if i >= n - 1:
                value = rolling / n if kind == "MA" or i == n - 1 else values[-1] + 2 / (n + 1) * (bar.close - values[-1])
            values.append(value)
        columns.append(values)
    previous = None
    result = []
    for fast, slow in zip(*columns):
        delta = None if fast is None or slow is None else fast - slow
        buy = sell = None
        if previous is not None and delta is not None:
            up, down = previous <= 0 < delta, previous >= 0 > delta
            buy, sell = (up, down) if buy_cross == "CROSSUP" else (down, up)
        result.append(dict(fast=fast, slow=slow, template_entry=buy, template_exit=sell))
        previous = delta
    return result


def chart_bar(bar, row):
    """单根 bar 的返回结构。

    旧 DSL 只回 4 个字段；Pine 会带任意多的 plot 列，所以这里改成
    「行情字段 + 行里出现过的其余键」的白名单式透传（行情字段以 bar 为准）。
    """
    data = {key: getattr(bar, key) for key in ("date", "open", "high", "low", "close", "volume")}
    for key, value in (row or {}).items():
        if key in data or str(key).startswith("__"):
            continue
        data[key] = value
    return data


def strategy_rows(bars, config, conditions, symbol):
    from src.modules.strategy.patterns import compute_features
    from src.modules.strategy.factor_engine import field_rows
    from src.modules.strategy.template_runtime import attach

    rows = [compute_features(bars, index, extended=False) for index in range(len(bars))]
    if config.get("template_id") or any("tf_" in str(c) for c in conditions):
        for row, extra in zip(rows, field_rows(bars)):
            row.update({key: value for key, value in extra.items() if key.startswith("tf_")})
    return attach(rows, bars, config, symbol)


def describe(source):
    """给前端用的脚本概要（引擎、标题、参数、绘图）。"""
    compiled = compile_script(source)
    if isinstance(compiled, dict) and compiled.get("engine") == "pine":
        return {
            "engine": "pine",
            "title": compiled.get("title"),
            "kind": compiled.get("kind"),
            "warnings": compiled.get("warnings") or [],
        }
    return {"engine": "v1", "title": "四语句脚本", "kind": "strategy", "warnings": []}
