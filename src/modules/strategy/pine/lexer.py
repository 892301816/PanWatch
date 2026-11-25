"""Pine Script v5 词法器。

只做「把源码切成带行列的 token」这一件事，缩进按 Python 风格转成
INDENT/DEDENT，从而让语法分析可以按块结构工作。

支持：
- 注释 ``//...`` 与 ``//@version=5`` 头
- 数字（整数/小数/科学计数）
- 字符串（单双引号，支持 ``\\n`` 等转义）
- 标识符与 ``.`` 成员访问（解析阶段组合）
- 运算符：``:=`` ``=>`` ``==`` ``!=`` ``<=`` ``>=`` ``+=`` ``-=`` ``*=`` ``/=``
- 括号/方括号内的换行视为续行；以运算符、逗号或 ``and``/``or`` 结尾的换行同样续行
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .errors import PineSyntaxError

KEYWORDS = {
    "if",
    "else",
    "for",
    "to",
    "by",
    "while",
    "switch",
    "case",
    "default",
    "var",
    "varip",
    "and",
    "or",
    "not",
    "true",
    "false",
    "na",
    "import",
    "export",
    "type",
    "method",
    "enum",
    "break",
    "continue",
}

# 按长度倒序匹配，避免 ``>=`` 被切成 ``>`` + ``=``
OPERATORS = (
    ":=",
    "=>",
    "==",
    "!=",
    "<=",
    ">=",
    "+=",
    "-=",
    "*=",
    "/=",
    "%=",
    "+",
    "-",
    "*",
    "/",
    "%",
    ">",
    "<",
    "=",
    "(",
    ")",
    "[",
    "]",
    "{",
    "}",
    ",",
    ".",
    ":",
    "?",
)

# 行尾出现这些符号说明语句没写完，下一行需要接续
CONTINUATION_TAIL = {
    "+",
    "-",
    "*",
    "/",
    "%",
    ",",
    "==",
    "!=",
    "<=",
    ">=",
    ">",
    "<",
    "=",
    ":=",
    "?",
    ".",
}
CONTINUATION_KEYWORDS = {"and", "or", "to", "by"}


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    line: int
    column: int

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Token({self.kind},{self.value!r},{self.line}:{self.column})"


def _continues(tokens: List[Token]) -> bool:
    """上一行是否还需要下一行接续（Pine 的隐式续行）。"""
    if not tokens:
        return False
    last = tokens[-1]
    if last.kind == "OP" and last.value in CONTINUATION_TAIL:
        return True
    if last.kind == "KEYWORD" and last.value in CONTINUATION_KEYWORDS:
        return True
    return False


def tokenize(source: str) -> List[Token]:
    if not isinstance(source, str):
        raise PineSyntaxError("脚本必须是字符串")
    # 统一换行，避免 CRLF 让列号错位
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    tokens: List[Token] = []
    indent_stack = [0]
    depth = 0  # 括号嵌套深度
    line_no = 1
    index = 0
    line_start_indent = None
    line_has_token = False
    length = len(source)

    def emit(kind: str, value: str, col: int) -> None:
        tokens.append(Token(kind, value, line_no, col))

    while index < length:
        # ---- 行首缩进处理 ----
        if line_start_indent is None:
            indent = 0
            probe = index
            while probe < length and source[probe] in " \t":
                indent += 4 if source[probe] == "\t" else 1
                probe += 1
            # 空行 / 纯注释行：直接跳到行尾，不参与缩进栈
            if probe >= length or source[probe] == "\n":
                index = probe
                if index < length:
                    index += 1
                    line_no += 1
                continue
            if source.startswith("//", probe):
                while probe < length and source[probe] != "\n":
                    probe += 1
                index = probe + 1 if probe < length else probe
                line_no += 1
                continue
            line_start_indent = indent
            if depth == 0:
                while indent < indent_stack[-1]:
                    indent_stack.pop()
                    emit("DEDENT", "", 0)
                if indent > indent_stack[-1]:
                    indent_stack.append(indent)
                    emit("INDENT", "", indent)
            index = probe

        if index >= length:
            break
        char = source[index]

        # ---- 换行 ----
        if char == "\n":
            if depth > 0 or _continues(tokens):
                # 续行：把换行当空格
                index += 1
                line_no += 1
                line_start_indent = None
                continue
            if line_has_token:
                emit("NEWLINE", "", 0)
                line_has_token = False
            index += 1
            line_no += 1
            line_start_indent = None
            continue

        # ---- 空白 ----
        if char in " \t":
            index += 1
            continue

        # ---- 注释 ----
        if source.startswith("//", index):
            while index < length and source[index] != "\n":
                index += 1
            continue

        column = index - (source.rfind("\n", 0, index) + 1) + 1

        # ---- 字符串 ----
        if char in ("'", '"'):
            quote = char
            cursor = index + 1
            buffer: List[str] = []
            while cursor < length:
                current = source[cursor]
                if current == "\\" and cursor + 1 < length:
                    escaped = source[cursor + 1]
                    buffer.append(
                        {"n": "\n", "t": "\t", "\\": "\\", '"': '"', "'": "'"}.get(escaped, escaped)
                    )
                    cursor += 2
                    continue
                if current == quote:
                    break
                buffer.append(current)
                cursor += 1
            if cursor >= length:
                raise PineSyntaxError("字符串没有闭合", line_no, column)
            emit("STRING", "".join(buffer), column)
            index = cursor + 1
            line_has_token = True
            continue

        # ---- 数字 ----
        if char.isdigit() or (char == "." and index + 1 < length and source[index + 1].isdigit()):
            cursor = index
            seen_dot = False
            seen_exp = False
            while cursor < length:
                current = source[cursor]
                if current.isdigit():
                    cursor += 1
                    continue
                if current == "." and not seen_dot and not seen_exp:
                    if cursor + 1 < length and source[cursor + 1].isdigit():
                        seen_dot = True
                        cursor += 1
                        continue
                    break
                if current in "eE" and not seen_exp and cursor + 1 < length:
                    seen_exp = True
                    cursor += 1
                    if source[cursor] in "+-":
                        cursor += 1
                    continue
                break
            emit("NUMBER", source[index:cursor], column)
            index = cursor
            line_has_token = True
            continue

        # ---- 标识符 / 关键字 ----
        if char.isalpha() or char == "_":
            cursor = index
            while cursor < length and (source[cursor].isalnum() or source[cursor] == "_"):
                cursor += 1
            word = source[index:cursor]
            emit("KEYWORD" if word in KEYWORDS else "IDENT", word, column)
            index = cursor
            line_has_token = True
            continue

        # ---- 运算符 ----
        matched = None
        for operator in OPERATORS:
            if source.startswith(operator, index):
                matched = operator
                break
        if matched is None:
            raise PineSyntaxError(f"无法识别的字符 {char!r}", line_no, column)
        if matched in ("(", "[", "{"):
            depth += 1
        elif matched in (")", "]", "}"):
            depth = max(0, depth - 1)
        emit("OP", matched, column)
        index += len(matched)
        line_has_token = True

    if line_has_token:
        emit("NEWLINE", "", 0)
    while len(indent_stack) > 1:
        indent_stack.pop()
        emit("DEDENT", "", 0)
    emit("EOF", "", 0)
    return tokens
