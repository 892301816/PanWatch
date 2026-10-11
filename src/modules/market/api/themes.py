"""每日炒作题材 API。

榜单是**规则算出来 + AI 补一句解读**的产物,不是投资建议 —— 它回答"今天市场在炒什么",
不回答"该买什么"。所有接口都返回 ``disclaimer`` 字段提醒这一点。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Query

from src.modules.market import theme_service

logger = logging.getLogger(__name__)
router = APIRouter()

_DISCLAIMER = "题材热度由涨停家数/连板高度/资金/涨幅/消息密度按规则加权得出，仅供研究参考，不构成投资建议。"


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s.strip())
    except ValueError:
        raise HTTPException(400, f"日期格式应为 YYYY-MM-DD，收到: {s}")


@router.get("/daily")
async def get_daily(date_str: str | None = Query(None, alias="date")):
    """当日炒作题材榜。不传 date 取库里最近一天。"""
    trade_date, items = await asyncio.to_thread(theme_service.load_daily, _parse_date(date_str))
    return {
        "trade_date": trade_date.isoformat(),
        "count": len(items),
        "items": items,
        "disclaimer": _DISCLAIMER,
    }


@router.get("/evolution")
async def get_evolution(days: int = Query(5, ge=2, le=30)):
    """近 N 个交易日的题材演进(持续发酵 / 新上榜 / 掉榜)。

    需要至少两天的落库数据;首次使用时只有一天,series 为空属正常。
    """
    data = await asyncio.to_thread(theme_service.load_evolution, days)
    return {**data, "disclaimer": _DISCLAIMER}


@router.post("/refresh")
async def refresh(date_str: str | None = Query(None, alias="date"), top_n: int = Query(10, ge=1, le=30)):
    """手动重算并落库(算榜 → AI 解读 → 写库)。整轮约 15~30 秒。"""
    try:
        diag = await theme_service.refresh_themes(
            trade_date=_parse_date(date_str), top_n=top_n
        )
    except Exception as e:
        logger.exception("[题材] 手动刷新失败")
        raise HTTPException(500, f"刷新失败: {e}")
    return {"ok": True, **diag}


@router.get("/{code}/news")
async def get_theme_news(code: str, limit: int = Query(20, ge=1, le=50)):
    """某题材相关的新闻快讯。

    两个来源合并:
    1. 榜单落库时命中的**快讯**快照(带来源与原文链接,满足"可追溯来源")
    2. 该题材**涨停股的个股新闻**(实时取,比快讯更贴个股)
    """
    trade_date, items = await asyncio.to_thread(theme_service.load_daily, None)
    theme = next((x for x in items if x.get("code") == code), None)
    if not theme:
        raise HTTPException(404, f"题材 {code} 不在 {trade_date} 的榜单中")

    snapshot = theme.get("news") or []

    # 涨停股的个股新闻(实时;失败则只返回快讯快照)
    stock_news: list[dict] = []
    symbols = [s.get("code") for s in (theme.get("limit_up_stocks") or []) if s.get("code")]
    if symbols:
        try:
            from src.platform.marketdata.collectors.news_collector import NewsCollector

            collector = NewsCollector.from_database()
            rows = await collector.fetch_all(
                symbols=symbols[:8],
                since_hours=72,
                symbol_names={s.get("code"): s.get("name") for s in theme["limit_up_stocks"]},
            )
            stock_news = [
                {
                    "source": r.source,
                    "title": r.title,
                    "time": r.publish_time.strftime("%m-%d %H:%M"),
                    "url": r.url,
                    "symbols": r.symbols,
                    "importance": r.importance,
                }
                for r in rows[:limit]
            ]
        except Exception as e:
            logger.warning(f"[题材] 个股新闻获取失败(只返回快讯): {e}")

    return {
        "code": code,
        "name": theme.get("name"),
        "trade_date": trade_date.isoformat(),
        "flash_news": snapshot,      # 落库时命中的快讯(带来源链接)
        "stock_news": stock_news,    # 涨停股的个股新闻(实时)
        "limit_up_stocks": theme.get("limit_up_stocks") or [],
        "ai_comment": theme.get("ai_comment") or "",
        "disclaimer": _DISCLAIMER,
    }
