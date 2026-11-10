"""盘中监测的方法必须在类里(防止缩进错误把方法挪到模块级)。"""
import asyncio
from types import SimpleNamespace

from src.modules.automation.base import AgentContext
from src.modules.automation.intraday_monitor import IntradayMonitorAgent


def test_class_keeps_its_overrides():
    for method in ("analyze", "should_notify", "run_single", "_format_human_readable_content"):
        assert method in IntradayMonitorAgent.__dict__, f"{method} 应定义在类内"


def test_skip_reason_short_circuits_without_ai(monkeypatch):
    agent = IntradayMonitorAgent()

    class _Boom:
        async def chat(self, *a, **k):
            raise AssertionError("非交易时段不应调用 AI")

    ctx = AgentContext.__new__(AgentContext)
    ctx.ai_client = _Boom()
    ctx.config = SimpleNamespace(watchlist=[])
    ctx._primary_model_label = ""

    result = asyncio.run(
        agent.analyze(ctx, {"skip_reason": "当前A股非交易时段，已跳过执行", "stock_data": None})
    )
    assert result.raw_data.get("skipped") is True
    assert asyncio.run(agent.should_notify(result)) is False
