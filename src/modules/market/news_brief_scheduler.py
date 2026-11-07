"""早间消息汇总调度器：交易日 08:00 生成并推送，容器重启后在早盘窗口内补跑一次。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.modules.market.news_brief import generate_news_brief, get_news_brief, today_str

logger = logging.getLogger(__name__)


class NewsBriefScheduler:
    def __init__(self, timezone: str = "Asia/Shanghai", hour: int = 8, minute: int = 0):
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self.timezone_name = timezone
        self.hour = max(0, min(23, int(hour)))
        self.minute = max(0, min(59, int(minute)))
        self._running = False

    def _now(self) -> datetime:
        from zoneinfo import ZoneInfo

        try:
            return datetime.now(ZoneInfo(self.timezone_name))
        except Exception:  # noqa: BLE001
            return datetime.now()

    @staticmethod
    def _is_trading_day() -> bool:
        try:
            from src.platform.scheduling.trading_calendar import is_trading_day

            return bool(is_trading_day("CN"))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[早间汇总] 交易日判断失败，按交易日处理: {e}")
            return True

    async def _job(self):
        if self._running:
            logger.debug("[早间汇总] 上一轮仍在执行，跳过本轮")
            return
        self._running = True
        try:
            if not self._is_trading_day():
                logger.info("[早间汇总] 今日非交易日，跳过")
                return
            result = await generate_news_brief(notify=True)
            if result.get("ok"):
                logger.info(
                    f"[早间汇总] 完成: date={result.get('date')} skipped={result.get('skipped') or ''} "
                    f"notified={result.get('notified')}"
                )
            else:
                logger.warning(f"[早间汇总] 未生成: {result.get('error')}")
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[早间汇总] 执行异常: {e}")
        finally:
            self._running = False

    async def _catch_up(self):
        """容器重启/错过 08:00 时补跑：仅早盘窗口（08:00–11:30）内、且当天还没生成。"""
        await asyncio.sleep(90)  # 等应用初始化（数据源、模型配置）就绪
        try:
            if not self._is_trading_day():
                return
            now = self._now()
            if now.hour < self.hour or now.hour > 11 or (now.hour == 11 and now.minute > 30):
                return
            if get_news_brief(today_str()):
                return
            logger.info("[早间汇总] 检测到今日尚未生成，补跑一次")
            await self._job()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[早间汇总] 补跑失败: {e}")

    def start(self):
        self.scheduler.add_job(
            self._job,
            "cron",
            hour=self.hour,
            minute=self.minute,
            id="news_brief_daily",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.start()
        try:
            from src.platform.scheduling.scheduler_registry import register

            register("news_brief", self.scheduler)
        except Exception:  # noqa: BLE001
            pass
        try:
            asyncio.create_task(self._catch_up())
        except Exception:  # noqa: BLE001
            pass
        logger.info(f"[早间汇总] 调度已启动: 每天 {self.hour:02d}:{self.minute:02d} ({self.timezone_name})")

    async def trigger_once(self) -> dict:
        return await generate_news_brief(force=True, notify=True)

    def shutdown(self):
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass
