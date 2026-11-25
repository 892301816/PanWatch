"""策略撮合器：把 ``strategy.*`` 指令变成可核对的成交账本。

口径（与观潮/TradingView 的本地券商模拟一致）
--------------------------------------------
- 脚本在 **收盘后** 产生指令，默认在 **下一根 bar 的开盘价** 成交；
  ``immediately=true`` 时才在同一根 bar 内按收盘价成交。
- 出场单（``strategy.exit``）是**持久挂单**：带 ``stop`` / ``limit`` /
  ``profit`` / ``loss`` / ``trail_*`` 时，每根 bar 用 high/low 判断是否触发，
  触发后按挂单价（而非收盘价）成交。
- 手续费支持 percent / cash_per_order / cash_per_contract 三种；
  滑点以「最小变动价位」为单位，按不利方向加价。
- 权益按每根 bar 收盘价 mark-to-market，未平仓强制平仓可选。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import PineRuntimeError


@dataclass
class ClosedTrade:
    entry_id: str
    direction: int
    size: float
    entry_price: float
    exit_price: float
    entry_index: int
    exit_index: int
    entry_date: str
    exit_date: str
    profit: float
    commission: float
    max_runup: float = 0.0
    max_drawdown: float = 0.0

    def as_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "direction": self.direction,
            "size": round(self.size, 6),
            "entry_price": round(self.entry_price, 6),
            "exit_price": round(self.exit_price, 6),
            "entry_date": self.entry_date,
            "exit_date": self.exit_date,
            "profit": round(self.profit, 4),
            "commission": round(self.commission, 4),
            "bars": self.exit_index - self.entry_index,
            "max_runup": round(self.max_runup, 4),
            "max_drawdown": round(self.max_drawdown, 4),
        }


@dataclass
class OpenTrade:
    entry_id: str
    direction: int
    size: float
    price: float
    bar_index: int
    date: str
    commission: float = 0.0
    max_runup: float = 0.0
    max_drawdown: float = 0.0


@dataclass
class Order:
    """一张待成交的委托。"""

    kind: str                      # entry / exit / close / order
    order_id: str
    direction: int = 0             # +1 多, -1 空（entry 用）
    qty: float | None = None
    limit: float | None = None
    stop: float | None = None
    from_entry: str | None = None
    profit_pct: float | None = None
    loss_pct: float | None = None
    trail_offset: float | None = None
    trail_points: float | None = None
    trail_price: float | None = None
    immediately: bool = False
    qty_percent: float | None = None
    created_index: int = 0
    active: bool = True
    oca_type: str = "none"
    comment: str | None = None


@dataclass
class BrokerConfig:
    initial_capital: float = 100_000.0
    commission_type: str = "percent"
    commission_value: float = 0.0
    slippage_ticks: float = 0.0
    mintick: float = 0.01
    default_qty_type: str = "fixed"
    default_qty_value: float = 1.0
    pyramiding: int = 1
    allow_short: bool = True
    lot_size: int = 1
    percent_of_equity: bool = True
    t_plus_one: bool = True
    force_close_at_end: bool = True
    # A 股仿真：佣金最低 5 元、卖出印花税（比例）
    commission_min: float = 0.0
    stamp_duty: float = 0.0
    market_sim: str = "off"          # off / ashare


class Broker:
    def __init__(
        self,
        config: BrokerConfig,
        *,
        gate: Callable[[str, Any, float | None], str | None] | None = None,
    ) -> None:
        self.config = config
        self.gate = gate
        self.cash = float(config.initial_capital)
        self.position_size = 0.0          # 带符号：>0 多, <0 空
        self.position_avg_price = 0.0
        self.open_trades: list[OpenTrade] = []
        self.closed_trades: list[ClosedTrade] = []
        self.pending: list[Order] = []
        self.exit_orders: dict[str, Order] = {}
        self.equity_curve: list[float] = []
        self.by_bar: list[dict] = []      # 每根 bar 的成交摘要（前端画 B/S）
        self.last_close = 0.0
        self.index = 0
        self.date = ""
        self.rejections: list[dict] = []
        self.max_drawdown_value = 0.0
        self._peak_equity = float(config.initial_capital)
        self._entry_bar: dict[str, int] = {}
        self._trail_state: dict[str, dict] = {}

    # ------------------------------------------------------------------ 查询
    @property
    def open_profit(self) -> float:
        if not self.position_size:
            return 0.0
        return self.position_size * (self.last_close - self.position_avg_price)

    @property
    def equity(self) -> float:
        """权益 = 现金 + 持仓市值（带符号）。逐 bar 用收盘价盯市。"""
        return self.cash + self.position_size * self.last_close

    @property
    def net_profit(self) -> float:
        return sum(trade.profit - trade.commission for trade in self.closed_trades)

    def gross_profit(self) -> float:
        return sum(trade.profit for trade in self.closed_trades if trade.profit > 0)

    def gross_loss(self) -> float:
        return sum(trade.profit for trade in self.closed_trades if trade.profit < 0)

    def state(self, name: str) -> Any:
        mapping = {
            "position_size": round(self.position_size, 6),
            "position_avg_price": round(self.position_avg_price, 6) if self.position_size else None,
            "equity": round(self.equity, 4),
            "netprofit": round(self.net_profit, 4),
            "openprofit": round(self.open_profit, 4),
            "closedtrades": len(self.closed_trades),
            "initial_capital": self.config.initial_capital,
            "grossprofit": round(self.gross_profit(), 4),
            "grossloss": round(self.gross_loss(), 4),
            "wintrades": sum(1 for t in self.closed_trades if t.profit > 0),
            "losstrades": sum(1 for t in self.closed_trades if t.profit <= 0),
            "max_drawdown": round(self.max_drawdown_value, 4),
            "opentrades": len(self.open_trades),
        }
        return mapping.get(name)

    def open_trade_field(self, name: str, number: int | None = None) -> Any:
        trades = self.open_trades
        if number is None:
            if not trades:
                return 0 if name == "size" else None
            if name == "size":
                return round(self.position_size, 6)
            if name == "profit":
                return round(self.open_profit, 4)
            target = trades[0]
        else:
            if number < 0 or number >= len(trades):
                return None
            target = trades[number]
        if name == "entry_price":
            return round(target.price, 6)
        if name == "entry_bar_index":
            return target.bar_index
        if name == "size":
            return round(target.size, 6)
        if name == "profit":
            return round(target.size * (self.last_close - target.price), 4)
        if name == "max_runup":
            return round(target.max_runup, 4)
        if name == "max_drawdown":
            return round(target.max_drawdown, 4)
        if name == "entry_time":
            return target.date
        return None

    def closed_trade_field(self, name: str, number: int) -> Any:
        if number < 0 or number >= len(self.closed_trades):
            return None
        trade = self.closed_trades[number]
        mapping = {
            "entry_price": round(trade.entry_price, 6),
            "exit_price": round(trade.exit_price, 6),
            "entry_bar_index": trade.entry_index,
            "exit_bar_index": trade.exit_index,
            "profit": round(trade.profit, 4),
            "size": round(trade.size, 6),
            "commission": round(trade.commission, 4),
            "entry_time": trade.entry_date,
            "exit_time": trade.exit_date,
            "max_runup": round(trade.max_runup, 4),
            "max_drawdown": round(trade.max_drawdown, 4),
        }
        return mapping.get(name)

    # ------------------------------------------------------------------ 下单
    def queue(self, order: Order) -> None:
        order.created_index = self.index
        if order.kind in ("entry", "order") and order.qty is None:
            # 数量在「下单这根 bar 收盘」就冻结成绝对股数（观潮口径）
            reference = order.limit or order.stop or self.last_close
            qty = self._size_for(float(reference or 0))
            lot = max(1, int(self.config.lot_size))
            qty = math.floor(qty / lot + 1e-9) * lot
            if qty <= 0:
                self.rejections.append(
                    {"index": self.index, "date": self.date, "reason": "qty_below_lot", "side": "buy"}
                )
                return
            order.qty = qty
        if order.kind == "exit":
            existing = self.exit_orders.get(order.order_id)
            if existing is not None and order.immediately:
                existing.active = False
            self.exit_orders[order.order_id] = order
            return
        self.pending.append(order)

    def cancel(self, order_id: str) -> None:
        for order in self.pending:
            if order.order_id == order_id:
                order.active = False
        order = self.exit_orders.get(order_id)
        if order is not None:
            order.active = False

    def cancel_all(self) -> None:
        for order in self.pending:
            order.active = False
        for order in self.exit_orders.values():
            order.active = False

    def close_position(self, entry_id: str | None, immediately: bool) -> None:
        size = abs(self.position_size)
        if size <= 0:
            return
        if entry_id:
            matched = [t for t in self.open_trades if t.entry_id == entry_id]
            size = sum(t.size for t in matched)
            if size <= 0:
                return
        self.queue(
            Order(
                kind="close",
                order_id=entry_id or "__all__",
                direction=0,
                qty=size,
                immediately=immediately,
                from_entry=entry_id,
            )
        )

    # ------------------------------------------------------------------ 结算
    def begin_bar(self, index: int, bar: Any, previous_close: float | None) -> None:
        self.index = index
        self.date = str(getattr(bar, "date", ""))
        self.last_close = float(bar.close)
        # 1) 先处理上一根收盘时排队的市价单
        ready = [order for order in self.pending if order.active]
        self.pending = [order for order in self.pending if not order.active]
        still_pending: list[Order] = []
        for order in ready:
            filled = self._try_fill(order, bar, previous_close, at_open=True)
            if not filled and order.active and (order.limit is not None or order.stop is not None):
                still_pending.append(order)
        # 2) 持久出场单：每根 bar 用高低点判断触发
        for order in list(self.exit_orders.values()):
            if not order.active:
                continue
            self._check_exit_order(order, bar)
        self.pending.extend(still_pending)
        # 3) 更新持仓浮盈极值
        self._update_runup(bar)

    def end_bar(self, index: int, bar: Any) -> None:
        self.last_close = float(bar.close)
        self._update_runup(bar)
        equity = self.equity
        self.equity_curve.append(round(equity, 4))
        if equity > self._peak_equity:
            self._peak_equity = equity
        drawdown = self._peak_equity - equity
        if drawdown > self.max_drawdown_value:
            self.max_drawdown_value = drawdown

    def finish(self, bar: Any) -> None:
        if self.config.force_close_at_end and abs(self.position_size) > 1e-9:
            price = float(bar.close)
            # 强制平仓：不额外收手续费，只记录成交（保持账本闭合）
            self._apply_exit(price, abs(self.position_size), self.index, self.date, 0.0, reason="forced")

    # ------------------------------------------------------------------ 内部
    def _commission(self, price: float, qty: float, side: str = "buy") -> float:
        config = self.config
        notional = abs(price * qty)
        if config.commission_type == "percent":
            fee = notional * config.commission_value / 100.0
        elif config.commission_type == "cash_per_order":
            fee = float(config.commission_value)
        elif config.commission_type == "cash_per_contract":
            fee = abs(qty) * float(config.commission_value)
        else:
            fee = 0.0
        if config.commission_min > 0 and fee > 0:
            fee = max(fee, config.commission_min)
        if side == "sell" and config.stamp_duty > 0:
            fee += notional * config.stamp_duty
        return fee

    def _slip(self, price: float, side: str) -> float:
        offset = self.config.slippage_ticks * self.config.mintick
        if not offset:
            return price
        return price + offset if side == "buy" else price - offset

    def _size_for(self, price: float) -> float:
        config = self.config
        if price <= 0:
            return 0.0
        if config.default_qty_type == "percent_of_equity":
            budget = max(0.0, self.equity) * config.default_qty_value / 100.0
            qty = budget / price
        elif config.default_qty_type == "cash":
            qty = config.default_qty_value / price
        else:  # fixed
            qty = config.default_qty_value
        lot = max(1, int(config.lot_size))
        return math.floor(qty / lot) * lot

    def _try_fill(self, order: Order, bar: Any, previous_close: float | None, at_open: bool) -> bool:
        high = float(bar.high)
        low = float(bar.low)
        open_price = float(bar.open)
        side = "buy" if order.direction > 0 else "sell"

        price: float | None = None
        if order.immediately:
            price = self.last_close
        elif order.limit is not None:
            limit = order.limit
            if order.direction > 0:
                if open_price <= limit:
                    price = open_price
                elif low <= limit:
                    price = limit
            else:
                if open_price >= limit:
                    price = open_price
                elif high >= limit:
                    price = limit
        elif order.stop is not None:
            stop = order.stop
            if order.direction > 0:
                if high >= stop:
                    price = max(open_price, stop)
            else:
                if low <= stop:
                    price = min(open_price, stop)
        elif at_open:
            price = open_price

        if price is None:
            return False
        if self.gate is not None and not order.immediately:
            blocked = self.gate(side, bar, previous_close)
            if blocked:
                self.rejections.append({"index": self.index, "date": self.date, "reason": blocked, "side": side})
                order.active = False
                return False
        self._execute(order, price, bar)
        order.active = False
        return True

    def _execute(self, order: Order, price: float, bar: Any) -> None:
        price = self._slip(price, "buy" if (order.direction or self.position_size) > 0 else "sell")
        if order.kind == "entry":
            direction = order.direction
            qty = order.qty if order.qty else self._size_for(price)
            if direction < 0 and not self.config.allow_short:
                self.rejections.append({"index": self.index, "date": self.date, "reason": "short_disabled", "side": "sell"})
                return
            same_direction = (self.position_size > 0 and direction > 0) or (self.position_size < 0 and direction < 0)
            entries = sum(1 for trade in self.open_trades if trade.direction == direction)
            limit = max(1, self.config.pyramiding)
            if same_direction and entries >= limit:
                return
            self._apply_entry(direction, qty, price, order.order_id)
        else:
            qty = order.qty if order.qty else abs(self.position_size)
            self._apply_exit(price, qty, self.index, self.date, 0.0)
    def _apply_entry(self, direction: int, qty: float, price: float, entry_id: str | None = None) -> None:
        if qty <= 0:
            return
        commission = self._commission(price, qty, "buy")
        signed = qty * (1 if direction > 0 else -1)
        notional = price * qty
        if direction > 0:
            self.cash -= notional + commission
        else:
            self.cash += notional - commission
        new_size = self.position_size + signed
        if abs(self.position_size) < 1e-9 or (self.position_size > 0) == (signed > 0):
            total_cost = self.position_avg_price * abs(self.position_size) + price * qty
            self.position_avg_price = total_cost / max(1e-9, abs(new_size))
        self.position_size = new_size
        trade = OpenTrade(
            entry_id=entry_id or f"t{len(self.open_trades) + 1}",
            direction=direction,
            size=qty,
            price=price,
            bar_index=self.index,
            date=self.date,
            commission=commission,
        )
        self.open_trades.append(trade)
        self.by_bar.append(
            {
                "index": self.index,
                "date": self.date,
                "side": "buy" if direction > 0 else "sell",
                "action": "entry",
                "price": round(price, 4),
                "size": round(qty, 4),
            }
        )

    def _apply_exit(self, price: float, qty: float, index: int, date: str, extra_commission: float, reason: str = "exit") -> None:
        if not self.open_trades or qty <= 0:
            return
        remaining = qty
        for trade in list(self.open_trades):
            if remaining <= 0:
                break
            take = min(remaining, trade.size)
            remaining -= take
            commission = self._commission(price, take, "sell")
            direction = trade.direction
            profit = (price - trade.price) * take * direction
            gross = price * take
            if direction > 0:
                self.cash += gross - commission
            else:
                self.cash -= gross + commission
            closed = ClosedTrade(
                entry_id=trade.entry_id,
                direction=direction,
                size=take,
                entry_price=trade.price,
                exit_price=price,
                entry_index=trade.bar_index,
                exit_index=index,
                entry_date=trade.date,
                exit_date=date,
                profit=profit,
                commission=commission + trade.commission * (take / max(trade.size, 1e-9)),
                max_runup=trade.max_runup,
                max_drawdown=trade.max_drawdown,
            )
            self.closed_trades.append(closed)
            trade.size -= take
            self.position_size -= take * direction
            if trade.size <= 1e-9:
                self.open_trades.remove(trade)
        if abs(self.position_size) < 1e-9:
            self.position_size = 0.0
            self.position_avg_price = 0.0
        self.by_bar.append(
            {
                "index": index,
                "date": date,
                "side": "sell" if (self.closed_trades[-1].direction > 0 if self.closed_trades else True) else "buy",
                "action": reason,
                "price": round(price, 4),
                "size": round(qty, 4),
            }
        )

    def _check_exit_order(self, order: Order, bar: Any) -> None:
        high = float(bar.high)
        low = float(bar.low)
        close = float(bar.close)
        if not self.open_trades:
            return
        direction = self.position_size > 0 and 1 or -1
        # T+1：当日买入当日不可卖
        if self.config.t_plus_one and self.position_size:
            entry_index = max((trade.bar_index for trade in self.open_trades), default=-1)
            if entry_index == self.index:
                return
        target_id = order.from_entry
        matching = [trade for trade in self.open_trades if not target_id or trade.entry_id == target_id]
        if not matching:
            return
        entry_price = matching[0].price
        qty = abs(self.position_size)
        if order.qty_percent is not None:
            qty = abs(self.position_size) * order.qty_percent / 100.0
        elif order.qty is not None:
            qty = min(qty, order.qty)

        exit_price: float | None = None
        # 盘中路径近似：最近的那个极值先到（观潮口径 xt()）
        high_first = (high - float(bar.open)) < (float(bar.open) - low)
        stop_price: float | None = None
        target_price: float | None = None
        if direction > 0:
            candidates: list[float] = []
            if order.loss_pct is not None:
                level = entry_price * (1 - order.loss_pct / 100.0)
                if low <= level:
                    candidates.append(level)
            if order.stop is not None and low <= order.stop:
                candidates.append(min(float(bar.open), order.stop))
            if candidates:
                stop_price = min(candidates)
            targets: list[float] = []
            if order.profit_pct is not None:
                level = entry_price * (1 + order.profit_pct / 100.0)
                if high >= level:
                    targets.append(level)
            if order.limit is not None and high >= order.limit:
                targets.append(max(float(bar.open), order.limit))
            if targets:
                target_price = max(targets)
        else:
            candidates = []
            if order.loss_pct is not None:
                level = entry_price * (1 + order.loss_pct / 100.0)
                if high >= level:
                    candidates.append(level)
            if order.stop is not None and high >= order.stop:
                candidates.append(max(float(bar.open), order.stop))
            if candidates:
                stop_price = max(candidates)
            targets = []
            if order.profit_pct is not None:
                level = entry_price * (1 - order.profit_pct / 100.0)
                if low <= level:
                    targets.append(level)
            if order.limit is not None and low <= order.limit:
                targets.append(min(float(bar.open), order.limit))
            if targets:
                target_price = min(targets)
        if stop_price is not None and target_price is not None:
            exit_price = target_price if high_first else stop_price
        elif stop_price is not None:
            exit_price = stop_price
        elif target_price is not None:
            exit_price = target_price

        # 跟踪止损
        if order.trail_offset is not None:
            state = self._trail_state.setdefault(order.order_id, {"peak": entry_price, "active": False})
            offset = order.trail_offset * self.config.mintick
            activation = None
            if order.trail_points is not None:
                activation = order.trail_points * self.config.mintick
            if direction > 0:
                state["peak"] = max(state["peak"], high)
                if activation is None or state["peak"] - entry_price >= activation:
                    state["active"] = True
                if state["active"] and low <= state["peak"] - offset:
                    exit_price = state["peak"] - offset
            else:
                state["peak"] = min(state["peak"], low)
                if activation is None or entry_price - state["peak"] >= activation:
                    state["active"] = True
                if state["active"] and high >= state["peak"] + offset:
                    exit_price = state["peak"] + offset

        if exit_price is None:
            return
        if self.gate is not None:
            blocked = self.gate("sell" if direction > 0 else "buy", bar, None)
            if blocked:
                self.rejections.append({"index": self.index, "date": self.date, "reason": blocked, "side": "sell"})
                return
        price = self._slip(exit_price, "sell" if direction > 0 else "buy")
        order.active = False
        self._apply_exit(price, qty, self.index, self.date, 0.0, reason="exit")

    def _update_runup(self, bar: Any) -> None:
        high = float(bar.high)
        low = float(bar.low)
        for trade in self.open_trades:
            if trade.direction > 0:
                trade.max_runup = max(trade.max_runup, (high - trade.price) * trade.size)
                trade.max_drawdown = min(trade.max_drawdown, (low - trade.price) * trade.size)
            else:
                trade.max_runup = max(trade.max_runup, (trade.price - low) * trade.size)
                trade.max_drawdown = min(trade.max_drawdown, (trade.price - high) * trade.size)

    # ------------------------------------------------------------------ 报告
    def as_report(self) -> dict:
        return {
            "trades": [trade.as_dict() for trade in self.closed_trades],
            "orders": self.by_bar,
            "equity_curve": self.equity_curve,
            "rejections": self.rejections[:200],
            "open_trades": [
                {
                    "entry_id": trade.entry_id,
                    "direction": trade.direction,
                    "size": round(trade.size, 4),
                    "price": round(trade.price, 4),
                    "date": trade.date,
                }
                for trade in self.open_trades
            ],
        }
