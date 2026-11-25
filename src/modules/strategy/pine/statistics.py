"""回测统计：按观潮（TideView）面板口径复刻的绩效指标。

与 TradingView 的差别（实测自 TideView 引擎）
--------------------------------------------
- ``total_return_pct`` 分母是**初始资金**，不是均权益。
- 夏普/索提诺的年化因子用 **barsPerYear**（按实际 bar 时间跨度推），不是 252。
- 索提诺下行方差的分母是**整条收益序列长度**，不是下跌样本个数。
- 胜率分母**不含平手单**。
- 最大回撤同时给金额与比例（比例不乘 100，展示时再乘）。
"""

from __future__ import annotations

import math
import statistics
from typing import Any

YEAR_MS = 365.25 * 24 * 3600 * 1000


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: list[float]) -> float:
    return statistics.stdev(values) if len(values) > 1 else 0.0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _longest_run(flags: list[bool]) -> int:
    best = current = 0
    for flag in flags:
        current = current + 1 if flag else 0
        best = max(best, current)
    return best


def build_stats(
    trades: list[dict],
    equity: list[float],
    *,
    initial_capital: float,
    dates: list[str] | None = None,
    risk_free_rate: float = 0.0,
    bars_per_year: float | None = None,
    start_equity: float | None = None,
) -> dict:
    """把成交列表 + 权益曲线算成面板要的全部指标。"""
    initial = float(initial_capital or 1.0)
    curve = [float(value) for value in equity] or [initial]
    if start_equity is not None and curve and curve[0] != start_equity:
        curve = [float(start_equity)] + curve[1:]

    net_pnls = [float(t.get("profit", 0)) - float(t.get("commission", 0)) for t in trades]
    fees = [float(t.get("commission", 0)) for t in trades]
    holding = [max(0, int(t.get("bars", 0))) for t in trades]

    wins = [pnl for pnl in net_pnls if pnl > 0]
    losses = [pnl for pnl in net_pnls if pnl < 0]
    decisive = len(wins) + len(losses)

    total_return = sum(net_pnls)

    # ---- 回撤 ----
    peak = curve[0] if curve else initial
    max_dd = 0.0
    max_dd_pct = 0.0
    for value in curve:
        peak = max(peak, value)
        drawdown = peak - value
        if drawdown > max_dd:
            max_dd = drawdown
        if peak > 0:
            max_dd_pct = max(max_dd_pct, drawdown / peak)

    # ---- 年化 ----
    periods = bars_per_year
    if periods is None:
        periods = _bars_per_year(dates, len(curve))
    years = (len(curve) - 1) / periods if periods else 0.0
    total_return_pct = total_return / initial * 100.0
    growth = 1 + total_return / initial
    if years > 0 and growth > 0:
        annualized = (growth ** (1 / years) - 1) * 100.0
    else:
        annualized = total_return_pct if years <= 0 else 0.0

    # ---- 风险调整 ----
    returns = [
        (curve[i] - curve[i - 1]) / curve[i - 1]
        for i in range(1, len(curve))
        if curve[i - 1] > 0
    ]
    risk_free_per_period = risk_free_rate / periods if periods else 0.0
    sharpe = 0.0
    stdev = _stdev(returns)
    if stdev > 0 and returns:
        sharpe = (_mean(returns) - risk_free_per_period) / stdev * math.sqrt(periods)

    sortino = 0.0
    if returns:
        downside = sum(value * value if value < 0 else value for value in returns) / len(returns)
        downside = max(0.0, downside)
        deviation = math.sqrt(downside)
        excess = _mean(returns) - risk_free_per_period
        if deviation > 0:
            sortino = excess / deviation * math.sqrt(periods)
        elif excess > 0:
            sortino = float("inf")

    calmar = 0.0
    if max_dd_pct > 0:
        calmar = annualized / (max_dd_pct * 100)
    elif annualized > 0:
        calmar = float("inf")

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    else:
        profit_factor = float("inf") if gross_win > 0 else 0.0

    avg_win = _mean(wins)
    avg_loss = abs(_mean(losses))
    expectancy = 0.0
    if decisive:
        expectancy = (len(wins) / decisive) * avg_win - (len(losses) / decisive) * avg_loss

    # 买入持有基准
    benchmark = 0.0
    if dates and len(dates) >= 2:
        benchmark = 0.0  # 由调用方补充（需要首末收盘价）

    return {
        "total_return": round(total_return, 4),
        "total_return_pct": round(total_return_pct, 4),
        "annualized_return_pct": round(annualized, 4) if math.isfinite(annualized) else 0.0,
        "max_drawdown": round(max_dd, 4),
        "max_drawdown_pct": round(max_dd_pct * 100, 4),
        "sharpe": round(sharpe, 4) if math.isfinite(sharpe) else 0.0,
        "sortino": round(sortino, 4) if math.isfinite(sortino) else 0.0,
        "calmar": round(calmar, 4) if math.isfinite(calmar) else 0.0,
        "profit_factor": round(profit_factor, 4) if math.isfinite(profit_factor) else 0.0,
        "win_rate_pct": round(len(wins) / decisive * 100, 2) if decisive else 0.0,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(trades) - decisive,
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "expectancy": round(expectancy, 4),
        "total_fees": round(sum(fees), 4),
        "initial_capital": round(initial, 2),
        "final_equity": round(curve[-1], 2) if curve else round(initial, 2),
        "avg_holding_bars": round(_mean(holding), 2),
        "min_holding_bars": min(holding) if holding else 0,
        "max_holding_bars": max(holding) if holding else 0,
        "median_holding_bars": round(_median(holding), 2),
        "max_consecutive_wins": _longest_run([pnl > 0 for pnl in net_pnls]),
        "max_consecutive_losses": _longest_run([pnl < 0 for pnl in net_pnls]),
        "years": round(years, 4),
        "bars_per_year": round(periods or 0, 2),
        "trades_per_month": round(len(trades) / (years * 12), 2) if years and years > 0 else float(len(trades)),
        "benchmark_return_pct": round(benchmark, 4),
        "equity_curve": [round(value, 4) for value in curve],
    }


def _bars_per_year(dates: list[str] | None, bar_count: int) -> float:
    """按真实时间跨度推每年多少根 bar（日线约 244）。"""
    import datetime as dt

    if dates and len(dates) >= 2:
        try:
            first = dt.date.fromisoformat(str(dates[0])[:10])
            last = dt.date.fromisoformat(str(dates[-1])[:10])
            span_days = max(1, (last - first).days)
            if bar_count <= 1:
                return 244.0
            average_ms = span_days * 86_400_000 / (bar_count - 1)
            if average_ms > 0:
                return max(1.0, YEAR_MS / average_ms)
        except Exception:  # pragma: no cover - 日期异常兜底
            pass
    return 244.0
