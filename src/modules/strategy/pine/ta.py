"""``ta.*`` 指标库入口。

优先使用完整实现；如果它尚未就位（或导入失败），退回到 :mod:`ta_core`
的核心集，保证引擎始终可跑。
"""

from __future__ import annotations

try:  # pragma: no cover - 正常情况下走 ta_full
    from .ta_full import TA, TA_ALIASES  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001 - 任何导入失败都退回核心集
    from .ta_core import TA, TA_ALIASES

__all__ = ["TA", "TA_ALIASES"]
