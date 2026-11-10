"""扶摇行情不带名称时,用本地 stocks 表补名。"""
from types import SimpleNamespace

import src.platform.marketdata.marketdata_client as mc
import src.platform.persistence.database as dbmod


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *a, **k):
        return _FakeQuery(self._rows)

    def close(self):
        pass


def test_fills_missing_names(monkeypatch):
    rows = [("600501", "航天晨光"), ("600900", "长江电力")]
    monkeypatch.setattr(dbmod, "SessionLocal", lambda: _FakeSession(rows))

    quotes = [
        SimpleNamespace(symbol="600501", name=""),
        SimpleNamespace(symbol="600900", name=""),
        SimpleNamespace(symbol="601728", name="中国电信"),
    ]
    mc.fill_missing_names(quotes)
    assert quotes[0].name == "航天晨光"
    assert quotes[1].name == "长江电力"
    assert quotes[2].name == "中国电信"  # 已有名称不动


def test_noop_when_all_named(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(dbmod, "SessionLocal", lambda: called.__setitem__("n", called["n"] + 1))
    quotes = [SimpleNamespace(symbol="600501", name="航天晨光")]
    assert mc.fill_missing_names(quotes) is quotes
    assert called["n"] == 0
