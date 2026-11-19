"""单票策略回测(同花顺「策略回测」那种简单形态)。

只做一只股票、全仓进出:

- 信号在**收盘后**产生,次日**开盘**成交(避免未来函数);
- T+1:当天买入最早次日才能卖;
- 涨停无法买入、跌停无法卖出，停牌/零量保守未成交，并保留逐笔原因;
- 费用复用 ``CostModel``(佣金/印花税/过户费/滑点)。
- 指标预热与收益区间分离，报告原始来源、复权口径与数据指纹。

规则沿用选股的条件格式(``{field, op, value}``),支持的字段见
:data:`src.modules.strategy.patterns.FEATURES`:

- 买入条件为「且」:全部满足才买;
- 卖出条件为「或」:任意一条命中即卖;
- 禁买条件为「或」:任意一条命中当日不买(高开低走未修复、放量滞涨等风控)。
"""

from __future__ import annotations

import logging
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

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
    source: str = "unspecified"
    adjustment: str = "unknown"
    amount: float | None = None
    raw_close: float | None = None
    turnover_rate: float | None = None
    price_limit_pct: float | None = None
    limit_up_locked: float | None = None
    consecutive_limit_ups: float | None = None


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
                open=float(r.open or 0),
                high=float(r.high or 0),
                low=float(r.low or 0),
                close=float(r.close),
                volume=float(r.volume or 0),
                source="marketdata:KlineCollector",
                adjustment="unknown",
            )
            for r in rows or []
            if r.close and float(r.close) > 0
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
                        "SELECT trade_date, open, high, low, close, volume, source FROM ("
                        "  SELECT trade_date, open, high, low, close, volume, source,"
                        "         ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn"
                        f"  FROM {table} WHERE symbol = :symbol"
                        ") WHERE rn <= :days ORDER BY trade_date ASC"
                    ),
                    {"symbol": symbol, "days": max(60, int(days))},
                ).fetchall()
            except Exception:  # noqa: BLE001 — 表不存在时换下一张
                continue
            if len(rows) >= 60:
                result = [
                    Bar(
                        str(d), float(o or 0), float(h or 0), float(l or 0),
                        float(c), float(v or 0),
                        source=f"{table}:{source or 'unspecified'}",
                        adjustment="qfq" if table == "daily_bars_qfq" else "raw",
                    )
                    for d, o, h, l, c, v, source in rows
                    if c is not None and float(c) > 0
                ]
                # Extra inputs retain their real date; never fill historical gaps with today's snapshot.
                extra = db.execute(text('SELECT d.trade_date,d.amount,d.close,s.turnover_rate FROM daily_bars d LEFT JOIN screener_snapshots s ON s.symbol=d.symbol AND s.trade_date=d.trade_date AND s.market=\'CN\' WHERE d.symbol=:symbol'), {'symbol':symbol}).fetchall()
                by_date = {str(d):(a,c,t) for d,a,c,t in extra}
                for bar in result:
                    values=by_date.get(bar.date)
                    if values:bar.amount,bar.raw_close,bar.turnover_rate=values
                from src.modules.strategy.data_capabilities import attach_references
                return attach_references(db,symbol,result)
    finally:
        db.close()
    if allow_network:
        return network_bars(symbol, days=max(120, int(days)), market=market)
    return []


def limit_threshold(symbol: str) -> float:
    """普通非ST、非上市特殊期的日涨跌幅比例；不推断历史特殊状态。"""
    code = str(symbol or "")
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("4", "8", "92")):
        return 0.30
    return 0.10


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

    peak = initial_capital
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


_MIN_HISTORY = 60
_PREFERRED_WARMUP = 120


def _condition_evidence(row: dict, conditions: list[dict]) -> list[dict]:
    return [
        {**condition, "actual": row.get(condition.get("field")), "matched": _match(row, condition)}
        for condition in conditions
    ]


def _fee_evidence(fill) -> dict:
    return {
        "commission": fill.commission,
        "stamp_duty": fill.stamp_duty,
        "transfer_fee": fill.transfer_fee,
        "explicit_fees": fill.explicit_fees,
        "slippage_cost": fill.slippage_cost,
        "friction": fill.friction,
    }


def _affordable_fill(cost: CostModel, cash: float, price: float, lot_size: int):
    """Find a whole-lot quantity affordable after slippage and all explicit fees."""
    low, high = 0, int(cash // price // lot_size)
    result = None
    while low < high:
        middle = (low + high + 1) // 2
        fill = cost.fill("buy", price, middle * lot_size)
        if -fill.cash_delta <= cash + 1e-8:
            low, result = middle, fill
        else:
            high = middle - 1
    if low == 0:
        return None
    if result is None or result.quantity != low * lot_size:
        result = cost.fill("buy", price, low * lot_size)
    return result


def _execution_failure(bar: Bar, previous_close: float, side: str, limit_pct: float) -> tuple[str, str] | None:
    if not math.isfinite(bar.volume) or bar.volume <= 0:
        return "suspended_or_zero_volume", "停牌或成交量为零，保守视为未成交"
    prices = [bar.open, bar.high, bar.low, bar.close]
    if any(not math.isfinite(value) or value <= 0 for value in prices):
        return "invalid_ohlc", "开高低收行情缺失或无效，未成交"
    if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(bar.open, bar.close, bar.high):
        return "invalid_ohlc", "开高低收关系异常，未成交"
    if previous_close <= 0:
        return "missing_previous_close", "缺少昨收，无法核验涨跌停"
    # Ordinary A-share limits are quoted to one fen; use decimal half-up rounding.
    upper = float((Decimal(str(previous_close)) * (Decimal(1) + Decimal(str(limit_pct)))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    lower = float((Decimal(str(previous_close)) * (Decimal(1) - Decimal(str(limit_pct)))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    if side == "buy" and bar.open >= upper - 1e-8:
        return "limit_up", "开盘达到涨停价，日线无法验证排队成交，保守不买入"
    if side == "sell" and bar.open <= lower + 1e-8:
        return "limit_down", "开盘达到跌停价，日线无法验证排队成交，保守不卖出"
    return None


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
    start: str | None = None,
    end: str | None = None,
    config: dict | None = None,
    bars_override: list[Bar] | None = None,
) -> dict:
    """Single-stock, next-open simulation with a separate indicator warm-up period.

    Explicit empty rules stay empty. Orders last for one next-bar attempt; a later
    retry needs a new closing signal. Historical bars preceding the chosen period
    initialize indicators only and never generate orders or enter return metrics.
    """
    symbol = str(symbol or "").strip()
    market = str(market or "CN").upper()
    buy = [dict(condition) for condition in (DEFAULT_BUY if buy is None else buy)]
    sell = [dict(condition) for condition in (DEFAULT_SELL if sell is None else sell)]
    avoid = [dict(condition) for condition in ([] if avoid is None else avoid)]
    config=config or {}
    rules = {"buy": buy, "sell": sell, "avoid": avoid, **({'config':config} if config else {})}
    from src.modules.strategy.rule_engine import evaluate, combine, execution_check
    from src.modules.strategy.factor_engine import field_rows, required_warmup, CATALOG
    from src.modules.strategy.template_runtime import attach
    preferred_warmup=max(_PREFERRED_WARMUP,required_warmup(buy+sell+avoid))

    def fail(code: str, message: str, data_info: dict | None = None) -> dict:
        return {
            "symbol": symbol, "market": market, "error_code": code, "error": message,
            "trades": [], "orders": [], "rules": rules, "data_info": data_info or {},
        }

    ordinary_prefixes = ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "689")
    if market != "CN" or len(symbol) != 6 or not symbol.isdigit() or not symbol.startswith(ordinary_prefixes):
        return fail("unsupported_instrument", "当前仅支持沪深普通 A 股；ETF、B股、北交所及港美股等场景未适配")
    lot_size = 200 if symbol.startswith(("688", "689")) else 100
    limit_pct = limit_threshold(symbol)
    try:
        initial_capital = float(initial_capital)
        if not math.isfinite(initial_capital) or initial_capital <= 0:
            raise ValueError
        requested_days = int(days)
        if requested_days <= 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        return fail("invalid_parameters", "初始资金和回测天数必须为正数")
    try:
        start_day = date.fromisoformat(start) if start else None
        end_day = date.fromisoformat(end) if end else None
    except (TypeError, ValueError):
        return fail("invalid_date_range", "日期必须为 YYYY-MM-DD")
    if start_day and end_day and start_day > end_day:
        return fail("invalid_date_range", "开始日期不能晚于结束日期")
    cost = cost_model or CostModel()
    if any(not math.isfinite(float(value)) or float(value) < 0 for value in asdict(cost.cfg).values()):
        return fail("invalid_cost_model", "费用和滑点参数必须是非负有限数")

    fetch_days = requested_days + preferred_warmup
    anchor = start_day or end_day
    if anchor:
        fetch_days = max(fetch_days, (date.today() - anchor).days + requested_days + _PREFERRED_WARMUP)
    # Local and network paths each supply one complete series; do not splice qfq/raw.
    loaded = bars_override if bars_override is not None else load_bars(symbol, days=min(20_000, max(120, fetch_days)), market=market)
    valid_closes = [bar for bar in loaded if math.isfinite(bar.close) and bar.close > 0]
    if len(valid_closes) != len(loaded):
        return fail("invalid_price_data", "存在无效收盘价，无法可靠估值；请修复日线数据后再回测")
    if any(not math.isfinite(value) for bar in valid_closes for value in (bar.open, bar.high, bar.low, bar.volume)):
        return fail("invalid_price_data", "日线包含非有限数值，无法可靠计算指标")
    bars = sorted(valid_closes, key=lambda bar: bar.date)
    if len({bar.date for bar in bars}) != len(bars):
        return fail("duplicate_dates", "同一日期存在多条日线，无法确定唯一价格序列")
    if len(bars) < _MIN_HISTORY:
        return fail("insufficient_history", "历史数据不足（需至少 60 根日K及所选区间）")
    eligible = [index for index, bar in enumerate(bars) if (not start_day or bar.date >= start_day.isoformat()) and (not end_day or bar.date <= end_day.isoformat())]
    if not start_day:
        eligible = eligible[-requested_days:]
    if not eligible:
        return fail("empty_date_range", "所选区间没有可用日线；请核对日期与数据覆盖范围")
    # The first 59 observations are context, not simulated investment days.
    period = [index for index in eligible if index >= _MIN_HISTORY - 1]
    if not period:
        return fail("insufficient_warmup", "所选区间缺少至少 59 根前置日线，无法初始化策略指标")
    first, last_index = period[0], period[-1]
    available_start, available_end = bars[0].date, bars[-1].date
    truncated_for_warmup = first > eligible[0]
    offset = max(0, first - preferred_warmup)
    bars = bars[offset:last_index + 1]
    period = [index - offset for index in period]
    first, last_index = period[0], period[-1]
    used_bars = bars[:last_index + 1]
    adjustments = sorted({bar.adjustment for bar in used_bars})
    if len(adjustments) > 1:
        return fail("mixed_adjustment", "输入混有不同复权口径，拒绝拼接回测", {"adjustments": adjustments})
    sources = sorted({bar.source for bar in used_bars})
    limitations = [
        "仅按普通非ST、常规交易日的涨跌幅与最小交易单位假设模拟；无法仅凭代码识别历史ST、上市初期及临时停复牌特殊规则。",
        "信号在收盘后产生，下一根日线开盘尝试成交；涨跌停、零成交量保守不成交，不模拟真实排队与盘口流动性。",
        "未成交委托只尝试一次，后续交易需重新满足收盘信号；期末未平仓仅按收盘价估值，不虚构卖出。",
        "不单独计现金分红、配股、送转、税率历史变化及券商差异；获利盘是历史量价近似值。",
        "买入持有基准为区间首末收盘价涨跌幅，不计交易成本；胜率与单笔盈亏指标只统计已平仓交易。",
    ]
    if adjustments == ["qfq"]:
        limitations.append("前复权价格用于连续收益模拟，不等于当时真实成交价；非逐时点复权数据可能带入公司行为调整。")
    elif adjustments == ["raw"]:
        limitations.append("原始日线未校正除权除息跳空，跨公司行为区间的收益与涨跌停判断可能失真。")
    else:
        limitations.append("上游未提供复权元数据，复权口径未知；本结果不应视为可审计的真实成交复刻。")
    if "marketdata:KlineCollector" in sources:
        limitations.append("网络适配器未透出底层供应商，source 标识采集路径而非供应商身份；未与本地价序列拼接。")
    if first < _PREFERRED_WARMUP:
        limitations.append(f"仅有 {first} 根前置日线，获利盘等较长窗口会使用可得历史，不是完整120日窗口。")
    if truncated_for_warmup:
        limitations.append("所选区间前部因缺少指标预热数据而截短，实际收益区间以 actual_start/actual_end 为准。")
    if lot_size == 200:
        limitations.append("科创板买入采用200股整数倍的保守简化，不模拟200股以上逐股递增申报。")
    evidence_rows = [bar.__dict__ for bar in used_bars]
    data_info = {
        "source": sources[0] if len(sources) == 1 else "multiple_identified_sources",
        "sources": sources, "adjustment": adjustments[0],
        "data_hash": hashlib.sha256(json.dumps(evidence_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "available_start": available_start, "available_end": available_end,
        "requested_start": start, "requested_end": end,
        "actual_start": bars[first].date, "actual_end": bars[last_index].date,
        "warmup_start": bars[0].date, "warmup_bars": first,
        "lot_size": lot_size, "limit_pct": limit_pct,
        "cost_config": asdict(cost.cfg), "limitations": limitations,
    }
    rows = [compute_features(bars, index, extended=False) for index in range(last_index + 1)]
    if config or any('tf_' in json.dumps(c) for c in buy+sell+avoid):
        expanded=field_rows(bars)
        for row,values in zip(rows,expanded):
            for key,value in values.items():
                if key.startswith('tf_'):row[key]=value
        rows=attach(rows,bars,config,symbol)
        data_info['factor_version']=CATALOG['version']
    known_bars=sum(all(evaluate(rows,i,c) is not None for c in buy+avoid) for i in period)
    data_info['condition_coverage']={'known_entry_bars':known_bars,'total_bars':len(period)}
    if buy and known_bars==0:
        return fail('missing_factor_data','所选区间没有可完整判断入场与禁买条件的日期，请检查指标预热、历史财务或交易状态数据',data_info)
    cash, shares = initial_capital, 0
    entry_order: dict | None = None
    entry_index = 0
    pending: dict | None = None
    orders: list[dict] = []
    trades: list[dict] = []
    curve: list[dict] = []

    def signal(side: str, index: int) -> dict:
        def evidence(conditions):
            return [{**c,'actual':rows[index-c.get('offset',0)].get(c['field']) if index>=c.get('offset',0) and not c.get('children') else None,'matched':evaluate(rows,index,c),'known':evaluate(rows,index,c) is not None} for c in conditions]
        conditions = {
            "buy": evidence(buy),
            "sell": evidence(sell),
            "avoid": evidence(avoid),
        }
        return {
            "side": side, "signal_date": bars[index].date,
            "execution_date": None, "status": "pending", "quantity": 0,
            "conditions": conditions, "fees": None, "reason_code": "", "reason": "",
        }

    for index in period:
        bar = bars[index]
        if pending is not None:
            order = pending
            pending = None
            order["execution_date"] = bar.date
            failure = _execution_failure(bar, bars[index - 1].close, order["side"], limit_pct)
            fill = None
            if failure is None and order["side"] == "buy" and shares == 0:
                fill = _affordable_fill(cost, cash, bar.open, lot_size)
                if fill is None:
                    failure = ("insufficient_cash_for_lot", f"扣除费用与滑点后不足买入 {lot_size} 股，未成交")
            elif failure is None and order["side"] == "sell" and shares > 0:
                if index <= entry_index:
                    failure = ("t_plus_one", "当日买入不能当日卖出")
                else:
                    fill = cost.fill("sell", bar.open, shares)
            if fill is not None:
                order.update({
                    "status": "filled", "quantity": fill.quantity,
                    "reference_price": bar.open, "fill_price": fill.fill_price,
                    "gross": fill.gross, "cash_delta": fill.cash_delta,
                    "cash_before": round(cash, 4), "fees": _fee_evidence(fill),
                })
                cash += fill.cash_delta
                if abs(cash) < 1e-8:
                    cash = 0.0
                order["cash_after"] = round(cash, 4)
                if order["side"] == "buy":
                    shares, entry_index, entry_order = fill.quantity, index, order
                else:
                    assert entry_order is not None
                    invested = -entry_order["cash_delta"]
                    pnl = fill.cash_delta - invested
                    trades.append({
                        "entry_signal_date": entry_order["signal_date"],
                        "entry_date": entry_order["execution_date"],
                        "entry_price": entry_order["fill_price"],
                        "exit_signal_date": order["signal_date"], "exit_date": bar.date,
                        "exit_price": fill.fill_price, "quantity": shares,
                        "pnl": round(pnl, 2), "pnl_pct": round(pnl / invested * 100, 2),
                        "hold_days": index - entry_index,
                        "buy_fees": entry_order["fees"]["explicit_fees"],
                        "sell_fees": fill.explicit_fees,
                        "fees": round(entry_order["fees"]["explicit_fees"] + fill.explicit_fees, 4),
                        "slippage_cost": round(entry_order["fees"]["slippage_cost"] + fill.slippage_cost, 4),
                        "trigger_conditions": {"entry": entry_order["conditions"], "exit": order["conditions"]},
                    })
                    shares, entry_order = 0, None
            else:
                code, reason = failure or ("position_state_changed", "持仓状态与信号不一致，未成交")
                order.update({"status": "unfilled", "reason_code": code, "reason": reason})
            orders.append(order)

        # Suspended/invalid bars cannot generate new signals. Existing holdings are
        # still marked using the available closing price, without inventing fills.
        valid_signal_bar = (
            math.isfinite(bar.volume) and bar.volume > 0
            and all(math.isfinite(value) and value > 0 for value in (bar.open, bar.high, bar.low, bar.close))
            and bar.high >= max(bar.open, bar.close, bar.low)
            and bar.low <= min(bar.open, bar.close, bar.high)
        )
        row = rows[index]
        if valid_signal_bar:
            risk = execution_check(config.get('execution',{}),bar.close,entry_order['fill_price'] if entry_order else None,index-entry_index+1 if shares else None)
            if shares > 0 and (combine([evaluate(rows,index,c) for c in sell],'any') is True or any(c['ok'] for c in risk)):
                pending = signal("sell", index)
                pending['execution_checks']=risk
            elif shares == 0 and buy and combine([evaluate(rows,index,c) for c in buy]) is True:
                candidate = signal("buy", index)
                if combine([evaluate(rows,index,c) for c in avoid],'any') is True:
                    candidate.update({"status": "unfilled", "reason_code": "avoid_condition", "reason": "禁买条件命中，未发出买入委托"})
                    orders.append(candidate)
                elif any(evaluate(rows,index,condition) is None for condition in avoid):
                    candidate.update({"status": "unfilled", "reason_code": "unknown_avoid_condition", "reason": "禁买条件所需数据缺失，保守不买入"})
                    orders.append(candidate)
                else:
                    pending = candidate
        equity = cash + shares * bar.close
        curve.append({"date": bar.date, "close": bar.close, "equity": equity, "cash": cash, "shares": shares})
    if pending is not None:
        pending.update({"status": "unfilled", "reason_code": "no_next_bar", "reason": "区间已结束，没有下一根可执行日线"})
        orders.append(pending)

    last = bars[last_index]
    equity = cash + shares * last.close
    period_bars = [bars[index] for index in period]
    base_close = bars[first].close
    buy_hold = (last.close / base_close - 1) * 100
    buy_dates = {order["execution_date"] for order in orders if order["status"] == "filled" and order["side"] == "buy"}
    sell_dates = {order["execution_date"] for order in orders if order["status"] == "filled" and order["side"] == "sell"}
    curve_out = [
        {"date": point["date"], "price_pct": round((point["close"] / base_close - 1) * 100, 2),
         "equity_pct": round((point["equity"] / initial_capital - 1) * 100, 2),
         "equity": round(point["equity"], 4), "cash": round(point["cash"], 4), "shares": point["shares"],
         "buy": point["date"] in buy_dates, "sell": point["date"] in sell_dates}
        for point in curve
    ]
    metrics = _metrics(trades=trades, curve=curve, equity=equity, initial_capital=initial_capital, bars=period_bars)
    total_return = round((equity / initial_capital - 1) * 100, 2)
    rule_text, _ = _explain_rules(buy, sell, avoid)
    filled_count = sum(order["status"] == "filled" for order in orders)
    unfilled_count = sum(order["status"] == "unfilled" for order in orders)
    if not filled_count:
        explain = f"区间 {len(period_bars)} 根日线没有触发任何交易成交，未成交/禁买记录 {unfilled_count} 条；请查看逐笔原因，不代表策略已通过验证。"
    else:
        explain = f"区间 {len(period_bars)} 根日线，{filled_count} 次成交、{len(trades)} 笔完整买卖，未成交 {unfilled_count} 条；信号收盘产生、次日开盘尝试，收益已计费用与滑点。"
        if shares:
            explain += " 期末仍持仓，未虚构平仓；胜率仅统计已平仓交易。"
    open_position = None
    if shares and entry_order:
        open_position = {
            "entry_signal_date": entry_order["signal_date"], "entry_date": entry_order["execution_date"],
            "entry_price": entry_order["fill_price"], "quantity": shares,
            "invested": round(-entry_order["cash_delta"], 4), "market_value": round(shares * last.close, 4),
            "unrealized_pnl": round(shares * last.close + entry_order["cash_delta"], 4),
            "fees": entry_order["fees"], "trigger_conditions": entry_order["conditions"],
        }
    return {
        "symbol": symbol, "market": market,
        "start": period_bars[0].date, "end": last.date, "bars": len(period_bars),
        "initial_capital": initial_capital, "final_cash": round(cash, 4), "final_equity": round(equity, 4),
        "trades": trades, "trade_count": len(trades), "orders": orders,
        "filled_count": filled_count, "unfilled_count": unfilled_count, "open_position": open_position,
        "win_rate": metrics["win_rate"], "total_return": total_return,
        "buy_hold_return": round(buy_hold, 2), "excess_return": round(total_return - buy_hold, 2),
        "current_status": "持仓中" if shares > 0 else "观望中", "curve": curve_out,
        "rules": rules, "rule_text": rule_text, "explain": explain, "metrics": metrics,
        "pattern": diagnose(bars, last_index, rows[last_index]), "current_features": rows[last_index],
        "data_info": data_info,
    }
