"""理财账户定时任务：定投到期提醒 + 分红自动入账。

- 每天 08:45 检查定投计划（到期当天/前一天），推送提醒；不会自动扣款。
- 每个交易日 16:40 处理当天除权的分红；红利账户按除权日收盘价再投资。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date

from apscheduler.schedulers.background import BackgroundScheduler

from src.platform.notifications.notifier import NotifierManager
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import NotifyChannel

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _build_notifier() -> NotifierManager | None:
    db = SessionLocal()
    try:
        channels = (
            db.query(NotifyChannel)
            .filter(NotifyChannel.enabled.is_(True), NotifyChannel.is_default.is_(True))
            .all()
        )
        if not channels:
            return None
        mgr = NotifierManager()
        for ch in channels:
            mgr.add_channel(ch.type, ch.config or {})
        return mgr
    finally:
        db.close()


def _notify(title: str, content: str) -> None:
    mgr = _build_notifier()
    if not mgr:
        logger.info("[理财] 无默认通知渠道，跳过提醒")
        return
    try:
        asyncio.run(mgr.notify(title, content))
    except Exception:  # noqa: BLE001
        logger.exception("[理财] 通知发送失败")


def run_dca_reminders() -> dict:
    """到期定投提醒：到期当天或前一天各提醒一次。"""
    from src.modules.portfolio import wealth
    from src.platform.persistence.models import DcaPlan

    db = SessionLocal()
    try:
        today = date.today().isoformat()
        rows = wealth.due_dca_plans(db, ahead_days=1)
        sent = 0
        for item in rows:
            plan = db.query(DcaPlan).filter(DcaPlan.id == item["id"]).first()
            if not plan or plan.last_notified_date == today:
                continue
            when = "今天" if item["next_run_date"] == today else item["next_run_date"]
            _notify(
                "定投提醒",
                (
                    f"{item['account_name']} · {item['name']}({item['symbol']})\n"
                    f"计划金额 ¥{item['amount']:.2f}，{when}到期。\n"
                    "确认成交后请在「持仓 → 定投」里记一笔，系统会更新持仓成本。"
                ),
            )
            plan.last_notified_date = today
            sent += 1
        db.commit()
        return {"due": len(rows), "sent": sent}
    finally:
        db.close()


def run_dividend_processing() -> dict:
    """按除权日处理最近 3 天的分红；失败不影响调度器。"""
    from src.modules.portfolio import wealth

    try:
        result = wealth.process_dividends(days_back=3)
        if result.get("recorded"):
            _notify(
                "分红已入账",
                f"本次自动处理 {result['recorded']} 笔分红，可在「持仓 → 理财」查看明细。"
                "红利账户默认按除权日收盘价再投资。",
            )
        return result
    except Exception:  # noqa: BLE001
        logger.exception("[理财] 分红处理失败")
        return {"recorded": 0, "error": "failed"}


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    sched.add_job(
        run_dca_reminders,
        "cron",
        hour=8,
        minute=45,
        id="wealth_dca_reminder",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        run_dividend_processing,
        "cron",
        day_of_week="mon-fri",
        hour=16,
        minute=40,
        id="wealth_dividend",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    sched.start()
    _scheduler = sched
    logger.info("[理财] 定时任务已启动（08:45 定投提醒，16:40 分红入账）")


def shutdown() -> None:
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=False)
    finally:
        _scheduler = None
        logger.info("[理财] 定时任务已关闭")
