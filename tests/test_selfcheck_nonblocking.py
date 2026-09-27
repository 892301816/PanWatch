"""自检探测必须把同步取数下线程,否则会拖死整个服务。

回归背景:``MarketData`` 的方法(klines/quotes/news/fundamentals/...)全是同步阻塞的,
早先这些探测直接在协程里调用,把 FastAPI 的事件循环堵死。后果是点一次系统自检,
探测期间**整个站点失去响应** —— 前端每项 30s 超时全变红(看起来像"全部失败"),
其它接口(板块、行情、持仓)也一起挂。而 ``asyncio.wait_for`` 的超时保护在事件循环
被堵时根本不会触发,一个连不上的源能跑满 20s 上限而不是被及时中断。

用例同时断言两件事,缺一不可:
1. 慢调用**确实执行了**(耗时够长)—— 否则探测提前 return 会让断言变成假通过;
2. 期间事件循环**仍在推进**(tick 数够多)—— 这才是"下没下线程"的判据。
"""

import asyncio
import time

import pytest

import src.modules.market.data_collector as dc

SLOW_S = 0.4          # 替身取数方法的阻塞时长
MIN_TICKS = 10        # 事件循环空闲时,0.4s 内约能推进 40 次


class _Ticker:
    """每 10ms 打一次点。事件循环被阻塞时点数会显著偏少。"""

    def __init__(self):
        self.ticks = 0
        self._stop = False

    async def run(self):
        while not self._stop:
            self.ticks += 1
            await asyncio.sleep(0.01)

    def stop(self):
        self._stop = True


class _SlowSyncMarketData:
    """替身:所有取数方法都是"同步阻塞 SLOW_S 秒",模拟真实 vendor。"""

    def __init__(self, *args, **kwargs):
        pass

    @staticmethod
    def _slow():
        time.sleep(SLOW_S)
        return []

    def klines(self, *a, **k):
        return self._slow()

    def quotes(self, *a, **k):
        return self._slow()

    def news(self, *a, **k):
        return self._slow()

    def flash_news(self, *a, **k):
        return self._slow()

    def fundamentals(self, *a, **k):
        return self._slow()

    def dragon_tiger(self, *a, **k):
        return self._slow()

    def margin(self, *a, **k):
        return self._slow()

    def shareholders(self, *a, **k):
        return self._slow()

    def dividend(self, *a, **k):
        return self._slow()

    def northbound(self, *a, **k):
        return self._slow()


@pytest.fixture
def slow_md(monkeypatch):
    """把探测方法内 `from marketdata import MarketData` 拿到的类换成慢速同步替身。"""
    import marketdata

    monkeypatch.setattr(marketdata, "MarketData", _SlowSyncMarketData)


async def _run_and_measure(coro) -> tuple[int, float]:
    """跑 coro,返回 (探测期间事件循环推进次数, 探测耗时)。"""
    ticker = _Ticker()
    task = asyncio.create_task(ticker.run())
    await asyncio.sleep(0.05)  # 先让 ticker 跑起来
    base = ticker.ticks
    t0 = time.monotonic()
    await coro
    elapsed = time.monotonic() - t0
    during = ticker.ticks - base
    ticker.stop()
    await task
    return during, elapsed


def _source(type_: str, provider: str):
    from src.platform.persistence.models import DataSource

    src = DataSource(name="t", type=type_, provider=provider, config={}, enabled=True, priority=0)
    src.test_symbols = ["600519"]
    return src


# (type, provider, 探测方法名)。各方法签名不一(有的收 test_symbols),由
# _build_call 按真实签名决定怎么调,避免签名变化时用例静默跑偏。
PROBES = [
    ("kline", "eastmoney", "_test_kline_source"),
    ("quote", "eastmoney", "_test_quote_source"),
    ("news", "eastmoney", "_test_news_source"),
    ("flash_news", "eastmoney", "_test_flash_news_source"),
    ("fundamentals", "eastmoney", "_test_fundamentals_source"),
    ("dragon_tiger", "eastmoney", "_test_dragon_tiger_source"),
    ("margin", "eastmoney", "_test_margin_source"),
    ("shareholders", "eastmoney", "_test_shareholders_source"),
    ("dividend", "eastmoney", "_test_dividend_source"),
    ("northbound", "ths", "_test_northbound_source"),
]


def _accepts_symbols(func) -> bool:
    import inspect

    return len(inspect.signature(func).parameters) >= 2


@pytest.mark.parametrize(
    "type_,provider,method",
    PROBES,
    ids=[p[2] for p in PROBES],
)
def test_probe_does_not_block_event_loop(slow_md, type_, provider, method):
    """每个探测方法都要把同步取数下线程:慢调用真的执行了,但事件循环没被堵。"""
    mgr = dc.DataCollectorManager()
    call = getattr(mgr, method)
    src = _source(type_, provider)
    coro = call(src, ["600519"]) if _accepts_symbols(call) else call(src)

    during, elapsed = asyncio.run(_run_and_measure(coro))

    # 1) 慢调用确实跑到了,否则这个用例什么都没测到
    assert elapsed >= SLOW_S * 0.8, (
        f"{method} 未走到实际取数(耗时 {elapsed:.2f}s) —— 用例失去意义,检查 provider 白名单"
    )
    # 2) 期间事件循环仍在推进(裸同步调用时这里会是 0)
    assert during >= MIN_TICKS, (
        f"{method} 阻塞了事件循环:探测 {elapsed:.2f}s 期间仅推进 {during} 次"
    )


def test_harness_detects_blocking(monkeypatch):
    """反向验证:故意做一次裸同步阻塞,测量手段必须能识别出来。

    防止上面那组用例因为测量方式失效而集体假通过。
    """
    import marketdata

    class _Blocking:
        def __init__(self, *a, **k):
            pass

        def klines(self, *a, **k):
            time.sleep(SLOW_S)  # 故意同步阻塞,模拟修复前的写法
            return []

    monkeypatch.setattr(marketdata, "MarketData", _Blocking)

    async def blocking_probe():
        mgr = dc.DataCollectorManager()
        md = marketdata.MarketData()
        md.klines("600519", market="CN", days=30)

    during, elapsed = asyncio.run(_run_and_measure(blocking_probe()))

    assert elapsed >= SLOW_S * 0.8
    assert during < MIN_TICKS, "测量手段失效:裸同步阻塞也应被识别为占用事件循环"
