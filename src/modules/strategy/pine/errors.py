"""Pine 引擎的公共错误类型。

所有解析/编译/运行期错误都归结为 :class:`PineError`，并带上源码行列，
前端可直接把 ``line``/``column`` 映射到编辑器光标位置。
"""

from __future__ import annotations


class PineError(Exception):
    """Pine 脚本的用户可见错误。"""

    def __init__(self, message: str, line: int | None = None, column: int | None = None):
        self.message = message
        self.line = line
        self.column = column
        super().__init__(self.render())

    def render(self) -> str:
        if self.line is None:
            return self.message
        if self.column is None:
            return f"第 {self.line} 行：{self.message}"
        return f"第 {self.line} 行第 {self.column} 列：{self.message}"

    def as_dict(self) -> dict:
        return {"message": self.message, "line": self.line, "column": self.column}


class PineSyntaxError(PineError):
    """语法/词法错误。"""


class PineRuntimeError(PineError):
    """运行期错误（类型不匹配、除零、na 用法错误等）。"""


class PineCompileLimit(PineError):
    """超出编译预算（行数、循环次数、请求次数）。"""
