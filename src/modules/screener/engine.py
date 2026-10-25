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
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from src.modules.screener import concepts as concept_index
from src.modules.screener import snapshot as snapshot_mod
from src.modules.screener.constants import FIELD_SCALE, FIELD_KEYS

logger = logging.getLogger(__name__)

_LADDER_CACHE: dict = {"date": "", "ts": 0.0, "items": []}
_LADDER_TTL_S = 120.0
_LADDER_LOCK = threading.Lock()

_SPARK_CACHE: dict[tuple[str, str], list[float]] = {}
_SPARK_LOCK = threading.Lock()


# ── 涨停池 ────────────────────────────────────────────────────────────
def _compact(date_str: str) -> str:
    return date_str.replace("-", "")


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
        _LADDER_CACHE.update(date=trade_date, ts=time.monotonic(), items=items)
        return items


def _ladder_by_symbol(date_str: str) -> dict[str, dict]:
    return {x["symbol"]: x for x in get_zt_pool(date_str) if x.get("symbol")}


# ── 近 N 日涨停(选"回踩""N天M板"必须的历史窗口)─────────────────────
# 当日涨停池里的 stat_days/stat_count 只对「今天涨停」的股票有值,
# 而"近5日有过涨停、今天回调"的股票不在今日池里 —— 必须回看历史池。
_RECENT_ZT_CACHE: dict = {"date": "", "days": 0, "ts": 0.0, "map": {}}
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
            try:
                from src.platform.marketdata.marketdata_client import get_market_data

                rows = get_market_data().zt_pool(date=d.strftime("%Y%m%d"), limit=300)
                return d, rows
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[选股] 历史涨停池 {d} 获取失败: {exc}")
                return d, []

        result: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(dates))) as pool:
            for d, rows in pool.map(_fetch, dates):
                ago = (end - d).days
                for r in rows:
                    sym = getattr(r, "code", "") or ""
                    if not sym:
                        continue
                    bucket = result.setdefault(
                        sym, {"count": 0, "max_days": 0, "last_days_ago": 99}
                    )
                    bucket["count"] += 1
                    bucket["max_days"] = max(
                        bucket["max_days"], int(getattr(r, "limit_up_days", 0) or 0)
                    )
                    bucket["last_days_ago"] = min(bucket["last_days_ago"], ago)

        _RECENT_ZT_CACHE.update(
            date=trade_date, days=days, ts=time.monotonic(), map=result
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


def _match(row: dict, cond: dict) -> bool:
    field = str(cond.get("field") or "").strip()
    op = str(cond.get("op") or "eq").strip()
    expected = cond.get("value")
    if field not in FIELD_KEYS:
        return False

    if field == "concept":
        names = [c.get("name", "") for c in (row.get("concepts") or [])]
        if op in ("in", "not_in"):
            wanted = expected if isinstance(expected, list) else [expected]
            hit = any(w and w in names for w in wanted)
            return hit if op == "in" else not hit
        if op == "contains":
            return bool(expected) and any(str(expected) in n for n in names)
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
    ladder = _ladder_by_symbol(trade_date)
    recent_zt = get_recent_limit_up(trade_date, 5)

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
        enriched.append(row)

    matched = _apply(enriched, payload)
    matched = _sort(matched, payload.get("order_by") or "change_pct desc")

    # limit<=0 表示返回全部;上限 6000 只(全市场约 5600)
    raw_limit = int(payload.get("limit") or 0)
    limit = 6000 if raw_limit <= 0 else max(1, min(raw_limit, 6000))
    matched = matched[:limit]

    for row in matched:
        row["tags"] = _tags(row)
        row["spark"] = []

    # 迷你走势默认不再随选股返回(前端按页懒加载),避免一次算几千只
    if bool(payload.get("with_spark", False)):
        _attach_spark(matched, trade_date, int(payload.get("spark_limit") or 80))

    return {
        "trade_date": trade_date,
        "count": len(matched),
        "universe": len(rows),
        "items": [_serialize_item(r) for r in matched],
        "as_of": time.strftime("%H:%M"),
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
def get_ladder(date_str: str | None = None) -> dict:
    trade_date = date_str or snapshot_mod.latest_trade_date().isoformat()
    stats = get_market_stats(trade_date)
    pool = get_zt_pool(trade_date)
    concept_map = concept_index.get_concept_map(trade_date)

    def concept_of(item: dict) -> str:
        cons = concept_map.get(item["symbol"]) or []
        if cons:
            return cons[0].get("name") or ""
        return item.get("industry") or ""

    buckets: dict[int, list[dict]] = {}
    for item in pool:
        days = int(item.get("limit_up_days") or 0)
        buckets.setdefault(days, []).append(item)

    max_days = stats.get("max_days") or 0
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


def get_boards(date_str: str | None = None, limit: int = 30) -> dict:
    trade_date = date_str or snapshot_mod.latest_trade_date().isoformat()
    concept_index.ensure_concept_index(trade_date)
    boards = concept_index.fetch_concept_boards()
    if not boards:
        return {"trade_date": trade_date, "items": [], "error": "概念板块数据源不可用"}

    zt_codes = {x["symbol"] for x in get_zt_pool(trade_date)}
    zt_stats = _concept_zt_stats(trade_date, zt_codes)

    items = []
    for b in boards:
        stat = zt_stats.get(b["code"]) or {}
        inflow_yi = (b.get("main_net_inflow") or 0.0) / 1e8
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
                "main_net_inflow": round(inflow_yi, 2),
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
