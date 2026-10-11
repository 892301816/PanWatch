"""快讯(7×24)vendor 测试:cls/sina/eastmoney 均离线构造样例,monkeypatch 各自模块内 market_get。

沙箱代理会拦真实端点,字段映射按文档尽力构造,未文档化字段(cls 的 level/stock_list、
sina 的 ext.stocks)用防御性样例覆盖,断言不崩且尽量抽取正确。
"""
from __future__ import annotations

from datetime import datetime, timezone

import marketdata.vendors.flash_news as fn
from marketdata.client import MarketData
from marketdata.defaults import StaticConfigProvider
from marketdata.ports import SourceConfig
from marketdata.types import FlashNews


# ---------------------------------------------------------------------------
# cls(财联社)
# ---------------------------------------------------------------------------

def test_cls_sign_is_deterministic():
    params = {
        "appName": "CailianpressWeb",
        "os": "web",
        "sv": "7.7.5",
        "last_time": "",
        "refresh_type": 1,
        "rn": 50,
    }
    sign1 = fn._cls_sign(params)
    sign2 = fn._cls_sign(params)
    # 同样的 params -> 同样的 sign(确定性,便于离线测试与调试)
    assert sign1 == sign2
    assert len(sign1) == 32  # md5 hex digest 长度固定
    assert all(c in "0123456789abcdef" for c in sign1)

    # 手算校验:sha1(qs).hexdigest() 再 md5
    import hashlib
    qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
    expected = hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()
    assert sign1 == expected


def test_cls_parses_and_maps_importance_and_symbols(monkeypatch):
    payload = {
        "data": {
            "roll_data": [
                {
                    "id": 1001,
                    "title": "央行公开市场操作",
                    "content": "央行今日开展逆回购操作",
                    "brief": "",
                    "ctime": 1752652800,  # 2025-07-16 08:00:00 UTC 附近的固定戳(可复现)
                    "level": "A",
                    "stock_list": [{"code": "600519", "name": "贵州茅台"}, {"SecurityCode": "000001"}],
                    "shareurl": "https://www.cls.cn/detail/1001",
                },
                {
                    # 无 title/content,回退 brief;level 为未识别字符串 -> importance=0
                    "id": 1002,
                    "title": "",
                    "content": "",
                    "brief": "简讯内容",
                    "ctime": "1752652900",
                    "level": "Z",
                    "stock_list": [],
                    "shareurl": "",
                },
            ]
        }
    }
    monkeypatch.setattr(fn, "market_get", lambda *a, **k: payload)

    out = fn.ClsFlashNewsVendor().fetch([], {"days": 20})
    assert len(out) == 2
    assert all(isinstance(x, FlashNews) for x in out)

    first = out[0]
    assert first.source == "cls"
    assert first.external_id == "1001"
    assert first.title == "央行公开市场操作"
    assert first.content == "央行今日开展逆回购操作"
    assert first.publish_time == datetime.fromtimestamp(1752652800, tz=timezone.utc)
    assert first.importance == 3  # level="A" -> 3
    assert first.symbols == ["600519", "000001"]
    assert first.url == "https://www.cls.cn/detail/1001"

    second = out[1]
    assert second.title == "简讯内容"  # 回退 brief
    assert second.content == "简讯内容"
    assert second.importance == 0  # 未识别 level
    assert second.symbols == []


def test_cls_empty_response(monkeypatch):
    monkeypatch.setattr(fn, "market_get", lambda *a, **k: None)
    assert fn.ClsFlashNewsVendor().fetch([], {}) == []

    monkeypatch.setattr(fn, "market_get", lambda *a, **k: {"data": {"roll_data": []}})
    assert fn.ClsFlashNewsVendor().fetch([], {}) == []


def test_cls_tolerates_broken_item(monkeypatch):
    """单条解析异常应被跳过而非整体失败。"""
    payload = {"data": {"roll_data": [{"id": 1, "title": None, "content": None, "brief": None}]}}
    monkeypatch.setattr(fn, "market_get", lambda *a, **k: payload)
    assert fn.ClsFlashNewsVendor().fetch([], {}) == []


# ---------------------------------------------------------------------------
# sina(新浪直播)
# ---------------------------------------------------------------------------

def test_sina_parses_ext_stocks_and_time(monkeypatch):
    payload = {
        "result": {
            "data": {
                "feed": {
                    "list": [
                        {
                            "id": "2001",
                            "rich_text": "沪指高开0.5%,两市成交额破万亿",
                            "create_time": "2026-07-16 09:31:00",
                            "ext": '{"stocks": [{"symbol": "sh600519", "name": "贵州茅台"}, {"code": "000001"}]}',
                        },
                        {
                            # 无 rich_text,回退 content;create_time 为秒戳字符串
                            "id": "2002",
                            "content": "深成指低开",
                            "create_time": "1752652800",
                            "ext": "",
                        },
                    ]
                }
            }
        }
    }
    monkeypatch.setattr(fn, "market_get", lambda *a, **k: payload)

    out = fn.SinaFlashNewsVendor().fetch([], {"days": 20})
    assert len(out) == 2
    first = out[0]
    assert first.source == "sina"
    assert first.external_id == "2001"
    assert first.content == "沪指高开0.5%,两市成交额破万亿"
    assert first.publish_time == datetime(2026, 7, 16, 9, 31, 0, tzinfo=timezone.utc)
    assert first.symbols == ["sh600519", "000001"]

    second = out[1]
    assert second.content == "深成指低开"  # 回退 content
    assert second.publish_time == datetime.fromtimestamp(1752652800, tz=timezone.utc)
    assert second.symbols == []


def test_sina_empty_response(monkeypatch):
    monkeypatch.setattr(fn, "market_get", lambda *a, **k: None)
    assert fn.SinaFlashNewsVendor().fetch([], {}) == []

    monkeypatch.setattr(fn, "market_get", lambda *a, **k: {"result": {"data": {"feed": {"list": []}}}})
    assert fn.SinaFlashNewsVendor().fetch([], {}) == []


def test_sina_tolerates_broken_ext_json(monkeypatch):
    """ext 不是合法 JSON 时不应崩,symbols 回退空列表。"""
    payload = {
        "result": {
            "data": {
                "feed": {
                    "list": [
                        {"id": "3", "rich_text": "测试内容", "create_time": "2026-07-16 10:00:00", "ext": "{not json"}
                    ]
                }
            }
        }
    }
    monkeypatch.setattr(fn, "market_get", lambda *a, **k: payload)
    out = fn.SinaFlashNewsVendor().fetch([], {})
    assert len(out) == 1
    assert out[0].symbols == []


# ---------------------------------------------------------------------------
# eastmoney(东财快讯)
# ---------------------------------------------------------------------------

def test_eastmoney_parses_title_summary_time(monkeypatch):
    payload = {
        "data": {
            "fastNewsList": [
                {
                    "id": "4001",
                    "title": "机构:三季度A股有望震荡上行",
                    "summary": "多家机构发布三季度策略展望",
                    "showTime": "2026-07-16 11:20:00",
                },
                {
                    # showTime 格式不可解析 -> 回退 EPOCH,不崩
                    "id": "4002",
                    "title": "快讯标题",
                    "summary": "摘要内容",
                    "showTime": "not-a-date",
                },
            ]
        }
    }
    monkeypatch.setattr(fn, "market_get", lambda *a, **k: payload)

    out = fn.EastmoneyFlashNewsVendor().fetch([], {"days": 30})
    assert len(out) == 2
    first = out[0]
    assert first.source == "eastmoney"
    assert first.title == "机构:三季度A股有望震荡上行"
    assert first.content == "多家机构发布三季度策略展望"
    assert first.publish_time == datetime(2026, 7, 16, 11, 20, 0, tzinfo=timezone.utc)
    assert first.symbols == []
    assert first.importance == 0

    second = out[1]
    assert second.publish_time == fn._EPOCH  # 不可解析时间的防御回退


def test_eastmoney_empty_response(monkeypatch):
    monkeypatch.setattr(fn, "market_get", lambda *a, **k: None)
    assert fn.EastmoneyFlashNewsVendor().fetch([], {}) == []

    monkeypatch.setattr(fn, "market_get", lambda *a, **k: {"data": {"fastNewsList": []}})
    assert fn.EastmoneyFlashNewsVendor().fetch([], {}) == []


# ---------------------------------------------------------------------------
# MarketData.flash_news() —— 走单源 Engine 出数
# ---------------------------------------------------------------------------

def test_marketdata_flash_news_single_source(monkeypatch):
    payload = {
        "data": {
            "roll_data": [
                {
                    "id": "9001",
                    "title": "测试快讯标题",
                    "content": "测试快讯正文包含关键字ABC",
                    "brief": "",
                    "ctime": 1752652800,
                    "level": "B",
                    "stock_list": [],
                    "shareurl": "",
                }
            ]
        }
    }
    monkeypatch.setattr(fn, "market_get", lambda *a, **k: payload)

    md = MarketData(
        config=StaticConfigProvider(
            {"flash_news": [SourceConfig(vendor="cls", config={}, enabled=True)]}
        )
    )
    out = md.flash_news(limit=20)
    assert len(out) == 1
    assert out[0].title == "测试快讯标题"
    assert out[0].importance == 2  # level="B" -> 2

    # keyword 过滤:命中 title/content 才保留
    out_kw = md.flash_news(limit=20, keyword="ABC")
    assert len(out_kw) == 1
    out_kw_miss = md.flash_news(limit=20, keyword="不存在的关键字")
    assert out_kw_miss == []


# ---------------------------------------------------------------------------
# ths(同花顺 A 股要闻)
# ---------------------------------------------------------------------------

def test_ths_parses_digest_time_and_stock_codes(monkeypatch):
    payload = {
        "code": 200,
        "data": {
            "list": [
                {
                    "id": "5360453",
                    "title": "华塑控股：就收购公告上传纰漏事项澄清并致歉",
                    "digest": "华塑控股公告称，公司将强化信息披露全流程管控。",
                    "url": "https://news.10jqka.com.cn/20261011/c680559574.shtml",
                    "rtime": "1791706450",  # 秒 -> 2026-10-11 08:14:10 UTC
                    "stock": [{"name": "华塑控股", "stockCode": "000509", "stockMarket": "33"}],
                }
            ]
        },
    }
    monkeypatch.setattr(fn, "market_get", lambda *a, **k: payload)

    out = fn.ThsFlashNewsVendor().fetch([], {})
    assert len(out) == 1
    n = out[0]
    assert isinstance(n, FlashNews)
    assert n.source == "ths"
    assert n.external_id == "5360453"
    assert n.title.startswith("华塑控股")
    assert n.content.startswith("华塑控股公告称")
    assert n.publish_time == datetime.fromtimestamp(1791706450, tz=timezone.utc)
    assert n.symbols == ["000509"]  # stockCode 键被 _extract_symbol_code 认到
    assert n.url.endswith("c680559574.shtml")


def test_ths_empty_and_missing_title(monkeypatch):
    monkeypatch.setattr(fn, "market_get", lambda *a, **k: None)
    assert fn.ThsFlashNewsVendor().fetch([], {}) == []

    monkeypatch.setattr(
        fn, "market_get",
        lambda *a, **k: {"data": {"list": [{"id": "1", "title": ""}, "坏数据"]}},
    )
    assert fn.ThsFlashNewsVendor().fetch([], {}) == []


# ---------------------------------------------------------------------------
# jin10(金十数据)
# ---------------------------------------------------------------------------

def test_jin10_title_falls_back_to_bracket_prefix():
    # data.title 恒空 -> 从 content 抽开头【...】
    assert fn._jin10_title("", "【分众传媒：收购获批】金十数据10月11日讯，…") == "分众传媒：收购获批"
    # 无【】-> 截断正文
    t = fn._jin10_title("", "声明：巴基斯坦总统宣布紧急状态。")
    assert t.startswith("声明：")
    # 有 title 优先用 title
    assert fn._jin10_title("显式标题", "【别的】正文") == "显式标题"


def test_jin10_parses_content_time_and_importance(monkeypatch):
    payload = {
        "status": 200,
        "data": [
            {
                "id": "20261011162350281800",
                "time": "2026-10-11 16:23:50",
                "important": 2,
                "data": {
                    "title": "",
                    "content": "【哈药股份：公司董事兼总裁被立案】金十数据10月11日讯，…",
                    "source": "CCTV国际时讯",
                },
            }
        ],
    }
    monkeypatch.setattr(fn, "market_get", lambda *a, **k: payload)

    out = fn.Jin10FlashNewsVendor().fetch([], {})
    assert len(out) == 1
    n = out[0]
    assert n.source == "jin10"
    assert n.external_id == "20261011162350281800"
    assert n.title == "哈药股份：公司董事兼总裁被立案"
    assert n.content.startswith("【哈药股份")
    assert n.publish_time == datetime(2026, 10, 11, 16, 23, 50, tzinfo=timezone.utc)
    assert n.importance == 2


def test_jin10_empty_and_broken_payload(monkeypatch):
    monkeypatch.setattr(fn, "market_get", lambda *a, **k: None)
    assert fn.Jin10FlashNewsVendor().fetch([], {}) == []

    monkeypatch.setattr(
        fn, "market_get",
        lambda *a, **k: {"data": [{"id": "1"}, "坏数据", {"id": "2", "data": {"content": "正常"}}]},
    )
    out = fn.Jin10FlashNewsVendor().fetch([], {})
    assert [n.external_id for n in out] == ["2"]


def test_marketdata_flash_news_single_source_ths_and_jin10(monkeypatch):
    monkeypatch.setattr(
        fn, "market_get",
        lambda *a, **k: {"data": {"list": [{"id": "1", "title": "同花顺要闻", "rtime": "1791706450"}]}},
    )
    md = MarketData(config=StaticConfigProvider({
        "flash_news": [SourceConfig(vendor="ths", config={}, enabled=True)],
    }))
    out = md.flash_news(limit=20)
    assert len(out) == 1 and out[0].source == "ths"

    monkeypatch.setattr(
        fn, "market_get",
        lambda *a, **k: {"data": [{"id": "9", "time": "2026-10-11 16:23:50",
                                   "data": {"content": "【金十标题】正文"}}]},
    )
    md2 = MarketData(config=StaticConfigProvider({
        "flash_news": [SourceConfig(vendor="jin10", config={}, enabled=True)],
    }))
    out2 = md2.flash_news(limit=20)
    assert len(out2) == 1 and out2[0].source == "jin10" and out2[0].title == "金十标题"


# ---------------------------------------------------------------------------
# flash_news 聚合去重(2026-10-11:主备语义 → 聚合语义)
# ---------------------------------------------------------------------------

from marketdata.client import _dedupe_flash_news, _flash_news_fingerprint


def _fn(source, ext_id, title, content=""):
    return FlashNews(
        source=source, external_id=ext_id, title=title, content=content,
        publish_time=datetime(2026, 10, 11, tzinfo=timezone.utc),
    )


def test_flash_fingerprint_normalizes_punctuation():
    """全角/半角标点差异不应影响判重。"""
    a = _flash_news_fingerprint(_fn("cls", "1", "哈药股份：公司董事兼总裁被立案"))
    b = _flash_news_fingerprint(_fn("jin10", "2", "哈药股份:公司董事兼总裁被立案"))
    assert a == b and len(a) >= 8


def test_flash_fingerprint_falls_back_to_content_and_strips_bracket():
    """标题为空时用正文,并剥掉开头【…】。"""
    fp = _flash_news_fingerprint(
        _fn("jin10", "3", "", "【牧原股份：9月商品猪销量642.3万头】金十数据10月11日讯，…")
    )
    assert fp.startswith("牧原股份")


def test_flash_dedupe_keeps_first_occurrence():
    """跨源同一事件只留一条,保留先出现(=高优先级源)的。"""
    a = _fn("cls", "1", "哈药股份：公司董事兼总裁被立案、实施留置")
    b = _fn("jin10", "20261011172134255800", "哈药股份:公司董事兼总裁被立案、实施留置")
    c = _fn("ths", "5360535", "东百集团：拟收购光探测芯片公司")
    out = _dedupe_flash_news([a, b, c])
    assert len(out) == 2
    assert out[0].source == "cls"
    assert {x.source for x in out} == {"cls", "ths"}


def test_flash_dedupe_keeps_short_fingerprints_separate():
    """指纹过短(不足以判定事件)不参与去重,避免误合并。"""
    assert len(_dedupe_flash_news([_fn("sina", "1", "ok"), _fn("sina", "2", "ok")])) == 2


def test_marketdata_flash_news_aggregates_all_sources(monkeypatch):
    """聚合语义:所有已启用源都被执行(主备语义下第二个源根本不会跑),跨源重复被去掉。"""
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        if "cls.cn" in url:
            return {"data": {"roll_data": [
                {"id": 1, "title": "哈药股份：公司董事兼总裁被立案", "ctime": 1791710457, "level": "A"},
            ]}}
        if "10jqka" in url:
            return {"data": {"list": [
                {"id": "5360535", "title": "哈药股份:公司董事兼总裁被立案", "rtime": "1791710457", "stock": []},
                {"id": "5360536", "title": "东百集团：拟收购光探测芯片公司", "rtime": "1791710458", "stock": []},
            ]}}
        return None

    monkeypatch.setattr(fn, "market_get", fake_get)

    md = MarketData(config=StaticConfigProvider({
        "flash_news": [
            SourceConfig(vendor="cls", config={}, enabled=True, priority=0),
            SourceConfig(vendor="ths", config={}, enabled=True, priority=1),
        ]
    }))
    out = md.flash_news(limit=20)

    assert len(calls) == 2, "两个源都应被调用"
    assert len(out) == 2, "3 条输入去重后应为 2 条"
    assert {x.source for x in out} == {"cls", "ths"}


def test_marketdata_flash_news_skips_disabled_source(monkeypatch):
    """未启用的源不参与聚合。"""
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return {"data": {"roll_data": [{"id": 1, "title": "仅财联社可见的条目内容", "ctime": 1791710457}]}}

    monkeypatch.setattr(fn, "market_get", fake_get)
    md = MarketData(config=StaticConfigProvider({
        "flash_news": [
            SourceConfig(vendor="cls", config={}, enabled=True, priority=0),
            SourceConfig(vendor="ths", config={}, enabled=False, priority=1),
        ]
    }))
    out = md.flash_news(limit=20)
    assert len(calls) == 1
    assert all(x.source == "cls" for x in out)


def test_cls_clamps_rn_to_api_limit(monkeypatch):
    """cls 接口 rn>50 会静默返空(不报错),故调用方传更大 limit 时必须钳制到 50。"""
    seen = {}

    def fake_get(url, *, params, **kw):
        seen["rn"] = params["rn"]
        return {"data": {"roll_data": []}}

    monkeypatch.setattr(fn, "market_get", fake_get)
    fn.ClsFlashNewsVendor().fetch([], {"days": 200})
    assert seen["rn"] == 50
    fn.ClsFlashNewsVendor().fetch([], {"days": 20})
    assert seen["rn"] == 20


def test_sina_clamps_page_size(monkeypatch):
    seen = {}

    def fake_get(url, *, params, **kw):
        seen["page_size"] = params["page_size"]
        return {"result": {"data": {"feed": {"list": []}}}}

    monkeypatch.setattr(fn, "market_get", fake_get)
    fn.SinaFlashNewsVendor().fetch([], {"days": 500})
    assert seen["page_size"] == 100
