"""Pine 语法树的节点定义。

刻意保持轻量：只有 dataclass 与一个 ``node_id``（用于给每个表达式挂
「历史序列缓冲区」，实现 ``expr[1]`` 这类写法）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count

_ids = count(1)


@dataclass
class Node:
    line: int = 0
    column: int = 0
    id: int = field(default_factory=lambda: next(_ids))

    def __hash__(self) -> int:  # 便于放进 dict
        return self.id


# --------------------------------------------------------------------------
# 表达式
# --------------------------------------------------------------------------


@dataclass
class Literal(Node):
    value: object = None


@dataclass
class Name(Node):
    name: str = ""


@dataclass
class Member(Node):
    """``obj.name`` —— obj 可能是 Name 或另一个 Member。"""

    obj: Node = None  # type: ignore[assignment]
    name: str = ""

    def dotted(self) -> str | None:
        parts = []
        current: Node | None = self
        while isinstance(current, Member):
            parts.append(current.name)
            current = current.obj
        if not isinstance(current, Name):
            return None
        parts.append(current.name)
        return ".".join(reversed(parts))


@dataclass
class Call(Node):
    func: Node = None  # type: ignore[assignment]
    args: list[Node] = field(default_factory=list)
    kwargs: dict[str, Node] = field(default_factory=dict)


@dataclass
class Unary(Node):
    op: str = ""
    operand: Node = None  # type: ignore[assignment]


@dataclass
class Binary(Node):
    op: str = ""
    left: Node = None  # type: ignore[assignment]
    right: Node = None  # type: ignore[assignment]


@dataclass
class Ternary(Node):
    condition: Node = None  # type: ignore[assignment]
    when_true: Node = None  # type: ignore[assignment]
    when_false: Node = None  # type: ignore[assignment]


@dataclass
class History(Node):
    """``base[n]`` —— 取 n 根之前的值。"""

    base: Node = None  # type: ignore[assignment]
    offset: Node = None  # type: ignore[assignment]


@dataclass
class TupleExpr(Node):
    items: list[Node] = field(default_factory=list)


@dataclass
class IfExpr(Node):
    """``if``/``switch`` 作为表达式时使用（分支体最后一个表达式即返回值）。"""

    branches: list = field(default_factory=list)  # list[tuple[cond|None, Block]]
    kind: str = "if"
    subject: Node | None = None


# --------------------------------------------------------------------------
# 语句与块
# --------------------------------------------------------------------------


@dataclass
class Block(Node):
    statements: list = field(default_factory=list)
    # 函数体/分支体最后的裸表达式就是返回值
    tail: Node | None = None


@dataclass
class Assign(Node):
    targets: list[str] = field(default_factory=list)
    value: Node | None = None
    declare: bool = True
    var_kind: str | None = None  # 'var' / 'varip'
    type_name: str | None = None


@dataclass
class ExprStmt(Node):
    expr: Node = None  # type: ignore[assignment]


@dataclass
class IfStmt(Node):
    branches: list = field(default_factory=list)  # list[tuple[cond|None, Block]]


@dataclass
class ForStmt(Node):
    var: str = "i"
    start: Node | None = None
    stop: Node | None = None
    step: Node | None = None
    body: Block | None = None


@dataclass
class WhileStmt(Node):
    condition: Node | None = None
    body: Block | None = None


@dataclass
class SwitchStmt(Node):
    subject: Node | None = None
    cases: list = field(default_factory=list)  # list[tuple[list[Node], Block]]
    default: Block | None = None


@dataclass
class FuncDef(Node):
    name: str = ""
    params: list[tuple[str, Node | None]] = field(default_factory=list)
    body: Block | None = None


@dataclass
class ImportStmt(Node):
    module: str = ""
    alias: str | None = None
    parts: list[str] = field(default_factory=list)


@dataclass
class IgnoredStmt(Node):
    """``type`` / ``enum`` / ``method`` 等本次不复刻的声明，解析但不执行。"""

    what: str = ""


@dataclass
class BreakStmt(Node):
    pass


@dataclass
class ContinueStmt(Node):
    pass


@dataclass
class Script:
    """整个脚本：声明 + 顶层语句。"""

    declaration: Call | None = None       # indicator(...) / strategy(...)
    declaration_kind: str = ""            # 'indicator' | 'strategy' | 'library'
    statements: list = field(default_factory=list)
    version: int = 5
    line_count: int = 0
