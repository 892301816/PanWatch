"""Pine Script v5 兼容引擎（观潮口径）。

对外只需要记住三件事::

    from src.modules.strategy.pine import compile_source, run_script, signal_rows

- :func:`compile_source` 做语法检查，返回带行列的诊断
- :func:`run_script`    在给定 K 线上执行，产出绘图 / 成交 / 统计
- :func:`signal_rows`   把成交压成 ``template_entry/exit`` 列，喂给现有回测内核

实现要点见 ``docs`` 与 ``_ops/re/pine-engine-spec.md``（逆向自 TideView 观潮）。
"""

from __future__ import annotations

from .engine import (
    ENGINE_NAME,
    PineBar,
    coerce_bars,
    compile_source,
    is_pine_source,
    resample,
    rows_from_result,
    run_script,
    signal_rows,
)
from .errors import PineCompileLimit, PineError, PineRuntimeError, PineSyntaxError
from .parser import parse

__all__ = [
    "ENGINE_NAME",
    "PineBar",
    "PineError",
    "PineSyntaxError",
    "PineRuntimeError",
    "PineCompileLimit",
    "coerce_bars",
    "compile_source",
    "is_pine_source",
    "parse",
    "resample",
    "rows_from_result",
    "run_script",
    "signal_rows",
]
