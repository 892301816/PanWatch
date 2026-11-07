"""早间消息汇总：把消息页的「AI 汇总」落库，供首页「今日要紧事」展示与推送。

流程：最近 24h 消息 → AI 汇总 → 写 analysis_history（每天一条）→ 可选推送默认通知渠道。
非交易日不自动生成；手动触发（force）不受限制。
"""

from __future__ import annotations

import logging
from datetime import date

logger = logging.getLogger(__name__)

AGENT_NAME = "morning_news"
AGENT_LABEL = "早间消息汇总"
STOCK_SYMBOL = "*"


def today_str() -> str:
    """按应用时区取今天（容器默认 Asia/Shanghai）。"""
    try:
        from src.platform.config.settings import Settings
        from zoneinfo import ZoneInfo
        from datetime import datetime

        tz = ZoneInfo(Settings().app_timezone or "Asia/Shanghai")
        return datetime.now(tz).date().isoformat()
    except Exception:  # noqa: BLE001
        return date.today().isoformat()


def get_news_brief(day: str | None = None) -> dict | None:
    """取某天（默认最近一次）的早间汇总。"""
    from src.platform.persistence.database import SessionLocal
    from src.platform.persistence.models import AnalysisHistory

    db = SessionLocal()
    try:
        q = db.query(AnalysisHistory).filter(
            AnalysisHistory.agent_name == AGENT_NAME,
            AnalysisHistory.stock_symbol == STOCK_SYMBOL,
        )
        if day:
            q = q.filter(AnalysisHistory.analysis_date == day)
        row = q.order_by(
            AnalysisHistory.analysis_date.desc(), AnalysisHistory.id.desc()
        ).first()
        if not row:
            return None
        return {
            "date": row.analysis_date,
            "title": row.title or AGENT_LABEL,
            "content": row.content or "",
            "updated_at": row.updated_at.isoformat() if row.updated_at else "",
        }
    finally:
        db.close()


async def generate_news_brief(*, force: bool = False, notify: bool = True) -> dict:
    """生成当日早间汇总。返回 {ok, skipped?, error?, date, brief, notified}。"""
    from src.modules.market import message_service
    from src.platform.persistence.database import SessionLocal
    from src.platform.persistence.models import AnalysisHistory, NotifyChannel
    from src.platform.notifications.notifier import NotifierManager

    day = today_str()

    db = SessionLocal()
    try:
        existing = (
            db.query(AnalysisHistory)
            .filter(
                AnalysisHistory.agent_name == AGENT_NAME,
                AnalysisHistory.stock_symbol == STOCK_SYMBOL,
                AnalysisHistory.analysis_date == day,
            )
            .first()
        )
        if existing and not force:
            return {
                "ok": True,
                "skipped": "already_generated",
                "date": day,
                "brief": existing.content or "",
            }

        data = await message_service.collect_messages(hours=24, limit=200)
        items = data.get("items") or []
        if not items:
            return {"ok": False, "error": "最近 24 小时没有消息", "date": day}

        brief = await message_service.write_ai_brief(items, data.get("category_counts") or {})
        if not brief:
            return {"ok": False, "error": "AI 汇总生成失败（模型未配置或调用失败）", "date": day}

        title = f"{AGENT_LABEL} {day}"
        row = existing
        if row:
            row.title = title
            row.content = brief
            row.raw_data = {
                "total": data.get("total", len(items)),
                "category_counts": data.get("category_counts") or {},
                "watchlist_hits": data.get("watchlist_hits", 0),
            }
        else:
            row = AnalysisHistory(
                agent_name=AGENT_NAME,
                stock_symbol=STOCK_SYMBOL,
                analysis_date=day,
                title=title,
                content=brief,
                raw_data={
                    "total": data.get("total", len(items)),
                    "category_counts": data.get("category_counts") or {},
                    "watchlist_hits": data.get("watchlist_hits", 0),
                },
                agent_kind_snapshot="news_brief",
            )
            db.add(row)
        db.commit()

        channels = (
            db.query(NotifyChannel)
            .filter(NotifyChannel.enabled == True, NotifyChannel.is_default == True)  # noqa: E712
            .all()
        )
        channel_cfgs = [(ch.type, ch.config or {}) for ch in channels]
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[早间汇总] 生成失败: {e}")
        return {"ok": False, "error": str(e), "date": day}
    finally:
        db.close()

    notify_error = ""
    if notify:
        if not channel_cfgs:
            notify_error = "没有启用的默认通知渠道"
        else:
            try:
                notifier = NotifierManager()
                for ctype, cfg in channel_cfgs:
                    notifier.add_channel(ctype, cfg)
                result = await notifier.notify_with_result(f"【{AGENT_LABEL}】{day}", brief)
                if not result.get("success"):
                    notify_error = str(
                        result.get("error") or result.get("skipped") or "notify_failed"
                    )
            except Exception as e:  # noqa: BLE001
                notify_error = str(e)
        if notify_error:
            logger.warning(f"[早间汇总] 推送失败: {notify_error}")

    logger.info(f"[早间汇总] 已生成 {day}（{len(brief)} 字），推送{'成功' if notify and not notify_error else '未成功'}")
    return {
        "ok": True,
        "date": day,
        "brief": brief,
        "notified": bool(notify) and not notify_error,
        "notify_error": notify_error,
    }
