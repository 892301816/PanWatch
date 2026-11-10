"""早间汇总要写股票名、企业微信超长要显式截断。"""
import src.modules.market.message_service as ms
from src.platform.notifications.notifier import truncate_for_wecom


def test_brief_payload_includes_tracked_stock_names(monkeypatch):
    monkeypatch.setattr(ms, "_symbol_names", lambda syms: {"600276": "药明康德"})
    messages = [
        {
            "category": "公司",
            "time": "08:10",
            "source": "公告",
            "title": "获美国国防部移出 1260H 名单",
            "symbols": ["600276"],
            "in_position": True,
            "in_watchlist": False,
        }
    ]
    text = ms._build_brief_payload(messages, {"公司": 1})
    assert "药明康德(600276)" in text
    assert "◆" in text


def test_brief_payload_has_no_hint_for_unmarked_messages(monkeypatch):
    monkeypatch.setattr(ms, "_symbol_names", lambda syms: {"600276": "药明康德"})
    messages = [
        {
            "category": "行业",
            "time": "08:10",
            "source": "快讯",
            "title": "某个行业新闻",
            "symbols": ["600276"],
            "in_position": False,
            "in_watchlist": False,
        }
    ]
    text = ms._build_brief_payload(messages, {"行业": 1})
    assert "标的" not in text and "◆" not in text


def test_wecom_truncates_long_content():
    short = "短消息"
    assert truncate_for_wecom(short) == short

    long_text = "中" * 3000  # 9000 字节
    out = truncate_for_wecom(long_text)
    assert len(out.encode("utf-8")) <= 4000
    assert out.endswith("完整内容请在 PanWatch 查看")
