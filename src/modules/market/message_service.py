"""消息聚合服务 —— **只做收集与分类，不产出任何交易信号**。

这个页面的用途是"观察最近发生了什么"，不是找买卖点。所以这里不做评分、不建议、
不排成"机会榜"，只把信息按用户的四个维度摆清楚：

1. **源自带标签** —— 快讯源自带的分类（同花顺的 tags、新浪的 tag 等）。
   ⚠️ 目前 PanWatch 的数据结构没有保留这些字段（``FlashNews`` 只有 importance），
   所以这一层暂时是空的，见文件末尾"待补"。
2. **关键词规则** —— 按词表把消息分进 政策监管/宏观数据/公司事件/行业催化/海外地缘 五个桶。
3. **关联标的** —— 命中自选股的消息单独拎出来置顶（自选股 = 绑定了 Agent 的股票）。
4. **重要性排序** —— 源自带的 level/important + 关键词加权。

关于"会漏"：关键词表需要维护，政策新提法不加进去就漏。这是规则方案的固定成本。
对"扫一眼最近发生了什么"这个用途，漏几条不致命（用户还能自己翻），故可接受。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

CATEGORY_OTHER = "其他"

# 分类规则:**顺序即优先级**，先匹配到的桶胜出。
# 海外地缘排最前，因为"美联储降息"这类消息同时含"降息"(政策词)，但它讲的是海外的事 ——
# 按地理词的优先级更高才不会归错桶。
CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("海外地缘", (
        "美联储", "美股", "纳斯达克", "标普", "道指", "欧洲央行", "日本央行",
        "俄乌", "乌克兰", "俄罗斯", "中东", "以色列", "伊朗", "巴勒斯坦", "哈马斯",
        "沙特", "阿联酋", "卡塔尔", "巴基斯坦", "印度", "韩国", "朝鲜", "日本",
        "德国", "法国", "英国", "意大利", "土耳其", "埃及", "巴西", "澳大利亚",
        "加拿大", "墨西哥", "波兰", "泰国", "越南", "新加坡",
        "关税", "制裁", "袭击", "空袭", "地缘", "北约", "特朗普", "白宫", "欧盟",
        "美军", "五角大楼", "联合国",
    )),
    ("政策监管", (
        "国务院", "发改委", "央行", "证监会", "财政部", "工信部", "商务部", "住建部",
        "银保监", "市场监管总局", "政治局", "水利部", "交通运输部", "农业农村部",
        "教育部", "科技部", "能源局", "医保局", "自然资源部", "生态环境部",
        "人社部", "审计署", "海关总署", "统计局", "交易所",
        "降准", "降息", "补贴", "新政", "意见", "规划", "试点", "监管", "处罚",
        "反垄断", "利好政策", "印发", "条例", "办法", "征求意见", "工作会议", "部署",
    )),
    ("宏观数据", (
        "CPI", "PPI", "GDP", "PMI", "社融", "非农", "失业率", "进出口", "贸易顺差",
        "贸易逆差", "外汇储备", "利率决议", "LPR", "M2", "工业增加值", "社会消费品零售",
        "同比", "环比", "统计", "前三季度", "上半年", "全年",
    )),
    ("公司事件", (
        "公告", "业绩", "年报", "半年报", "季报", "预增", "预减", "预亏", "扭亏",
        "回购", "增持", "减持", "重组", "并购", "收购", "停牌", "复牌", "退市",
        "股权转让", "解禁", "分红", "担保", "问询", "澄清", "致歉", "异动",
        "立案", "留置", "监管函", "立案调查",
    )),
    ("行业催化", (
        "涨价", "提价", "涨价函", "量产", "技术突破", "投产", "扩产", "产能",
        "专利", "中标", "供货", "商业化", "订单", "产能利用率", "新产线",
        "签约", "开工", "下线", "交付", "首批", "量产", "上市申请", "获批",
    )),
)

# 重要性关键词：命中即提档(源自带的 level/important 之外再加一层)
_HIGH_IMPACT_WORDS = ("重大", "突发", "紧急", "独家", "重磅", "首次", "历史性", "紧急")
_MID_IMPACT_WORDS = ("公告", "快讯", "研报", "业绩", "发布", "宣布")


@dataclass
class Message:
    """一条已分类的消息。字段刻意保持扁平,便于前端直接用。"""

    id: str
    source: str
    title: str
    content: str
    time: str          # 展示用 "MM-DD HH:MM"
    ts: float          # 排序用(unix 秒)
    url: str
    category: str
    importance: int    # 0~3
    symbols: list[str] = field(default_factory=list)   # 消息提到/关联的股票
    in_watchlist: bool = False                          # 是否命中自选股


def classify_category(title: str, content: str = "") -> str:
    """按关键词把消息分桶。先匹配 CATEGORY_RULES 的顺序,都不中归"其他"。"""
    text = f"{title or ''} {content or ''}"
    if not text.strip():
        return CATEGORY_OTHER
    for name, words in CATEGORY_RULES:
        if any(w in text for w in words):
            return name
    return CATEGORY_OTHER


def compute_importance(title: str, source_importance: int = 0) -> int:
    """综合源自带重要性与关键词的最终重要性(0~3)。"""
    base = max(0, min(int(source_importance or 0), 3))
    text = title or ""
    if any(w in text for w in _HIGH_IMPACT_WORDS):
        return 3
    if base >= 2:
        return base
    if any(w in text for w in _MID_IMPACT_WORDS):
        return max(base, 1)
    return base


def _load_watchlist_symbols() -> set[str]:
    """自选股 = 绑定了 Agent 的股票(与 load_watchlist_for_agent 同口径)。失败返回空集。"""
    try:
        from src.platform.persistence.database import SessionLocal
        from src.platform.persistence.models import Stock, StockAgent

        db = SessionLocal()
        try:
            ids = {sa.stock_id for sa in db.query(StockAgent.stock_id).all()}
            if not ids:
                return set()
            return {
                str(s.symbol).strip()
                for s in db.query(Stock).filter(Stock.id.in_(ids)).all()
                if s.symbol
            }
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"[消息] 自选股读取失败(关联标记将为空): {e}")
        return set()


async def _collect_flash() -> list[dict]:
    """快讯池(已聚合去重)。"""
    try:
        from src.platform.marketdata.marketdata_client import get_market_data

        md = get_market_data()
        rows = await asyncio.to_thread(md.flash_news, limit=300)
        return [
            {
                "id": f"{x.source}:{x.external_id}",
                "source": x.source,
                "title": x.title or "",
                "content": x.content or "",
                "datetime": x.publish_time,
                "url": x.url or "",
                "importance": x.importance or 0,
                "symbols": list(getattr(x, "symbols", []) or []),
            }
            for x in rows
        ]
    except Exception as e:
        logger.warning(f"[消息] 快讯获取失败: {e}")
        return []


async def _collect_stock_news(symbols: list[str], *, hours: int = 24) -> list[dict]:
    """自选股的个股新闻(按 symbol)。symbols 为空则跳过。"""
    if not symbols:
        return []
    try:
        from src.platform.marketdata.collectors.news_collector import NewsCollector

        collector = NewsCollector.from_database()
        rows = await collector.fetch_all(symbols=symbols[:20], since_hours=hours)
        return [
            {
                "id": f"{r.source}:{r.external_id}",
                "source": r.source,
                "title": r.title or "",
                "content": r.content or "",
                "datetime": r.publish_time,
                "url": r.url or "",
                "importance": r.importance or 0,
                "symbols": list(r.symbols or []),
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[消息] 个股新闻获取失败: {e}")
        return []


async def collect_messages(
    *,
    hours: int = 24,
    category: str | None = None,
    source: str | None = None,
    watchlist_only: bool = False,
    limit: int = 200,
) -> dict:
    """收集并分类最近的消息。

    Args:
        hours: 时间窗(小时)。个股新闻按此窗口取;快讯本身是实时的,只做窗口过滤。
        category: 只要某个分类(如"政策监管");None 表示全部。
        source: 只要某个来源(如"jin10")。
        watchlist_only: 只保留命中自选股的消息。
        limit: 返回上限(按重要性+时间排序后截断)。
    """
    watch = _load_watchlist_symbols()
    flash, stock_news = await asyncio.gather(
        _collect_flash(),
        _collect_stock_news(sorted(watch), hours=hours),
    )

    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, hours))
    messages: list[Message] = []
    seen: set[str] = set()

    for raw in flash + stock_news:
        dt = raw.get("datetime")
        if dt is None:
            continue
        # publish_time 是 aware(UTC);naive 的按 UTC 处理
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt < cutoff:
            continue

        mid = raw["id"]
        if mid in seen:
            continue
        seen.add(mid)

        title = raw["title"]
        symbols = [str(s).strip() for s in (raw.get("symbols") or []) if s]
        hit_watch = bool(watch and any(s in watch for s in symbols))

        messages.append(Message(
            id=mid,
            source=raw["source"],
            title=title,
            content=raw["content"],
            time=dt.strftime("%m-%d %H:%M"),
            ts=dt.timestamp(),
            url=raw["url"],
            category=classify_category(title, raw["content"]),
            importance=compute_importance(title, raw["importance"]),
            symbols=symbols,
            in_watchlist=hit_watch,
        ))

    # 第 3 层:命中自选股的优先;第 4 层:重要性;再按时间
    messages.sort(key=lambda m: (not m.in_watchlist, -m.importance, -m.ts))

    # 分类统计在过滤前算,这样切到某个分类时还能看到全貌
    counts: dict[str, int] = {}
    for m in messages:
        counts[m.category] = counts.get(m.category, 0) + 1
    watch_count = sum(1 for m in messages if m.in_watchlist)
    source_counts: dict[str, int] = {}
    for m in messages:
        source_counts[m.source] = source_counts.get(m.source, 0) + 1

    if category:
        messages = [m for m in messages if m.category == category]
    if source:
        messages = [m for m in messages if m.source == source]
    if watchlist_only:
        messages = [m for m in messages if m.in_watchlist]

    total = len(messages)
    messages = messages[: max(1, limit)]

    return {
        "total": total,
        "returned": len(messages),
        "hours": hours,
        "watchlist_size": len(watch),
        "watchlist_hits": watch_count,
        "category_counts": counts,
        "source_counts": source_counts,
        "items": [
            {
                "id": m.id,
                "source": m.source,
                "title": m.title,
                "content": m.content[:200],
                "time": m.time,
                "url": m.url,
                "category": m.category,
                "importance": m.importance,
                "symbols": m.symbols,
                "in_watchlist": m.in_watchlist,
            }
            for m in messages
        ],
    }


# ---------------------------------------------------------------------------
# AI 建议
# ---------------------------------------------------------------------------

def _brief_prompt_path():
    from pathlib import Path

    return Path(__file__).parent.parent.parent.parent / "prompts" / "message_brief.txt"


def _build_brief_payload(messages: list[dict], category_counts: dict) -> str:
    """把消息喂给 AI 的紧凑文本。按分类分组，每组最多 8 条，带来源便于引用。"""
    from collections import defaultdict

    grouped: dict[str, list[dict]] = defaultdict(list)
    for m in messages:
        grouped[m["category"]].append(m)

    lines = [
        "分类统计: " + "、".join(f"{k}{v}条" for k, v in sorted(category_counts.items())),
        "",
    ]
    for cat, items in grouped.items():
        lines.append(f"## {cat}")
        for m in items[:8]:
            star = "★" if m["in_watchlist"] else ""
            lines.append(f"- [{m['time']}][{m['source']}]{star} {m['title']}")
        lines.append("")
    return "\n".join(lines)


async def write_ai_brief(messages: list[dict], category_counts: dict) -> str:
    """对分类后的消息做一次 AI 汇总。失败返回空串(fail-soft)。"""
    if not messages:
        return ""
    try:
        from src.platform.ai.ai_failover import get_configured_failover_client
        from src.platform.persistence.database import SessionLocal

        system_prompt = _brief_prompt_path().read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"[消息] AI 建议依赖/prompt 不可用,跳过: {e}")
        return ""

    db = SessionLocal()
    try:
        client = get_configured_failover_client(db)
        raw = await client.chat(
            system_prompt, _build_brief_payload(messages, category_counts), temperature=0.3
        )
        return (raw or "").strip()[:1200]
    except Exception as e:
        logger.warning(f"[消息] AI 建议失败(留空继续): {e}")
        return ""
    finally:
        db.close()
