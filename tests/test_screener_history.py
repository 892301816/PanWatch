"""选股历史:保存/去重/清理/后续表现。"""
from datetime import datetime, timedelta

import src.modules.screener.history as history
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import ScreenerRun


def _payload(conditions=None, offset=0, save=True):
    return {
        "conditions": conditions or [{"field": "change_pct", "op": "gte", "value": 12345}],
        "logic": "all",
        "exclude": ["st"],
        "order_by": "change_pct desc",
        "offset": offset,
        "save": save,
    }


def _result(symbols=("600519", "000001")):
    return {
        "universe": 5000,
        "count": len(symbols),
        "total": len(symbols),
        "warnings": [],
        "_history": {
            "symbols": list(symbols),
            "preview": [
                {
                    "rank": i + 1,
                    "symbol": s,
                    "name": s,
                    "price": 10.0,
                    "change_pct": 1.0,
                    "turnover_rate": 2.0,
                    "amount": 3.0,
                    "float_mv": 4.0,
                    "limit_up_days": 0,
                    "concept_rank": None,
                    "concepts": [],
                }
                for i, s in enumerate(symbols)
            ],
        },
    }


def test_conditions_hash_stable_and_sensitive():
    a = history.conditions_hash([{"field": "x", "op": "eq", "value": 1}], "all", ["st"], "a desc")
    b = history.conditions_hash([{"field": "x", "op": "eq", "value": 1}], "all", ["st"], "a desc")
    c = history.conditions_hash([{"field": "x", "op": "eq", "value": 2}], "all", ["st"], "a desc")
    assert a == b and a != c


def test_save_dedupes_same_conditions_and_skips_paging():
    p = _payload()
    run_id = history.save_run(trade_date="2099-01-05", payload=p, result=_result())
    try:
        assert run_id
        again = history.save_run(trade_date="2099-01-05", payload=p, result=_result())
        assert again == run_id  # 10 分钟内同条件合并
        assert history.save_run(trade_date="2099-01-05", payload=_payload(offset=80), result=_result()) is None
        assert history.save_run(trade_date="2099-01-05", payload=_payload(save=False), result=_result()) is None

        detail = history.get_run(run_id)
        assert detail and detail["matched_count"] == 2
        assert len(detail["items"]) == 2
    finally:
        history.delete_run(run_id)


def test_cleanup_removes_old_items_and_runs():
    run_id = history.save_run(trade_date="2099-01-06", payload=_payload(), result=_result())
    assert run_id
    db = SessionLocal()
    try:
        row = db.query(ScreenerRun).filter(ScreenerRun.id == run_id).first()
        row.created_at = datetime.now() - timedelta(days=400)
        db.commit()
    finally:
        db.close()

    history.cleanup(items_days=90, runs_days=365)
    assert history.get_run(run_id) is None


def test_performance_uses_history_bars(monkeypatch):
    # 用合成行情验证统计口径,不依赖本机数据库里有没有历史日K
    def _fake(symbols, since, until):
        return {
            "600519": [("2026-10-23", 10.0), ("2026-10-26", 11.0), ("2026-10-27", 9.0)],
            "000001": [("2026-10-23", 20.0), ("2026-10-26", 22.0), ("2026-10-27", 19.0)],
        }

    monkeypatch.setattr(history, "_load_closes", _fake)
    p = _payload([{"field": "change_pct", "op": "gte", "value": 54321}])
    run_id = history.save_run(trade_date="2026-10-23", payload=p, result=_result())
    try:
        assert run_id
        data = history.performance(run_id)
        h1 = data["horizons"]["1"]
        assert h1["count"] == 2
        assert h1["avg"] == 10.0          # (+10% +10%) / 2
        assert h1["win_rate"] == 100.0
        assert data["horizons"]["5"]["count"] == 0
    finally:
        history.delete_run(run_id)
