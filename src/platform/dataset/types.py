"""数据集层的公共类型与异常。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


class DatasetError(Exception):
    """数据集层通用错误。"""


class ProviderUnavailable(DatasetError):
    """provider 缺少依赖 / 未配置 Key / 被禁用。"""


class ProviderFetchError(DatasetError):
    """provider 取数失败(网络、限频、返回为空、数据不完整)。"""


@dataclass
class FetchResult:
    """一次取数的结果和来源。"""

    df: object                      # pandas.DataFrame
    provider: str
    rows: int = 0
    fallbacks: list[dict] = field(default_factory=list)  # 失败过的源及原因
    elapsed_s: float = 0.0


@dataclass
class SyncProgress:
    dataset: str
    stage: str = "idle"             # idle / resolving / fetching / writing / done / error
    message: str = ""
    provider: str = ""
    rows: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str = ""
    trade_date: date | None = None
