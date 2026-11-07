"""消息聚合 API —— 只收集与分类，不产出交易信号。

与 /api/themes 的区别：题材是"市场在炒什么"（有热度排序），这里是"最近发生了什么"
（纯信息陈列，不做优劣判断）。两者共享同一批快讯数据，但用途不同。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from src.modules.market import message_service

logger = logging.getLogger(__name__)
router = APIRouter()

_DISCLAIMER = "本页仅汇总公开消息，按关键词规则分类，可能存在漏判或误判；不构成任何投资建议。"


@router.get("/feed")
async def get_feed(
    hours: int = Query(24, ge=1, le=168, description="时间窗(小时)"),
    category: str | None = Query(None, description="只看某个分类，如 政策监管"),
    source: str | None = Query(None, description="只看某个来源，如 jin10"),
    watchlist_only: bool = Query(False, description="只看命中自选股的"),
    position_only: bool = Query(False, description="只看命中持仓股的"),
    limit: int = Query(200, ge=1, le=500),
    with_brief: bool = Query(False, description="是否附带 AI 汇总(会调用一次模型)"),
):
    """最近消息（已按 分类 / 自选股关联 / 重要性 排好序）。"""
    data = await message_service.collect_messages(
        hours=hours, category=category, source=source,
        watchlist_only=watchlist_only, position_only=position_only, limit=limit,
    )
    if with_brief:
        data["brief"] = await message_service.write_ai_brief(
            data["items"], data["category_counts"]
        )
    else:
        data["brief"] = ""
    return {**data, "disclaimer": _DISCLAIMER}


@router.post("/brief")
async def generate_brief(notify: bool = Query(False, description="是否同时推送到默认通知渠道")):
    """手动生成当天的「早间消息汇总」（首页今日要紧事用）。"""
    from src.modules.market import news_brief

    result = await news_brief.generate_news_brief(force=True, notify=notify)
    if not result.get("ok"):
        raise HTTPException(500, result.get("error") or "AI 汇总生成失败")
    return result


@router.get("/brief")
def latest_brief():
    """最近一次早间汇总；当天没有则回退到最近一条并带上生成日期。"""
    from src.modules.market import news_brief

    row = news_brief.get_news_brief()
    if not row:
        return {"empty": True}
    return {"empty": False, **row, "today": news_brief.today_str()}


@router.get("/categories")
async def get_categories():
    """分类清单（供前端筛选器使用）。规则顺序即优先级。"""
    return {
        "categories": [name for name, _ in message_service.CATEGORY_RULES]
        + [message_service.CATEGORY_OTHER],
        "note": "按关键词规则分类；先匹配到的桶胜出，都不中归「其他」。",
    }
