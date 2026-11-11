"""单票策略回测(同花顺「策略回测」那种简单形态)。

只做一只股票、全仓进出:

- 信号在**收盘后**产生,次日**开盘**成交(避免未来函数);
- T+1:当天买入最早次日才能卖;
- 涨停无法买入(次日开盘涨幅达到板块阈值视为未成交),停牌跳过;
- 费用复用 ``CostModel``(佣金/印花税/过户费/滑点)。

规则沿用选股的条件格式(``{field, op, value}``),支持的字段见 ``FEATURES``。
卖出条件为「或」:任意一条命中即卖出;买入条件为「且」:全部满足才买。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text

from src.modules.strategy.backtest.cost_model import CostModel
from src.platform.persistence.database import SessionLocal

logger = logging.getLogger(__name__)

_ALLOWED_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "between", "in", "not_in", "contains"}

# 回测可用的字段(与前端下拉保持一致)
FEATURES: list[dict] = [
    {"key": "change_pct", "label": "当日涨跌幅", "unit": "%"},
    {"key": "runup_3d", "label": "近3日涨跌幅", "unit": "%"},
    {"key": "runup_5d", "label": "近5日涨跌幅", "unit": "%"},
    {"key": "runup_10d", "label": "近10日涨跌幅", "unit": "%"},
    {"key": "ma5_dev", "label": "距5日均线", "unit": "%"},
    {"key": "ma20_dev", "label": "距20日均线", "unit": "%"},
    {"key": "high20_gap", "label": "距20日新高", "unit": "%"},
    {"key": "volume_ratio_5d", "label": "量能比(5日)", "unit": "倍"},
    {"key": "profit_ratio", "label": "获利盘(近似)", "unit": "%"},
]

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


def load_bars(symbol: str, days: int = 500) -> list[Bar]:
    """前复权日K(缺失时退回原始日K),按日期升序。"""
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
            if rows:
                return [
                    Bar(str(d), float(o), float(h), float(l), float(c), float(v or 0))
                    for d, o, h, l, c, v in rows
                    if o is not None and c is not None and float(c) > 0
                ]
    finally:
        db.close()
    return []


def limit_threshold(symbol: str) -> float:
    code = str(symbol or "")
    if code.startswith(("30", "68")):
        return 0.195
    if code.startswith(("4", "8", "92")):
        return 0.295
    return 0.095


def _profit_ratio(bars: list[Bar], idx: int, window: int = 120) -> float | None:
    """获利盘近似:过去 N 日按成交量加权,成本(均价)低于现价的占比。

    真实筹码分布要考虑换手衰减;这里用「越近权重越高」的指数加权近似,
    方向一致、数值有偏差,界面上标注为近似值。
    """
    start = max(0, idx - window + 1)
    price = bars[idx].close
    total = 0.0
    profit = 0.0
    for j in range(start, idx + 1):
        b = bars[j]
        weight = b.volume * (0.98 ** (idx - j))
        total += weight
        avg_cost = (b.high + b.low + b.close) / 3.0
        if avg_cost <= price:
            profit += weight
    return round(profit / total * 100, 2) if total > 0 else None


def _features(bars: list[Bar], idx: int) -> dict:
    price = bars[idx].close

    def ret(n: int) -> float | None:
        if idx - n < 0:
            return None
        past = bars[idx - n].close
        return round((price / past - 1) * 100, 2) if past > 0 else None

    def ma(n: int) -> float | None:
        if idx - n + 1 < 0:
            return None
        window = [b.close for b in bars[idx - n + 1: idx + 1]]
        return sum(window) / len(window) if window else None

    ma5, ma20 = ma(5), ma(20)
    highs = [b.high for b in bars[max(0, idx - 19): idx + 1]]
    vols = [b.volume for b in bars[max(0, idx - 5): idx]]
    return {
        "change_pct": ret(1),
        "runup_3d": ret(3),
        "runup_5d": ret(5),
        "runup_10d": ret(10),
        "ma5_dev": round((price / ma5 - 1) * 100, 2) if ma5 else None,
        "ma20_dev": round((price / ma20 - 1) * 100, 2) if ma20 else None,
        "high20_gap": round((price / max(highs) - 1) * 100, 2) if highs else None,
        "volume_ratio_5d": round(bars[idx].volume / (sum(vols) / len(vols)), 2)
        if vols and sum(vols) > 0
        else None,
        "profit_ratio": _profit_ratio(bars, idx),
    }


def _match(row: dict, cond: dict) -> bool:
    field = str(cond.get("field") or "").strip()
    op = str(cond.get("op") or "eq").strip()
    expected = cond.get("value")
    if field not in {f["key"] for f in FEATURES} or op not in _ALLOWED_OPS:
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


def run_single_backtest(
    symbol: str,
    *,
    buy: list[dict] | None = None,
    sell: list[dict] | None = None,
    days: int = 500,
    initial_capital: float = 100_000.0,
    cost_model: CostModel | None = None,
) -> dict:
    """跑一次单票回测,返回指标、曲线与买卖点。"""
    symbol = str(symbol).strip()
    bars = load_bars(symbol, days=days)
    if len(bars) < 60:
        return {"symbol": symbol, "error": "历史数据不足(需至少 60 根日K)", "trades": []}

    buy = buy or DEFAULT_BUY
    sell = sell or DEFAULT_SELL
    cost = cost_model or CostModel()
    threshold = limit_threshold(symbol)

    rows = [_features(bars, i) for i in range(len(bars))]

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
    wins = [t for t in trades if t["pnl"] > 0]
    curve_out = [
        {
            "date": p["date"],
            "price_pct": round((p["close"] / bars[0].close - 1) * 100, 2),
            "equity_pct": round(
                (p["equity"] / curve[0]["equity"] - 1) * 100, 2
            )
            if curve and curve[0]["equity"]
            else 0.0,
        }
        for p in curve
    ]

    return {
        "symbol": symbol,
        "start": bars[0].date,
        "end": last.date,
        "bars": len(bars),
        "trades": trades,
        "trade_count": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "total_return": round((equity / base - 1) * 100, 2),
        "buy_hold_return": round(buy_hold, 2),
        "excess_return": round((equity / base - 1) * 100 - buy_hold, 2),
        "current_status": "持仓中" if shares > 0 else "观望中",
        "curve": curve_out,
        "rules": {"buy": buy, "sell": sell},
    }
