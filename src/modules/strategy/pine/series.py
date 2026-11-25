"""Pine 的「序列」基础设施。

Pine 的执行模型是「逐根 K 线重放整个脚本」，所以脚本里每个变量、每个
内置函数调用点的实参，本质上都是一条序列。这里用最小的对象把这件事
表达清楚：

- :class:`Series` —— 一条随 bar 增长的序列，支持 ``[n]`` 回看与 na 对齐。
- :class:`ArgView` —— 内置函数的实参视图，既能取当前值也能回看历史。

na 用 ``None`` 表示（Pine 的 na 不区分空数字与空字符串）。
"""

from __future__ import annotations

import math
from typing import Any

NA = None


def is_na(value: Any) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def to_number(value: Any, default: float = 0.0) -> float:
    """Pine 在算术里把 na 当「传染性空值」，这里显式转成 float 便于计算。"""
    if is_na(value) or isinstance(value, bool):
        return default
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class Series:
    """一条按 bar 递增的序列。

    ``set(index, value)`` 允许当前 bar 还没被赋值（例如 ``if`` 分支没走到），
    此时序列沿用上一根的值 —— 与 Pine 里变量「保持上次赋值」一致。
    """

    __slots__ = ("values", "last_index")

    def __init__(self) -> None:
        self.values: list[Any] = []
        self.last_index: int = -1

    # -- 写入 ---------------------------------------------------------
    def set(self, index: int, value: Any) -> None:
        if index <= self.last_index:
            # 同一根 bar 内重复赋值（循环/多分支）：覆盖当根
            if self.values:
                self.values[-1] = value
            else:  # pragma: no cover - 理论不可达
                self.values.append(value)
            return
        previous = self.values[-1] if self.values else None
        gap = index - self.last_index - 1
        if gap > 0:
            self.values.extend([previous] * gap)
        self.values.append(value)
        self.last_index = index

    def init(self) -> None:
        """把尚未开始写的序列在首个 bar 上初始化为 na。"""
        if self.last_index < 0:
            self.values.append(None)
            self.last_index = 0

    # -- 读取 ---------------------------------------------------------
    def at(self, offset: int = 0) -> Any:
        """``offset`` 为 0 表示当前 bar，1 表示上一根。"""
        if offset < 0:
            return None
        position = len(self.values) - 1 - offset
        if position < 0:
            return None
        return self.values[position]

    @property
    def current(self) -> Any:
        return self.values[-1] if self.values else None

    def __len__(self) -> int:
        return len(self.values)


class ArgView:
    """内置函数实参：当前值 + 历史回看。"""

    __slots__ = ("series", "now")

    def __init__(self, series: Series, now: Any) -> None:
        self.series = series
        self.now = now

    def __getitem__(self, offset: int) -> Any:
        return self.series.at(offset)

    def history(self, length: int) -> list[Any]:
        """取最近 ``length`` 个值（含当前），不足补 na。"""
        if length <= 0:
            return []
        available = len(self.series.values)
        take = min(length, available)
        values = self.series.values[available - take:] if take else []
        if take < length:
            values = [None] * (length - take) + list(values)
        return values

    def as_float(self, default: float = 0.0) -> float:
        return to_number(self.now, default)

    def as_int(self, default: int = 0) -> int:
        return int(round(to_number(self.now, default)))

    def as_bool(self, default: bool = False) -> bool:
        if is_na(self.now):
            return default
        return bool(self.now)


class SiteState:
    """内置函数调用点的持久状态（递归类指标在这里存上一根的结果）。"""

    __slots__ = ("values", "last_index", "env")

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.last_index: int = -1
        # 引擎注入的行情上下文（open/high/low/close/volume 的 ArgView）
        self.env: dict[str, "ArgView"] = {}
