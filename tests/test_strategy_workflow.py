"""Workflow integration against isolated SQLite only; no model/quote network calls."""
import asyncio
import json
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.strategy import playbooks as P, workflow as W, gate as G
from src.modules.strategy.api import playbooks as API
from src.modules.strategy.backtest.single import Bar
from src.platform.persistence.database import Base
from src.platform.persistence.models import Account, Stock, Position, TradeRecord, StrategyPlaybook, StrategyRevision, StrategyDecision, BacktestRun


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def policy(**changes):
    return {"scope": "stock", "symbol": "600000", "market": "CN", "name": "纪律策略", "buy": [{"field": "runup_3d", "op": "gte", "value": -10}], "sell": [{"field": "change_pct", "op": "lte", "value": -4}], "avoid": [], **changes}


def setup_account(db, *, risk=None, stop=None):
    account = Account(name="测试账户", available_funds=100000, risk_policy=risk or {})
    stock = Stock(symbol="600000", name="测试股", market="CN")
    db.add_all([account, stock]); db.flush()
    db.add(Position(account_id=account.id, stock_id=stock.id, cost_price=9, quantity=1000, stop_loss_price=stop))
    db.commit()
    return account, stock


@pytest.fixture
def market(monkeypatch):
    bars = [Bar((date.today() - timedelta(days=130-i)).isoformat(), 10, 10.2, 9.8, 10, 1000) for i in range(130)]
    monkeypatch.setattr(P, "_current_bars", lambda *args, **kwargs: list(bars))
    monkeypatch.setattr(W, "load_quote_snapshots", lambda symbols: {f"{m}:{s}": {"price": 10, "fresh": True, "timestamp": "2026-11-13T02:00:00Z"} for s, m in symbols})
    return bars


def intent(account, **changes):
    return {"account_id": account.id, "symbol": "600000", "market": "CN", "side": "buy", "price": 10, "quantity": 100, "use_ai": False, **changes}


def test_draft_does_not_apply_and_revisions_are_immutable(db):
    draft = W.save_draft(db, policy())
    assert db.query(StrategyPlaybook).count() == 0
    active = W.activate_draft(db, draft["id"])
    first = db.get(StrategyRevision, active["revision_id"])
    assert first.payload["buy"][0]["value"] == -10
    W.save_draft(db, {**policy(buy=[]), "draft_id": draft["id"]})
    assert P.resolve("600000", db).buy_conditions[0]["value"] == -10
    second = W.activate_draft(db, draft["id"])
    assert second["revision_id"] != first.id and second["version"] == 2
    assert P.list_playbooks(db)["items"][0]["version"] == 2
    assert first.payload["buy"][0]["value"] == -10
    first.payload = policy(buy=[])
    with pytest.raises(ValueError, match="不可修改"):
        db.commit()
    db.rollback()


def test_invalid_avoid_rule_and_unconfirmed_ai_questions_cannot_activate(db):
    with pytest.raises(ValueError, match="未知字段"):
        W.save_draft(db, policy(avoid=[{"field": "magic_signal", "op": "eq", "value": 1}]))
    draft = W.save_draft(db, policy(questions=["需要明确情绪稳定的可执行标准"]))
    with pytest.raises(ValueError, match="待确认"):
        W.activate_draft(db, draft["id"])
    assert db.query(StrategyRevision).count() == 0


def test_ai_only_creates_constrained_draft(db, monkeypatch):
    import src.platform.ai.ai_failover as clients

    class Client:
        async def chat(self, *_args, **_kwargs):
            return json.dumps({"name": "草案", "buy": [{"field": "emotion", "op": "eq", "value": 1}], "sell": [], "avoid": [], "note": "冷静买入", "questions": []})

    monkeypatch.setattr(clients, "get_configured_failover_client", lambda *args: Client())
    draft = asyncio.run(W.generate_draft(db, {"text": "心情好时买入", "symbol": "600000", "scope": "stock"}))
    assert draft["questions"] and draft["buy"] == []
    assert draft["source_text"] == "心情好时买入"
    assert db.query(StrategyPlaybook).count() == 0


def test_ai_reuses_current_draft_without_creating_empty_duplicates(db, monkeypatch):
    import src.platform.ai.ai_failover as clients
    from src.platform.persistence.models import StrategyDraft
    saved = W.save_draft(db, policy())
    class Client:
        async def chat(self, *_args, **_kwargs):
            return json.dumps({**policy(name='AI 完善后的策略'), 'questions': []})
    monkeypatch.setattr(clients, 'get_configured_failover_client', lambda *args: Client())
    updated = asyncio.run(W.generate_draft(db, {'text': '完善规则', 'draft_id': saved['id'], 'expected_hash': saved['rules_hash'], 'scope': 'stock', 'symbol': '600000'}))
    assert updated['id'] == saved['id']
    assert db.query(StrategyDraft).count() == 1
    assert db.query(StrategyPlaybook).count() == 0


def test_ai_cannot_overwrite_rules_changed_during_generation(db, monkeypatch):
    import src.platform.ai.ai_failover as clients
    saved = W.save_draft(db, policy())
    class Client:
        async def chat(self, *_args, **_kwargs):
            W.save_draft(db, {**policy(buy=[]), 'draft_id': saved['id']})
            return json.dumps({**policy(), 'questions': []})
    monkeypatch.setattr(clients, 'get_configured_failover_client', lambda *args: Client())
    with pytest.raises(ValueError, match='已变化'):
        asyncio.run(W.generate_draft(db, {'text': '完善规则', 'draft_id': saved['id'], 'scope': 'stock', 'symbol': '600000'}))


def test_partial_missing_and_no_policy_are_never_pass(db, market, monkeypatch):
    account, _ = setup_account(db)
    no_policy = asyncio.run(W.create_decision(db, intent(account)))
    assert no_policy["status"] == "NO_POLICY"
    P.save_playbook(policy(buy=[{"field": "runup_3d", "op": "gte", "value": -10}, {"field": "runup_3d", "op": "gte", "value": 50}, {"field": "runup_5d", "op": "gte", "value": -10}]), db=db)
    assert asyncio.run(W.create_decision(db, intent(account)))["status"] == "BLOCK"
    P.save_playbook(policy(avoid=[{"field": "volume_ratio_5d", "op": "gte", "value": 2}]), db=db)
    monkeypatch.setattr(P, "_current_bars", lambda *args, **kwargs: [Bar(b.date, b.open, b.high, b.low, b.close, 0) for b in market])
    assert asyncio.run(W.create_decision(db, intent(account)))["status"] == "UNKNOWN"


def test_completed_daily_ignores_today_provisional_bar(db, market, monkeypatch):
    P.save_playbook(policy(), db=db)
    today = Bar(date.today().isoformat(), 10, 20, 1, 19, 99999)
    monkeypatch.setattr(P, "_current_bars", lambda *args, **kwargs: market + [today])
    check = P.check_symbol("600000", db=db)
    assert check["as_of"] == market[-1].date
    assert not check["estimated"]


def test_decision_snapshot_exception_and_trade_link_do_not_rewrite_status(db, market):
    account, stock = setup_account(db)
    P.save_playbook(policy(buy=[{"field": "runup_3d", "op": "gte", "value": 99}]), db=db)
    result = asyncio.run(W.create_decision(db, intent(account)))
    assert result["status"] == "BLOCK" and result["revision_id"]
    excepted = W.add_exception(db, result["id"], "需要按已有计划分批建仓")
    assert excepted["status"] == "BLOCK" and excepted["exception_reason"]
    record = TradeRecord(account_id=account.id, stock_id=stock.id, stock_symbol="600000", stock_name="测试股", action="buy", quantity=100, price=10.05, amount=1005, trade_date=date.today().isoformat())
    db.add(record); db.commit()
    linked = W.link_trade(db, result["id"], record.id)
    assert linked["linked_trade_id"] == record.id and linked["status"] == "BLOCK"
    record.quantity = 200; db.commit()
    with pytest.raises(ValueError, match="数量"):
        W.link_trade(db, result["id"], record.id)
    assert db.get(TradeRecord, record.id) is not None
    record.quantity = 100
    record.trade_date = (date.today() - timedelta(days=2)).isoformat()
    db.commit()
    with pytest.raises(ValueError, match="历史依据"):
        W.link_trade(db, result["id"], record.id)


def test_account_valuation_is_frozen_and_missing_quotes_remain_unknown(db, market, monkeypatch):
    account, _ = setup_account(db, risk={"max_single_weight": .25})
    P.save_playbook(policy(), db=db)
    result = asyncio.run(W.create_decision(db, intent(account)))
    assert result["status"] == "PASS"
    check = next(c for c in result["account_checks"] if c["field"] == "max_single_weight")
    assert check["status"] == "PASS" and check["actual"] < .25
    assert result["input"]["account"]["quotes"]["CN:600000"]["timestamp"]
    changed = asyncio.run(W.create_decision(db, intent(account, quantity=5000)))
    assert changed["input_hash"] != result["input_hash"] and changed["status"] == "BLOCK"
    monkeypatch.setattr(W, "load_quote_snapshots", lambda _symbols: {})
    missing = asyncio.run(W.create_decision(db, intent(account)))
    assert missing["status"] == "UNKNOWN"


def test_recent_decisions_are_scoped_and_never_turn_old_evidence_into_new_approval(db, market):
    account, _ = setup_account(db)
    P.save_playbook(policy(), db=db)
    first = asyncio.run(W.create_decision(db, intent(account)))
    second = asyncio.run(W.create_decision(db, intent(account, quantity=200)))
    rows = API.list_decisions(symbol="600000", market="CN", account_id=account.id, limit=8, db=db)["items"]
    assert [row["id"] for row in rows] == [second["id"], first["id"]]
    assert API.list_decisions(symbol="600000", market="CN", account_id=999, limit=8, db=db)["items"] == []
    assert rows[0]["input"]["quantity"] == 200
    assert rows[0]["linked_trade_id"] is None


def test_missing_account_is_unknown_even_without_technical_policy(db, market):
    result = asyncio.run(W.create_decision(db, {"symbol": "600000", "market": "CN", "side": "buy", "price": 10, "quantity": 100, "use_ai": False}))
    assert result["status"] == "UNKNOWN"


def test_fractional_or_non_lot_order_cannot_pass(db, market):
    account, _ = setup_account(db)
    P.save_playbook(policy(), db=db)
    assert asyncio.run(W.create_decision(db, intent(account, quantity=100.5)))["status"] == "BLOCK"
    assert asyncio.run(W.create_decision(db, intent(account, quantity=150)))["status"] == "BLOCK"


def test_live_position_stop_exit_is_not_vetoed_by_ai_or_missing_playbook(db, market, monkeypatch):
    account, _ = setup_account(db, stop=10.5)

    async def against(*_args, **_kwargs):
        return {"ok": True, "tone": "反对", "reason": "等等", "content": "反对"}

    monkeypatch.setattr(G, "_ai_analyze", against)
    decision = asyncio.run(W.create_decision(db, intent(account, side="sell", use_ai=True)))
    assert decision["status"] == "PASS" and decision["mandatory_exit"] is True
    assert decision["risk_exit"]["timing"] == "live_price"


def test_cache_binds_quantity_market_model_and_note():
    check = {"name": "规则", "as_of": "2026-11-12", "note": "纪律"}
    key = G._cache_key("600000", "buy", 10.01, check, quantity=100, market="CN", model_id=1, account_context={"cash": 10000})
    assert key != G._cache_key("600000", "buy", 10.01, check, quantity=200, market="CN", model_id=1, account_context={"cash": 10000})
    assert key != G._cache_key("600000", "buy", 10.02, check, quantity=100, market="CN", model_id=1, account_context={"cash": 10000})
    assert key != G._cache_key("600000", "buy", 10.01, {**check, "note": "不同备注"}, quantity=100, market="CN", model_id=1, account_context={"cash": 10000})
    _, prompt = G._ai_prompt({**check, "account_context": {"available_funds": 12345}}, "buy", 10, 100)
    assert "12345" in prompt and "背景解释" in prompt


def test_ai_explanation_uses_one_saved_snapshot_and_can_only_downgrade(db, market, monkeypatch):
    account, _ = setup_account(db)
    P.save_playbook(policy(), db=db)
    decision = asyncio.run(W.create_decision(db, intent(account)))
    assert decision["status"] == "PASS"
    captured = {}

    async def ai(check, *_args, **_kwargs):
        captured.update(check)
        return {"ok": True, "tone": "反对", "reason": "等待确认", "content": "反对"}

    def unexpected_fetch(_symbols):
        raise AssertionError("explain must not load a second market snapshot")

    monkeypatch.setattr(G, "_ai_analyze", ai)
    monkeypatch.setattr(W, "load_quote_snapshots", unexpected_fetch)
    result = asyncio.run(W.explain_decision(db, decision["id"]))
    assert result["id"] == decision["id"] and result["status"] == "BLOCK"
    assert result["input_hash"] == decision["input_hash"]
    assert captured["account_context"] == decision["input"]["account"]
    assert result["ai_bound_to_input_hash"] == decision["input_hash"]
    assert db.query(StrategyDecision).count() == 1

    async def supportive(*_args, **_kwargs):
        return {"ok": True, "tone": "支撑", "reason": "", "content": "支撑"}

    monkeypatch.setattr(G, "_ai_analyze", supportive)
    assert asyncio.run(W.explain_decision(db, decision["id"], model_id=2))["status"] == "BLOCK"


def test_ai_explanation_cannot_veto_a_saved_live_stop_exit(db, market, monkeypatch):
    account, _ = setup_account(db, stop=10.5)
    decision = asyncio.run(W.create_decision(db, intent(account, side="sell")))

    async def against(*_args, **_kwargs):
        return {"ok": True, "tone": "反对", "reason": "等待", "content": "反对"}

    monkeypatch.setattr(G, "_ai_analyze", against)
    result = asyncio.run(W.explain_decision(db, decision["id"]))
    assert result["status"] == "PASS" and result["mandatory_exit"]


def test_changed_policy_invalidates_ai_explanation_and_exception(db, market):
    account, _ = setup_account(db)
    P.save_playbook(policy(), db=db)
    decision = asyncio.run(W.create_decision(db, intent(account)))
    P.save_playbook(policy(buy=[]), db=db)
    with pytest.raises(ValueError, match="策略已变化"):
        asyncio.run(W.explain_decision(db, decision["id"]))
    with pytest.raises(ValueError, match="策略已变化"):
        W.add_exception(db, decision["id"], "希望继续按原计划操作")


def test_backtest_stores_exact_draft_and_cost_snapshot_without_activating(db, monkeypatch):
    draft = W.save_draft(db, policy(buy=[]))
    captured = {}

    def run(symbol, **kwargs):
        captured.update(kwargs)
        return {"symbol": symbol, "start": "2026-01-01", "end": "2026-03-01", "trades": [], "data_info": {"data_hash": "bars-hash"}}

    monkeypatch.setattr(API, "run_single_backtest", run)
    result = API.backtest(API.BacktestIn(symbol="600000", draft_id=draft["id"], start_date="2026-01-01", end_date="2026-03-01", initial_capital=50000, slippage_bps=8), db=db)
    stored = db.get(BacktestRun, result["backtest_id"])
    assert captured["buy"] == [] and captured["initial_capital"] == 50000
    assert captured["cost_model"].cfg.slippage_bps == 8
    assert stored.input_snapshot["rules"]["buy"] == []
    assert stored.input_snapshot["data_info"]["data_hash"] == "bars-hash"
    assert db.query(StrategyPlaybook).count() == 0


def test_incremental_workflow_migration_keeps_existing_policy():
    from src.platform.persistence.migrations import _m136_strategy_workflow

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE strategy_playbooks (id INTEGER PRIMARY KEY, name TEXT)"))
        connection.execute(text("INSERT INTO strategy_playbooks VALUES (1, 'existing')"))
        _m136_strategy_workflow(connection)
        _m136_strategy_workflow(connection)
        assert connection.execute(text("SELECT name,market,timing FROM strategy_playbooks")).one() == ("existing", "CN", "completed_daily")
        assert connection.execute(text("SELECT COUNT(*) FROM strategy_revisions")).scalar() == 0
    engine.dispose()
