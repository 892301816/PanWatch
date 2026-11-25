import asyncio
import json

import pytest

from src.modules.strategy import chart_writer


SOURCE = "FAST = MA(5)\nSLOW = EMA(20)\nBUY = CROSSUP(FAST,SLOW)\nSELL = CROSSDOWN(FAST,SLOW)"


@pytest.mark.parametrize("candidate,expected_error", [
    ({"name": "均线", "source": SOURCE, "explanation": "交叉"}, None),
    ({"name": "", "source": "", "explanation": "RSI 暂不支持"}, None),
    ({"name": "错误", "source": "ta.rsi(close,14)", "explanation": ""}, "四行"),
])
def test_writer_retrieves_reference_and_checks_output(monkeypatch, candidate, expected_error):
    class Client:
        async def chat(self, system, prompt, temperature):
            context = json.loads(prompt)
            assert context["当前脚本"] == SOURCE
            assert any(line.startswith("ta.rsi(") for line in context["函数参考"])
            return json.dumps(candidate)
    monkeypatch.setattr(chart_writer, "get_configured_failover_client", lambda db: Client())
    call = chart_writer.write_script(None, "解释 RSI，保留当前交叉规则", SOURCE)
    if expected_error:
        with pytest.raises(ValueError, match=expected_error):
            asyncio.run(call)
    else:
        result = asyncio.run(call)
        assert result["source"] == candidate["source"]
        assert result["explanation"] == candidate["explanation"]
