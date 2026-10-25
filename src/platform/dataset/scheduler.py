"""数据集每日同步调度(时间可在设置页修改)。"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import AppSettings
from src.platform.dataset import sync as sync_service

logger = logging.getLogger(__name__)

SETTING_KEY = "dataset_sync_schedule"
DEFAULT_SCHEDULE = {"enabled": True, "hour": 15, "minute": 45}
_JOB_ID = "dataset_daily_sync"
_scheduler: BackgroundScheduler | None = None


def get_schedule() -> dict:
    db = SessionLocal()
    try:
        row = db.query(AppSettings).filter(AppSettings.key == SETTING_KEY).first()
        if row and row.value:
            try:
                data = json.loads(row.value)
                return {**DEFAULT_SCHEDULE, **data}
            except json.JSONDecodeError:
                pass
        return dict(DEFAULT_SCHEDULE)
    finally:
        db.close()


def set_schedule(config: dict) -> dict:
    merged = {**DEFAULT_SCHEDULE, **{k: config[k] for k in config if k in DEFAULT_SCHEDULE}}
    merged["hour"] = max(0, min(23, int(merged.get("hour") or 0)))
    merged["minute"] = max(0, min(59, int(merged.get("minute") or 0)))
    merged["enabled"] = bool(merged.get("enabled"))

    db = SessionLocal()
    try:
        row = db.query(AppSettings).filter(AppSettings.key == SETTING_KEY).first()
        payload = json.dumps(merged, ensure_ascii=False)
        if row is None:
            db.add(AppSettings(key=SETTING_KEY, value=payload, description="日K自动同步时间"))
        else:
            row.value = payload
        db.commit()
    finally:
        db.close()

    _apply(merged)
    return merged


def _job() -> None:
    logger.info("[数据集] 定时同步开始")
    started = sync_service.start_sync_async(mode="incremental")
    if not started:
        logger.warning("[数据集] 定时同步跳过:已有任务在运行")


def _apply(config: dict) -> None:
    if _scheduler is None:
        return
    existing = _scheduler.get_job(_JOB_ID)
    if existing:
        _scheduler.remove_job(_JOB_ID)
    if not config.get("enabled"):
        logger.info("[数据集] 定时同步已关闭")
        return
    _scheduler.add_job(
        _job,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=int(config.get("hour") or 15),
            minute=int(config.get("minute") or 45),
            timezone="Asia/Shanghai",
        ),
        id=_JOB_ID,
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info(
        "[数据集] 定时同步已设置: 工作日 %02d:%02d",
        int(config.get("hour") or 15),
        int(config.get("minute") or 45),
    )


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    _scheduler.start()
    _apply(get_schedule())


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def next_run_at() -> str:
    if _scheduler is None:
        return ""
    job = _scheduler.get_job(_JOB_ID)
    if job is None or job.next_run_time is None:
        return ""
    return job.next_run_time.isoformat()
