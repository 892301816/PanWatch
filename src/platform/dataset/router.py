"""多源路由:优先级链 + 失败切换 + 冷却 + 手动指定。

与 TSP 的「每个数据集单选一个源」不同,这里是有序候选链:
    daily: fuyao(10) → tickflow(20) → tdxquant(30)
任一源失败(异常 / 返回行数明显不足)就切下一个;失败源进入 60 秒冷却。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date

from src.platform.dataset import store
from src.platform.dataset.providers import get_provider_class
from src.platform.dataset.providers.base import ProgressFn
from src.platform.dataset.types import FetchResult, ProviderUnavailable

logger = logging.getLogger(__name__)

_COOLDOWN_S = 60.0
_fail_until: dict[int, float] = {}
_instances: dict[tuple[str, str], object] = {}


def _config_key(config: dict) -> str:
    # 只把影响连接的字段纳入 key,避免每次请求都重建客户端
    keys = ("api_key", "base_url", "lib_root")
    return json.dumps({k: (config or {}).get(k, "") for k in keys}, sort_keys=True)


def get_instance(row: dict):
    name = row["provider"]
    cls = get_provider_class(name)
    if cls is None:
        raise ProviderUnavailable(f"未知数据源: {name}")
    key = (name, _config_key(row.get("config") or {}))
    inst = _instances.get(key)
    if inst is None:
        inst = cls(row.get("config") or {})
        _instances[key] = inst
    return inst


def probe(name: str, config: dict) -> tuple[bool, str]:
    cls = get_provider_class(name)
    if cls is None:
        return False, f"未知数据源: {name}"
    inst = cls(config or {})
    try:
        return inst.probe()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]
    finally:
        close = getattr(inst, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass


def _min_rows(symbols: int) -> int:
    return max(10, int(symbols * 0.5))


def fetch_daily(
    dataset: str,
    symbols: list[str],
    start: date,
    end: date,
    *,
    force: str | None = None,
    allow_fallback: bool = True,
    progress: ProgressFn | None = None,
) -> FetchResult:
    """按优先级取日K。force 指定源;失败时按 allow_fallback 决定是否继续。"""
    rows = [r for r in store.list_providers(dataset) if r.get("enabled")]
    if force:
        forced = [r for r in rows if r["provider"] == force]
        if not forced:
            raise ProviderUnavailable(f"数据源 {force} 未启用或不存在")
        rows = forced if not allow_fallback else forced + [r for r in rows if r["provider"] != force]
    if not rows:
        raise ProviderUnavailable("没有启用的数据源")

    started = time.monotonic()
    fallbacks: list[dict] = []
    threshold = _min_rows(len(symbols))

    for row in rows:
        pid = row["id"]
        if time.monotonic() < _fail_until.get(pid, 0.0):
            fallbacks.append({"provider": row["provider"], "error": "冷却中"})
            continue

        try:
            inst = get_instance(row)
            ok, detail = inst.available()
            if not ok:
                raise ProviderUnavailable(detail)

            if progress:
                progress(f"使用 {row['provider']} 取日K", 0, 1)

            df = inst.fetch_daily(symbols, start, end, progress=progress)
            rows_out = 0 if df is None else len(df)
            if rows_out < threshold:
                raise RuntimeError(f"返回行数不足({rows_out} < {threshold})")

            store.record_success(pid)
            _fail_until.pop(pid, None)
            return FetchResult(
                df=df,
                provider=row["provider"],
                rows=rows_out,
                fallbacks=fallbacks,
                elapsed_s=round(time.monotonic() - started, 1),
            )
        except Exception as exc:  # noqa: BLE001
            message = str(exc) or exc.__class__.__name__
            logger.warning(f"[数据源] {row['provider']} 失败,尝试下一个: {message}")
            fallbacks.append({"provider": row["provider"], "error": message[:300]})
            store.record_failure(pid, message)
            _fail_until[pid] = time.monotonic() + _COOLDOWN_S

    raise ProviderUnavailable(
        "所有日K数据源均失败: "
        + "; ".join(f"{f['provider']}={f['error']}" for f in fallbacks)
    )


def reset_cooldowns() -> None:
    _fail_until.clear()
    _instances.clear()
