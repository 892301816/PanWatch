from types import SimpleNamespace

from marketdata import Quote
import src.platform.marketdata.marketdata_client as mc


def test_quote_to_row_keys():
    q = Quote(symbol="600519", market="CN", current_price=1700.0, name="贵州茅台",
              change_pct=1.2, volume_ratio=1.1, pe_ratio=35.0)
    row = mc._quote_to_row(q)
    assert row["symbol"] == "600519" and row["name"] == "贵州茅台"
    assert row["current_price"] == 1700.0 and row["change_pct"] == 1.2
    assert row["volume_ratio"] == 1.1 and row["pe_ratio"] == 35.0
    # 兼容旧 orchestrator dict 的关键键都在
    for k in ("symbol", "name", "market", "current_price", "change_pct",
              "change_amount", "prev_close", "open_price", "high_price",
              "low_price", "volume", "turnover", "turnover_rate",
              "circulating_market_value", "total_market_value"):
        assert k in row


def test_md_quote_rows_uses_marketdata(monkeypatch):
    class _MD:
        def quotes(self, symbols, *, market):
            return [Quote(symbol=s, market=market, current_price=9.0) for s in symbols]

    monkeypatch.setattr(mc, "get_market_data", lambda: _MD())
    rows = mc.md_quote_rows(["600519", "000001"], "CN")
    assert [r["symbol"] for r in rows] == ["600519", "000001"]
    assert all(r["current_price"] == 9.0 and r["market"] == "CN" for r in rows)


def test_db_config_provider_maps_rows(monkeypatch):
    rows = [
        SimpleNamespace(provider="tencent", priority=1, config={"k": "v"}, supports_batch=True),
        SimpleNamespace(provider="yfinance", priority=2, config=None, supports_batch=False),
    ]
    cp = mc.DbConfigProvider()
    monkeypatch.setattr(cp, "_query_rows", lambda datatype: rows)
    got = cp.sources_for("quote", "CN")
    assert [(s.vendor, s.priority, s.enabled, s.config) for s in got] == [
        ("tencent", 1, True, {"k": "v"}),
        ("yfinance", 2, True, {}),
    ]


def test_db_config_provider_keeps_tencent_for_us_kline(monkeypatch):
    """美股 K 线不得跳过腾讯:跳过会覆盖用户配的优先级,导致美股 K 线取不到数据。

    原先为规避"腾讯美股返回 501"硬编码跳过 kline+US+tencent。但实测(2026-09)腾讯美股
    K 线 9/9 成功、单次 0.2s 内,而它退让给的 stooq 在本网络单次可挂 90s+、yahoo 也拿不到
    数据 —— 整条链反而取不到数据。真遇到 501 时 Engine 会按 priority 继续失败转移。
    """
    rows = [
        SimpleNamespace(provider="tencent", priority=0, config={}, supports_batch=False),
        SimpleNamespace(provider="stooq", priority=15, config={}, supports_batch=False),
    ]
    cp = mc.DbConfigProvider()
    monkeypatch.setattr(cp, "_query_rows", lambda datatype: rows)

    got = cp.sources_for("kline", "US")

    # 顺序与优先级完全按数据源表,不做任何静态改写
    assert [s.vendor for s in got] == ["tencent", "stooq"]
    assert [s.priority for s in got] == [0, 15]


def test_db_config_provider_does_not_rewrite_any_market(monkeypatch):
    """其它 datatype/market 组合同样原样透传,不做隐藏改写。"""
    rows = [
        SimpleNamespace(provider="tencent", priority=0, config={}, supports_batch=True),
        SimpleNamespace(provider="sina", priority=5, config={}, supports_batch=True),
    ]
    cp = mc.DbConfigProvider()
    monkeypatch.setattr(cp, "_query_rows", lambda datatype: rows)

    for datatype in ("quote", "kline", "news"):
        for market in ("CN", "HK", "US"):
            got = cp.sources_for(datatype, market)
            assert [s.vendor for s in got] == ["tencent", "sina"], f"{datatype}/{market}"
