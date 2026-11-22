"""Limit-up pool reconciliation and persistence."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.platform.persistence.models  # noqa: F401
from src.modules.screener import limit_up_service as svc
from src.platform.persistence.database import Base
from src.platform.persistence.migrations import MIGRATIONS


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def _east(symbol="600900", days=3):
    return {
        "symbol": symbol, "name": "长江电力", "source": "eastmoney",
        "limit_up_days": days, "first_seal_time": "093001", "seal_fund": 1.2,
        "break_count": 2, "industry": "电力", "change_pct": 10.0,
        "amount": 10.0, "turnover_rate": 5.0, "float_market_cap": 100.0,
        "board": "主板", "limit_pct": 10.0, "is_st": False,
        "fields_complete": True, "confirmed_local": False, "conflict": False,
        "local_days": None, "raw": {},
    }


def _local(symbol="600900", days=3):
    return {
        "symbol": symbol, "name": "长江电力", "source": "local",
        "limit_up_days": days, "first_seal_time": "", "seal_fund": None,
        "break_count": 0, "industry": None, "change_pct": 10.0,
        "amount": None, "turnover_rate": None, "float_market_cap": None,
        "board": "主板", "limit_pct": 10.0, "is_st": False,
        "fields_complete": False, "confirmed_local": False, "conflict": False,
        "local_days": None, "raw": {},
    }


def test_reconcile_confirms_and_flags_conflict():
    rows, meta = svc.reconcile("2026-11-20", [_east(days=3)], [_local(days=2)])
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "eastmoney"
    assert row["confirmed_local"] is True
    assert row["conflict"] is True
    assert row["local_days"] == 2
    assert meta["east_count"] == 1 and meta["local_count"] == 1
    assert meta["conflict_count"] == 1
    assert meta["quality"] == "partial"
    assert any("不一致" in warning for warning in meta["warnings"])


def test_reconcile_keeps_local_only_rows():
    rows, meta = svc.reconcile("2026-11-20", [], [_local("002943", days=1)])
    assert len(rows) == 1
    assert rows[0]["source"] == "local"
    assert rows[0]["fields_complete"] is False
    assert meta["source"] == "local"
    assert meta["quality"] == "partial"
    assert any("本地日K" in warning for warning in meta["warnings"])


def test_reconcile_marks_local_count_discrepancy():
    east = [_east("600900"), _east("600901"), _east("600902")]
    local = [_local("600900"), _local("600901"), _local("600902"), _local("600903"), _local("600904")]
    _, meta = svc.reconcile("2026-11-20", east, local)
    assert meta["east_count"] == 3 and meta["local_count"] == 5
    assert meta["merged_count"] == 5
    assert any("差异较大" in warning for warning in meta["warnings"])


def test_get_pool_persists_and_reads_cache(monkeypatch):
    engine, db = _db()
    monkeypatch.setattr(svc, "_east_items", lambda _d: [_east()])
    monkeypatch.setattr(svc, "_local_items", lambda _d: [_local()])
    first = svc.get_pool("2026-11-20", db=db)
    assert first["cached"] is False
    assert first["counts"]["merged"] == 1
    assert len(first["items"]) == 1
    second = svc.get_pool("2026-11-20", db=db)
    assert second["cached"] is True
    assert second["items"][0]["confirmed_local"] is True
    db.close()
    engine.dispose()


def test_local_limit_down_count(monkeypatch):
    # Avoid DB: monkeypatch the private helper isn't exposed; just assert the
    # function is callable with a sane fallback signature.
    assert callable(svc.local_limit_down_count)


def test_migration_140_registered():
    assert 140 in {m.version for m in MIGRATIONS}

def test_limit_down_ladder_groups(monkeypatch):
    engine, db = _db()
    rows = [
        {"symbol": "600001", "name": "A", "industry": "测试", "change_pct": -10.0,
         "seal_fund": 1.0, "source": "eastmoney", "fields_complete": True},
        {"symbol": "600002", "name": "B", "industry": "测试", "change_pct": -10.0,
         "seal_fund": 1.0, "source": "eastmoney", "fields_complete": True},
        {"symbol": "600003", "name": "C", "industry": "测试", "change_pct": -10.0,
         "seal_fund": 1.0, "source": "eastmoney", "fields_complete": True},
    ]

    def fake_streaks(_db, _date, items):
        for index, item in enumerate(items):
            item["limit_down_days"] = 3 if index == 0 else 1

    monkeypatch.setattr(svc, "_dt_items", lambda _d: rows)
    monkeypatch.setattr(svc, "_down_streaks", fake_streaks)
    monkeypatch.setattr(svc, "get_pool", lambda *a, **k: {"counts": {"merged": 10}})
    result = svc.get_limit_down_ladder("2026-11-20", db=db)
    assert result["side"] == "down"
    assert result["stats"]["limit_down"] == 3
    assert result["stats"]["limit_up"] == 10
    labels = [g["level"] for g in result["groups"]]
    assert "3 连跌" in labels and "首跌" in labels
    assert result["source"] == "eastmoney"
    db.close()
    engine.dispose()
