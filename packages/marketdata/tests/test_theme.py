"""题材 vendor 测试:涨停/炸板/跌停池 + 概念板块。全部离线构造样例,monkeypatch market_get。

字段以实测响应为准(2026-10-11 抓的 20261010 交易日数据):
- 涨停池 push2ex: c/n/zdp/amount/hs/lbc/fbt/fund/zbc/hybk/zttj{days,ct}/ltsz
- 概念板块 push2 clist: f12/f14/f3/f6/f62/f104/f105/f128/f140/f136
"""
from __future__ import annotations

import marketdata.vendors.theme as th
from marketdata.client import MarketData
from marketdata.defaults import StaticConfigProvider
from marketdata.types import ConceptBoard, ZtPoolItem


def _zt_payload():
    return {
        "data": {
            "tc": 2,
            "pool": [
                {
                    "c": "600825", "n": "新华传媒", "zdp": 10.008779525756836,
                    "amount": 500000000, "hs": 12.5, "lbc": 9, "fbt": 92500,
                    "fund": 1421180000, "zbc": 0, "hybk": "出版",
                    "zttj": {"days": 9, "ct": 9}, "ltsz": 8000000000,
                },
                {
                    "c": "002058", "n": "紫竹高科", "zdp": 9.990999221801758,
                    "amount": 100791831, "hs": 2.8765933513641357, "lbc": 4, "fbt": 93018,
                    "fund": 89302489, "zbc": 1, "hybk": "电池",
                    "zttj": {"days": 4, "ct": 4}, "ltsz": 3503860934.08,
                },
            ],
        }
    }


def test_zt_pool_parses_all_fields(monkeypatch):
    monkeypatch.setattr(th, "market_get", lambda *a, **k: _zt_payload())

    out = th.ThemeVendor().zt_pool(date="20261010")
    assert len(out) == 2
    a = out[0]
    assert isinstance(a, ZtPoolItem)
    assert a.code == "600825" and a.name == "新华传媒"
    assert a.limit_up_days == 9            # 连板数
    assert a.first_seal_time == "092500"   # HHMMSS 补零成 6 位
    assert a.seal_fund == 1421180000       # 封板资金
    assert a.industry == "出版"
    assert a.stat_days == 9 and a.stat_count == 9
    assert out[1].break_count == 1         # 炸板次数
    assert out[1].first_seal_time == "093018"


def test_pool_passes_date_and_sort(monkeypatch):
    seen = {}

    def fake_get(url, *, params, **kw):
        seen["url"] = url
        seen["date"] = params["date"]
        seen["sort"] = params["sort"]
        seen["ut"] = params["ut"]
        return {"data": {"pool": []}}

    monkeypatch.setattr(th, "market_get", fake_get)
    v = th.ThemeVendor()
    v.zt_pool(date="20261010")
    assert "getTopicZTPool" in seen["url"] and seen["date"] == "20261010"
    # 日期必须透传,否则会拿错交易日的池子
    v.zb_pool(date="20261010")
    assert "getTopicZBPool" in seen["url"]
    v.dt_pool(date="20261010")
    assert "getTopicDTPool" in seen["url"] and seen["sort"] == "fund:asc"


def test_pool_handles_missing_and_dash(monkeypatch):
    """东财常把缺失值写成 "-";不能因此崩掉整条。"""
    monkeypatch.setattr(th, "market_get", lambda *a, **k: {"data": {"pool": [
        {"c": "000001", "n": "缺字段", "zdp": "-", "hs": "-", "fbt": None,
         "lbc": None, "fund": "-", "hybk": None, "zttj": None},
        "不是 dict",
        {"c": "000002", "n": "正常", "zdp": 5.0, "lbc": 2, "hybk": "测试"},
    ]}})
    out = th.ThemeVendor().zt_pool(date="20261010")
    assert [x.code for x in out] == ["000001", "000002"]
    assert out[0].change_pct is None and out[0].limit_up_days == 0
    assert out[0].first_seal_time == "" and out[0].industry == ""
    assert out[1].limit_up_days == 2


def test_zt_pool_empty_on_failure(monkeypatch):
    monkeypatch.setattr(th, "market_get", lambda *a, **k: None)
    assert th.ThemeVendor().zt_pool(date="20261010") == []


def test_concept_boards_parses_multi_fields(monkeypatch):
    payload = {"data": {"total": 2, "diff": [
        {"f12": "BK0968", "f14": "固态电池", "f3": 0.6, "f6": 178549859699.0,
         "f62": 4815399680.0, "f104": 126, "f105": 99,
         "f128": "紫竹高科", "f140": "002058", "f136": 9.99},
        {"f12": "BK0547", "f14": "黄金概念", "f3": 2.42, "f6": 47122358838.0,
         "f62": 3395899392.0, "f104": 64, "f105": 9,
         "f128": "西部黄金", "f140": "601069", "f136": 9.98},
    ]}}
    monkeypatch.setattr(th, "market_get", lambda *a, **k: payload)

    out = th.ThemeVendor().concept_boards()
    assert len(out) == 2
    b = out[0]
    assert isinstance(b, ConceptBoard)
    assert b.code == "BK0968" and b.name == "固态电池"
    assert b.change_pct == 0.6
    assert b.main_net_inflow == 4815399680.0
    assert b.up_count == 126 and b.down_count == 99
    assert b.leader_name == "紫竹高科" and b.leader_code == "002058"
    assert b.leader_change_pct == 9.99


def test_concept_boards_handles_dict_diff(monkeypatch):
    """东财 diff 有时是 {"0": {...}} 形式,必须归一化成 list。"""
    monkeypatch.setattr(th, "market_get", lambda *a, **k: {
        "data": {"diff": {"0": {"f12": "BK0001", "f14": "概念甲", "f3": 1.0}}}
    })
    out = th.ThemeVendor().concept_boards()
    assert len(out) == 1 and out[0].name == "概念甲"


def test_concept_boards_only_cn_and_skips_no_code(monkeypatch):
    monkeypatch.setattr(th, "market_get", lambda *a, **k: {"data": {"diff": [
        {"f14": "缺代码"}, {"f12": "BK0002", "f14": "有效"},
    ]}})
    v = th.ThemeVendor()
    assert v.concept_boards(market="US") == []          # 只覆盖 A 股
    out = v.concept_boards()
    assert [x.code for x in out] == ["BK0002"]


def test_marketdata_dispatch_by_vendor(monkeypatch):
    """MarketData 上的分派方法应把调用转给 theme vendor。"""
    monkeypatch.setattr(th, "market_get", lambda *a, **k: _zt_payload())

    md = MarketData(config=StaticConfigProvider({}))
    assert len(md.zt_pool(date="20261010")) == 2
    try:
        md.zt_pool(vendor="不存在的源", date="20261010")
        raise AssertionError("未知 vendor 应抛 ValueError")
    except ValueError:
        pass
