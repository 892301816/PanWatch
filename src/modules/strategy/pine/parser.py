"""Pine Script v5 递归下降语法分析器。

设计目标：覆盖「写策略/指标真正会用到的 95%」语法，遇到不支持的声明
（``type`` / ``enum`` / ``method``）时**跳过而不是报错**，避免用户脚本
因为边角语法整体跑不起来。

不支持的部分会在 :mod:`runtime` 里以明确的中文错误提示暴露。
"""

from __future__ import annotations

from . import nodes as N
from .errors import PineSyntaxError
from .lexer import Token, tokenize

COMPARISON_OPS = {"==", "!=", "<", ">", "<=", ">="}
ADDITIVE_OPS = {"+", "-"}
MULTIPLICATIVE_OPS = {"*", "/", "%"}
ASSIGN_OPS = {"=", ":=", "+=", "-=", "*=", "/=", "%="}
TYPE_WORDS = {"float", "int", "bool", "string", "color", "line", "label", "box", "table", "array", "matrix"}
DECLARATION_NAMES = {"indicator", "strategy", "library"}
MAX_LINES = 4000


class Parser:
    def __init__(self, tokens: list[Token], source_lines: int = 0):
        self.tokens = tokens
        self.pos = 0
        self.source_lines = source_lines

    # ---------------------------------------------------------------- 基础
    def peek(self, offset: int = 0) -> Token:
        index = min(self.pos + offset, len(self.tokens) - 1)
        return self.tokens[index]

    def advance(self) -> Token:
        token = self.tokens[self.pos]
        if token.kind != "EOF":
            self.pos += 1
        return token

    def at(self, kind: str, value: str | None = None) -> bool:
        token = self.peek()
        return token.kind == kind and (value is None or token.value == value)

    def at_op(self, value: str) -> bool:
        return self.at("OP", value)

    def at_keyword(self, value: str) -> bool:
        return self.at("KEYWORD", value)

    def accept(self, kind: str, value: str | None = None) -> Token | None:
        if self.at(kind, value):
            return self.advance()
        return None

    def expect(self, kind: str, value: str | None = None, message: str = "") -> Token:
        if self.at(kind, value):
            return self.advance()
        token = self.peek()
        found = token.value or token.kind
        raise PineSyntaxError(
            message or f"期望 {'`' + value + '`' if value else kind}，实际是 `{found}`",
            token.line,
            token.column,
        )

    def skip_newlines(self) -> None:
        while self.at("NEWLINE"):
            self.advance()

    # ---------------------------------------------------------------- 入口
    def parse_script(self) -> N.Script:
        script = N.Script()
        script.line_count = self.source_lines
        self.skip_newlines()
        # 第一步：吃掉 import（Pine 要求 import 在声明之前）
        while self.at_keyword("import"):
            script.statements.append(self.parse_import())
            self.skip_newlines()
        # 第二步：可选的声明语句
        if self.peek().kind in ("IDENT", "KEYWORD"):
            saved = self.pos
            if self.at("IDENT") and self.peek().value in DECLARATION_NAMES and self.peek(1).kind == "OP" and self.peek(1).value == "(":
                call = self.parse_expression()
                if isinstance(call, N.Call):
                    script.declaration = call
                    script.declaration_kind = call.func.name if isinstance(call.func, N.Name) else ""
                else:  # 退化成普通表达式语句
                    self.pos = saved
                self.expect_line_end()
            else:
                self.pos = saved
        # 第三步：语句
        while not self.at("EOF"):
            self.skip_newlines()
            if self.at("EOF"):
                break
            script.statements.append(self.parse_statement())
        return script

    def expect_line_end(self) -> None:
        while self.at("NEWLINE") or self.at("OP", ";"):
            self.advance()

    # ------------------------------------------------------------ 语句分发
    def parse_statement(self) -> N.Node:
        token = self.peek()

        if token.kind == "KEYWORD":
            word = token.value
            if word == "if":
                return self.parse_if_statement()
            if word == "for":
                return self.parse_for()
            if word == "while":
                return self.parse_while()
            if word == "switch":
                return self.parse_switch(as_expression=False)
            if word == "var" or word == "varip":
                return self.parse_assignment(var_kind=word)
            if word == "break":
                self.advance()
                self.expect_line_end()
                return N.BreakStmt(token.line, token.column)
            if word == "continue":
                self.advance()
                self.expect_line_end()
                return N.ContinueStmt(token.line, token.column)
            if word == "import":
                return self.parse_import()
            if word in ("type", "enum", "method"):
                return self.parse_ignored_declaration(word)

        # 函数定义： name(params) =>
        if token.kind == "IDENT" and self.peek(1).kind == "OP" and self.peek(1).value == "(":
            if self._looks_like_func_def():
                return self.parse_func_def()

        # 赋值： x = / x := / [a,b] =
        if self._looks_like_assignment():
            return self.parse_assignment()

        expression = self.parse_expression()
        self.expect_line_end()
        return N.ExprStmt(expression.line, expression.column, expr=expression)

    def _looks_like_func_def(self) -> bool:
        """``name(a, b) =>`` 与 ``name(a, b)`` 调用语句的区分。"""
        depth = 0
        index = self.pos + 1
        while index < len(self.tokens):
            token = self.tokens[index]
            if token.kind == "OP" and token.value == "(":
                depth += 1
            elif token.kind == "OP" and token.value == ")":
                depth -= 1
                if depth == 0:
                    following = self.tokens[index + 1] if index + 1 < len(self.tokens) else None
                    return bool(following and following.kind == "OP" and following.value == "=>")
            elif token.kind in ("NEWLINE", "EOF"):
                return False
            index += 1
        return False

    def _looks_like_assignment(self) -> bool:
        token = self.peek()
        following = self.peek(1)
        if token.kind == "OP" and token.value == "[":
            # [a, b] = ... 解构
            depth = 0
            index = self.pos
            while index < len(self.tokens):
                current = self.tokens[index]
                if current.kind == "OP" and current.value == "[":
                    depth += 1
                elif current.kind == "OP" and current.value == "]":
                    depth -= 1
                    if depth == 0:
                        after = self.tokens[index + 1] if index + 1 < len(self.tokens) else None
                        return bool(after and after.kind == "OP" and after.value in ASSIGN_OPS)
                elif current.kind in ("NEWLINE", "EOF"):
                    return False
                index += 1
            return False
        if token.kind != "IDENT":
            return False
        if following.kind == "OP" and following.value in ASSIGN_OPS:
            return True
        # 带类型声明的局部变量： float x = ... （脚本里少见，但支持）
        if token.value in TYPE_WORDS and following.kind == "IDENT":
            after = self.peek(2)
            return bool(after.kind == "OP" and after.value in ASSIGN_OPS)
        return False

    # ------------------------------------------------------------ 具体语句
    def parse_import(self) -> N.ImportStmt:
        token = self.expect("KEYWORD", "import")
        parts: list[str] = []
        while True:
            current = self.peek()
            if current.kind in ("IDENT", "KEYWORD", "NUMBER"):
                parts.append(self.advance().value)
            else:
                break
            if self.at_op("/") or self.at_op("."):
                self.advance()
                continue
            break
        alias = None
        if self.at_keyword("as"):
            self.advance()
            alias = self.expect("IDENT", message="import as 之后需要别名").value
        self.expect_line_end()
        module = parts[0] if parts else ""
        return N.ImportStmt(token.line, token.column, module=module, alias=alias, parts=parts)

    def parse_ignored_declaration(self, word: str) -> N.IgnoredStmt:
        token = self.advance()
        # 吃掉声明头和可能的缩进块
        while not self.at("NEWLINE") and not self.at("EOF"):
            self.advance()
        self.skip_newlines()
        if self.at("INDENT"):
            self.advance()
            depth = 1
            while depth > 0 and not self.at("EOF"):
                current = self.advance()
                if current.kind == "INDENT":
                    depth += 1
                elif current.kind == "DEDENT":
                    depth -= 1
        return N.IgnoredStmt(token.line, token.column, what=word)

    def parse_func_def(self) -> N.FuncDef:
        name_token = self.expect("IDENT")
        self.expect("OP", "(")
        params: list[tuple[str, N.Node | None]] = []
        if not self.at_op(")"):
            while True:
                # 参数可带类型： float length = 14
                if self.peek().kind == "IDENT" and self.peek().value in TYPE_WORDS and self.peek(1).kind == "IDENT":
                    self.advance()
                param = self.expect("IDENT", message="函数参数需要标识符")
                default = None
                if self.at_op("="):
                    self.advance()
                    default = self.parse_expression()
                params.append((param.value, default))
                if self.at_op(","):
                    self.advance()
                    continue
                break
        self.expect("OP", ")")
        self.expect("OP", "=>")
        if self.at("NEWLINE"):
            body = self.parse_block()
        else:
            expression = self.parse_expression()
            self.expect_line_end()
            body = N.Block(expression.line, expression.column, statements=[], tail=expression)
        return N.FuncDef(name_token.line, name_token.column, name=name_token.value, params=params, body=body)

    def parse_assignment(self, var_kind: str | None = None) -> N.Assign:
        start = self.peek()
        type_name = None
        if var_kind:
            self.advance()
            if self.peek().kind == "IDENT" and self.peek().value in TYPE_WORDS:
                type_name = self.advance().value
        targets: list[str] = []
        if self.at_op("["):
            self.advance()
            while not self.at_op("]"):
                target = self.expect("IDENT", message="解构赋值左侧只能是变量名")
                targets.append(target.value)
                if self.at_op(","):
                    self.advance()
                    continue
                break
            self.expect("OP", "]")
        else:
            if self.peek().kind == "IDENT" and self.peek().value in TYPE_WORDS and self.peek(1).kind == "IDENT":
                type_name = self.advance().value
            target = self.expect("IDENT", message="赋值左侧需要变量名")
            targets.append(target.value)
        operator = self.peek()
        if operator.kind != "OP" or operator.value not in ASSIGN_OPS:
            raise PineSyntaxError("赋值缺少 `=` 或 `:=`", operator.line, operator.column)
        self.advance()
        value = self.parse_value_expression()
        self.expect_line_end()
        declare = var_kind is not None or operator.value == "="
        if operator.value in ("+=", "-=", "*=", "/=", "%="):
            # 复合赋值展开成二元表达式
            value = N.Binary(
                operator.line,
                operator.column,
                op=operator.value[0],
                left=N.Name(operator.line, operator.column, name=targets[0]),
                right=value,
            )
            declare = False
        return N.Assign(
            start.line,
            start.column,
            targets=targets,
            value=value,
            declare=declare,
            var_kind=var_kind,
            type_name=type_name,
        )

    def parse_value_expression(self) -> N.Node:
        """赋值右侧：允许 if / switch 作为表达式。"""
        if self.at_keyword("if"):
            return self.parse_if(as_expression=True)
        if self.at_keyword("switch"):
            return self.parse_switch(as_expression=True)
        return self.parse_expression()

    def parse_if_statement(self) -> N.IfStmt:
        expression = self.parse_if(as_expression=False)
        return N.IfStmt(expression.line, expression.column, branches=expression.branches)

    def parse_if(self, as_expression: bool) -> N.IfExpr:
        token = self.expect("KEYWORD", "if")
        branches: list[tuple[N.Node | None, N.Block]] = []
        condition = self.parse_expression()
        branches.append((condition, self.parse_body_block()))
        while True:
            save = self.pos
            self._consume_line_end_before_else()
            if self.at_keyword("else"):
                self.advance()
                if self.at_keyword("if"):
                    self.advance()
                    nested_condition = self.parse_expression()
                    branches.append((nested_condition, self.parse_body_block()))
                    continue
                branches.append((None, self.parse_body_block()))
                break
            self.pos = save
            break
        node = N.IfExpr(token.line, token.column, branches=branches, kind="if")
        if not as_expression:
            return node
        return node

    def _consume_line_end_before_else(self) -> None:
        while self.at("NEWLINE") or self.at("DEDENT"):
            if self.at("DEDENT"):
                # DEDENT 属于外层块，不能吃掉
                return
            self.advance()

    def parse_body_block(self) -> N.Block:
        if self.at("NEWLINE") or self.at("DEDENT"):
            return self.parse_block()
        # 单行形式： if cond expr
        expression = self.parse_expression()
        self.expect_line_end()
        return N.Block(expression.line, expression.column, statements=[], tail=expression)

    def parse_block(self) -> N.Block:
        self.skip_newlines()
        self.expect("INDENT", message="这里需要一个缩进块")
        block = N.Block(self.peek().line, self.peek().column)
        while not self.at("DEDENT") and not self.at("EOF"):
            self.skip_newlines()
            if self.at("DEDENT") or self.at("EOF"):
                break
            statement = self.parse_statement()
            # 块内最后一个裸表达式即返回值
            if isinstance(statement, N.ExprStmt) and len(block.statements) >= 0:
                # 先入栈，稍后再把最后一个抽成 tail
                block.statements.append(statement)
            else:
                block.statements.append(statement)
        if self.at("DEDENT"):
            self.advance()
        if block.statements and isinstance(block.statements[-1], N.ExprStmt):
            block.tail = block.statements.pop().expr
        return block

    def parse_for(self) -> N.ForStmt:
        token = self.expect("KEYWORD", "for")
        var = self.expect("IDENT", message="for 之后需要循环变量").value
        self.expect("OP", "=")
        start = self.parse_expression()
        self.expect("KEYWORD", "to")
        stop = self.parse_expression()
        step = None
        if self.at_keyword("by"):
            self.advance()
            step = self.parse_expression()
        body = self.parse_block()
        return N.ForStmt(token.line, token.column, var=var, start=start, stop=stop, step=step, body=body)

    def parse_while(self) -> N.WhileStmt:
        token = self.expect("KEYWORD", "while")
        condition = self.parse_expression()
        body = self.parse_block()
        return N.WhileStmt(token.line, token.column, condition=condition, body=body)

    def parse_switch(self, as_expression: bool) -> N.Node:
        token = self.expect("KEYWORD", "switch")
        subject = None
        if not self.at("NEWLINE"):
            subject = self.parse_expression()
        self.skip_newlines()
        self.expect("INDENT", message="switch 需要缩进的 case 分支")
        cases: list[tuple[list[N.Node], N.Block]] = []
        default: N.Block | None = None
        while not self.at("DEDENT") and not self.at("EOF"):
            self.skip_newlines()
            if self.at("DEDENT") or self.at("EOF"):
                break
            if self.at_op("=>"):
                self.advance()
                default = self._parse_arrow_body()
                continue
            patterns: list[N.Node] = []
            if self.at_keyword("default"):
                self.advance()
                self.expect("OP", "=>")
                default = self._parse_arrow_body()
                continue
            if self.at_keyword("case"):
                self.advance()
            patterns.append(self.parse_expression())
            while self.at_op(","):
                self.advance()
                patterns.append(self.parse_expression())
            self.expect("OP", "=>", message="switch 分支需要 `=>`")
            cases.append((patterns, self._parse_arrow_body()))
        if self.at("DEDENT"):
            self.advance()
        if as_expression:
            return N.IfExpr(token.line, token.column, branches=cases, kind="switch", subject=subject)
        return N.SwitchStmt(token.line, token.column, subject=subject, cases=cases, default=default)

    def _parse_arrow_body(self) -> N.Block:
        if self.at("NEWLINE"):
            return self.parse_block()
        expression = self.parse_expression()
        self.expect_line_end()
        return N.Block(expression.line, expression.column, statements=[], tail=expression)

    # ------------------------------------------------------------ 表达式
    def parse_expression(self) -> N.Node:
        return self.parse_ternary()

    def parse_ternary(self) -> N.Node:
        condition = self.parse_or()
        if self.at_op("?"):
            token = self.advance()
            when_true = self.parse_expression()
            self.expect("OP", ":", message="三元表达式缺少 `:`")
            when_false = self.parse_expression()
            return N.Ternary(token.line, token.column, condition=condition, when_true=when_true, when_false=when_false)
        return condition

    def parse_or(self) -> N.Node:
        left = self.parse_and()
        while self.at_keyword("or"):
            token = self.advance()
            left = N.Binary(token.line, token.column, op="or", left=left, right=self.parse_and())
        return left

    def parse_and(self) -> N.Node:
        left = self.parse_not()
        while self.at_keyword("and"):
            token = self.advance()
            left = N.Binary(token.line, token.column, op="and", left=left, right=self.parse_not())
        return left

    def parse_not(self) -> N.Node:
        if self.at_keyword("not"):
            token = self.advance()
            return N.Unary(token.line, token.column, op="not", operand=self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self) -> N.Node:
        left = self.parse_additive()
        while self.peek().kind == "OP" and self.peek().value in COMPARISON_OPS:
            token = self.advance()
            left = N.Binary(token.line, token.column, op=token.value, left=left, right=self.parse_additive())
        return left

    def parse_additive(self) -> N.Node:
        left = self.parse_multiplicative()
        while self.peek().kind == "OP" and self.peek().value in ADDITIVE_OPS:
            token = self.advance()
            left = N.Binary(token.line, token.column, op=token.value, left=left, right=self.parse_multiplicative())
        return left

    def parse_multiplicative(self) -> N.Node:
        left = self.parse_unary()
        while self.peek().kind == "OP" and self.peek().value in MULTIPLICATIVE_OPS:
            token = self.advance()
            left = N.Binary(token.line, token.column, op=token.value, left=left, right=self.parse_unary())
        return left

    def parse_unary(self) -> N.Node:
        token = self.peek()
        if token.kind == "OP" and token.value in ("-", "+"):
            self.advance()
            operand = self.parse_unary()
            if token.value == "+":
                return operand
            if isinstance(operand, N.Literal) and isinstance(operand.value, (int, float)):
                return N.Literal(token.line, token.column, value=-operand.value)
            return N.Unary(token.line, token.column, op="-", operand=operand)
        return self.parse_postfix()

    def parse_postfix(self) -> N.Node:
        expression = self.parse_primary()
        while True:
            token = self.peek()
            if token.kind != "OP":
                break
            if token.value == "[":
                self.advance()
                offset = self.parse_expression()
                self.expect("OP", "]")
                expression = N.History(token.line, token.column, base=expression, offset=offset)
                continue
            if token.value == "(":
                expression = self._parse_call_arguments(expression, token)
                continue
            if token.value == ".":
                self.advance()
                name = self.expect("IDENT", message="`.` 之后需要属性名")
                expression = N.Member(token.line, token.column, obj=expression, name=name.value)
                continue
            break
        return expression

    def _parse_call_arguments(self, func: N.Node, token: Token) -> N.Call:
        self.expect("OP", "(")
        args: list[N.Node] = []
        kwargs: dict[str, N.Node] = {}
        if not self.at_op(")"):
            while True:
                if self.peek().kind == "IDENT" and self.peek(1).kind == "OP" and self.peek(1).value == "=":
                    key = self.advance().value
                    self.advance()
                    kwargs[key] = self.parse_expression()
                else:
                    args.append(self.parse_expression())
                if self.at_op(","):
                    self.advance()
                    continue
                break
        self.expect("OP", ")", message="函数调用缺少 `)`")
        return N.Call(token.line, token.column, func=func, args=args, kwargs=kwargs)

    def parse_primary(self) -> N.Node:
        token = self.peek()
        if token.kind == "NUMBER":
            self.advance()
            text = token.value
            try:
                value = float(text) if any(character in text for character in ".eE") else int(text)
            except ValueError:  # pragma: no cover - 词法器已保证格式
                raise PineSyntaxError(f"无法解析数字 {text!r}", token.line, token.column) from None
            return N.Literal(token.line, token.column, value=value)
        if token.kind == "STRING":
            self.advance()
            return N.Literal(token.line, token.column, value=token.value)
        if token.kind == "IDENT":
            self.advance()
            return N.Name(token.line, token.column, name=token.value)
        if token.kind == "KEYWORD":
            if token.value == "true":
                self.advance()
                return N.Literal(token.line, token.column, value=True)
            if token.value == "false":
                self.advance()
                return N.Literal(token.line, token.column, value=False)
            if token.value == "na":
                self.advance()
                return N.Name(token.line, token.column, name="na")
            if token.value == "if":
                return self.parse_if(as_expression=True)
            if token.value == "switch":
                return self.parse_switch(as_expression=True)
        if token.kind == "OP" and token.value == "(":
            self.advance()
            expression = self.parse_expression()
            self.expect("OP", ")")
            return expression
        if token.kind == "OP" and token.value == "[":
            self.advance()
            items: list[N.Node] = []
            if not self.at_op("]"):
                while True:
                    items.append(self.parse_expression())
                    if self.at_op(","):
                        self.advance()
                        continue
                    break
            self.expect("OP", "]", message="元组缺少 `]`")
            return N.TupleExpr(token.line, token.column, items=items)
        found = token.value or token.kind
        raise PineSyntaxError(f"无法解析的表达式起始符号 `{found}`", token.line, token.column)


def parse(source: str) -> N.Script:
    if isinstance(source, str) and len(source) > 200_000:
        raise PineSyntaxError("脚本过长（上限 200KB）")
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n") if isinstance(source, str) else []
    if len(lines) > MAX_LINES:
        raise PineSyntaxError(f"脚本行数过多（上限 {MAX_LINES} 行）", MAX_LINES + 1, 1)
    tokens = tokenize(source)
    script = Parser(tokens, len(lines)).parse_script()
    script.version = 5
    return script
