"""题材热度服务:把「涨停池 + 概念板块 + 快讯」揉成当日炒作题材榜。

**判定思路**(榜单本身纯规则,AI 只负责事后写一句解读):

    题材热度 = 涨停强度 + 连板高度 + 主力净流入 + 板块涨幅 + 消息密度

**为什么用「涨停占比」而不是「涨停家数」做主信号**:
概念板块里有"融资融券""深股通""富时罗素"这类**标签型概念**,成分股 2000+ 只,
涨停家数天然就多,但它们不是炒作题材。用 涨停家数/成分股数 才能把"真题材"和
"大标签"区分开 —— 固态电池 100 只里涨停 6 只(6%)远强于融资融券 2314 只里涨停 10 只(0.4%)。

**已知边界**:
- 概念名与新闻措辞未必一致("固态电池"在快讯里可能写成"半固态电池"),消息匹配会漏。
- 概念板块的划分是东财自己的口径,与游资常用的"题材"不完全一致。
- 涨停池的 ``hybk`` 是行业不是概念,故本模块不用它做聚合,只在展示层当参考。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

# 明显的"标签型概念",不是炒作题材,直接排除(否则永远霸榜)。
# 维护成本:东财会新增概念,漏掉的新标签要靠"涨停占比"自然压下去。
EXCLUDED_CONCEPTS: frozenset[str] = frozenset({
    "融资融券", "深股通", "沪股通", "富时罗素", "MSCI中国", "标准普尔",
    "机构重仓", "基金重仓", "社保重仓", "QFII重仓", "券商重仓",
    "预盈预增", "预亏预减", "业绩预增", "业绩预减",
    "转债标的", "破净股", "高送转", "次新股", "AB股", "AH股", "GDR",
    "昨日涨停", "昨日连板", "昨日触板", "昨日打二板以上表现",
    "国企改革", "央企改革", "参股新三板", "股权转让",
    "东方财富热股", "最近多板", "最近涨停", "首板", "打板",
})

# 除精确匹配外,名字里含这些词的也当状态标签排掉(东财这类标签会随日期换名字,
# 逐个枚举跟不上,故用关键词兜一层)。
EXCLUDED_NAME_KEYWORDS: tuple[str, ...] = (
    "昨日", "最近", "热股", "多板", "首板", "连板", "涨停", "打板",
    "融资融券", "股通", "重仓", "标的", "预盈", "预亏",
)


def is_excluded_concept(name: str) -> bool:
    """是否属于"标签型概念"(非炒作题材)。"""
    n = (name or "").strip()
    if not n:
        return True
    if n in EXCLUDED_CONCEPTS:
        return True
    return any(k in n for k in EXCLUDED_NAME_KEYWORDS)

# 五维权重(和恒为 1.0)。涨停维度占大头,因为它是"炒作"最直接的定义。
W_LIMIT_RATIO = 0.22   # 涨停占比
W_LIMIT_COUNT = 0.13   # 涨停家数(避免只命中 1-2 家的小概念靠占比虚高)
W_MAX_DAYS = 0.15      # 最高连板(空间高度)
W_INFLOW = 0.20        # 主力净流入
W_CHANGE = 0.15        # 板块涨幅
W_NEWS = 0.15          # 消息密度

# 归一化上限:达到这个值即记满分
CAP_RATIO = 0.10       # 涨停占比 10% 即满分
CAP_COUNT = 8          # 涨停 8 家即满分
CAP_DAYS = 5           # 5 连板即满分
CAP_NEWS = 8           # 命中 8 条快讯即满分

# 候选池:按资金初筛后,只对前 N 个概念拉成分股(每个概念一次请求)
DEFAULT_CANDIDATE_POOL = 40
DEFAULT_TOP_N = 10


@dataclass
class ThemeItem:
    """一个炒作题材的完整信息。"""

    code: str
    name: str
    score: float = 0.0
    rank: int = 0
    limit_up_count: int = 0
    limit_up_ratio: float = 0.0
    max_limit_up_days: int = 0
    member_count: int = 0
    main_net_inflow: float | None = None
    change_pct: float | None = None
    up_count: int = 0
    leader_name: str = ""
    leader_code: str = ""
    # 该题材的涨停股(供展示与喂 AI)
    limit_up_stocks: list[dict] = field(default_factory=list)
    # 命中的快讯/新闻,每条带 source/title/time/url(用户要求"可查看消息来源")
    news: list[dict] = field(default_factory=list)
    # AI 写的一句话解读(失败时为空)
    ai_comment: str = ""


def _norm(value: float | None, cap: float) -> float:
    """把绝对值归一化到 [0,1],超过 cap 记 1。"""
    if value is None or cap <= 0:
        return 0.0
    return max(0.0, min(1.0, float(value) / cap))


def _percentile_rank(values: list[float], v: float | None) -> float:
    """v 在 values 中的百分位(0~1)。用于"资金/涨幅"这类量纲差异大的维度。"""
    if v is None or not values:
        return 0.0
    clean = [x for x in values if x is not None]
    if not clean:
        return 0.0
    less = sum(1 for x in clean if x < v)
    return less / len(clean)


def _concept_keywords(name: str) -> list[str]:
    """概念名 → 用于匹配新闻的关键词。

    东财概念名常带"概念/板块"等后缀,新闻里通常写核心词(如"固态电池概念" → "固态电池")。
    """
    n = (name or "").strip()
    if not n:
        return []
    out = [n]
    for suffix in ("概念", "板块", "指数", "产业"):
        if n.endswith(suffix) and len(n) > len(suffix) + 1:
            out.append(n[: -len(suffix)])
    return list(dict.fromkeys(out))


def match_news(theme_name: str, news_items: list[dict]) -> list[dict]:
    """从快讯/新闻里挑出与该题材相关的条目。

    纯子串匹配 —— 会漏(措辞不一致),但不会误报(不会把不相关的算进来)。
    对"可查看消息来源"这个诉求,宁可少而准。
    """
    kws = _concept_keywords(theme_name)
    if not kws:
        return []
    hit: list[dict] = []
    for it in news_items:
        text = f"{it.get('title') or ''} {it.get('content') or ''}"
        if any(k in text for k in kws):
            hit.append(it)
    return hit


def score_theme(
    *,
    limit_up_count: int,
    member_count: int,
    max_limit_up_days: int,
    main_net_inflow: float | None,
    change_pct: float | None,
    news_count: int,
    inflow_values: list[float],
    change_values: list[float],
    excluded: bool = False,
) -> float:
    """五维加权算出热度分(0~100)。纯函数,便于单测。"""
    if excluded:
        return 0.0

    ratio = (limit_up_count / member_count) if member_count > 0 else 0.0
    score = 100.0 * (
        W_LIMIT_RATIO * _norm(ratio, CAP_RATIO)
        + W_LIMIT_COUNT * _norm(limit_up_count, CAP_COUNT)
        + W_MAX_DAYS * _norm(max_limit_up_days, CAP_DAYS)
        + W_INFLOW * _percentile_rank(inflow_values, main_net_inflow)
        + W_CHANGE * _percentile_rank(change_values, change_pct)
        + W_NEWS * _norm(news_count, CAP_NEWS)
    )
    # 一个涨停都没有的概念不可能是"炒作题材",直接压到 0。
    if limit_up_count <= 0:
        return 0.0
    return round(min(100.0, score), 2)


def recent_trade_date(today: date | None = None, *, market: str = "CN") -> date:
    """最近一个交易日(含当天)。周末/节假日自动往前退。

    题材榜应该看"最近一个已收盘的交易日" —— 今天还没开盘时看昨天,周一开盘前看周五。
    """
    from src.platform.scheduling.trading_calendar import is_trading_day

    d = today or datetime.now().date()
    for _ in range(15):
        try:
            if is_trading_day(market, d):
                return d
        except Exception:
            break
    # 日历不可用:退化为跳过周末
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


async def _fetch_limit_up_pool(md, *, date_str: str, limit: int = 200) -> list:
    """拉涨停池。失败返回空 —— 涨停维度缺失时整榜会退化成"空",不抛异常拖垮调度。"""
    try:
        return await asyncio.to_thread(md.zt_pool, date=date_str, limit=limit)
    except Exception as e:
        logger.warning(f"[题材] 涨停池获取失败({date_str}): {e}")
        return []


async def _fetch_concept_boards(md, *, limit: int = 300) -> list:
    try:
        return await asyncio.to_thread(md.concept_boards, limit=limit)
    except Exception as e:
        logger.warning(f"[题材] 概念板块获取失败: {e}")
        return []


async def _fetch_members(md, *, board_code: str, limit: int = 100) -> list[str]:
    """某概念的成分股代码集合。单个概念失败只影响它自己。

    走 ``md.concept_stocks``(push2)而不是 ``md.board_stocks``(discovery,push2delay)——
    后者用的延迟行情主机在部分网络出口已被 reset,实测 40 个概念全失败。
    """
    try:
        rows = await asyncio.to_thread(
            md.concept_stocks, board_code=board_code, limit=limit
        )
        return [str(r.get("code") or "").strip() for r in rows]
    except Exception as e:
        logger.debug(f"[题材] 成分股获取失败 {board_code}: {e}")
        return []


def _serialize_news(items: list[dict]) -> list[dict]:
    """只保留展示/溯源需要的字段,避免把整个对象塞进库和 prompt。"""
    out = []
    for it in items[:10]:
        out.append({
            "source": it.get("source") or "",
            "title": (it.get("title") or "")[:120],
            "time": it.get("time") or "",
            "url": it.get("url") or "",
            "importance": it.get("importance") or 0,
        })
    return out


async def compute_daily_themes(
    *,
    trade_date: date | None = None,
    top_n: int = DEFAULT_TOP_N,
    candidate_pool: int = DEFAULT_CANDIDATE_POOL,
    news_items: list[dict] | None = None,
) -> tuple[list[ThemeItem], dict]:
    """算出当日炒作题材榜(不含 AI 解读与落库,那两步由上层调用)。

    Returns:
        (题材列表, 诊断信息)。诊断里带各环节的取数情况,便于"榜是空的"时定位是哪一步失败。
    """
    from src.platform.marketdata.marketdata_client import get_market_data

    td = trade_date or recent_trade_date()
    date_str = td.strftime("%Y%m%d")
    diag: dict = {"trade_date": td.isoformat(), "limit_up_pool": 0, "concept_boards": 0,
                  "members_fetched": 0, "news_pool": 0}

    md = get_market_data()

    zt_items, board_items = await asyncio.gather(
        _fetch_limit_up_pool(md, date_str=date_str),
        _fetch_concept_boards(md),
    )
    diag["limit_up_pool"] = len(zt_items)
    diag["concept_boards"] = len(board_items)

    if not zt_items or not board_items:
        logger.warning(f"[题材] 数据不足,无法出榜: 涨停池={len(zt_items)} 概念={len(board_items)}")
        return [], diag

    news_items = news_items or []
    diag["news_pool"] = len(news_items)

    # 涨停股索引:{代码: 条目},供成分股求交
    zt_by_code = {x.code: x for x in zt_items if x.code}

    # 候选:排除标签型概念后,按主力净流入取前 N(资金认可度是最不 noise 的初筛)
    candidates = [b for b in board_items if not is_excluded_concept(b.name)]
    candidates.sort(key=lambda b: (b.main_net_inflow or 0.0), reverse=True)
    candidates = candidates[: max(1, candidate_pool)]

    # 并发拉成分股(每个概念一次请求;throttle 在 http 层,这里只控制并发度)
    sem = asyncio.Semaphore(6)

    async def _members_of(b):
        async with sem:
            return b, await _fetch_members(md, board_code=b.code)

    pairs = await asyncio.gather(*[_members_of(b) for b in candidates])
    diag["members_fetched"] = sum(1 for _, m in pairs if m)

    inflow_values = [b.main_net_inflow for b in candidates if b.main_net_inflow is not None]
    change_values = [b.change_pct for b in candidates if b.change_pct is not None]

    themes: list[ThemeItem] = []
    for board, members in pairs:
        member_set = set(members)
        # 成分股接口是分页的,limit 截断会让"涨停占比"的分母失真(固态电池实际 100+ 只
        # 却只拉到 100)。改用概念板块自带的 上涨家数+下跌家数 当分母,它不受分页影响;
        # 拿不到时(平盘股不计入,极端情况下为 0)才退回实际拉到的数量。
        declared_total = (board.up_count or 0) + (board.down_count or 0)
        member_total = declared_total if declared_total > 0 else len(member_set)

        # 涨停股一定排在涨幅榜最前,故交集不受分页影响,是可靠的
        hit = [zt_by_code[c] for c in member_set if c in zt_by_code]
        if not hit:
            continue

        news_hit = match_news(board.name, news_items)
        max_days = max((x.limit_up_days or 0) for x in hit)

        item = ThemeItem(
            code=board.code,
            name=board.name,
            limit_up_count=len(hit),
            limit_up_ratio=(len(hit) / member_total) if member_total else 0.0,
            max_limit_up_days=max_days,
            member_count=member_total,
            main_net_inflow=board.main_net_inflow,
            change_pct=board.change_pct,
            up_count=board.up_count or 0,
            leader_name=board.leader_name,
            leader_code=board.leader_code,
            limit_up_stocks=[
                {
                    "code": x.code,
                    "name": x.name,
                    "change_pct": x.change_pct,
                    "limit_up_days": x.limit_up_days,
                    "industry": x.industry,
                    "seal_fund": x.seal_fund,
                }
                for x in sorted(hit, key=lambda y: (y.limit_up_days or 0), reverse=True)
            ],
            news=_serialize_news(news_hit),
        )
        item.score = score_theme(
            limit_up_count=item.limit_up_count,
            member_count=item.member_count,
            max_limit_up_days=item.max_limit_up_days,
            main_net_inflow=item.main_net_inflow,
            change_pct=item.change_pct,
            news_count=len(news_hit),
            inflow_values=inflow_values,
            change_values=change_values,
        )
        if item.score > 0:
            themes.append(item)

    themes.sort(key=lambda x: x.score, reverse=True)
    themes = themes[: max(1, top_n)]
    for i, t in enumerate(themes, 1):
        t.rank = i

    diag["themes"] = len(themes)
    logger.info(
        f"[题材] {td} 出榜 {len(themes)} 个: 涨停池={diag['limit_up_pool']} "
        f"概念={diag['concept_boards']} 成分股拉到={diag['members_fetched']} 快讯池={diag['news_pool']}"
    )
    return themes, diag


# ---------------------------------------------------------------------------
# AI 解读
# ---------------------------------------------------------------------------

def _prompt_path():
    from pathlib import Path

    return Path(__file__).parent.parent.parent.parent / "prompts" / "theme_comment.txt"


def _build_ai_payload(themes: list[ThemeItem]) -> str:
    """把 Top N 题材拼成紧凑输入。

    刻意只给"涨停股 + 命中快讯 + 关键数字" —— 榜单排序已在规则层定好,AI 只负责说出
    "这些股票凑在一起在炒什么",不让它重排(重排既不稳定又费 token)。
    10 个题材合并成一次调用,比逐条调用省约一个数量级的 token。
    """
    lines: list[str] = []
    for t in themes:
        stocks = "、".join(
            "{}({}板)".format(s.get("name"), s.get("limit_up_days"))
            for s in t.limit_up_stocks[:6]
        )
        lines.append(
            "[{}] {} | 涨停{}家 最高{}板 涨幅{:.2f}% 主力净流入{:.2f}亿".format(
                t.code, t.name, t.limit_up_count, t.max_limit_up_days,
                t.change_pct or 0.0, (t.main_net_inflow or 0.0) / 1e8,
            )
        )
        if stocks:
            lines.append("  涨停股: " + stocks)
        for n in t.news[:3]:
            lines.append("  消息: " + str(n.get("title") or ""))
    return "\n".join(lines)


def _parse_ai_comments(raw: str) -> dict[str, str]:
    """解析 AI 返回的 JSON 数组 [{code, comment}]。容错:剥 ``` 围栏、退化为取首个数组。"""
    import json
    import re

    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text).strip()
    try:
        arr = json.loads(text)
    except Exception:
        m = re.search(r"\[[\s\S]*\]", text)
        if not m:
            return {}
        try:
            arr = json.loads(m.group(0))
        except Exception:
            return {}
    if not isinstance(arr, list):
        return {}
    out: dict[str, str] = {}
    for it in arr:
        if not isinstance(it, dict):
            continue
        code = str(it.get("code") or "").strip()
        comment = str(it.get("comment") or "").strip()
        if code and comment:
            out[code] = comment
    return out


async def write_ai_comments(themes: list[ThemeItem]) -> None:
    """给每个题材写一句解读(原地写入 ai_comment)。失败静默留空,绝不拖垮主流程。"""
    if not themes:
        return
    try:
        from src.platform.ai.ai_failover import get_configured_failover_client
        from src.platform.persistence.database import SessionLocal

        system_prompt = _prompt_path().read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"[题材] AI 解读依赖/prompt 不可用,跳过: {e}")
        return

    db = SessionLocal()
    try:
        client = get_configured_failover_client(db)
        raw = await client.chat(system_prompt, _build_ai_payload(themes), temperature=0.3)
    except Exception as e:
        logger.warning(f"[题材] AI 解读失败(留空继续): {e}")
        return
    finally:
        db.close()

    comments = _parse_ai_comments(raw)
    if not comments:
        logger.warning("[题材] AI 解读返回无法解析,留空")
        return
    for t in themes:
        t.ai_comment = comments.get(t.code, "")[:120]


# ---------------------------------------------------------------------------
# 落库与读取
# ---------------------------------------------------------------------------

def save_themes(themes: list[ThemeItem], trade_date: date) -> int:
    """把当天的题材榜 upsert 进库(同一天重跑先删后写,不产生重复)。返回写入条数。"""
    from src.platform.persistence.database import SessionLocal
    from src.platform.persistence.models import HotTheme

    ds = trade_date.isoformat()
    db = SessionLocal()
    try:
        db.query(HotTheme).filter(HotTheme.trade_date == ds).delete()
        for t in themes:
            db.add(HotTheme(
                trade_date=ds,
                code=t.code,
                name=t.name,
                rank=t.rank,
                score=t.score,
                limit_up_count=t.limit_up_count,
                limit_up_ratio=t.limit_up_ratio,
                max_limit_up_days=t.max_limit_up_days,
                member_count=t.member_count,
                main_net_inflow=t.main_net_inflow,
                change_pct=t.change_pct,
                up_count=t.up_count,
                leader_name=t.leader_name,
                leader_code=t.leader_code,
                limit_up_stocks=t.limit_up_stocks,
                news=t.news,
                ai_comment=t.ai_comment,
            ))
        db.commit()
        return len(themes)
    except Exception as e:
        db.rollback()
        logger.warning(f"[题材] 落库失败: {e}")
        return 0
    finally:
        db.close()


def _row_to_theme(row) -> dict:
    return {
        "code": row.code,
        "name": row.name,
        "rank": row.rank,
        "score": row.score,
        "limit_up_count": row.limit_up_count,
        "limit_up_ratio": row.limit_up_ratio,
        "max_limit_up_days": row.max_limit_up_days,
        "member_count": row.member_count,
        "main_net_inflow": row.main_net_inflow,
        "change_pct": row.change_pct,
        "up_count": row.up_count,
        "leader_name": row.leader_name,
        "leader_code": row.leader_code,
        "limit_up_stocks": row.limit_up_stocks or [],
        "news": row.news or [],
        "ai_comment": row.ai_comment or "",
    }


def load_daily(trade_date: date | None = None) -> tuple[date, list[dict]]:
    """读某日题材榜;不传日期或该日无数据时,回退到库里最近的一天。"""
    from src.platform.persistence.database import SessionLocal
    from src.platform.persistence.models import HotTheme

    db = SessionLocal()
    try:
        if trade_date is not None:
            ds = trade_date.isoformat()
        else:
            latest = (
                db.query(HotTheme.trade_date)
                .order_by(HotTheme.trade_date.desc())
                .first()
            )
            if not latest:
                return trade_date or recent_trade_date(), []
            ds = latest[0]
        rows = (
            db.query(HotTheme)
            .filter(HotTheme.trade_date == ds)
            .order_by(HotTheme.rank.asc())
            .all()
        )
        return date.fromisoformat(ds), [_row_to_theme(r) for r in rows]
    finally:
        db.close()


def load_evolution(days: int = 5) -> dict:
    """近 N 个交易日的题材演进。

    这是"落库"相对"实时算"的唯一增量价值 —— 只有存了历史才看得出题材的生命周期:
    - **persistent 持续发酵**:连续多日上榜且今天仍在前 10
    - **new 新上榜**:今天进榜、上一日不在
    - **dropped 掉榜**:上一日在榜、今天不在
    """
    from src.platform.persistence.database import SessionLocal
    from src.platform.persistence.models import HotTheme

    db = SessionLocal()
    try:
        dates = [
            r[0]
            for r in db.query(HotTheme.trade_date)
            .distinct()
            .order_by(HotTheme.trade_date.desc())
            .limit(max(1, days))
            .all()
        ]
        if not dates:
            return {"dates": [], "series": [], "new": [], "dropped": [], "persistent": []}

        dates = sorted(dates)
        rows = (
            db.query(HotTheme)
            .filter(HotTheme.trade_date.in_(dates))
            .order_by(HotTheme.trade_date.asc(), HotTheme.rank.asc())
            .all()
        )

        by_date: dict[str, list] = {d: [] for d in dates}
        for r in rows:
            by_date[r.trade_date].append(r)

        names: dict[str, str] = {}
        ranks: dict[str, dict[str, int]] = {}
        for d in dates:
            for r in by_date[d]:
                names[r.code] = r.name
                ranks.setdefault(r.code, {})[d] = r.rank

        today = dates[-1]
        yesterday = dates[-2] if len(dates) > 1 else None
        today_codes = {r.code for r in by_date[today]}
        yest_codes = {r.code for r in by_date[yesterday]} if yesterday else set()

        series = []
        for code, per_day in ranks.items():
            if len(per_day) < 2:
                continue
            series.append({
                "code": code,
                "name": names.get(code, code),
                "ranks": [per_day.get(d) for d in dates],
                "days_on": len(per_day),
            })
        series.sort(key=lambda x: (-x["days_on"], x["ranks"][-1] or 999))

        return {
            "dates": dates,
            "series": series,
            "new": [{"code": c, "name": names.get(c, c)} for c in sorted(today_codes - yest_codes)],
            "dropped": [{"code": c, "name": names.get(c, c)} for c in sorted(yest_codes - today_codes)],
            "persistent": [
                s for s in series if s["days_on"] >= 2 and (s["ranks"][-1] or 999) <= 10
            ],
        }
    finally:
        db.close()


async def _collect_news_pool() -> list[dict]:
    """取快讯池(已聚合去重)转成 match_news 期望的 dict 形态。"""
    try:
        from src.platform.marketdata.marketdata_client import get_market_data

        md = get_market_data()
        flash = await asyncio.to_thread(md.flash_news, limit=300)
        return [
            {
                "source": x.source,
                "title": x.title or "",
                "content": x.content or "",
                "time": x.publish_time.strftime("%m-%d %H:%M"),
                "url": x.url or "",
                "importance": x.importance or 0,
            }
            for x in flash
        ]
    except Exception as e:
        logger.warning(f"[题材] 快讯池获取失败,消息维度将为空: {e}")
        return []


async def refresh_themes(
    *, trade_date: date | None = None, top_n: int = DEFAULT_TOP_N,
    news_items: list[dict] | None = None,
) -> dict:
    """完整刷新:算榜 → AI 解读 → 落库。供定时任务与手动刷新共用,返回诊断信息。"""
    if news_items is None:
        news_items = await _collect_news_pool()

    themes, diag = await compute_daily_themes(
        trade_date=trade_date, top_n=top_n, news_items=news_items
    )
    if themes:
        await write_ai_comments(themes)
        diag["saved"] = save_themes(themes, trade_date or recent_trade_date())
        diag["ai_commented"] = sum(1 for t in themes if t.ai_comment)
    return diag
