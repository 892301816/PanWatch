"""数据集 provider 实现。"""

from __future__ import annotations

PROVIDER_CLASSES = {}


def register(cls):
    PROVIDER_CLASSES[cls.name] = cls
    return cls


def get_provider_class(name: str):
    # 延迟导入,避免缺少可选依赖时整个模块无法加载
    if not PROVIDER_CLASSES:
        from src.platform.dataset.providers import fuyao, tdxquant, tickflow  # noqa: F401

    return PROVIDER_CLASSES.get((name or "").strip().lower())
