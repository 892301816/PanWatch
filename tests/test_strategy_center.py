from datetime import date, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.modules.strategy import center, playbooks as P, workflow as W
from src.modules.strategy.api import playbooks as api
from src.modules.strategy.backtest.single import Bar
from src.platform.persistence.database import Base
from src.platform.persistence.models import StrategyRevision


@pytest.fixture
def db():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def draft(db, **changes):
    return W.save_draft(db, {'scope': 'global', 'symbol': '', 'market': 'CN', 'name': '我的回踩规则', 'buy': [{'field': 'change_pct', 'op': 'gte', 'value': -1}], 'sell': [], 'avoid': [], **changes})


def apply(db, saved, symbol):
    return center.apply_library(db, saved['id'], {'scope': 'stock', 'symbol': symbol, 'expected_hash': saved['rules_hash']})


@pytest.fixture
def bars(monkeypatch):
    rows = [Bar((date.today() - timedelta(days=90-i)).isoformat(), 10, 10.1, 9.9, 10, 1000) for i in range(90)]
    monkeypatch.setattr(P, '_current_bars', lambda *a, **kw: list(rows))
    return rows


def test_library_reuses_one_draft_for_multiple_immutable_bindings(db):
    saved = draft(db)
    assert center.library(db)['bindings'] == []
    a, b = apply(db, saved, '600000'), apply(db, saved, '600001')
    assert a['revision_id'] != b['revision_id']
    listed = center.library(db)['items'][0]
    assert listed['scope'] == 'global' and listed['symbol'] == ''
    assert {binding['symbol'] for binding in listed['bindings']} == {'600000', '600001'}
    changed = draft(db, draft_id=saved['id'], buy=[])
    assert P.resolve('600000', db).buy_conditions
    assert listed['rules_hash'] != changed['rules_hash']
    assert db.get(StrategyRevision, a['revision_id']).payload['buy']


def test_apply_rejects_changed_hash_questions_and_incompatible_stock_scope(db):
    saved = draft(db)
    draft(db, draft_id=saved['id'], buy=[])
    with pytest.raises(ValueError, match='已变化'):
        apply(db, saved, '600000')
    pending = draft(db, questions=['消息面尚无法执行'])
    with pytest.raises(ValueError, match='待确认'):
        apply(db, pending, '600000')
    own = draft(db, scope='stock', symbol='600000')
    with pytest.raises(ValueError, match='通用策略'):
        apply(db, own, '600001')


def test_discovery_uses_selected_rules_and_does_not_replace_actual_policy(db, bars):
    P.save_playbook({'scope': 'global', 'buy': [{'field': 'change_pct', 'op': 'gte', 'value': 99}]}, db)
    saved = draft(db, sell=[{'field': 'change_pct', 'op': 'gte', 'value': -1}])
    result = center.scan(db, saved['id'], [{'symbol': '600000', 'market': 'CN', 'name': '示例'}], saved['rules_hash'])
    assert result['items'][0]['status'] == 'MATCH'
    assert result['items'][0]['sell_hits'] == 1
    assert result['items'][0]['rules_hash'] == saved['rules_hash']
    assert P.resolve('600000', db).buy_conditions[0]['value'] == 99
    blocked = draft(db, avoid=[{'field': 'change_pct', 'op': 'gte', 'value': -1}])
    assert center.scan(db, blocked['id'], [{'symbol': '600000', 'market': 'CN'}], blocked['rules_hash'])['items'][0]['status'] == 'BLOCK'


def test_missing_stale_or_incompatible_data_never_matches(db, bars, monkeypatch):
    saved = draft(db)
    assert center.scan(db, saved['id'], [{'symbol': 'AAPL', 'market': 'US'}], saved['rules_hash'])['items'][0]['status'] == 'UNKNOWN'
    monkeypatch.setattr(P, '_current_bars', lambda *a, **kw: bars[:30])
    assert center.scan(db, saved['id'], [{'symbol': '600000', 'market': 'CN'}], saved['rules_hash'])['items'][0]['status'] == 'UNKNOWN'
    monkeypatch.setattr(P, '_current_bars', lambda *a, **kw: [])
    assert center.scan(db, saved['id'], [{'symbol': '600000', 'market': 'CN'}], saved['rules_hash'])['items'][0]['status'] == 'UNKNOWN'


def test_scan_rejects_rule_edits_during_market_loading(db, bars, monkeypatch):
    saved = draft(db)
    def changed(*args, **kwargs):
        draft(db, draft_id=saved['id'], buy=[])
        return bars
    monkeypatch.setattr(P, '_current_bars', changed)
    with pytest.raises(ValueError, match='扫描期间'):
        center.scan(db, saved['id'], [{'symbol': '600000', 'market': 'CN'}], saved['rules_hash'])


def test_disabling_binding_preserves_library_lineage_and_applied_rules(db):
    saved = draft(db)
    applied = apply(db, saved, '600000')
    draft(db, draft_id=saved['id'], buy=[])
    api.set_binding_enabled(applied['id'], api.BindingEnabledIn(enabled=False), db)
    row = center.library(db)['items'][0]['bindings'][0]
    assert row['enabled'] is False
    assert row['buy']
    assert db.get(StrategyRevision, row['revision_id']).source_draft_id == saved['id']


def test_known_avoid_trigger_blocks_even_if_a_buy_feature_is_missing(db, bars, monkeypatch):
    saved = draft(db, buy=[{'field': 'ma5_dev', 'op': 'gte', 'value': 0}], avoid=[{'field': 'change_pct', 'op': 'gte', 'value': -1}])
    original = P.compute_features
    def missing(*args):
        return {**original(*args), 'ma5_dev': None}
    monkeypatch.setattr(P, 'compute_features', missing)
    result = center.scan(db, saved['id'], [{'symbol': '600000', 'market': 'CN'}], saved['rules_hash'])
    assert result['items'][0]['status'] == 'BLOCK'


def test_api_bounds_scan_size():
    with pytest.raises(ValidationError):
        api.LibraryScanIn(expected_hash='hash', symbols=[{'symbol': '600000'}] * 31)


def test_delete_retains_evidence_and_restore_never_reenables_policy(db):
    from src.platform.persistence.models import StrategyDraft, BacktestRun
    saved = draft(db)
    applied = apply(db, saved, '600000')
    report = BacktestRun(strategy_code='playbook', market='CN', status='succeeded', start_date='2026-01-01', end_date='2026-11-01', input_snapshot={'draft_id': saved['id'], 'rules_hash': saved['rules_hash']}, result={'total_return': 1})
    db.add(report); db.commit()
    with pytest.raises(ValueError, match='正在使用'):
        center.delete_library(db, saved['id'], saved['rules_hash'])
    assert db.get(StrategyDraft, saved['id']).archived_at is None
    center.delete_library(db, saved['id'], saved['rules_hash'], True, [applied['id']])
    assert center.library(db)['items'] == []
    assert len(center.library(db, include_deleted=True)['deleted_items']) == 1
    assert db.get(StrategyRevision, applied['revision_id']).payload['buy']
    assert db.get(BacktestRun, report.id).input_snapshot['draft_id'] == saved['id']
    assert P.resolve('600000', db) is None
    with pytest.raises(ValueError, match='已删除'):
        draft(db, draft_id=saved['id'])
    with pytest.raises(ValueError, match='不存在'):
        apply(db, saved, '600000')
    center.restore_library(db, saved['id'])
    assert len(center.library(db)['items']) == 1
    assert not center.library(db)['items'][0]['bindings'][0]['enabled']
    assert P.resolve('600000', db) is None


def test_delete_rejects_changed_rule_or_unreviewed_bindings(db):
    saved = draft(db)
    a = apply(db, saved, '600000')
    with pytest.raises(ValueError, match='范围已变化'):
        center.delete_library(db, saved['id'], saved['rules_hash'], True, [])
    apply(db, saved, '600001')
    with pytest.raises(ValueError, match='范围已变化'):
        center.delete_library(db, saved['id'], saved['rules_hash'], True, [a['id']])
    assert P.resolve('600000', db) and P.resolve('600001', db)
    draft(db, draft_id=saved['id'], buy=[])
    with pytest.raises(ValueError, match='已变化'):
        center.delete_library(db, saved['id'], saved['rules_hash'], True, [a['id']])


def test_deleting_stock_override_inherits_unrelated_default(db):
    default = draft(db, name='默认')
    center.apply_library(db, default['id'], {'scope': 'global', 'symbol': '', 'expected_hash': default['rules_hash']})
    own = draft(db, name='个股')
    binding = apply(db, own, '600000')
    center.delete_library(db, own['id'], own['rules_hash'], True, [binding['id']])
    assert P.resolve('600000', db).name == '默认'
    center.restore_library(db, own['id'])
    assert P.resolve('600000', db).name == '默认'


def test_trash_migration_upgrades_existing_table_idempotently():
    from sqlalchemy import text, inspect
    from src.platform.persistence.migrations import _m138_strategy_draft_trash
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE strategy_drafts(id INTEGER PRIMARY KEY, payload JSON)'))
        conn.execute(text("INSERT INTO strategy_drafts(id,payload) VALUES(1,'{}')"))
        _m138_strategy_draft_trash(conn); _m138_strategy_draft_trash(conn)
        assert 'archived_at' in {c['name'] for c in inspect(conn).get_columns('strategy_drafts')}
        assert conn.execute(text('SELECT archived_at FROM strategy_drafts WHERE id=1')).scalar() is None
    engine.dispose()
