"""分时 API:时段过滤 + 累计值 → 图用形态的换算。

源数据是**累计**量额且含收盘后的填充点,这两点都在接口层处理掉:
- 15:06–15:30 的点价格与量都不再变化,留着会把横轴拉长 25/242、走势被压扁;
- 图需要的是每分钟增量(量柱)与均价(累计额/累计量),不该让前端各自做一遍。
"""
from types import SimpleNamespace

import pytest

from src.modules.market.api import klines as kl


def _pt(t, price, cum_vol, cum_amt):
    return SimpleNamespace(time=t, price=price, volume=cum_vol, amount=cum_amt)


@pytest.fixture
def fake_md(monkeypatch):
    """替身行情层:回放一段含午休、含盘后填充的分时序列。"""

    def _install(points, prev_close=10.0):
        class _MD:
            def intraday(self, symbol, *, market=None):
                return points, prev_close

        monkeypatch.setattr(
            "src.platform.marketdata.marketdata_client.get_market_data", lambda: _MD()
        )
    return _install


class TestSessionFilter:
    @pytest.mark.parametrize(
        "t,expected",
        [
            ("0930", True), ("1130", True), ("1300", True), ("1500", True),
            ("0929", False), ("1131", False), ("1259", False), ("1501", False),
            ("1530", False),   # 实测腾讯会带出的盘后填充点
        ],
    )
    def test_in_session(self, t, expected):
        assert kl._in_session(t) is expected


class TestIntradayEndpoint:
    def test_filters_post_close_padding(self, fake_md):
        fake_md([
            _pt("1459", 10.0, 100, 1000.0),
            _pt("1500", 10.1, 110, 1100.0),
            _pt("1506", 10.1, 110, 1100.0),   # 盘后填充,价格与量都不再变
            _pt("1530", 10.1, 110, 1100.0),
        ])
        out = kl.get_intraday("600519", market="CN")
        assert [p["time"] for p in out["points"]] == ["1459", "1500"]
        assert out["count"] == 2

    def test_converts_cumulative_to_per_minute(self, fake_md):
        """量柱要的是每分钟增量,不是累计值。"""
        fake_md([
            _pt("0930", 10.0, 100, 1_000_000.0),
            _pt("0931", 10.2, 250, 2_530_000.0),
            _pt("0932", 10.1, 400, 4_040_000.0),
        ])
        out = kl.get_intraday("600519", market="CN")
        vols = [p["volume"] for p in out["points"]]
        # 首点增量即其自身(前值为 0),之后是相邻差值
        assert vols == [100.0, 150.0, 150.0]
        amts = [p["amount"] for p in out["points"]]
        assert amts == [1_000_000.0, 1_530_000.0, 1_510_000.0]

    def test_avg_price_uses_cumulative(self, fake_md):
        """均价 = 累计额 / 累计量 / 100(量单位是手)。"""
        # 累计 200 手 = 20000 股,累计额 250000 元 → 均价 12.5
        fake_md([_pt("0930", 12.5, 200, 250_000.0)])
        out = kl.get_intraday("600519", market="CN")
        assert out["points"][0]["avg_price"] == pytest.approx(12.5)

    def test_zero_volume_yields_no_avg(self, fake_md):
        """开盘首点前量为 0 时不该除零。"""
        fake_md([_pt("0930", 10.0, 0, 0.0)])
        out = kl.get_intraday("600519", market="CN")
        assert out["points"][0]["avg_price"] is None
        assert out["points"][0]["volume"] == 0.0

    def test_prev_close_passthrough(self, fake_md):
        fake_md([_pt("0930", 10.0, 10, 1000.0)], prev_close=9.87)
        assert kl.get_intraday("600519", market="CN")["prev_close"] == 9.87

    def test_empty_source_returns_empty_points(self, fake_md):
        fake_md([])
        out = kl.get_intraday("600519", market="CN")
        assert out["points"] == [] and out["count"] == 0


class TestMinuteKlineEndpoint:
    def test_rejects_unknown_period(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            kl.get_minute_klines("600519", market="CN", period="h1")
        assert exc.value.status_code == 400

    def test_passes_period_through(self, monkeypatch):
        seen = {}

        class _MD:
            def minute_klines(self, symbol, *, market, period, count):
                seen.update(symbol=symbol, market=market, period=period, count=count)
                return []

        monkeypatch.setattr(
            "src.platform.marketdata.marketdata_client.get_market_data", lambda: _MD()
        )
        out = kl.get_minute_klines("600519", market="CN", period="M15", count=50)
        assert seen == {"symbol": "600519", "market": "CN", "period": "m15", "count": 50}
        assert out["period"] == "m15" and out["klines"] == []
