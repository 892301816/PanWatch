"""bkzj 榜单 vendor 用例(data.eastmoney.com,可切换的第二来源)。

这个接口与 push2 语义不同,容易踩的三个点都在这里钉住:
1. ``key`` 既决定返回哪个字段、也决定排序,所以响应里只有该字段;
2. 数值是定点整数,价格/涨跌幅/涨跌额要 ÷100;
3. 排序键不是 f3 时涨跌幅拿不到,需要再取一次 f3 按 f12 关联(板块卡片要显示涨跌幅)。
"""
import marketdata.vendors.discovery as dv
from marketdata.types import HotBoard, HotStock

BKZJ_URL = "https://data.eastmoney.com/dataapi/bkzj/getbkzj"


def _router(responses: dict[tuple[str, str], dict], calls: list | None = None):
    """按 (key, code) 返回不同 payload 的假 market_get。"""

    def fake_market_get(url, *, params=None, **kwargs):
        if calls is not None:
            calls.append(dict(params or {}))
        assert url == BKZJ_URL, f"未预期的 URL: {url}"
        return responses.get((params["key"], params["code"]), {"data": {"diff": []}})

    return fake_market_get


def test_hot_boards_gainers_uses_f3_and_scales(monkeypatch):
    """涨跌幅榜:一次 f3 请求,定点值 ÷100,只填 board 卡片用得到的字段。"""
    payload = {
        "data": {
            "diff": [
                {"f3": 583, "f12": "BK1502", "f13": 90, "f14": "林业Ⅲ"},
                {"f3": -249, "f12": "BK1201", "f13": 90, "f14": "电子"},
            ]
        }
    }
    calls: list = []
    monkeypatch.setattr(dv, "market_get", _router({("f3", "m:90+t:2"): payload}, calls))

    out = dv.DiscoveryBkzjVendor().hot_boards(market="CN", mode="gainers", limit=12)

    assert calls == [{"key": "f3", "code": "m:90+t:2"}]
    assert len(out) == 2 and isinstance(out[0], HotBoard)
    assert out[0].code == "BK1502" and out[0].name == "林业Ⅲ"
    assert out[0].change_pct == 5.83
    assert out[1].change_pct == -2.49
    # 单 key 请求拿不到这两项,由宿主按需补;此处应为 None 而不是 0。
    assert out[0].change_amount is None
    assert out[0].turnover is None


def test_hot_boards_turnover_joins_change_pct(monkeypatch):
    """成交额榜:f6 定序 + 再取一次 f3 关联涨跌幅,避免卡片涨跌幅为空。"""
    turnover = {
        "data": {
            "diff": [
                {"f6": 449248639818, "f12": "BK1201", "f13": 90, "f14": "电子"},
                {"f6": 206455963777, "f12": "BK1036", "f13": 90, "f14": "半导体"},
            ]
        }
    }
    pct = {
        "data": {
            "diff": [
                {"f3": -247, "f12": "BK1201", "f13": 90, "f14": "电子"},
                {"f3": -213, "f12": "BK1036", "f13": 90, "f14": "半导体"},
            ]
        }
    }
    monkeypatch.setattr(
        dv,
        "market_get",
        _router({("f6", "m:90+t:2"): turnover, ("f3", "m:90+t:2"): pct}),
    )

    out = dv.DiscoveryBkzjVendor().hot_boards(market="CN", mode="turnover", limit=12)

    # 顺序来自 f6 请求(已按成交额降序),涨跌幅来自第二次 f3 请求。
    assert [b.code for b in out] == ["BK1201", "BK1036"]
    assert out[0].change_pct == -2.47
    assert out[1].change_pct == -2.13
    assert out[0].turnover == 449248639818


def test_board_stocks_uses_b_prefix(monkeypatch):
    """成分股:板块代码须带 b: 前缀(不带会被服务端判为非法并返回 rc=102)。"""
    payload = {
        "data": {
            "diff": [
                {"f3": 1098, "f12": "301560", "f13": 0, "f14": "众捷股份"},
            ]
        }
    }
    calls: list = []
    monkeypatch.setattr(dv, "market_get", _router({("f3", "b:BK0481"): payload}, calls))

    out = dv.DiscoveryBkzjVendor().board_stocks(board_code="BK0481", mode="gainers", limit=20)

    assert calls == [{"key": "f3", "code": "b:BK0481"}]
    assert len(out) == 1 and isinstance(out[0], HotStock)
    assert out[0].symbol == "301560"
    assert out[0].market == "CN"
    assert out[0].name == "众捷股份"
    assert out[0].change_pct == 10.98
    # 价格/量额一律留空,由宿主用行情层补 —— 这里不能编造 0。
    assert out[0].price is None
    assert out[0].turnover is None
    assert out[0].volume is None


def test_board_stocks_turnover_leaves_pct_for_enrichment(monkeypatch):
    """成交额榜的成分股不再额外请求 f3:个股涨跌幅由宿主行情层补更准也更省。"""
    payload = {"data": {"diff": [{"f6": 2951694585, "f12": "600699", "f13": 1, "f14": "均胜电子"}]}}
    calls: list = []
    monkeypatch.setattr(dv, "market_get", _router({("f6", "b:BK0481"): payload}, calls))

    out = dv.DiscoveryBkzjVendor().board_stocks(board_code="BK0481", mode="turnover", limit=20)

    assert len(calls) == 1  # 只请求了一次
    assert out[0].change_pct is None
    assert out[0].symbol == "600699"


def test_hot_stocks_scales_price_scale_fields(monkeypatch):
    """全市场榜单可合并多段选择器,一次请求覆盖沪深主板/创业板/科创板。"""
    payload = {
        "data": {
            "diff": [
                {"f3": 2003, "f12": "301311", "f13": 0, "f14": "昆船智能"},
            ]
        }
    }
    calls: list = []
    monkeypatch.setattr(
        dv, "market_get", _router({("f3", dv._CN_ALL_STOCKS): payload}, calls)
    )

    out = dv.DiscoveryBkzjVendor().hot_stocks(market="CN", mode="gainers", limit=20)

    assert calls == [{"key": "f3", "code": dv._CN_ALL_STOCKS}]
    assert out[0].change_pct == 20.03
    assert out[0].price is None


def test_suspended_dash_becomes_none(monkeypatch):
    """停牌股返回 "-",应转成 None 而不是抛异常或变成 0。"""
    payload = {"data": {"diff": [{"f3": "-", "f12": "301560", "f13": 0, "f14": "众捷股份"}]}}
    monkeypatch.setattr(dv, "market_get", _router({("f3", "b:BK0481"): payload}))

    out = dv.DiscoveryBkzjVendor().board_stocks(board_code="BK0481", mode="gainers", limit=20)

    assert out[0].change_pct is None


def test_empty_rows_and_blank_code_are_dropped(monkeypatch):
    """缺代码的行应被丢弃,不留空 symbol 给下游。"""
    payload = {"data": {"diff": [{"f3": 100, "f14": "无代码"}, {"f3": 200, "f12": "600000", "f14": "浦发银行"}]}}
    monkeypatch.setattr(dv, "market_get", _router({("f3", "b:BK0481"): payload}))

    out = dv.DiscoveryBkzjVendor().board_stocks(board_code="BK0481", mode="gainers", limit=20)

    assert [s.symbol for s in out] == ["600000"]


def test_board_stocks_empty_code_makes_no_request(monkeypatch):
    """空板块代码不应发请求(与 DiscoveryVendor 行为一致)。"""
    called: list = []
    monkeypatch.setattr(dv, "market_get", lambda *a, **k: called.append(1))
    assert dv.DiscoveryBkzjVendor().board_stocks(board_code="", mode="gainers", limit=20) == []
    assert called == []


def test_non_cn_market_returns_empty(monkeypatch):
    """bkzj 只覆盖 A 股,港美股返回空(由宿主回落合成桶)。"""
    called: list = []
    monkeypatch.setattr(dv, "market_get", lambda *a, **k: called.append(1))
    v = dv.DiscoveryBkzjVendor()
    assert v.hot_boards(market="HK", limit=5) == []
    assert v.hot_stocks(market="US", limit=5) == []
    assert called == []


def test_limit_is_clamped(monkeypatch):
    """limit 上限 100,防止一次拉全量。"""
    payload = {"data": {"diff": [{"f3": 100, "f12": f"BK{i:04d}", "f14": f"板块{i}"} for i in range(150)]}}
    monkeypatch.setattr(dv, "market_get", _router({("f3", "m:90+t:2"): payload}))

    out = dv.DiscoveryBkzjVendor().hot_boards(market="CN", mode="gainers", limit=999)

    assert len(out) == 100


def test_registry_exposes_both_providers():
    """权威表里两个 provider 都在,宿主 DataSource 表才能按名切换。"""
    assert set(dv.DISCOVERY_VENDOR_CLASSES) == {"eastmoney", "eastmoney_bkzj"}
    assert dv.DISCOVERY_PROVIDERS == frozenset({"eastmoney", "eastmoney_bkzj"})
