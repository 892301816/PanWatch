"""Pine 求值器：把 AST 逐 bar 执行成序列。"""

from __future__ import annotations

import math
from typing import Any

from . import nodes as N
from .errors import PineCompileLimit, PineRuntimeError
from .functions import FUNCTIONS, _truthy
from .runtime import CONSTANTS, DYNAMIC_MEMBERS, MAX_LOOP_ITERATIONS, PineContext
from .series import NA, ArgView, Series, SiteState, is_na, to_number

_TA_FUNCTIONS: dict = {}
_TA_ALIASES: dict = {}

# 指标库来自 ta.py（完整实现）与 ta_core.py（核心兜底），两者合并：
# 同名键以 ta.py 为准，ta.py 缺失的部分由 ta_core 补齐。
try:  # pragma: no cover - 导入期兜底
    from .ta import TA as _TA_TA, TA_ALIASES as _TA_ALIAS_TA

    _TA_FUNCTIONS.update(_TA_TA or {})
    _TA_ALIASES.update(_TA_ALIAS_TA or {})
except Exception:  # noqa: BLE001 - ta.py 不可用时完全依赖 ta_core
    pass

try:  # pragma: no cover - 导入期兜底
    from .ta_core import TA as _TA_CORE, TA_ALIASES as _TA_ALIAS_CORE

    for _key, _value in (_TA_CORE or {}).items():
        _TA_FUNCTIONS.setdefault(_key, _value)
    for _key, _value in (_TA_ALIAS_CORE or {}).items():
        _TA_ALIASES.setdefault(_key, _value)
except Exception:  # noqa: BLE001
    pass


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


class Interpreter:
    def __init__(self, ctx: PineContext, script: N.Script) -> None:
        self.ctx = ctx
        self.script = script
        self.buffers: dict[int, Series] = {}
        self.arg_buffers: dict[tuple[int, int], Series] = {}
        self.site_states: dict[int, SiteState] = {}
        self.frames: list[dict[str, Series]] = []
        self.local_scopes: dict[int, dict[str, Series]] = {}
        self._hline_seen: set = set()
        self._warned: set[str] = set()
        self._security_cache: dict[tuple, Any] = {}
        self.security_provider: Any = None

    # ------------------------------------------------------------------ 主循环
    def run(self) -> dict:
        self._apply_declaration()
        ctx = self.ctx
        for index in range(ctx.count):
            ctx.index = index
            ctx.loop_budget = MAX_LOOP_ITERATIONS
            broker = ctx.broker
            previous_close = ctx.bars[index - 1].close if index > 0 else None
            if broker is not None:
                broker.begin_bar(index, ctx.bar, previous_close)
            try:
                self.exec_statements(self.script.statements)
            except (_Break, _Continue):  # pragma: no cover - 顶层 break/continue
                pass
            if broker is not None:
                broker.end_bar(index, ctx.bar)
        if ctx.broker is not None and ctx.bars:
            ctx.broker.finish(ctx.bars[-1])
        return self._result()

    # ------------------------------------------------------------------ 声明
    def _apply_declaration(self) -> None:
        declaration = self.script.declaration
        if declaration is None:
            return
        ctx = self.ctx
        kind = self.script.declaration_kind
        kwargs = {}
        for key, node in declaration.kwargs.items():
            kwargs[key] = self.eval(node)
        title = None
        if declaration.args:
            title = self.eval(declaration.args[0])
        ctx.declaration_title = str(title) if title not in (None, "") else "Pine 脚本"
        ctx.declaration_kind = kind
        if kind == "indicator":
            ctx.overlay = bool(kwargs.get("overlay", True))
            ctx.is_strategy = False
        elif kind == "strategy":
            ctx.overlay = bool(kwargs.get("overlay", True))
            ctx.is_strategy = True
            ctx.broker = self._build_broker(kwargs)
        else:
            ctx.is_strategy = False

    def _build_broker(self, kwargs: dict):
        from .broker import Broker, BrokerConfig

        existing = self.ctx.broker
        initial = to_number(kwargs.get("initial_capital"), 100_000.0)
        config = BrokerConfig(
            initial_capital=initial,
            commission_type=str(kwargs.get("commission_type") or "percent"),
            commission_value=to_number(kwargs.get("commission_value"), 0.0),
            slippage_ticks=to_number(kwargs.get("slippage"), 0.0),
            mintick=self.ctx.mintick,
            default_qty_type=str(kwargs.get("default_qty_type") or "fixed"),
            default_qty_value=to_number(kwargs.get("default_qty_value"), 1.0),
            pyramiding=max(1, int(to_number(kwargs.get("pyramiding"), 1))),
        )
        # 引擎外部（A 股口径）可能已经准备好带成交约束的 broker
        if existing is not None:
            existing.config = config
            existing.cash = initial
            return existing
        return Broker(config)

    # ------------------------------------------------------------------ 语句
    def exec_statements(self, statements: list) -> Any:
        result = None
        for statement in statements:
            result = self.exec_statement(statement)
        return result

    def exec_statement(self, node: N.Node) -> Any:
        if isinstance(node, N.Assign):
            return self._exec_assign(node)
        if isinstance(node, N.ExprStmt):
            return self.eval(node.expr)
        if isinstance(node, N.IfStmt):
            return self._exec_if(node.branches)
        if isinstance(node, N.ForStmt):
            return self._exec_for(node)
        if isinstance(node, N.WhileStmt):
            return self._exec_while(node)
        if isinstance(node, N.SwitchStmt):
            return self._exec_switch(node)
        if isinstance(node, N.FuncDef):
            self.ctx.functions[node.name] = node
            return None
        if isinstance(node, N.ImportStmt):
            self.ctx.imports[node.alias or node.module] = node.module
            return None
        if isinstance(node, N.IgnoredStmt):
            key = f"ignored:{node.what}"
            if key not in self._warned:
                self._warned.add(key)
                self.ctx.warn(f"`{node.what}` 声明已跳过（当前版本不执行类型/枚举/方法定义）")
            return None
        if isinstance(node, N.BreakStmt):
            raise _Break()
        if isinstance(node, N.ContinueStmt):
            raise _Continue()
        raise PineRuntimeError(f"未支持的语句类型 {type(node).__name__}", node.line, node.column)

    def _exec_assign(self, node: N.Assign) -> None:
        if node.value is None:
            return
        value = self.eval(node.value)
        if len(node.targets) > 1:
            if isinstance(value, (tuple, list)):
                items = list(value)
            else:
                items = [value] + [None] * (len(node.targets) - 1)
            for name, item in zip(node.targets, items):
                self._store(name, item, node)
            return
        self._store(node.targets[0], value, node)

    def _store(self, name: str, value: Any, node: N.Node) -> None:
        ctx = self.ctx
        if node.var_kind:
            # var / varip 只在第一根 bar 初始化
            series = self._lookup_series(name)
            if series is not None and series.last_index >= 0:
                return
            self._write(name, value)
            return
        self._write(name, value)

    def _lookup_series(self, name: str) -> Series | None:
        if self.frames:
            frame = self.frames[-1]
            if name in frame:
                return frame[name]
        return self.ctx.variables.get(name)

    def _write(self, name: str, value: Any) -> None:
        if self.frames:
            frame = self.frames[-1]
            series = frame.get(name)
            if series is None:
                series = Series()
                frame[name] = series
            series.set(self.ctx.index, value)
            return
        self.ctx.declare(name).set(self.ctx.index, value)

    def _read(self, name: str) -> Any:
        if self.frames:
            for frame in reversed(self.frames):
                if name in frame:
                    return frame[name].current
        series = self.ctx.variables.get(name)
        if series is not None:
            return series.current
        return self._builtin_value(name)

    def _exec_if(self, branches: list) -> Any:
        for condition, block in branches:
            if condition is None or _truthy(self.eval(condition)):
                return self.exec_block(block)
        return None

    def _exec_for(self, node: N.ForStmt) -> Any:
        start = int(to_number(self.eval(node.start), 0))
        stop = int(to_number(self.eval(node.stop), 0))
        step = int(to_number(self.eval(node.step), 1)) if node.step is not None else 1
        step = 1 if step == 0 else step
        count = abs(stop - start) + 1
        self.ctx.budget(max(1, count))
        result = None
        value = start
        guard = 0
        while (step > 0 and value <= stop) or (step < 0 and value >= stop):
            guard += 1
            if guard > MAX_LOOP_ITERATIONS:
                raise PineCompileLimit("for 循环次数超出上限")
            self._write(node.var, value)
            try:
                result = self.exec_block(node.body)
            except _Continue:
                pass
            except _Break:
                break
            value += step
        return result

    def _exec_while(self, node: N.WhileStmt) -> Any:
        result = None
        guard = 0
        while _truthy(self.eval(node.condition)):
            guard += 1
            if guard > MAX_LOOP_ITERATIONS:
                raise PineCompileLimit("while 循环次数超出上限")
            try:
                result = self.exec_block(node.body)
            except _Continue:
                continue
            except _Break:
                break
        return result

    def _exec_switch(self, node: N.SwitchStmt) -> Any:
        subject = self.eval(node.subject) if node.subject is not None else None
        for patterns, block in node.cases:
            for pattern in patterns:
                candidate = self.eval(pattern)
                matched = _truthy(candidate) if subject is None else _equal(subject, candidate)
                if matched:
                    return self.exec_block(block)
        if node.default is not None:
            return self.exec_block(node.default)
        return None

    def exec_block(self, block: N.Block | None) -> Any:
        if block is None:
            return None
        value = self.exec_statements(block.statements)
        if block.tail is not None:
            return self.eval(block.tail)
        return value

    # ------------------------------------------------------------------ 表达式
    def eval(self, node: N.Node) -> Any:
        value = self._eval(node)
        buffer = self.buffers.get(node.id)
        if buffer is not None:
            buffer.set(self.ctx.index, value)
        return value

    def _eval(self, node: N.Node) -> Any:
        if isinstance(node, N.Literal):
            return node.value
        if isinstance(node, N.Name):
            return self._read(node.name)
        if isinstance(node, N.Member):
            return self._member(node)
        if isinstance(node, N.Call):
            return self._call(node)
        if isinstance(node, N.Unary):
            return self._unary(node)
        if isinstance(node, N.Binary):
            return self._binary(node)
        if isinstance(node, N.Ternary):
            return self.eval(node.when_true) if _truthy(self.eval(node.condition)) else self.eval(node.when_false)
        if isinstance(node, N.History):
            return self._history(node)
        if isinstance(node, N.TupleExpr):
            return tuple(self.eval(item) for item in node.items)
        if isinstance(node, N.IfExpr):
            if node.kind == "switch":
                return self._switch_expression(node)
            return self._exec_if(node.branches)
        raise PineRuntimeError(f"未支持的表达式 {type(node).__name__}", node.line, node.column)

    def _switch_expression(self, node: N.IfExpr) -> Any:
        subject = self.eval(node.subject) if node.subject is not None else None
        for patterns, block in node.branches:
            if isinstance(patterns, list):
                for pattern in patterns:
                    candidate = self.eval(pattern)
                    matched = _truthy(candidate) if subject is None else _equal(subject, candidate)
                    if matched:
                        return self.exec_block(block)
            else:  # pragma: no cover - switch 表达式里 default 分支
                return self.exec_block(patterns)
        return None

    def _member(self, node: N.Member) -> Any:
        dotted = node.dotted()
        if dotted is not None:
            if dotted in DYNAMIC_MEMBERS:
                return DYNAMIC_MEMBERS[dotted](self.ctx)
            if dotted in CONSTANTS:
                return CONSTANTS[dotted]
            # import 别名： mu.roundTo(...) 由调用处理，这里只兜底
            if "." in dotted:
                head = dotted.split(".", 1)[0]
                if head in self.ctx.imports:
                    return None
            # strategy.* 状态变量（position_size / equity / …）
            if dotted.startswith("strategy.") and self.ctx.broker is not None:
                value = self.ctx.broker.state(dotted.split(".", 1)[1])
                if value is not None:
                    return value
            raise PineRuntimeError(f"未知的内置常量 `{dotted}`", node.line, node.column)
        raise PineRuntimeError("不支持该成员访问写法", node.line, node.column)

    def _unary(self, node: N.Unary) -> Any:
        value = self.eval(node.operand)
        if node.op == "not":
            return not _truthy(value)
        if is_na(value):
            return None
        return -to_number(value)

    def _binary(self, node: N.Binary) -> Any:
        op = node.op
        if op == "and":
            return _truthy(self.eval(node.left)) and _truthy(self.eval(node.right))
        if op == "or":
            return _truthy(self.eval(node.left)) or _truthy(self.eval(node.right))
        left = self.eval(node.left)
        right = self.eval(node.right)
        if op in ("==", "!="):
            equal = _equal(left, right)
            return equal if op == "==" else not equal
        if op in (">", "<", ">=", "<="):
            if is_na(left) or is_na(right):
                return None
            a, b = to_number(left), to_number(right)
            return {">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}[op]
        if is_na(left) or is_na(right):
            return None
        a, b = to_number(left), to_number(right)
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            return None if b == 0 else a / b
        if op == "%":
            return None if b == 0 else math.fmod(a, b)
        raise PineRuntimeError(f"未知运算符 `{op}`", node.line, node.column)

    def _history(self, node: N.History) -> Any:
        base = node.base
        buffer = self.buffers.get(base.id)
        if buffer is None:
            buffer = Series()
            self.buffers[base.id] = buffer
        self.eval(base)  # 确保当根值已进缓冲
        offset = int(to_number(self.eval(node.offset), 0))
        if offset < 0:
            raise PineRuntimeError("历史引用 `[n]` 的 n 不能为负", node.line, node.column)
        return buffer.at(offset)

    # ------------------------------------------------------------------ 调用
    def _call(self, node: N.Call) -> Any:
        dotted = _dotted_name(node.func)
        if dotted is None:
            raise PineRuntimeError("只支持调用内置函数或已定义的函数", node.line, node.column)

        if dotted in ("request.security", "request_security"):
            return self._security(node)
        if dotted in self.ctx.functions:
            return self._call_user(node, self.ctx.functions[dotted])

        handler = FUNCTIONS.get(dotted)
        if handler is not None:
            return self._invoke(handler, node, node.id, strategy=False)

        # ta.*：签名是 (args, kwargs, site)，并额外注入行情上下文
        short = _TA_ALIASES.get(dotted)
        resolved = short or dotted
        ta_handler = _TA_FUNCTIONS.get(resolved)
        if ta_handler is None and "." not in dotted:
            ta_handler = _TA_FUNCTIONS.get("ta." + dotted)
            if ta_handler is not None:
                resolved = "ta." + dotted
        if ta_handler is None:
            alias = FUNCTIONS.get(dotted.split(".")[-1]) if "." in dotted else None
            if alias is not None:
                return self._invoke(alias, node, node.id, strategy=False)
            raise PineRuntimeError(f"暂不支持函数 `{dotted}`", node.line, node.column)
        return self._invoke_ta(ta_handler, node)

    def _invoke_ta(self, handler, node: N.Call) -> Any:
        args: list[ArgView] = []
        for position, argument in enumerate(node.args):
            args.append(self._arg_view(node, position, argument))
        kwargs: dict[str, ArgView] = {}
        for offset, (key, argument) in enumerate(node.kwargs.items()):
            kwargs[key] = self._arg_view(node, 10_000 + offset, argument)
        site = self.site_states.get(node.id)
        if site is None:
            site = SiteState()
            self.site_states[node.id] = site
        # 注入 open/high/low/close/volume 到 site.env，让 ta.atr/ta.willr 这类
        # 「不吃 source 参数」的指标也能拿到行情（不写进 kwargs，避免遮蔽位置参数）
        env = site.env
        for slot, name in enumerate(("open", "high", "low", "close", "volume")):
            value = self._builtin_value(name)
            key = (node.id, 20_000 + slot)
            buffer = self.arg_buffers.get(key)
            if buffer is None:
                buffer = Series()
                self.arg_buffers[key] = buffer
            buffer.set(self.ctx.index, value)
            env[name] = ArgView(buffer, value)
        try:
            return handler(args, kwargs, site)
        except PineError:
            raise
        except ZeroDivisionError:
            return None
        except (TypeError, ValueError, IndexError, KeyError) as error:
            raise PineRuntimeError(
                f"调用 `{_dotted_name(node.func)}` 失败：{error}", node.line, node.column
            ) from None

    def _invoke(self, handler, node: N.Call, site_id: int, strategy: bool) -> Any:
        args: list[ArgView] = []
        for position, argument in enumerate(node.args):
            args.append(self._arg_view(node, position, argument))
        kwargs: dict[str, ArgView] = {}
        for offset, (key, argument) in enumerate(node.kwargs.items()):
            kwargs[key] = self._arg_view(node, 10_000 + offset, argument)
        site = self.site_states.get(site_id)
        if site is None:
            site = SiteState()
            self.site_states[site_id] = site
        node._called = getattr(node, "_called", None) or _dotted_name(node.func) or ""
        try:
            return handler(self, args, kwargs, node)
        except PineRuntimeError:
            raise
        except ZeroDivisionError:
            return None
        except (TypeError, ValueError, IndexError, KeyError) as error:
            raise PineRuntimeError(f"调用 `{_dotted_name(node.func)}` 失败：{error}", node.line, node.column) from None

    def _arg_view(self, node: N.Call, slot: int, argument: N.Node) -> ArgView:
        value = self.eval(argument)
        key = (node.id, slot)
        buffer = self.arg_buffers.get(key)
        if buffer is None:
            buffer = Series()
            self.arg_buffers[key] = buffer
        buffer.set(self.ctx.index, value)
        return ArgView(buffer, value)

    def _call_user(self, node: N.Call, definition: N.FuncDef) -> Any:
        values = [self.eval(argument) for argument in node.args]
        values += [None] * (len(definition.params) - len(values))
        store = self.local_scopes.setdefault(node.id, {})
        frame: dict[str, Series] = {}
        args = [view for view in values]
        for (name, default), value in zip(definition.params, args):
            series = store.get(name)
            if series is None:
                series = Series()
                store[name] = series
            series.set(self.ctx.index, value if value is not None else (self.eval(default) if default is not None else None))
            frame[name] = series
        for key, value in node.kwargs.items():
            series = store.get(key)
            if series is None:
                series = Series()
                store[key] = series
            series.set(self.ctx.index, self.eval(value))
            frame[key] = series
        self.frames.append(frame)
        try:
            return self.exec_block(definition.body)
        finally:
            self.frames.pop()

    # ------------------------------------------------------------------ 内置值
    def _builtin_value(self, name: str) -> Any:
        ctx = self.ctx
        dynamic = DYNAMIC_MEMBERS.get(name)
        if dynamic is not None:
            return dynamic(ctx)
        if name == "na":
            return None
        if name in ("open", "high", "low", "close", "volume"):
            return ctx.value(name)
        if name == "hl2":
            return (ctx.value("high") + ctx.value("low")) / 2
        if name == "hlc3":
            return (ctx.value("high") + ctx.value("low") + ctx.value("close")) / 3
        if name == "ohlc4":
            return (ctx.value("open") + ctx.value("high") + ctx.value("low") + ctx.value("close")) / 4
        if name == "hlcc4":
            return (ctx.value("high") + ctx.value("low") + 2 * ctx.value("close")) / 4
        # strategy.* 状态
        if name.startswith("strategy.") and ctx.broker is not None:
            value = ctx.broker.state(name.split(".", 1)[1])
            if value is not None:
                return value
        raise PineRuntimeError(f"未定义的变量 `{name}`")

    # ------------------------------------------------------------------ request.security
    def _security(self, node: N.Call) -> Any:
        if self.security_provider is None:
            self.ctx.warn("request.security 在当前环境不可用，返回 na")
            return None
        symbol_node = node.args[0] if len(node.args) > 0 else node.kwargs.get("symbol")
        timeframe_node = node.args[1] if len(node.args) > 1 else node.kwargs.get("timeframe")
        expression = node.args[2] if len(node.args) > 2 else node.kwargs.get("expression")
        if expression is None:
            return None
        symbol = self.eval(symbol_node) if symbol_node is not None else self.ctx.symbol
        timeframe = self.eval(timeframe_node) if timeframe_node is not None else "D"
        lookahead = node.kwargs.get("lookahead")
        lookahead_on = lookahead is not None and self.eval(lookahead) == "lookahead_on"
        try:
            return self.security_provider(self, str(symbol or self.ctx.symbol), str(timeframe or "D"), expression, lookahead_on)
        except PineRuntimeError:
            raise
        except Exception as error:  # pragma: no cover - 外部数据异常一律降级为 na
            self.ctx.warn(f"request.security({symbol}, {timeframe}) 失败：{error}")
            return None

    # ------------------------------------------------------------------ 结果
    def _result(self) -> dict:
        ctx = self.ctx
        plots = [ctx.plots[name].as_dict() for name in ctx.plot_order]
        return {
            "title": getattr(ctx, "declaration_title", "Pine 脚本"),
            "kind": getattr(ctx, "declaration_kind", "indicator"),
            "overlay": ctx.overlay,
            "plots": plots,
            "shapes": [shape for shape in ctx.shapes][:2000],
            "hlines": ctx.hlines,
            "fills": ctx.fills,
            "backgrounds": [None if is_na(v) else v for v in ctx.backgrounds.values],
            "bar_colors": [None if is_na(v) else v for v in ctx.bar_colors.values],
            "labels": [label for label in ctx.labels if not label.get("deleted")][:500],
            "lines": [line for line in ctx.lines if not line.get("deleted")][:500],
            "boxes": [box for box in ctx.boxes if not box.get("deleted")][:200],
            "inputs": ctx.inputs_meta,
            "warnings": ctx.warnings,
            "dates": ctx._dates,
        }


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _dotted_name(node: N.Node) -> str | None:
    if isinstance(node, N.Name):
        return node.name
    if isinstance(node, N.Member):
        return node.dotted()
    return None


def _equal(left: Any, right: Any) -> bool:
    if is_na(left) or is_na(right):
        return False
    if isinstance(left, bool) or isinstance(right, bool):
        return bool(left) == bool(right)
    if isinstance(left, str) or isinstance(right, str):
        return str(left) == str(right)
    try:
        return abs(to_number(left) - to_number(right)) < 1e-12
    except (TypeError, ValueError):  # pragma: no cover
        return left == right
