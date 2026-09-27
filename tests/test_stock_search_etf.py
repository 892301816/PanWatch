"""东方财富实时搜索的场内基金（ETF/LOF）识别用例。

回归背景：东财对 ETF 返回 ``Classify=Fund``，旧逻辑只认 AStock/BJStock，
ETF 落到 ``else: continue`` 被整条丢弃，导致 510300 等场内基金无法搜索，
也就无法加入自选和持仓。这里锁定放宽的边界：

- 场内基金（MktNum 0=深 / 1=沪）放行；
- 场外基金（OTCFUND / MktNum=150）与债券仍然排除，避免把无行情、
  不可持仓的场外基金混进搜索结果。
"""

import unittest
from unittest.mock import patch

from src.platform.marketdata import stock_list


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """替身 httpx.Client —— 只回放固定搜索结果，不触网。"""

    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None, **kwargs):
        return _FakeResponse(self._payload)


def _search_with(items: list[dict], query: str = "510300", market: str = "", limit: int = 20):
    payload = {"QuotationCodeTable": {"Data": items}}
    with patch.object(stock_list.httpx, "Client", lambda *a, **kw: _FakeClient(payload)):
        return stock_list._realtime_search(query, market, limit)


def _item(code: str, name: str, classify: str, mkt_num: str | int, type_us: str = "9") -> dict:
    return {
        "Code": code,
        "Name": name,
        "Classify": classify,
        "SecurityTypeName": "基金" if classify != "Bond" else "债券",
        "MktNum": mkt_num,
        "TypeUS": type_us,
    }


class TestEtfSearch(unittest.TestCase):
    def test_sh_etf_is_returned(self):
        """沪市 ETF — Classify=Fund/MktNum=1 应作为 CN 标的返回"""
        items = [_item("510300", "沪深300ETF华泰柏瑞", "Fund", "1")]
        self.assertEqual(
            _search_with(items),
            [{"symbol": "510300", "name": "沪深300ETF华泰柏瑞", "market": "CN"}],
        )

    def test_sz_etf_is_returned(self):
        """深市 ETF — Classify=Fund/MktNum=0 应作为 CN 标的返回"""
        items = [_item("159915", "创业板ETF易方达", "Fund", "0", type_us="10")]
        self.assertEqual(
            _search_with(items, query="159915"),
            [{"symbol": "159915", "name": "创业板ETF易方达", "market": "CN"}],
        )

    def test_sz_etf_with_integer_mkt_num(self):
        """深市 ETF — MktNum 为整数 0 时不能被 falsy 判空误杀"""
        items = [_item("159915", "创业板ETF易方达", "Fund", 0, type_us="10")]
        self.assertEqual(
            _search_with(items, query="159915"),
            [{"symbol": "159915", "name": "创业板ETF易方达", "market": "CN"}],
        )

    def test_lof_is_returned(self):
        """场内 LOF — 与 ETF 同为交易所挂牌，一并放行"""
        items = [_item("161725", "白酒基金LOF", "Fund", "0", type_us="10")]
        self.assertEqual(
            _search_with(items, query="161725"),
            [{"symbol": "161725", "name": "白酒基金LOF", "market": "CN"}],
        )

    def test_otc_fund_is_still_excluded(self):
        """场外基金 — OTCFUND/MktNum=150 无交易所行情，仍应排除"""
        items = [_item("161725", "招商中证白酒指数(LOF)A", "OTCFUND", "150", type_us="7")]
        self.assertEqual(_search_with(items, query="161725"), [])

    def test_bond_is_still_excluded(self):
        """债券 — Classify=Bond 仍应排除，避免混入持仓候选"""
        items = [_item("019547", "16国债19", "Bond", "1", type_us="4")]
        self.assertEqual(_search_with(items, query="019547"), [])

    def test_stock_still_returned(self):
        """普通 A 股 — 原有行为不受影响"""
        items = [_item("600519", "贵州茅台", "AStock", "1", type_us="2")]
        self.assertEqual(
            _search_with(items, query="600519"),
            [{"symbol": "600519", "name": "贵州茅台", "market": "CN"}],
        )

    def test_market_filter_applies_to_etf(self):
        """市场筛选 — 只搜港股时不应返回 A 股 ETF"""
        items = [_item("510300", "沪深300ETF华泰柏瑞", "Fund", "1")]
        self.assertEqual(_search_with(items, market="HK"), [])

    def test_etf_and_stock_coexist(self):
        """混合结果 — ETF 与股票同时命中时顺序和数量都保留"""
        items = [
            _item("510300", "沪深300ETF华泰柏瑞", "Fund", "1"),
            _item("600519", "贵州茅台", "AStock", "1", type_us="2"),
            _item("019547", "16国债19", "Bond", "1", type_us="4"),
        ]
        self.assertEqual(
            [r["symbol"] for r in _search_with(items, query="300")],
            ["510300", "600519"],
        )


if __name__ == "__main__":
    unittest.main()
