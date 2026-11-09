"""选股执行器 + 涨停天梯 + 板块强弱。

三个阶段(顺序固定,不能乱):
1. 粗筛:本地快照表上按行情字段过滤,5500 只缩到几百只;
2. 精算:叠加涨停池(连板数/封单/炸板)与概念索引;
3. 补齐:只对最终结果的前 N 只拉 20 日收盘做迷你图。

所有网络请求都放在阶段 1 之外,所以条件越多、结果越少,反而越快。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, timedelta

from src.modules.screener import concepts as concept_index
from src.modules.screener import snapshot as snapshot_mod
from src.modules.screener.constants import FIELD_SCALE, FIELD_KEYS

logger = logging.getLogger(__name__)

_LADDER_CACHE: dict = {"date": "", "ts": 0.0, "items": [], "local": False}
_LADDER_TTL_S = 120.0
_LADDER_LOCK = threading.Lock()

_SPARK_CACHE: dict[tuple[str, str], list[float]] = {}
_SPARK_LOCK = threading.Lock()


# ── 涨停池 ────────────────────────────────────────────────────────────
def _compact(date_str: str) -> str:
    return date_str.replace("-", "")


def _limit_threshold(code: str) -> float:
    """按板块返回涨停阈值(留 0.5 个点容差,避免四舍五入误差)。"""
    if code.startswith(("30", "68")):
        return 0.19
    if code.startswith(("4", "8")) or code.startswith("92"):
        return 0.29
    return 0.09


def _local_ladder(trade_date: str, *, days: int = 21) -> list[dict]:
    """用本地 daily_bars 推算当日涨停与连板数(涨停池不可用时的兜底)。

    只能给出:代码、名称、涨跌幅、连板数、涨停统计(近似)。
    封板资金、首封时间、炸板次数这些只有东财涨停池才有,这里留空。
    """
    from sqlalchemy import text

    from src.platform.persistence.database import SessionLocal

    end = date.fromisoformat(trade_date)
    since = (end - timedelta(days=max(30, days * 2))).isoformat()
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                "SELECT symbol, trade_date, close, prev_close FROM ("
                "  SELECT symbol, trade_date, close,"
                "         LAG(close) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_close"
                "  FROM daily_bars WHERE trade_date BETWEEN :since AND :day"
                ") WHERE prev_close IS NOT NULL AND prev_close > 0"
            ),
            {"since": since, "day": trade_date},
        ).fetchall()
    finally:
        db.close()
    if not rows:
        return []

    dates = sorted({str(r[1]) for r in rows})
    if trade_date not in dates:
        return []
    day_index = {d: i for i, d in enumerate(dates)}

    hit: dict[tuple[str, str], float] = {}
    pct_by_symbol: dict[str, float] = {}
    for symbol, day, close, prev in rows:
        code = str(symbol)
        pct = (float(close) - float(prev)) / float(prev)
        if pct >= _limit_threshold(code):
            hit[(code, str(day))] = pct * 100
            if str(day) == trade_date:
                pct_by_symbol[code] = pct * 100
    if not pct_by_symbol:
        return []

    try:
        names = {r["symbol"]: r.get("name") or "" for r in snapshot_mod.get_snapshot()}
    except Exception:  # noqa: BLE001
        names = {}

    items: list[dict] = []
    for symbol, pct in pct_by_symbol.items():
        streak = 0
        i = day_index[trade_date]
        while i >= 0 and (symbol, dates[i]) in hit:
            streak += 1
            i -= 1
        items.append(
            {
                "symbol": symbol,
                "name": names.get(symbol, ""),
                "change_pct": round(pct, 2),
                # 本地只算得上「连续涨停天数」,涨停统计用同一值近似
                "limit_up_days": streak,
                "stat_days": streak,
                "stat_count": streak,
                "first_seal_time": "",
                "seal_fund": None,
                "break_count": 0,
                "amount": None,
                "turnover_rate": None,
                "industry": "",
                "float_market_cap": None,
            }
        )
    items.sort(key=lambda x: (-x["limit_up_days"], -x["change_pct"]))
    logger.warning(
        f"[选股] 涨停池不可用,已用本地日K推算 {trade_date}: {len(items)} 只涨停"
    )
    return items


def get_zt_pool(date_str: str | None = None) -> list[dict]:
    """当日/指定日涨停池。单次请求,失败返回空列表而不是抛错。"""
    trade_date = date_str or snapshot_mod.latest_trade_date().isoformat()
    now = time.monotonic()
    if (
        _LADDER_CACHE["date"] == trade_date
        and now - _LADDER_CACHE["ts"] <= _LADDER_TTL_S
    ):
        return _LADDER_CACHE["items"]

    with _LADDER_LOCK:
        if (
            _LADDER_CACHE["date"] == trade_date
            and time.monotonic() - _LADDER_CACHE["ts"] <= _LADDER_TTL_S
        ):
            return _LADDER_CACHE["items"]
        items: list[dict] = []
        try:
            from src.platform.marketdata.marketdata_client import get_market_data

            md = get_market_data()
            rows = md.zt_pool(date=_compact(trade_date), limit=300)
            items = [
                {
                    "symbol": r.code,
                    "name": r.name,
                    "change_pct": r.change_pct,
                    "amount": r.amount,
                    "turnover_rate": r.turnover_rate,
                    "limit_up_days": int(r.limit_up_days or 0),
                    "first_seal_time": r.first_seal_time,
                    "seal_fund": r.seal_fund,
                    "break_count": int(r.break_count or 0),
                    "industry": r.industry,
                    "stat_days": int(r.stat_days or 0),
                    "stat_count": int(r.stat_count or 0),
                    "float_market_cap": r.float_market_cap,
                }
                for r in rows
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[选股] 涨停池获取失败({trade_date}): {exc}")
        local = False
        if not items:
            # 东财不可用:用本地日K补出「今日涨停 + 连板数」,否则首板/连板梯队会全是 0
            items = _local_ladder(trade_date)
            local = bool(items)
        _LADDER_CACHE.update(
            date=trade_date, ts=time.monotonic(), items=items, local=local
        )
        return items


_TECH_CACHE: dict = {"date": "", "ts": 0.0, "map": {}}
_TECH_TTL_S = 600.0
_TECH_LOCK = threading.Lock()


def _technical_map(trade_date: str) -> dict[str, dict]:
    """本地日K算出的技术指标(近20日均线/新高/量能)。

    行情快照只有当天,趋势类条件必须回看日K;这里一次批量算全市场,结果缓存 10 分钟。
    ``ma20_dev`` 单位 %,``high20_gap`` 单位 %(0 = 正好新高),``amount_ratio_5d`` 倍数。
    """
    now = time.monotonic()
    if (
        _TECH_CACHE["date"] == trade_date
        and now - _TECH_CACHE["ts"] <= _TECH_TTL_S
    ):
        return _TECH_CACHE["map"]

    with _TECH_LOCK:
        if (
            _TECH_CACHE["date"] == trade_date
            and time.monotonic() - _TECH_CACHE["ts"] <= _TECH_TTL_S
        ):
            return _TECH_CACHE["map"]

        from sqlalchemy import text

        from src.platform.persistence.database import SessionLocal

        end = date.fromisoformat(trade_date)
        since = (end - timedelta(days=45)).isoformat()
        db = SessionLocal()
        try:
            rows = db.execute(
                text(
                    "SELECT symbol, trade_date, close, high, amount FROM ("
                    "  SELECT symbol, trade_date, close, high, amount,"
                    "         ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn"
                    "  FROM daily_bars WHERE trade_date BETWEEN :since AND :day"
                    ") WHERE rn <= 21"
                ),
                {"since": since, "day": trade_date},
            ).fetchall()
        finally:
            db.close()

        grouped: dict[str, list] = {}
        for symbol, day, close, high, amount in rows:
            grouped.setdefault(str(symbol), []).append((str(day), close, high, amount))

        # 前复权日K:除权日不会制造假跳空;没有的股票退回不复权
        try:
            from src.platform.dataset import qfq as qfq_store

            qfq_map = qfq_store.load_recent(days=26)
        except Exception:  # noqa: BLE001
            qfq_map = {}

        # 快照里的现价/成交额(日K可能还没同步到当天)
        try:
            quotes = {r["symbol"]: r for r in snapshot_mod.get_snapshot()}
        except Exception:  # noqa: BLE001
            quotes = {}

        out: dict[str, dict] = {}
        for symbol, bars in grouped.items():
            bars.sort(key=lambda x: x[0], reverse=True)
            if bars and bars[0][0] == trade_date:
                bars = bars[1:]  # 已同步当天,避免和现价重复
            # 成交额是真实金额,不受复权影响,始终用原始表
            amounts = [float(b[3]) for b in bars[:5] if b[3] is not None]

            qbars = [b for b in (qfq_map.get(symbol) or []) if b["date"] != trade_date]
            if len(qbars) >= 15:
                closes = [float(b["close"]) for b in qbars[-20:] if b["close"] is not None]
                highs = [float(b["high"]) for b in qbars[-20:] if b["high"] is not None]
            else:
                closes = [float(b[1]) for b in bars[:20] if b[1] is not None]
                highs = [float(b[2]) for b in bars[:20] if b[2] is not None]
            quote = quotes.get(symbol) or {}
            price = quote.get("price")
            amount = quote.get("amount")

            ma20_dev = None
            if closes:
                window = closes[:19] if price else closes[:20]
                if price:
                    window = [float(price)] + window
                if len(window) >= 15:
                    ma = sum(window) / len(window)
                    if ma > 0:
                        ma20_dev = round((float(price or closes[0]) - ma) / ma * 100, 2)

            high20_gap = None
            if highs:
                peak = max(highs)
                base = float(price) if price else (closes[0] if closes else None)
                if peak > 0 and base:
                    high20_gap = round((base - peak) / peak * 100, 2)

            amount_ratio = None
            if amounts and amount:
                avg = sum(amounts) / len(amounts)
                if avg > 0:
                    amount_ratio = round(float(amount) / avg, 2)

            out[symbol] = {
                "ma20_dev": ma20_dev,
                "high20_gap": high20_gap,
                "amount_ratio_5d": amount_ratio,
            }

        _TECH_CACHE.update(date=trade_date, ts=time.monotonic(), map=out)
        logger.info(f"[选股] 技术指标计算完成: {len(out)} 只")
        return out


def _ladder_by_symbol(date_str: str) -> dict[str, dict]:
    return {x["symbol"]: x for x in get_zt_pool(date_str) if x.get("symbol")}


def ladder_is_local() -> bool:
    """当前涨停池是否来自本地推算(用于给用户提示数据降级)。"""
    return bool(_LADDER_CACHE.get("local"))


# ── 近 N 日涨停(选"回踩""N天M板"必须的历史窗口)─────────────────────
# 当日涨停池里的 stat_days/stat_count 只对「今天涨停」的股票有值,
# 而"近5日有过涨停、今天回调"的股票不在今日池里 —— 必须回看历史池。
_RECENT_ZT_CACHE: dict = {"date": "", "days": 0, "ts": 0.0, "map": {}, "local_dates": []}
_RECENT_ZT_TTL_S = 600.0
_RECENT_ZT_LOCK = threading.Lock()


def _recent_weekdays(end: date, n: int) -> list[date]:
    out: list[date] = []
    d = end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


def _local_limit_up_symbols(day: str) -> set[str]:
    """用本地 daily_bars 推算某日涨停,东财涨停池取不到时兜底。

    阈值按板块:创业板/科创板 19.5%,北交所 29.5%,其余 9.5%;ST 的 5% 涨停会漏,
    但选股规则通常已排除 ST。返回空集表示本地也没有该日数据。
    """
    from sqlalchemy import text

    from src.platform.persistence.database import SessionLocal

    db = SessionLocal()
    try:
        # 昨收取「该股自己最近的一根」,而不是固定前一天:个别股票当天缺一根
        # (停牌/同步漏抓),用固定日期 join 会把它们直接漏掉。
        rows = db.execute(
            text(
                "SELECT b.symbol, b.close, ("
                "  SELECT p.close FROM daily_bars p "
                "  WHERE p.symbol = b.symbol AND p.trade_date < b.trade_date "
                "  ORDER BY p.trade_date DESC LIMIT 1"
                ") AS prev_close "
                "FROM daily_bars b WHERE b.trade_date = :day AND b.close IS NOT NULL"
            ),
            {"day": day},
        ).fetchall()
    finally:
        db.close()

    out: set[str] = set()
    for symbol, close, prev_close in rows:
        try:
            ratio = float(close) / float(prev_close) - 1.0
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        code = str(symbol)
        # 阈值比名义涨跌幅略放宽:涨停价按昨收四舍五入到分,加上前复权与实时价的差异,
        # 实际算出来常常是 19.9x% / 9.9x%,卡 19.5% 会漏掉临界的那一只。
        if code.startswith(("300", "301", "688", "689")):
            threshold = 0.19
        elif code.startswith(("4", "8")) or code.startswith("92"):
            threshold = 0.29
        else:
            threshold = 0.09
        if ratio >= threshold:
            out.add(code)
    return out


def get_recent_limit_up(trade_date: str, days: int = 5) -> dict[str, dict]:
    """近 N 个交易日的涨停统计:{symbol: {count, max_days, last_days_ago}}。

    节假日会返回空池,只影响计数不影响正确性(节假日本来也没有涨停)。
    每个日期一次请求,并发执行,结果缓存 10 分钟。
    """
    now = time.monotonic()
    if (
        _RECENT_ZT_CACHE["date"] == trade_date
        and _RECENT_ZT_CACHE["days"] == days
        and now - _RECENT_ZT_CACHE["ts"] <= _RECENT_ZT_TTL_S
    ):
        return _RECENT_ZT_CACHE["map"]

    with _RECENT_ZT_LOCK:
        if (
            _RECENT_ZT_CACHE["date"] == trade_date
            and _RECENT_ZT_CACHE["days"] == days
            and time.monotonic() - _RECENT_ZT_CACHE["ts"] <= _RECENT_ZT_TTL_S
        ):
            return _RECENT_ZT_CACHE["map"]

        end = date.fromisoformat(trade_date)
        dates = _recent_weekdays(end, max(1, int(days)))

        def _fetch(d: date) -> tuple[date, list]:
            # 东财偶发 TLS 中断,单日失败重试一次;两次都失败再由本地日K兜底
            for attempt in (1, 2):
                try:
                    from src.platform.marketdata.marketdata_client import get_market_data

                    rows = get_market_data().zt_pool(date=d.strftime("%Y%m%d"), limit=300)
                    if rows:
                        return d, rows
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[选股] 历史涨停池 {d} 获取失败({attempt}): {exc}")
                if attempt == 1:
                    time.sleep(0.5)
            return d, []

        local_used: list[str] = []
        day_maps: dict[date, dict[str, int]] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(dates))) as pool:
            for d, rows in pool.map(_fetch, dates):
                remote = {
                    getattr(r, "code", "") or "": int(getattr(r, "limit_up_days", 0) or 0)
                    for r in rows
                    if getattr(r, "code", "")
                }
                if not remote:
                    # 东财涨停池取不到(常见的是 TLS 被中断):用本地日K推算,避免条件全部落空
                    local = _local_limit_up_symbols(d.isoformat())
                    if local:
                        local_used.append(d.isoformat())
                        remote = {s: 0 for s in local}
                day_maps[d] = remote

        result: dict[str, dict] = {}
        for d, remote in day_maps.items():
            ago = (end - d).days
            for sym, zt_days in remote.items():
                bucket = result.setdefault(
                    sym, {"count": 0, "max_days": 0, "last_days_ago": 99}
                )
                bucket["count"] += 1
                bucket["max_days"] = max(bucket["max_days"], zt_days)
                bucket["last_days_ago"] = min(bucket["last_days_ago"], ago)

        # 本地推算拿不到「连板数」,用窗口内的连续天数补一个近似值
        if local_used:
            for sym, bucket in result.items():
                if bucket["max_days"] > 0:
                    continue
                streak = 0
                for d in dates:  # 由近到远
                    if sym not in day_maps.get(d, {}):
                        break
                    streak += 1
                bucket["max_days"] = streak

        _RECENT_ZT_CACHE.update(
            date=trade_date,
            days=days,
            ts=time.monotonic(),
            map=result,
            local_dates=local_used,
        )
        if local_used:
            logger.warning(
                f"[选股] 涨停池取数失败,已用本地日K推算 {len(local_used)} 天: {', '.join(local_used)}"
            )
        logger.info(
            f"[选股] 近{days}日涨停统计完成: {len(result)} 只 / {len(dates)} 个交易日"
        )
        return result


def get_market_stats(date_str: str | None = None) -> dict:
    """涨停/跌停家数与最高连板,供天梯页顶部使用。"""
    trade_date = date_str or snapshot_mod.latest_trade_date().isoformat()
    zt = get_zt_pool(trade_date)
    dt_count = 0
    try:
        from src.platform.marketdata.marketdata_client import get_market_data

        dt_rows = get_market_data().dt_pool(date=_compact(trade_date), limit=200)
        dt_count = len(dt_rows)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[选股] 跌停池获取失败: {exc}")

    max_days = max((x["limit_up_days"] for x in zt), default=0)
    return {
        "trade_date": trade_date,
        "limit_up": len(zt),
        "limit_down": dt_count,
        "max_days": max_days,
    }


# ── 条件求值 ──────────────────────────────────────────────────────────
def _scaled(row: dict, field: str):
    value = row.get(field)
    if value is None:
        return None
    scale = FIELD_SCALE.get(field)
    return value / scale if scale else value


# 概念的「概念/板块/指数」后缀在两家数据源里不一致(东财「光伏概念」/ 通达信「光伏」),
# 比较前统一剥掉,否则用户从另一个入口带过来的名字会一个都匹配不上。
_CONCEPT_SUFFIXES = ("概念", "板块", "指数")


def _norm_concept(name: str) -> str:
    text = str(name or "").strip()
    for suffix in _CONCEPT_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)]
    return text


def _concept_warnings(conditions: list[dict], concept_map: dict) -> list[str]:
    """概念条件里的名字如果今日索引里根本没有,给出提示。

    否则用户只能看到「0 只」,分不清是条件太严还是名字写错。
    """
    wanted: list[str] = []
    for cond in conditions:
        if str(cond.get("field") or "") != "concept":
            continue
        value = cond.get("value")
        values = value if isinstance(value, list) else [value]
        wanted.extend(str(v).strip() for v in values if str(v or "").strip())
    if not wanted:
        return []

    raw_names: set[str] = set()
    for cons in concept_map.values():
        for c in cons:
            name = str(c.get("name") or "").strip()
            if name:
                raw_names.add(name)
    known = {_norm_concept(n) for n in raw_names}

    out: list[str] = []
    for word in dict.fromkeys(wanted):
        key = _norm_concept(word)
        if key in known:
            continue
        near = sorted((n for n in raw_names if key and (key in n or n in key)), key=len)[:2]
        hint = f",最接近的是「{'」「'.join(near)}」" if near else ""
        out.append(
            f"概念「{word}」不在今日板块索引里{hint};请从候选里选,或点「刷新数据」重建索引"
        )
    return out


def _match(row: dict, cond: dict) -> bool:
    field = str(cond.get("field") or "").strip()
    op = str(cond.get("op") or "eq").strip()
    expected = cond.get("value")
    if field not in FIELD_KEYS:
        return False

    if field == "concept":
        names = [_norm_concept(c.get("name", "")) for c in (row.get("concepts") or [])]
        if op in ("in", "not_in"):
            wanted = expected if isinstance(expected, list) else [expected]
            keys = {_norm_concept(w) for w in wanted if str(w or "").strip()}
            hit = any(n in keys for n in names)
            return hit if op == "in" else not hit
        if op == "contains":
            key = _norm_concept(expected)
            return bool(key) and any(key in n for n in names)
        return bool(names)

    actual = _scaled(row, field)
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


def _apply(rows: list[dict], payload: dict) -> list[dict]:
    conditions = payload.get("conditions") or []
    logic = str(payload.get("logic") or "all").lower()
    exclude = set(payload.get("exclude") or [])

    out = []
    for row in rows:
        if "st" in exclude and row.get("is_st"):
            continue
        if "suspended" in exclude and row.get("is_suspended"):
            continue
        checks = [_match(row, c) for c in conditions if c.get("field")]
        if not checks:
            out.append(row)
        elif (all(checks) if logic == "all" else any(checks)):
            out.append(row)
    return out


def _sort(rows: list[dict], order_by: str) -> list[dict]:
    raw = (order_by or "change_pct desc").strip()
    parts = raw.split()
    field = parts[0] if parts else "change_pct"
    desc = len(parts) < 2 or parts[1].lower() != "asc"

    def key(row):
        value = _scaled(row, field)
        if value is None:
            return float("-inf") if desc else float("inf")
        return value

    return sorted(rows, key=key, reverse=desc)


def _board(symbol: str) -> str:
    """按代码段判断上市板块。AI 的「排除创业板/科创板」需要这个字段。"""
    code = str(symbol or "").strip()
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("4", "8")) or code.startswith("92"):
        return "北交所"
    if code.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        return "主板"
    return "其他"


def _tags(row: dict) -> list[str]:
    tags: list[str] = []
    days = int(row.get("limit_up_days") or 0)
    if days >= 2:
        tags.append(f"{days}连板")
    elif days == 1:
        tags.append("首板")
    stat_days = int(row.get("limit_up_stat_days") or 0)
    stat_count = int(row.get("limit_up_stat_count") or 0)
    if stat_count and (days == 0 or stat_days > days):
        tags.append(f"{stat_days}天{stat_count}板")
    if int(row.get("break_count") or 0) > 0 and days == 0:
        tags.append("炸板")
    if row.get("is_st"):
        tags.append("ST")
    return tags


# ── 迷你走势 ──────────────────────────────────────────────────────────
def _spark_for(symbol: str, trade_date: str) -> list[float]:
    key = (symbol, trade_date)
    with _SPARK_LOCK:
        if key in _SPARK_CACHE:
            return _SPARK_CACHE[key]
    try:
        from src.platform.marketdata.collectors.kline_collector import KlineCollector
        from src.platform.marketdata.models import MarketCode

        bars = KlineCollector(MarketCode.CN).get_klines(symbol, days=20)
        closes = [round(float(b.close), 2) for b in bars if b.close is not None][-20:]
    except Exception:  # noqa: BLE001 — 迷你图失败不影响选股本身
        closes = []
    with _SPARK_LOCK:
        _SPARK_CACHE[key] = closes
    return closes


def _attach_spark(rows: list[dict], trade_date: str, limit: int) -> None:
    """优先读本地 daily_bars(0 网络);缺数据的个股再回退逐只 K 线。"""
    targets = rows[: max(0, limit)]
    if not targets:
        return

    local: dict[str, list[float]] = {}
    try:
        from src.platform.dataset.store import recent_closes

        local = recent_closes([r["symbol"] for r in targets], 20)
    except Exception:  # noqa: BLE001 — 本地库不可用时退回网络
        local = {}

    missing = [r for r in targets if len(local.get(r["symbol"], [])) < 2]
    if missing:
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(
                pool.map(lambda r: _spark_for(r["symbol"], trade_date), missing)
            )
        for row, closes in zip(missing, results):
            local[row["symbol"]] = closes

    for row in targets:
        row["spark"] = local.get(row["symbol"], [])


# ── 主流程 ────────────────────────────────────────────────────────────
def run_screen(payload: dict) -> dict:
    trade_date = snapshot_mod.latest_trade_date().isoformat()
    rows = snapshot_mod.get_snapshot(force_refresh=bool(payload.get("refresh")))

    concept_index.ensure_concept_index(trade_date)
    concept_map = concept_index.get_concept_map(trade_date)

    # 涨停池和近5日统计都要联网,并行取并设总预算:东财不通时不能把请求拖到几十秒
    budget = float(payload.get("remote_budget_s") or 8.0)
    remote_timeout: list[str] = []
    ladder_local = False
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        f_ladder = pool.submit(_ladder_by_symbol, trade_date)
        f_recent = pool.submit(get_recent_limit_up, trade_date, 5)
        done, _pending = wait([f_ladder, f_recent], timeout=budget)
        if f_ladder in done:
            try:
                ladder = f_ladder.result()
            except Exception:  # noqa: BLE001
                ladder = {}
        else:
            remote_timeout.append("涨停池")
            ladder_local = True
            ladder = {x["symbol"]: x for x in _local_ladder(trade_date)}
        if f_recent in done:
            try:
                recent_zt = f_recent.result()
            except Exception:  # noqa: BLE001
                recent_zt = {}
        else:
            remote_timeout.append("近5日涨停")
            recent_zt = {}
    finally:
        # 超时的线程留在后台自己收尾,不阻塞本次响应
        pool.shutdown(wait=False, cancel_futures=True)

    tech = _technical_map(trade_date)
    enriched: list[dict] = []
    for base in rows:
        symbol = base["symbol"]
        zt = ladder.get(symbol) or {}
        recent = recent_zt.get(symbol) or {}
        cons = concept_map.get(symbol) or []
        row = dict(base)
        row["limit_up_days"] = int(zt.get("limit_up_days") or 0)
        row["limit_up_stat_days"] = int(zt.get("stat_days") or 0)
        row["limit_up_stat_count"] = int(zt.get("stat_count") or 0)
        row["limit_up_count_5d"] = int(recent.get("count") or 0)
        row["limit_up_days_5d"] = int(recent.get("max_days") or 0)
        row["limit_up_last_days_ago"] = int(recent.get("last_days_ago") or 99)
        row["seal_fund"] = zt.get("seal_fund")
        row["break_count"] = int(zt.get("break_count") or 0)
        row["first_seal_time"] = zt.get("first_seal_time") or ""
        row["concepts"] = cons
        row["board"] = _board(symbol)
        row["concept_rank"] = min((c.get("rank") or 9999) for c in cons) if cons else None
        t = tech.get(symbol) or {}
        row["ma20_dev"] = t.get("ma20_dev")
        row["high20_gap"] = t.get("high20_gap")
        row["amount_ratio_5d"] = t.get("amount_ratio_5d")
        enriched.append(row)

    matched = _apply(enriched, payload)
    matched = _sort(matched, payload.get("order_by") or "change_pct desc")

    # limit<=0 表示返回全部;上限 6000 只(全市场约 5600)
    # offset + limit 供服务端分页:首屏只回一页,避免一次传几 MB
    total = len(matched)
    offset = max(0, int(payload.get("offset") or 0))
    raw_limit = int(payload.get("limit") or 0)
    limit = 6000 if raw_limit <= 0 else max(1, min(raw_limit, 6000))

    # 历史落库只在首屏请求时准备(全量代码 + 前 300 条明细),翻页不算新运行
    history_data = None
    if offset == 0 and payload.get("save", True):
        history_data = {
            "symbols": [r["symbol"] for r in matched],
            "preview": [_history_item(r, i + 1) for i, r in enumerate(matched[:300])],
        }
    matched = matched[offset: offset + limit]

    for row in matched:
        row["tags"] = _tags(row)
        row["spark"] = []

    # 迷你走势默认不再随选股返回(前端按页懒加载),避免一次算几千只
    if bool(payload.get("with_spark", False)):
        _attach_spark(matched, trade_date, int(payload.get("spark_limit") or 80))

    warnings: list[str] = []
    local_dates = _RECENT_ZT_CACHE.get("local_dates") or []
    if local_dates:
        warnings.append(
            f"东财涨停池暂不可用,近 5 日涨停次数已用本地日K推算({len(local_dates)} 个交易日)"
        )
    if remote_timeout:
        warnings.append(
            f"{'、'.join(remote_timeout)}获取超过 {int(budget)} 秒,已按本地数据降级"
        )
    elif not ladder:
        warnings.append("今日涨停池为空:涨停相关字段(连板数、封板资金)可能不准确")
    elif ladder_local or ladder_is_local():
        warnings.append(
            "涨停池暂不可用,连板数已用本地日K推算(封板资金、首封时间、炸板次数缺失)"
        )

    warnings.extend(_concept_warnings(payload.get("conditions") or [], concept_map))

    return {
        "trade_date": trade_date,
        # count/total = 命中的总数;returned = 本次实际返回条数(分页后)
        "count": total,
        "total": total,
        "returned": len(matched),
        "offset": offset,
        "universe": len(rows),
        "items": [_serialize_item(r) for r in matched],
        "as_of": time.strftime("%H:%M"),
        "warnings": warnings,
        "_history": history_data,
        # 回显本次真正生效的条件,前端可直接展示,避免"条件看起来是 A、实际按 B 跑"
        "applied": {
            "logic": str(payload.get("logic") or "all").lower(),
            "conditions": payload.get("conditions") or [],
            "exclude": payload.get("exclude") or [],
            "order_by": payload.get("order_by") or "change_pct desc",
            "limit": limit,
        },
    }


def _yi(value):
    return round(value / 1e8, 2) if value is not None else None


def _history_item(row: dict, rank: int) -> dict:
    """历史明细的冻结字段(够回看,不追求全量)。"""
    return {
        "rank": rank,
        "symbol": row.get("symbol"),
        "name": row.get("name") or "",
        "price": row.get("price"),
        "change_pct": row.get("change_pct"),
        "turnover_rate": row.get("turnover_rate"),
        "amount": _yi(row.get("amount")),
        "float_mv": _yi(row.get("float_mv")),
        "limit_up_days": int(row.get("limit_up_days") or 0),
        "concept_rank": row.get("concept_rank"),
        "concepts": [c.get("name") for c in (row.get("concepts") or [])[:3]],
    }


def _serialize_item(row: dict) -> dict:
    return {
        "symbol": row.get("symbol"),
        "name": row.get("name"),
        "market": "CN",
        "price": row.get("price"),
        "change_pct": row.get("change_pct"),
        "amplitude": row.get("amplitude"),
        "turnover_rate": row.get("turnover_rate"),
        "volume_ratio": row.get("volume_ratio"),
        "amount": _yi(row.get("amount")),
        "float_mv": _yi(row.get("float_mv")),
        "total_mv": _yi(row.get("total_mv")),
        "main_net_inflow": _yi(row.get("main_net_inflow")),
        "ma20_dev": row.get("ma20_dev"),
        "high20_gap": row.get("high20_gap"),
        "amount_ratio_5d": row.get("amount_ratio_5d"),
        "pe_ttm": row.get("pe_ttm"),
        "pb": row.get("pb"),
        "industry": row.get("industry") or "",
        "board": row.get("board") or "",
        "is_st": bool(row.get("is_st")),
        "limit_up_days": row.get("limit_up_days") or 0,
        "limit_up_stat_days": row.get("limit_up_stat_days") or 0,
        "limit_up_stat_count": row.get("limit_up_stat_count") or 0,
        "limit_up_count_5d": row.get("limit_up_count_5d") or 0,
        "limit_up_days_5d": row.get("limit_up_days_5d") or 0,
        "limit_up_last_days_ago": row.get("limit_up_last_days_ago") or 99,
        "seal_fund": _yi(row.get("seal_fund")),
        "break_count": row.get("break_count") or 0,
        "first_seal_time": row.get("first_seal_time") or "",
        "concepts": [
            {"code": c.get("code"), "name": c.get("name"), "change_pct": c.get("change_pct")}
            for c in (row.get("concepts") or [])[:6]
        ],
        "tags": row.get("tags") or [],
        "spark": row.get("spark") or [],
    }


# ── 连涨天梯 ──────────────────────────────────────────────────────────
def get_ladder(date_str: str | None = None, concept: str | None = None) -> dict:
    trade_date = date_str or snapshot_mod.latest_trade_date().isoformat()
    stats = get_market_stats(trade_date)
    pool = get_zt_pool(trade_date)
    # 板块筛选依赖概念索引:当天还没构建时在这里补一次(异步,构建中则先返回空)
    if (concept or "").strip():
        concept_index.ensure_concept_index(trade_date)
    concept_map = concept_index.get_concept_map(trade_date)

    def concept_names(symbol: str) -> list[str]:
        return [c.get("name") or "" for c in (concept_map.get(symbol) or [])]

    def ordered_concepts(symbol: str) -> list[str]:
        """按「主题优先、风格靠后」排好序的概念名（最多 3 个）。"""
        cons = concept_map.get(symbol) or []
        themes = [c for c in cons if concept_index.classify_board(c.get("name") or "") == "theme"]
        others = [c for c in cons if concept_index.classify_board(c.get("name") or "") != "theme"]
        return [(c.get("name") or "") for c in themes + others if c.get("name")][:3]

    def concept_of(item: dict) -> str:
        names = ordered_concepts(item["symbol"])
        if names:
            return names[0]
        return item.get("industry") or ""

    # 板块筛选:按概念成分归属过滤涨停池,天梯分组保持不变,空分组不返回
    wanted = (concept or "").strip()
    if wanted:
        exact = [x for x in pool if wanted in concept_names(x["symbol"])]
        pool = exact or [
            x
            for x in pool
            if any(
                wanted in name or name in wanted
                for name in concept_names(x["symbol"])
                if name
            )
        ]

    buckets: dict[int, list[dict]] = {}
    for item in pool:
        days = int(item.get("limit_up_days") or 0)
        buckets.setdefault(days, []).append(item)

    # 筛选后「打开高度」按当前可见的最高板算,避免空分组占位
    max_days = (max(buckets) if wanted and buckets else (stats.get("max_days") or 0))
    groups = []
    for days in sorted(buckets, reverse=True):
        label = "打开高度" if days == max_days and days >= 3 else (f"{days} 板" if days > 1 else "首板")
        items = sorted(
            buckets[days],
            key=lambda x: (x.get("first_seal_time") or "99:99"),
        )
        groups.append(
            {
                "level": label,
                "days": days,
                "items": [
                    {
                        "symbol": x["symbol"],
                        "name": x["name"],
                        "time": x.get("first_seal_time") or "",
                        "concept": concept_of(x),
                        "concepts": ordered_concepts(x["symbol"]),
                        "industry": x.get("industry") or "",
                        "stat": (
                            f"{x['stat_days']}天{x['stat_count']}板"
                            if x.get("stat_days") and x.get("stat_count")
                            else ""
                        ),
                        "seal_fund": _yi(x.get("seal_fund")),
                        "break_count": x.get("break_count") or 0,
                    }
                    for x in items
                ],
            }
        )

    return {
        "trade_date": trade_date,
        "stats": stats,
        "groups": groups,
        "filter": {
            "concept": wanted or None,
            "count": sum(len(g["items"]) for g in groups),
            "max_days": max((g["days"] for g in groups), default=0),
        },
        "concept_index_ready": concept_index.concept_index_status(trade_date)["rows"] > 0,
    }


# ── 板块强弱 ──────────────────────────────────────────────────────────
def _concept_zt_stats(trade_date: str, zt_codes: set[str]) -> dict[str, dict]:
    """按概念统计:成分股数 / 涨停家数 / 最高连板。一次查询聚合。"""
    from src.platform.persistence.database import SessionLocal
    from src.platform.persistence.models import ScreenerConceptMember

    db = SessionLocal()
    try:
        rows = (
            db.query(ScreenerConceptMember.concept_code, ScreenerConceptMember.symbol)
            .filter(ScreenerConceptMember.trade_date == trade_date)
            .all()
        )
    finally:
        db.close()

    ladder = _ladder_by_symbol(trade_date)
    out: dict[str, dict] = {}
    for code, symbol in rows:
        bucket = out.setdefault(
            code, {"count": 0, "max_days": 0, "members": 0}
        )
        bucket["members"] += 1
        if symbol not in zt_codes:
            continue
        bucket["count"] += 1
        bucket["max_days"] = max(
            bucket["max_days"], int((ladder.get(symbol) or {}).get("limit_up_days") or 0)
        )
    return out


def _tdx_zt_stats(trade_date: str, zt_codes: set[str]) -> dict[str, dict]:
    """通达信兜底时的板块统计:直接按板块文件算,不依赖当天索引。"""
    from src.modules.screener import tdx_blocks

    groups = tdx_blocks.fetch_groups()
    ladder = _ladder_by_symbol(trade_date)
    out: dict[str, dict] = {}
    for kind in ("theme", "style", "index"):
        for name, codes in (groups.get(kind) or {}).items():
            bucket = out.setdefault(
                f"tdx:{name}", {"count": 0, "max_days": 0, "members": len(codes)}
            )
            for symbol in codes:
                if symbol not in zt_codes:
                    continue
                bucket["count"] += 1
                bucket["max_days"] = max(
                    bucket["max_days"],
                    int((ladder.get(symbol) or {}).get("limit_up_days") or 0),
                )
    return out


def get_boards(date_str: str | None = None, limit: int = 30, include_style: bool = False) -> dict:
    trade_date = date_str or snapshot_mod.latest_trade_date().isoformat()
    concept_index.ensure_concept_index(trade_date)
    boards = concept_index.fetch_concept_boards()
    if boards and not include_style:
        # 默认只留主题类:新高/破净/红利/昨日连板这些是选股条件,不是板块
        boards = [
            b for b in boards
            if (b.get("kind") or concept_index.classify_board(b.get("name") or ""))
            == "theme"
        ]
    if not boards:
        return {"trade_date": trade_date, "items": [], "error": "概念板块数据源不可用"}

    zt_codes = {x["symbol"] for x in get_zt_pool(trade_date)}
    if concept_index.board_source() == "tdx":
        # 通达信源的板块代码是 tdx:<名称>,和东财索引对不上,现算
        zt_stats = _tdx_zt_stats(trade_date, zt_codes)
    else:
        zt_stats = _concept_zt_stats(trade_date, zt_codes)

    items = []
    for b in boards:
        stat = zt_stats.get(b["code"]) or {}
        raw_inflow = b.get("main_net_inflow")
        inflow_yi = (raw_inflow or 0.0) / 1e8
        change_pct = b.get("change_pct") or 0.0
        score = (
            0.35 * min(1.0, stat.get("count", 0) / 15)
            + 0.20 * min(1.0, stat.get("max_days", 0) / 5)
            + 0.25 * min(1.0, max(0.0, inflow_yi) / 50)
            + 0.20 * min(1.0, max(0.0, change_pct) / 5)
        )
        items.append(
            {
                "code": b["code"],
                "name": b["name"],
                "change_pct": change_pct,
                # 通达信兜底时没有资金流数据,给 None 而不是 0,避免误读
                "main_net_inflow": (
                    round(inflow_yi, 2) if raw_inflow is not None else None
                ),
                "limit_up_count": stat.get("count", 0),
                "max_days": stat.get("max_days", 0),
                "members": stat.get("members", 0),
                "leader_name": "",
                "leader_code": "",
                "leader_change_pct": None,
                "score": round(score * 100, 1),
            }
        )

    items.sort(key=lambda x: x["score"], reverse=True)
    return {
        "trade_date": trade_date,
        "items": items[: max(1, min(int(limit), 100))],
    }
