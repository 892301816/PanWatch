import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HotStock:
    symbol: str
    market: str
    name: str
    price: float | None
    change_pct: float | None
    turnover: float | None
    volume: float | None


@dataclass(frozen=True)
class HotBoard:
    code: str
    name: str
    change_pct: float | None
    change_amount: float | None
    turnover: float | None


def get_market_data():
    """惰性导入,避免模块加载时的循环依赖(便于测试 monkeypatch)。"""
    from src.platform.marketdata.marketdata_client import get_market_data as _g

    return _g()


DEFAULT_DISCOVERY_PROVIDER = "eastmoney"


class EastMoneyDiscoveryCollector:
    """Discovery ranks (CN/HK/US),经 marketdata 包统一取数。

    ``provider`` 决定用包内哪个 discovery vendor(见 marketdata 包的
    ``DISCOVERY_VENDOR_CLASSES``):``eastmoney`` 走 push2,``eastmoney_bkzj`` 走
    ``data.eastmoney.com``。宿主按 DataSource 表的 provider 传入,便于在 push2
    不可达的网络出口切换。未指定时沿用历史默认,行为不变。
    """

    def __init__(self, *, proxy: str | None = None, provider: str = DEFAULT_DISCOVERY_PROVIDER):
        self.proxy = proxy
        self.provider = (provider or DEFAULT_DISCOVERY_PROVIDER).strip() or DEFAULT_DISCOVERY_PROVIDER

    async def fetch_hot_stocks(
        self,
        *,
        market: str = "CN",
        mode: str = "turnover",
        limit: int = 20,
    ) -> list[HotStock]:
        import asyncio as _aio

        pkg_items = await _aio.to_thread(
            get_market_data().hot_stocks,
            vendor=self.provider,
            market=market,
            mode=mode,
            limit=limit,
            proxy=self.proxy,
        )
        return [
            HotStock(
                symbol=it.symbol,
                market=it.market,
                name=it.name,
                price=it.price,
                change_pct=it.change_pct,
                turnover=it.turnover,
                volume=it.volume,
            )
            for it in pkg_items
        ]

    async def fetch_hot_boards(
        self,
        *,
        market: str = "CN",
        mode: str = "gainers",
        limit: int = 12,
    ) -> list[HotBoard]:
        import asyncio as _aio

        pkg_items = await _aio.to_thread(
            get_market_data().hot_boards,
            vendor=self.provider,
            market=market,
            mode=mode,
            limit=limit,
            proxy=self.proxy,
        )
        return [
            HotBoard(
                code=it.code,
                name=it.name,
                change_pct=it.change_pct,
                change_amount=it.change_amount,
                turnover=it.turnover,
            )
            for it in pkg_items
        ]

    async def fetch_board_stocks(
        self,
        *,
        board_code: str,
        mode: str = "gainers",
        limit: int = 20,
    ) -> list[HotStock]:
        import asyncio as _aio

        pkg_items = await _aio.to_thread(
            get_market_data().board_stocks,
            vendor=self.provider,
            board_code=board_code,
            mode=mode,
            limit=limit,
            proxy=self.proxy,
        )
        return [
            HotStock(
                symbol=it.symbol,
                market=it.market,
                name=it.name,
                price=it.price,
                change_pct=it.change_pct,
                turnover=it.turnover,
                volume=it.volume,
            )
            for it in pkg_items
        ]
