"""TradingAgents 走 LangChain 时也要带 x-opencode-session,否则深度分析 400。"""
import pytest

from src.modules.automation.tradingagents.runtime_support import apply_compat_patches


@pytest.fixture(scope="module", autouse=True)
def _patched():
    apply_compat_patches()


def test_langchain_client_gets_header_for_opencode():
    pytest.importorskip("langchain_openai")
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        model="deepseek-v4.1-flash",
        base_url="https://opencode.ai/zen/go/v1",
        api_key="sk-test",
    )
    assert (llm.default_headers or {}).get("x-opencode-session", "").startswith("ses_")


def test_langchain_client_untouched_for_other_hosts():
    pytest.importorskip("langchain_openai")
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(model="deepseek-chat", base_url="https://api.deepseek.com", api_key="sk-test")
    assert "x-opencode-session" not in (llm.default_headers or {})


def test_patch_is_idempotent():
    langchain_openai = pytest.importorskip("langchain_openai")

    before = langchain_openai.ChatOpenAI.__init__
    apply_compat_patches()
    assert langchain_openai.ChatOpenAI.__init__ is before
