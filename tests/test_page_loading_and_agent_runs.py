"""页面首屏不得等待外部行情/新闻；检测与推送需分别留痕。"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.platform.persistence.database import Base


@pytest.fixture
def local_book(monkeypatch):
    from src.modules.screener import snapshot, concepts
    from src.platform.dataset import store
    from src.platform.persistence.models import DailyBar, ScreenerSnapshot
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)
    for module in (snapshot, concepts, store):
        monkeypatch.setattr(module, 'SessionLocal', session)
    monkeypatch.setattr(snapshot, '_MEM', {'trade_date': '', 'rows': None, 'ts': 0.0})
    monkeypatch.setattr(snapshot, 'latest_trade_date', lambda: datetime(2026, 11, 19).date())
    monkeypatch.setattr(snapshot, 'request_refresh', Mock(return_value=True))
    monkeypatch.setattr(concepts, 'refresh_index_background', Mock())
    db = session()
    db.add_all([
        DailyBar(symbol='600900', trade_date='2026-11-17', open=9, high=10, low=9, close=10, volume=100),
        DailyBar(symbol='600900', trade_date='2026-11-18', open=10, high=11, low=10, close=11, volume=120),
        ScreenerSnapshot(trade_date='2026-11-18', symbol='600900', name='长江电力', price=11, prev_close=10,
                         open_price=10, high_price=11, low_price=10, volume=12000, change_pct=10),
    ])
    db.commit()
    yield db, session
    db.close()
    engine.dispose()


def test_screener_returns_previous_local_snapshot_and_queues_refresh(local_book, monkeypatch):
    from src.modules.screener import snapshot
    monkeypatch.setattr(snapshot, 'fetch_full_market', Mock(side_effect=AssertionError('首屏不应联网')))
    rows = snapshot.get_snapshot()
    assert rows[0]['price'] == 11
    assert snapshot._MEM['trade_date'] == '2026-11-18'
    snapshot.request_refresh.assert_called_once()
    snapshot.fetch_full_market.assert_not_called()


def test_screener_can_start_from_local_daily_bars(local_book):
    from src.modules.screener import snapshot
    from src.platform.persistence.models import ScreenerSnapshot
    db, _session = local_book
    db.query(ScreenerSnapshot).delete()
    db.commit()
    rows = snapshot.get_snapshot()
    assert rows[0]['price'] == 11 and rows[0]['change_pct'] == 10
    assert rows[0]['volume'] == 12000  # 本地手数→行情股数
    assert snapshot._MEM['trade_date'] == '2026-11-18'


def test_candles_use_local_bars_and_replace_same_day_from_cached_quote(local_book):
    from src.modules.screener import snapshot
    from src.platform.dataset import api
    snapshot.get_snapshot()
    snapshot._MEM['rows'][0].update(price=11.5, high_price=11.6, volume=16000)
    result = api.get_spark_ohlc(symbols='600900', days=20, fallback=False, include_live=True)
    bars = result['data']['600900']
    assert len(bars) == 2
    assert bars[-1]['d'] == '2026-11-18' and bars[-1]['c'] == 11.5
    assert bars[-1]['v'] == 160


def test_local_screen_does_not_call_remote_ladder_or_concept_builder(local_book, monkeypatch):
    from src.modules.screener import snapshot, engine, concepts
    monkeypatch.setattr(engine, '_technical_map', lambda _day: {})
    monkeypatch.setattr(engine, '_local_ladder', lambda _day: [])
    recent = Mock(return_value={})
    monkeypatch.setattr(engine, 'get_recent_limit_up', recent)
    monkeypatch.setattr(engine, '_ladder_by_symbol', Mock(side_effect=AssertionError('不应同步抓涨停池')))
    monkeypatch.setattr(concepts, 'ensure_concept_index', Mock(side_effect=AssertionError('不应同步建索引')))
    result = engine.run_screen({'conditions': [], 'save': False})
    assert result['trade_date'] == '2026-11-18' and result['items'][0]['symbol'] == '600900'
    recent.assert_called_once_with('2026-11-18', 5, local_only=True)
    assert any('2026-11-18' in warning for warning in result['warnings'])


def test_messages_return_cached_items_while_external_sources_wait(monkeypatch):
    from src.modules.market import message_service as messages
    async def check():
        release = asyncio.Event()
        async def blocked(*args, **kwargs):
            await release.wait()
            return []
        row = dict(id='local:1', source='local', title='公告', content='', datetime=datetime.now(timezone.utc),
                   url='', importance=1, symbols=['600900'])
        monkeypatch.setattr(messages, '_RAW_CACHE', dict(rows=[row], ts=0, updated_at='', error='', task=None))
        monkeypatch.setattr(messages, '_load_tracked_symbols', lambda: ({'600900'}, {'600900'}))
        monkeypatch.setattr(messages, '_collect_flash', blocked)
        monkeypatch.setattr(messages, '_collect_stock_news', blocked)
        result = await asyncio.wait_for(messages.collect_messages(), timeout=0.5)
        assert result['items'][0]['id'] == 'local:1' and result['refreshing'] is True
        filtered = await messages.collect_messages(position_only=True)
        assert filtered['total'] == 1
        task = messages._RAW_CACHE['task']
        release.set()
        await task
        assert messages._RAW_CACHE['rows'][0]['id'] == 'local:1'
    asyncio.run(check())


@pytest.mark.parametrize('notified,reason,expected', [(False, 'no_signal', '未触发提醒信号'),
    (False, 'throttled', '处于通知间隔内'), (True, '', '已推送')])
def test_single_agent_records_each_check_and_notification_reason(monkeypatch, notified, reason, expected):
    from src.modules.automation import agent_scheduler as module
    scheduler = module.AgentScheduler()
    stock = SimpleNamespace(name='普利特', symbol='002324', market='test')
    context = SimpleNamespace(watchlist=[stock], model_label='test')
    result = SimpleNamespace(content='规则检查结果', raw_data={'notified': notified, 'notify_skipped': reason})
    agent = SimpleNamespace(name='intraday_monitor', display_name='盘中监测', run_single=AsyncMock(return_value=result))
    scheduler.agents[agent.name] = agent
    scheduler.execution_modes[agent.name] = 'single'
    scheduler.set_context_builder(lambda _name: context)
    record = Mock()
    monkeypatch.setattr(module, 'record_agent_run', record)
    asyncio.run(scheduler._run_agent(agent.name))
    data = record.call_args.kwargs
    assert data['status'] == 'success' and data['notify_sent'] is notified
    assert expected in data['result'] and '002324' in data['result']


def test_saving_schedule_applies_only_that_running_job(monkeypatch):
    import server
    from src.platform.persistence.models import AgentConfig
    cfg = SimpleNamespace(name='intraday_monitor', enabled=True, kind='workflow', schedule='*/10 * * * *',
                          config={}, execution_mode='single')
    db = Mock()
    db.query.return_value.filter.return_value.first.return_value = cfg
    monkeypatch.setattr(server, 'SessionLocal', lambda: db)
    scheduler = Mock()
    monkeypatch.setattr(server, 'scheduler', scheduler)
    assert server.refresh_agent_schedule(cfg.name)
    assert scheduler.register.call_args.args[1:] == (cfg.schedule, 'single')
    scheduler.shutdown.assert_not_called()


def test_saving_schedule_api_updates_runtime_after_commit(local_book, monkeypatch):
    import server
    from src.modules.automation.api import agents
    from src.platform.persistence.models import AgentConfig
    db, _session = local_book
    cfg = AgentConfig(name='intraday_monitor', display_name='盘中监测', kind='workflow', schedule='*/30 * * * *')
    db.add(cfg)
    db.commit()
    apply = Mock(return_value=True)
    monkeypatch.setattr(server, 'refresh_agent_schedule', apply)
    result = asyncio.run(agents.update_agent(cfg.name, agents.AgentConfigUpdate(schedule='*/10 * * * *'), db=db))
    assert result['schedule'] == '*/10 * * * *'
    apply.assert_called_once_with(cfg.name)


def test_fast_news_source_becomes_visible_before_slow_source_finishes(monkeypatch):
    from src.modules.market import message_service as messages
    async def check():
        release = asyncio.Event()
        async def slow(*args, **kwargs):
            await release.wait()
            return []
        row = dict(id='fast:1', source='fast', title='快讯', content='', datetime=datetime.now(timezone.utc),
                   url='', importance=1, symbols=[])
        monkeypatch.setattr(messages, '_RAW_CACHE', dict(rows=[], ts=0, updated_at='', error='', task=None))
        monkeypatch.setattr(messages, '_collect_flash', AsyncMock(return_value=[row]))
        monkeypatch.setattr(messages, '_collect_stock_news', slow)
        monkeypatch.setattr(messages, '_save_cached_news', Mock())
        task = asyncio.create_task(messages._refresh_cached_news([]))
        for _ in range(30):
            await asyncio.sleep(0.005)
            if messages._RAW_CACHE['rows']:
                break
        assert messages._RAW_CACHE['rows'][0]['id'] == 'fast:1' and not task.done()
        release.set()
        await task
    asyncio.run(check())
