"""形态与量价特征引擎。

把「高开低走」「放量」「冲高回落」「放量高开低走后没修复」这类盘感，
变成可计算、可回测、可让 AI 引用的字段。

设计约束：

- **无未来函数**:索引 ``idx`` 处的特征只用 ``bars[0..idx]``;
- **只读**:不访问数据库、不发网络请求,纯函数,方便单测与回测复用;
- 字段目录 ``FEATURES`` 是唯一来源,策略卡、回测、前端下拉都从它生成。

字段分三组:

1. 涨跌/位置:当日涨跌幅、近 3/5/10/20 日涨跌、距均线、距 20 日新高;
2. 量能与形态:量能比、开盘跳空、日内实体、上影线、收盘位置、
   放量高开低走标记与「未修复天数」、连续跌破 5 日线天数;
3. 筹码:获利盘(近似)。
"""

from __future__ import annotations

from typing import Any, Sequence

# ── 形态判定阈值(集中在这里,方便调参与写文档)────────────────────────────
GAP_DROP_GAP_PCT = 2.0  # 高开幅度 ≥ +2%
GAP_DROP_BODY_PCT = -3.0  # 日内实体 ≥ -3%(高开后明显回落)
GAP_DROP_VOL_RATIO = 1.3  # 量能 ≥ 5 日均量的 1.3 倍
GAP_DROP_LOOKBACK = 20  # 事件回看窗口(交易日)
REPAIR_TOLERANCE = 0.995  # 收盘 ≥ 事件开盘价 × 0.995 视为已修复
PROFIT_WINDOW = 120  # 获利盘回看窗口
PROFIT_DECAY = 0.98  # 获利盘按成交量加权的日衰减

# ── 字段目录 ───────────────────────────────────────────────────────────────
# group 供前端分组展示;unit 供格式化。
FEATURES: list[dict] = [
    {"key": "change_pct", "label": "当日涨跌幅", "unit": "%", "group": "涨跌"},
    {"key": "runup_3d", "label": "近3日涨跌幅", "unit": "%", "group": "涨跌"},
    {"key": "runup_5d", "label": "近5日涨跌幅", "unit": "%", "group": "涨跌"},
    {"key": "runup_10d", "label": "近10日涨跌幅", "unit": "%", "group": "涨跌"},
    {"key": "runup_20d", "label": "近20日涨跌幅", "unit": "%", "group": "涨跌"},
    {"key": "ma5_dev", "label": "距5日均线", "unit": "%", "group": "均线"},
    {"key": "ma20_dev", "label": "距20日均线", "unit": "%", "group": "均线"},
    {"key": "high20_gap", "label": "距20日新高", "unit": "%", "group": "均线"},
    {"key": "ma5_below_days", "label": "连续跌破5日线", "unit": "天", "group": "均线"},
    {"key": "volume_ratio_5d", "label": "量能比(5日)", "unit": "倍", "group": "量价形态"},
    {"key": "open_gap", "label": "开盘跳空", "unit": "%", "group": "量价形态"},
    {"key": "body_pct", "label": "日内实体", "unit": "%", "group": "量价形态"},
    {"key": "upper_shadow", "label": "上影线", "unit": "%", "group": "量价形态"},
    {"key": "close_pos", "label": "收盘位置", "unit": "%", "group": "量价形态"},
    {"key": "gap_drop_flag", "label": "放量高开低走(当日)", "unit": "", "group": "量价形态"},
    {"key": "gap_drop_days", "label": "高开低走未修复天数", "unit": "天", "group": "量价形态"},
    {"key": "profit_ratio", "label": "获利盘(近似)", "unit": "%", "group": "筹码"},
]

FIELD_MAP = {f["key"]: f for f in FEATURES}
VALID_FIELDS = set(FIELD_MAP)


def _num(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v


def _ret(bars: Sequence[Any], idx: int, n: int) -> float | None:
    if idx - n < 0:
        return None
    past = float(bars[idx - n].close)
    if past <= 0:
        return None
    return round((float(bars[idx].close) / past - 1) * 100, 2)


def _ma(bars: Sequence[Any], idx: int, n: int) -> float | None:
    if idx - n + 1 < 0:
        return None
    window = [float(b.close) for b in bars[idx - n + 1: idx + 1]]
    return sum(window) / len(window) if window else None


def _volume_ratio(bars: Sequence[Any], idx: int, n: int = 5) -> float | None:
    """今日成交量 / 前 n 日均量(不含今日)。"""
    if idx - n < 0:
        return None
    prev = [float(b.volume or 0) for b in bars[idx - n: idx]]
    base = sum(prev) / len(prev) if prev else 0.0
    if base <= 0:
        return None
    return round(float(bars[idx].volume or 0) / base, 2)


def _profit_ratio(bars: Sequence[Any], idx: int, window: int = PROFIT_WINDOW) -> float | None:
    """获利盘近似:过去 N 日按成交量加权,成本(均价)低于现价的占比。

    真实筹码分布要考虑换手衰减;这里用「越近权重越高」的指数加权近似,
    方向一致、数值有偏差,界面与提示里都标注为近似值。
    """
    start = max(0, idx - window + 1)
    price = float(bars[idx].close)
    total = 0.0
    profit = 0.0
    for j in range(start, idx + 1):
        b = bars[j]
        weight = float(b.volume or 0) * (PROFIT_DECAY ** (idx - j))
        total += weight
        avg_cost = (float(b.high) + float(b.low) + float(b.close)) / 3.0
        if avg_cost <= price:
            profit += weight
    return round(profit / total * 100, 2) if total > 0 else None


def gap_drop_event(bars: Sequence[Any], idx: int) -> dict | None:
    """判断 ``idx`` 当天是否「放量高开低走」,返回事件明细或 None。

    条件(全部满足):

    - 开盘相对昨收高开 ≥ :data:`GAP_DROP_GAP_PCT`;
    - 日内实体 ``(close-open)/open`` ≤ :data:`GAP_DROP_BODY_PCT`;
    - 收盘价低于昨收(收跌);
    - 量能比(5 日) ≥ :data:`GAP_DROP_VOL_RATIO`。
    """
    if idx <= 0 or idx >= len(bars):
        return None
    prev = float(bars[idx - 1].close or 0)
    bar = bars[idx]
    open_ = float(bar.open or 0)
    close = float(bar.close or 0)
    if prev <= 0 or open_ <= 0:
        return None

    gap_pct = (open_ / prev - 1) * 100
    body_pct = (close - open_) / open_ * 100
    vol_ratio = _volume_ratio(bars, idx)
    if gap_pct < GAP_DROP_GAP_PCT:
        return None
    if body_pct > GAP_DROP_BODY_PCT:
        return None
    if close >= prev:
        return None
    if vol_ratio is None or vol_ratio < GAP_DROP_VOL_RATIO:
        return None

    high = float(bar.high or max(open_, close))
    upper = (high - max(open_, close)) / prev * 100
    return {
        "index": idx,
        "date": str(bar.date),
        "open": round(open_, 3),
        "close": round(close, 3),
        "high": round(high, 3),
        "prev_close": round(prev, 3),
        "gap_pct": round(gap_pct, 2),
        "body_pct": round(body_pct, 2),
        "change_pct": round((close / prev - 1) * 100, 2),
        "vol_ratio": vol_ratio,
        "upper_shadow": round(upper, 2),
    }


def unrepaired_gap_drop(bars: Sequence[Any], idx: int, lookback: int = GAP_DROP_LOOKBACK) -> dict | None:
    """最近一次「放量高开低走」且至今未修复的事件(不含今天本身)。

    「修复」定义为事件之后任一交易日收盘 ≥ 事件开盘价 × :data:`REPAIR_TOLERANCE`,
    即重新站回高开当天开盘位置(大部分追高筹码解套)。
    """
    if idx <= 1:
        return None
    latest: dict | None = None
    start = max(1, idx - lookback)
    for d in range(start, idx):
        event = gap_drop_event(bars, d)
        if event is None:
            continue
        repair_target = event["open"] * REPAIR_TOLERANCE
        repaired = any(float(bars[j].close) >= repair_target for j in range(d + 1, idx + 1))
        if repaired:
            continue
        peak = max(float(bars[j].close) for j in range(d + 1, idx + 1))
        latest = {
            **event,
            "days": idx - d,
            "peak_close": round(peak, 3),
            "repair_pct": round((peak / event["open"] - 1) * 100, 2),
        }
    return latest


def compute_features(bars: Sequence[Any], idx: int) -> dict:
    """返回 ``idx`` 当日的全部特征(键与 :data:`FEATURES` 一致,值可能为 None)。"""
    if not bars or idx < 0 or idx >= len(bars):
        return {}
    bar = bars[idx]
    price = float(bar.close)
    prev = float(bars[idx - 1].close) if idx > 0 else 0.0
    high = float(bar.high or price)
    low = float(bar.low or price)
    open_ = float(bar.open or price)

    ma5 = _ma(bars, idx, 5)
    ma20 = _ma(bars, idx, 20)
    highs = [float(b.high) for b in bars[max(0, idx - 19): idx + 1]]

    # 连续跌破 5 日线天数(最多数到 20 天,避免长表遍历)
    below_days = 0
    for j in range(idx, max(-1, idx - 20), -1):
        m5 = _ma(bars, j, 5)
        if m5 is None or float(bars[j].close) >= m5:
            break
        below_days += 1

    today_event = gap_drop_event(bars, idx)
    unrepaired = unrepaired_gap_drop(bars, idx)

    return {
        "change_pct": round((price / prev - 1) * 100, 2) if prev > 0 else None,
        "runup_3d": _ret(bars, idx, 3),
        "runup_5d": _ret(bars, idx, 5),
        "runup_10d": _ret(bars, idx, 10),
        "runup_20d": _ret(bars, idx, 20),
        "ma5_dev": round((price / ma5 - 1) * 100, 2) if ma5 else None,
        "ma20_dev": round((price / ma20 - 1) * 100, 2) if ma20 else None,
        "high20_gap": round((price / max(highs) - 1) * 100, 2) if highs else None,
        "ma5_below_days": below_days,
        "volume_ratio_5d": _volume_ratio(bars, idx),
        "open_gap": round((open_ / prev - 1) * 100, 2) if prev > 0 and open_ > 0 else None,
        "body_pct": round((price - open_) / open_ * 100, 2) if open_ > 0 else None,
        "upper_shadow": round((high - max(open_, price)) / prev * 100, 2) if prev > 0 else None,
        "close_pos": round((price - low) / (high - low) * 100, 1) if high > low else 50.0,
        "gap_drop_flag": 1 if today_event else 0,
        "gap_drop_days": int(unrepaired["days"]) if unrepaired else 0,
        "profit_ratio": _profit_ratio(bars, idx),
    }


def _fmt_pct(value: Any) -> str:
    v = _num(value)
    return f"{v:+.1f}%" if v is not None else "--"


def diagnose(bars: Sequence[Any], idx: int, features: dict | None = None) -> dict:
    """形态诊断:给出门槛化的标签 + 一句话白话 + 风险提示。

    供策略检查、买卖前体检与 AI 提示词共用,保证「我们看到的是同一件事」。
    """
    if not bars or idx < 0 or idx >= len(bars):
        return {"tags": [], "summary": "", "risk_hint": "", "event": None}

    f = features or compute_features(bars, idx)
    tags: list[str] = []
    event = gap_drop_event(bars, idx)
    unrepaired = unrepaired_gap_drop(bars, idx)

    if event:
        tags.append("放量高开低走")
    if unrepaired:
        tags.append(f"高开低走未修复({unrepaired['days']}天)")
    vr = _num(f.get("volume_ratio_5d"))
    if vr is not None and vr >= GAP_DROP_VOL_RATIO:
        tags.append("放量")
    elif vr is not None and vr <= 0.7:
        tags.append("缩量")
    upper = _num(f.get("upper_shadow"))
    body = _num(f.get("body_pct"))
    if upper is not None and upper >= 2.0 and (body is None or body < 0):
        tags.append("冲高回落")
    below = int(f.get("ma5_below_days") or 0)
    if below >= 2:
        tags.append(f"跌破5日线({below}天)")
    ma5_dev = _num(f.get("ma5_dev"))
    ma20_dev = _num(f.get("ma20_dev"))
    if ma5_dev is not None and ma20_dev is not None and ma5_dev > 0 and ma20_dev > 0:
        tags.append("均线上方")
    elif ma5_dev is not None and ma20_dev is not None and ma5_dev < 0 and ma20_dev < 0:
        tags.append("均线下方")
    runup10 = _num(f.get("runup_10d"))
    chg = _num(f.get("change_pct"))
    if runup10 is not None and runup10 >= 10 and chg is not None and chg <= -2:
        tags.append("上涨后回落")
    high_gap = _num(f.get("high20_gap"))
    if high_gap is not None and high_gap >= -0.5:
        tags.append("20日新高附近")
    profit = _num(f.get("profit_ratio"))
    if profit is not None and profit >= 90:
        tags.append("获利盘高")

    parts: list[str] = []
    if unrepaired:
        parts.append(
            f"近10日{_fmt_pct(f.get('runup_10d'))}，{unrepaired['date']} 高开"
            f"{unrepaired['gap_pct']:+.1f}% 后收跌 {unrepaired['change_pct']:+.1f}%"
            f"（实体 {unrepaired['body_pct']:+.1f}%，量能 {unrepaired['vol_ratio']:.1f} 倍）；"
            f"此后 {unrepaired['days']} 个交易日最高只回到 {unrepaired['repair_pct']:+.1f}%，"
            f"未收复当日开盘价 {unrepaired['open']:.2f}，属放量高开低走后的未修复弱势。"
        )
    elif event:
        parts.append(
            f"今日高开 {event['gap_pct']:+.1f}% 后回落（实体 {event['body_pct']:+.1f}%），"
            f"量能 {event['vol_ratio']:.1f} 倍，收盘位于当日振幅 {(f.get('close_pos') or 0):.0f}% 位置。"
        )
    else:
        parts.append(
            f"近10日{_fmt_pct(f.get('runup_10d'))}，今日{_fmt_pct(f.get('change_pct'))}，"
            f"量能 {(vr if vr is not None else 0):.2f} 倍，"
            f"收盘位于当日振幅 {(f.get('close_pos') if f.get('close_pos') is not None else 50):.0f}% 位置。"
        )

    if unrepaired:
        risk_hint = (
            "放量高开低走且至今未修复：追高筹码被套，短线偏弱，"
            f"等收盘重新站上 {unrepaired['open']:.2f}（事件日开盘价）再考虑。"
        )
    elif event:
        risk_hint = "今日放量高开低走：先观察 1~2 日能否收复开盘价，不宜当天追高。"
    elif below >= 3:
        risk_hint = "连续跌破 5 日线，短线走弱，等企稳再看。"
    else:
        risk_hint = ""

    return {
        "tags": tags[:8],
        "summary": "".join(parts),
        "risk_hint": risk_hint,
        "event": unrepaired or event,
    }


def recent_bars_text(bars: Sequence[Any], idx: int, n: int = 6) -> str:
    """最近 n 根日K的紧凑文本表(AI 提示词用)。"""
    if not bars:
        return ""
    start = max(0, idx - n + 1)
    lines = []
    for j in range(start, idx + 1):
        b = bars[j]
        prev = float(bars[j - 1].close) if j > 0 else float(b.open)
        chg = (float(b.close) / prev - 1) * 100 if prev else 0
        gap = (float(b.open) / prev - 1) * 100 if prev else 0
        body = (float(b.close) - float(b.open)) / float(b.open) * 100 if float(b.open) else 0
        lines.append(
            f"{b.date} 开{float(b.open):.2f} 高{float(b.high):.2f} 低{float(b.low):.2f} "
            f"收{float(b.close):.2f} 涨跌{chg:+.2f}% 跳空{gap:+.2f}% 实体{body:+.2f}%"
        )
    return "\n".join(lines)


# ── 条件 -> 中文 ───────────────────────────────────────────────────────────
_OPS_TEXT = {
    "gte": "≥",
    "lte": "≤",
    "gt": ">",
    "lt": "<",
    "eq": "=",
    "ne": "≠",
}


def format_value(field: str, value: Any) -> str:
    """把条件值格式化成带单位的字符串。"""
    unit = FIELD_MAP.get(field, {}).get("unit", "")
    if isinstance(value, (list, tuple)):
        return " ~ ".join(f"{_num(v) if _num(v) is not None else v}{unit}" for v in value)
    v = _num(value)
    if v is None:
        return str(value)
    if field == "gap_drop_flag":
        return "是" if v else "否"
    return f"{v:g}{unit}"


def describe_condition(cond: dict) -> str:
    """单条条件 -> 「近3日涨跌幅 介于 -5% ~ 0%」。"""
    field = str(cond.get("field") or "")
    op = str(cond.get("op") or "eq")
    value = cond.get("value")
    label = FIELD_MAP.get(field, {}).get("label", field or "?")
    if op == "between" and isinstance(value, (list, tuple)) and len(value) == 2:
        return f"{label} 介于 {format_value(field, value)}"
    if op in ("in", "not_in"):
        op_text = "属于" if op == "in" else "不属于"
        return f"{label} {op_text} {value}"
    if op == "contains":
        return f"{label} 包含 {value}"
    return f"{label} {_OPS_TEXT.get(op, op)} {format_value(field, value)}"


def describe_conditions(conds: list[dict] | None, *, joiner: str = "、") -> str:
    items = [describe_condition(c) for c in (conds or []) if isinstance(c, dict) and c.get("field")]
    return joiner.join(items)
