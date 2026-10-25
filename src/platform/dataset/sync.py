"""全市场日K同步编排:股票池 → 路由取数 → 落库 → 游标。"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta

from src.platform.dataset import router, store
from src.platform.dataset.types import DatasetError

logger = logging.getLogger(__name__)

_PROGRESS_LOCK = threading.Lock()
_PROGRESS: dict = {
    "running": False,
    "stage": "idle",
    "message": "",
    "provider": "",
    "rows": 0,
    "total": 0,
    "done": 0,
    "started_at": 0.0,
    "finished_at": 0.0,
    "error": "",
}
_RUN_LOCK = threading.Lock()

DEFAULT_FULL_DAYS = 1095  # 3 年


def _set_progress(**fields) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS.update(fields)


def get_progress() -> dict:
    with _PROGRESS_LOCK:
        return dict(_PROGRESS)


def _progress_cb(message: str, done: int, total: int) -> None:
    _set_progress(message=message, done=done, total=total, stage="fetching")


def resolve_universe() -> list[str]:
    """全市场股票池。优先本地股票清单,失败退回已有快照/日K里的 symbol。"""
    try:
        from src.platform.marketdata.stock_list import get_stock_list

        rows = get_stock_list() or []
        symbols = [str(r.get("symbol") or "").strip() for r in rows]
        # 只要 6 位 A 股代码,排除港股(5 位)/美股(带字母)
        symbols = [s for s in symbols if len(s) == 6 and s.isdigit()]
        if len(symbols) > 3000:
            return sorted(set(symbols))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[数据集] 股票清单不可用,尝试本地快照: {exc}")

    from sqlalchemy import text

    from src.platform.persistence.database import SessionLocal

    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                "SELECT DISTINCT symbol FROM screener_snapshots "
                "WHERE trade_date = (SELECT MAX(trade_date) FROM screener_snapshots)"
            )
        ).fetchall()
        symbols = [str(r[0]) for r in rows if r[0]]
        if symbols:
            return sorted(set(symbols))
        rows = db.execute(
            text("SELECT DISTINCT symbol FROM daily_bars")
        ).fetchall()
        return sorted({str(r[0]) for r in rows if r[0]})
    finally:
        db.close()


def _latest_trade_date() -> date:
    d = date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _completed_trade_date() -> date:
    """最近一个「已收盘」的交易日。

    盘中(工作日 15:45 前)日K还没定版,增量同步只取到上一个交易日,
    避免把当天半截数据写进本地库。
    """
    now = datetime.now()
    d = _latest_trade_date()
    if now.weekday() < 5 and (now.hour, now.minute) < (15, 45):
        d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    return d


def sync_daily(
    *,
    mode: str = "incremental",
    days: int | None = None,
    provider: str | None = None,
    allow_fallback: bool = True,
) -> dict:
    """执行一次同步。同步函数,调用方负责放进后台线程。"""
    if not _RUN_LOCK.acquire(blocking=False):
        raise DatasetError("已有同步任务在运行")
    started = time.time()
    _set_progress(
        running=True, stage="resolving", message="准备股票池",
        provider=provider or "auto", rows=0, total=0, done=0,
        started_at=started, finished_at=0.0, error="",
    )
    try:
        store.ensure_defaults()
        symbols = resolve_universe()
        if not symbols:
            raise DatasetError("股票池为空,无法同步")

        end = _latest_trade_date() if mode == "full" else _completed_trade_date()
        last = store.latest_bar_date()
        if mode == "full":
            span = int(days or DEFAULT_FULL_DAYS)
            start = end - timedelta(days=max(30, span))
        else:
            if last:
                start = date.fromisoformat(last) + timedelta(days=1)
            else:
                start = end - timedelta(days=DEFAULT_FULL_DAYS)
            # 增量窗口太窄时向前多取几天,避免节假日/停牌造成空窗
            if (end - start).days < 3:
                start = end - timedelta(days=7)
        if start > end:
            _set_progress(
                running=False, stage="done", message="数据已是最新",
                finished_at=time.time(),
            )
            return {"skipped": True, "reason": "already fresh", "last_date": last}

        _set_progress(
            stage="fetching",
            message=f"{start} → {end}, {len(symbols)} 只",
        )
        result = router.fetch_daily(
            "daily", symbols, start, end,
            force=provider, allow_fallback=allow_fallback,
            progress=_progress_cb,
        )

        _set_progress(
            stage="writing",
            message=f"写入 {result.rows} 行",
            provider=result.provider,
            rows=result.rows,
        )
        written = store.upsert_bars(result.df, result.provider, chunk_size=20000)

        store.update_sync_state(
            "daily",
            last_date=end.isoformat(),
            last_mode=mode,
            rows=int(written),
            status="ok",
            error="",
            started_at=datetime.fromtimestamp(started),
            finished_at=datetime.fromtimestamp(time.time()),
        )
        _set_progress(
            running=False, stage="done",
            message=f"完成:{result.provider} 写入 {written} 行",
            rows=int(written), provider=result.provider,
            finished_at=time.time(), error="",
        )
        return {
            "ok": True,
            "provider": result.provider,
            "rows": int(written),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "elapsed_s": round(time.time() - started, 1),
            "fallbacks": result.fallbacks,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("[数据集] 日K同步失败")
        store.update_sync_state("daily", status="error", error=str(exc)[:500])
        _set_progress(
            running=False, stage="error", error=str(exc)[:500],
            message="同步失败", finished_at=time.time(),
        )
        raise
    finally:
        _RUN_LOCK.release()


def start_sync_async(**kwargs) -> bool:
    if not _RUN_LOCK.acquire(blocking=False):
        return False
    _RUN_LOCK.release()

    def _run():
        try:
            sync_daily(**kwargs)
        except Exception:  # noqa: BLE001 — 已记录到进度
            pass

    threading.Thread(target=_run, name="dataset-sync", daemon=True).start()
    return True


def is_stale() -> bool:
    last = store.latest_bar_date()
    if not last:
        return True
    return date.fromisoformat(last) < _completed_trade_date()


_AUTO_LAST_TS = 0.0
_AUTO_MIN_INTERVAL_S = 600.0


def maybe_auto_sync() -> bool:
    """打开页面/启动时检查:数据落后且距上次自动尝试超过 10 分钟 → 后台增量。

    不阻塞请求;失败也只是记日志,由用户手动重试或等下一次定时任务。
    """
    global _AUTO_LAST_TS
    if get_progress().get("running"):
        return False
    if not is_stale():
        return False
    now = time.time()
    if now - _AUTO_LAST_TS < _AUTO_MIN_INTERVAL_S:
        return False
    _AUTO_LAST_TS = now
    logger.info("[数据集] 检测到本地日K落后,后台触发增量同步")
    return start_sync_async(mode="incremental")
