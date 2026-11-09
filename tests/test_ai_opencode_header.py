"""OpenCode Zen/Go 网关要求每个请求带 x-opencode-session,否则 400 MissingSessionID。"""
from src.platform.ai.ai_client import (
    AIClient,
    _is_opencode,
    _opencode_session_id,
)


def test_detects_opencode_hosts():
    assert _is_opencode("https://opencode.ai/zen/go/v1")
    assert _is_opencode("https://opencode.ai/zen/v1")
    assert _is_opencode("https://api.opencode.ai/v1")
    assert not _is_opencode("https://api.deepseek.com")
    assert not _is_opencode("https://opencode.ai.evil.com/v1")


def test_session_id_is_stable_per_model():
    a = _opencode_session_id("https://opencode.ai/zen/go/v1", "deepseek-v4.1-flash")
    b = _opencode_session_id("https://opencode.ai/zen/go/v1", "deepseek-v4.1-flash")
    c = _opencode_session_id("https://opencode.ai/zen/go/v1", "glm-5.2")
    assert a == b
    assert a != c
    assert a.startswith("ses_") and len(a) == 36


def test_client_sends_header_only_for_opencode():
    oc = AIClient("https://opencode.ai/zen/go/v1", "sk-test", "deepseek-v4.1-flash")
    assert oc.client.default_headers.get("x-opencode-session", "").startswith("ses_")

    other = AIClient("https://api.deepseek.com", "sk-test", "deepseek-chat")
    assert "x-opencode-session" not in other.client.default_headers


def test_explicit_session_id_wins():
    c = AIClient(
        "https://opencode.ai/zen/go/v1", "sk-test", "glm-5.2", session_id="ses_mine"
    )
    assert c.client.default_headers["x-opencode-session"] == "ses_mine"
