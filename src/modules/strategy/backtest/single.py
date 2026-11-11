"""单票策略回测(同花顺「策略回测」那种简单形态)。

只做一只股票、全仓进出:

- 信号在**收盘后**产生,次日**开盘**成交(避免未来函数);
- T+1:当天买入最早次日才能卖;
- 涨停无法买入(次日开盘涨幅达到板块阈值视为未成交),停牌跳过;
- 费用复用 ``CostModel``(佣金/印花税/过户费/滑点)。

规则沿用选股的条件格式(``{field, op, value}``),支持的字段见
:data:`src.modules.strategy.patterns.FEATURES`:

- 买入条件为「且」:全部满足才买;
- 卖出条件为「或」:任意一条命中即卖;
- 禁买条件为「或」:任意一条命中当日不买(高开低走未修复、放量滞涨等风控)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text

from src.modules.strategy.backtest.cost_model import CostModel
from src.modules.strategy.patterns import (
    FEATURES,
    VALID_FIELDS,
    compute_features,
    describe_conditions,
    diagnose,
)
from src.platform.persistence.database import SessionLocal

logger = logging.getLogger(__name__)

_ALLOWED_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "between", "in", "not_in", "contains"}

# 兼容旧引用:字段目录已迁移到 patterns.py
__all__ = ["FEATURES", "Bar", "load_bars", "run_single_backtest", "limit_threshold"]

DEFAULT_BUY = [{"field": "runup_3d", "op": "between", "value": [-5, 0]}]
DEFAULT_SELL = [{"field": "profit_ratio", "op": "gte", "value": 95}]


@dataclass
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


def network_bars(symbol: str, days: int = 520, market: str = "CN") -> list[Bar]:
    """网络日K兜底(本地库没有这只票时用一次,数据源由数据源表决定)。"""
    try:
        from src.platform.marketdata.collectors.kline_collector import KlineCollector
        from src.platform.marketdata.models import MarketCode

        try:
            code = MarketCode(str(market or "CN").upper())
        except ValueError:
            code = MarketCode.CN
        rows = KlineCollector(code).get_klines(symbol, days=max(120, int(days)))
        return [
            Bar(
                date=str(r.date),
                open=float(r.open),
                high=float(r.high),
                low=float(r.low),
                close=float(r.close),
                volume=float(r.volume or 0),
            )
            for r in rows or []
            if r.open and r.close and float(r.close) > 0
        ]
    except Exception as exc:  # noqa: BLE001 — 补数失败就当没有数据
        logger.debug(f"[策略] {symbol} 网络日K不可用: {exc}")
        return []


def load_bars(
    symbol: str, days: int = 500, *, allow_network: bool = True, market: str = "CN"
) -> list[Bar]:
    """前复权日K(缺失时退回原始日K),按日期升序。

    本地 ``daily_bars`` / ``daily_bars_qfq`` 不够 60 根时,按 :func:`network_bars`
    补一次网络日K(自选之外的新票也能直接体检/回测,结果有数据源缓存)。
    """
    symbol = str(symbol or "").strip()
    db = SessionLocal()
    try:
        for table in ("daily_bars_qfq", "daily_bars"):
            try:
                rows = db.execute(
                    text(
                        "SELECT trade_date, open, high, low, close, volume FROM ("
                        "  SELECT trade_date, open, high, low, close, volume,"
                        "         ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn"
                        f"  FROM {table} WHERE symbol = :symbol"
                        ") WHERE rn <= :days ORDER BY trade_date ASC"
                    ),
                    {"symbol": symbol, "days": max(60, int(days))},
                ).fetchall()
            except Exception:  # noqa: BLE001 — 表不存在时换下一张
                continue
            if len(rows) >= 60:
                return [
                    Bar(str(d), float(o), float(h), float(l), float(c), float(v or 0))
                    for d, o, h, l, c, v in rows
                    if o is not None and c is not None and float(c) > 0
                ]
    finally:
        db.close()
    if allow_network:
        return network_bars(symbol, days=max(120, int(days)), market=market)
    return []


def limit_threshold(symbol: str) -> float:
    code = str(symbol or "")
    if code.startswith(("30", "68")):
        return 0.195
    if code.startswith(("4", "8", "92")):
        return 0.295
    return 0.095


def _features(bars: list[Bar], idx: int) -> dict:
    """兼容旧引用:委托给 :func:`patterns.compute_features`。"""
    return compute_features(bars, idx)


def _match(row: dict, cond: dict) -> bool:
    field = str(cond.get("field") or "").strip()
    op = str(cond.get("op") or "eq").strip()
    expected = cond.get("value")
    if field not in VALID_FIELDS or op not in _ALLOWED_OPS:
        return False
    actual = row.get(field)
    if actual is None:
        return False
    try:
        if op == "eq":
            return actual == expected
        if op == "ne":
            return actual != expected
        if op == "gt":
            return actual > float(expected)
        if op == "gte":
            return actual >= float(expected)
        if op == "lt":
            return actual < float(expected)
        if op == "lte":
            return actual <= float(expected)
        if op == "between":
            lo, hi = float(expected[0]), float(expected[1])
            return lo <= actual <= hi
        if op in ("in", "not_in"):
            wanted = expected if isinstance(expected, list) else [expected]
            hit = actual in wanted
            return hit if op == "in" else not hit
        if op == "contains":
            return str(expected) in str(actual)
    except (TypeError, ValueError, IndexError):
        return False
    return False


def _metrics(
    *,
    trades: list[dict],
    curve: list[dict],
    equity: float,
    initial_capital: float,
    bars: list[Bar],
) -> dict:
    """回测指标:胜率、盈亏比、最大回撤、年化、平均持有天数。"""
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))

    peak = 0.0
    max_dd = 0.0
    for point in curve:
        value = float(point["equity"])
        peak = max(peak, value)
        if peak > 0:
            max_dd = min(max_dd, (value / peak - 1) * 100)

    years = len(bars) / 252 if bars else 0
    annual = 0.0
    if years > 0 and equity > 0 and initial_capital > 0:
        annual = ((equity / initial_capital) ** (1 / years) - 1) * 100

    hold_days = [t["hold_days"] for t in trades]
    win_pcts = [t["pnl_pct"] for t in wins]
    loss_pcts = [t["pnl_pct"] for t in losses]
    return {
        "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "avg_hold_days": round(sum(hold_days) / len(hold_days), 1) if hold_days else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "best_trade_pct": round(max((t["pnl_pct"] for t in trades), default=0.0), 2),
        "worst_trade_pct": round(min((t["pnl_pct"] for t in trades), default=0.0), 2),
        "avg_win_pct": round(sum(win_pcts) / len(win_pcts), 2) if win_pcts else 0.0,
        "avg_loss_pct": round(sum(loss_pcts) / len(loss_pcts), 2) if loss_pcts else 0.0,
        "max_drawdown": round(max_dd, 2),
        "annual_return": round(annual, 2),
    }


def _explain_rules(buy: list[dict], sell: list[dict], avoid: list[dict]) -> tuple[str, str]:
    """规则条款与「交易解读」两段中文。"""
    buy_text = describe_conditions(buy, joiner=" 且 ") or "未设置"
    sell_text = describe_conditions(sell, joiner=" 或 ") or "未设置"
    avoid_text = describe_conditions(avoid, joiner=" 或 ")
    rule_text = f"买入（全部满足）：{buy_text}；卖出（任一满足）：{sell_text}"
    if avoid_text:
        rule_text += f"；禁买（任一命中即不买）：{avoid_text}"
    return rule_text, buy_text


def run_single_backtest(
    symbol: str,
    *,
    buy: list[dict] | None = None,
    sell: list[dict] | None = None,
    avoid: list[dict] | None = None,
    days: int = 500,
    market: str = "CN",
    initial_capital: float = 100_000.0,
    cost_model: CostModel | None = None,
) -> dict:
    """跑一次单票回测,返回指标、曲线与买卖点。"""
    symbol = str(symbol).strip()
    bars = load_bars(symbol, days=days, market=market)
    if len(bars) < 60:
        return {"symbol": symbol, "error": "历史数据不足(需至少 60 根日K)", "trades": []}

    buy = buy or DEFAULT_BUY
    sell = sell or DEFAULT_SELL
    avoid = avoid or []
    cost = cost_model or CostModel()
    threshold = limit_threshold(symbol)

    rows = [compute_features(bars, i) for i in range(len(bars))]

    cash = float(initial_capital)
    shares = 0
    entry_price = 0.0
    entry_date = ""
    entry_idx = 0
    entry_cost = 0.0

    trades: list[dict] = []
    curve: list[dict] = []
    pending_buy = False
    pending_sell = False

    for i, bar in enumerate(bars):
        # 先按上一交易日的信号,以今日开盘成交
        if pending_sell and shares > 0:
            fill = cost.fill("sell", bar.open, shares)
            proceeds = fill.cash_delta
            pnl = proceeds - entry_cost
            trades.append(
                {
                    "entry_date": entry_date,
                    "entry_price": round(entry_price, 3),
                    "exit_date": bar.date,
                    "exit_price": round(bar.open, 3),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl / entry_cost * 100, 2) if entry_cost else 0.0,
                    "hold_days": i - entry_idx,
                }
            )
            cash = proceeds
            shares = 0
            pending_sell = False

        if pending_buy and shares == 0:
            # 次日开盘涨停视为买不进
            prev_close = bars[i - 1].close if i > 0 else bar.open
            if prev_close > 0 and (bar.open / prev_close - 1) >= threshold:
                pending_buy = False
            else:
                fill = cost.fill("buy", bar.open, int(cash // bar.open // 100) * 100)
                if fill.quantity > 0:
                    shares = fill.quantity
                    entry_price = fill.fill_price
                    entry_date = bar.date
                    entry_idx = i
                    entry_cost = -fill.cash_delta
                    cash += fill.cash_delta
                pending_buy = False

        # 收盘后判断下一交易日的动作
        row = rows[i]
        if shares > 0:
            if any(_match(row, c) for c in sell):
                pending_sell = True
        else:
            if buy and all(_match(row, c) for c in buy):
                # 禁买条件:任一命中则放弃这次买入
                if not any(_match(row, c) for c in avoid):
                    pending_buy = True

        equity = cash + shares * bar.close
        curve.append(
            {
                "date": bar.date,
                "close": round(bar.close, 3),
                "equity": round(equity, 2),
            }
        )

    # 未平仓:按最后收盘价标记市值
    last = bars[-1]
    equity = cash + shares * last.close
    base = initial_capital
    buy_hold = (last.close / bars[0].close - 1) * 100 if bars[0].close > 0 else 0.0
    curve_out = [
        {
            "date": p["date"],
            "price_pct": round((p["close"] / bars[0].close - 1) * 100, 2),
            "equity_pct": round(
                (p["equity"] / curve[0]["equity"] - 1) * 100
            )
            if curve and curve[0]["equity"]
            else 0.0,
            "buy": False,
            "sell": False,
        }
        for p in curve
    ]
    buy_dates = {t["entry_date"] for t in trades}
    sell_dates = {t["exit_date"] for t in trades}
    for point in curve_out:
        point["buy"] = point["date"] in buy_dates
        point["sell"] = point["date"] in sell_dates

    metrics = _metrics(
        trades=trades,
        curve=curve,
        equity=equity,
        initial_capital=base,
        bars=bars,
    )
    total_return = round((equity / base - 1) * 100, 2)
    rule_text, buy_text = _explain_rules(buy, sell, avoid)
    sell_text = describe_conditions(sell, joiner=" 或 ") or "未设置"
    avoid_text = describe_conditions(avoid, joiner=" 或 ")

    if trades:
        profit_factor = metrics["profit_factor"]
        pf_text = f"{profit_factor:.2f}" if profit_factor is not None else "无亏损"
        explain = (
            f"当「{buy_text}」时于次日开盘买入，"
            f"「{sell_text}」任一成立时次日开盘卖出"
            + (f"；出现「{avoid_text}」时放弃买入" if avoid_text else "")
            + f"。区间 {len(bars)} 个交易日共 {len(trades)} 笔，"
            f"胜率 {metrics['win_rate']}%，平均持有 {metrics['avg_hold_days']} 天，"
            f"盈亏比 {pf_text}，单笔最好 {metrics['best_trade_pct']:+.1f}%、"
            f"最差 {metrics['worst_trade_pct']:+.1f}%，最大回撤 {metrics['max_drawdown']:.1f}%。"
        )
    else:
        explain = (
            f"当「{buy_text}」时买入、「{sell_text}」时卖出"
            + (f"，「{avoid_text}」时放弃买入" if avoid_text else "")
            + f"。区间 {len(bars)} 个交易日没有触发任何交易，说明条件偏严，"
            "可以把阈值放宽或换成更常用的形态再试。"
        )

    last_diag = diagnose(bars, len(bars) - 1, rows[-1])
    return {
        "symbol": symbol,
        "start": bars[0].date,
        "end": last.date,
        "bars": len(bars),
        "trades": trades,
        "trade_count": len(trades),
        "win_rate": metrics["win_rate"],
        "total_return": total_return,
        "buy_hold_return": round(buy_hold, 2),
        "excess_return": round(total_return - buy_hold, 2),
        "current_status": "持仓中" if shares > 0 else "观望中",
        "curve": curve_out,
        "rules": {"buy": buy, "sell": sell, "avoid": avoid},
        "rule_text": rule_text,
        "explain": explain,
        "metrics": metrics,
        "pattern": last_diag,
        "current_features": rows[-1],
    }
