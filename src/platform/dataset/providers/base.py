"""provider 协议与公共工具。

数据契约(pandas.DataFrame):
    symbol      str   6 位数字
    trade_date  str   YYYY-MM-DD
    open/high/low/close  float  元（原始价，不复权）
    volume      float 手
    amount      float 元
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Callable, Protocol

import pandas as pd

DAILY_COLUMNS = [
    "symbol", "trade_date", "open", "high", "low", "close", "volume", "amount",
]

ProgressFn = Callable[[str, int, int], None]  # (message, done, total)


def empty_daily() -> pd.DataFrame:
    return pd.DataFrame(columns=DAILY_COLUMNS)


def finalize_daily(df: pd.DataFrame) -> pd.DataFrame:
    """统一 dtype、去重、剔除非交易日/非法行。"""
    if df is None or len(df) == 0:
        return empty_daily()

    out = df.copy()
    for col in DAILY_COLUMNS:
        if col not in out.columns:
            out[col] = None

    out = out[DAILY_COLUMNS]
    out["symbol"] = out["symbol"].astype(str).str.strip()
    out["trade_date"] = out["trade_date"].astype(str).str.slice(0, 10)
    for col in ("open", "high", "low", "close", "volume", "amount"):
        out[col] = pd.to_numeric(out[col], errors="coerce")

    out = out.dropna(subset=["symbol", "trade_date", "close"])
    out = out[(out["symbol"] != "") & (out["trade_date"] != "")]
    out = out[out["close"] > 0]
    out = out.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
    return out.reset_index(drop=True)


@dataclass
class ProviderHealth:
    available: bool
    detail: str = ""


class DailyProvider(Protocol):
    name: str
    datasets: set[str]

    def available(self) -> tuple[bool, str]: ...
    def probe(self) -> tuple[bool, str]: ...
    def fetch_daily(
        self,
        symbols: list[str],
        start: date,
        end: date,
        progress: ProgressFn | None = None,
    ) -> pd.DataFrame: ...


class BaseDailyProvider:
    """所有 provider 的基类:配置取值、可用性判断、空实现。"""

    name = ""
    datasets: set[str] = {"daily"}
    api_key_env = ""

    def __init__(self, config: dict | None = None) -> None:
        self.config = dict(config or {})

    # ---- 配置 ----
    def api_key(self) -> str:
        return str(self.config.get("api_key") or os.environ.get(self.api_key_env, "")).strip()

    def base_url(self, default: str = "") -> str:
        return str(self.config.get("base_url") or default).strip()

    # ---- 可用性 ----
    def available(self) -> tuple[bool, str]:
        return True, "ok"

    def probe(self) -> tuple[bool, str]:
        """默认用一次最小日K请求探活,子类可覆盖。"""
        try:
            df = self.fetch_daily(["600000"], date.today(), date.today())
            return True, f"ok({len(df)} 行)"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:200]

    # ---- 取数 ----
    def fetch_daily(
        self,
        symbols: list[str],
        start: date,
        end: date,
        progress: ProgressFn | None = None,
    ) -> pd.DataFrame:
        raise NotImplementedError
