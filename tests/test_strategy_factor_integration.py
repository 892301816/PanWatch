from datetime import date,timedelta
from threading import Event
import hashlib,json
import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from src.platform.persistence.database import Base
from src.modules.strategy.factor_engine import CATALOG,compute_frame,field_rows
from src.modules.strategy.template_runtime import TEMPLATES,presets,execute
from src.modules.strategy.rule_engine import evaluate,validate_condition
from src.modules.strategy.patterns import VALID_FIELDS
from src.modules.strategy.backtest.single import Bar,run_single_backtest
from src.modules.strategy.backtest.portfolio import run_portfolio,Cancelled
from src.modules.strategy import workflow as W
from src.modules.strategy.api.playbooks import DraftIn
from src.modules.strategy.data_capabilities import import_references,attach_references

def bars(n=280):
    out=[]
    for i in range(n):
        c=10+i*.005+np.sin(i/7)*.4
        b=Bar(str(date(2025,1,1)+timedelta(days=i)),float(c-.05),float(c+.1),float(c-.12),float(c),1000+i*3+(i%9)*100,adjustment='qfq',amount=float(c)*100*(1000+i*3+(i%9)*100),raw_close=float(c),turnover_rate=2+i%7)
        out.append(b)
    return out

def policy(**change):return {'scope':'global','symbol':'','market':'CN','name':'测试','buy':[{'field':'change_pct','op':'gte','value':-99}],'sell':[],'avoid':[],**change}

@pytest.fixture
def db():
    engine=create_engine('sqlite://');Base.metadata.create_all(engine)
    with Session(engine) as session:yield session
    engine.dispose()

def test_all_factor_catalog_computations_and_missing_dependencies():
    assert len(CATALOG['factors'])==77 and len(TEMPLATES)==26
    frame=compute_frame(bars());last=frame.iloc[-1]
    for f in CATALOG['factors']:
        if f['dependency'] in ('daily','amount','turnover'):assert np.isfinite(last[f['id']]),f['id']
        else:assert np.isnan(last[f['id']]),f['id']
    assert field_rows(bars(50))[-1]['tf_position_240d'] is None

def test_factor_causality_and_raw_vwap():
    history=bars();before=compute_frame(history[:210]).iloc[-1]
    history[-1].close=10000
    after=compute_frame(history).iloc[209]
    np.testing.assert_allclose(before.to_numpy(),after.to_numpy(),equal_nan=True)
    sample=bars(30)
    for b in sample:b.raw_close=10;b.close=5;b.amount=b.volume*100*10
    assert compute_frame(sample).iloc[-1]['vwap_bias']==pytest.approx(0)

def test_groups_offsets_and_three_valued_logic():
    child={'field':'change_pct','op':'gte','value':2,'offset':1,'consecutive':2}
    c=validate_condition({'children':[child,{'field':'tf_rsi_14','op':'gte','value':50}],'logic':'any'},VALID_FIELDS)
    assert evaluate([{'change_pct':3},{'change_pct':4},{}],2,c) is True
    assert evaluate([{}],0,c) is None
    with pytest.raises(ValueError):validate_condition({**child,'offset':-1},VALID_FIELDS)

def test_policy_configuration_roundtrip_and_legacy_hash(db):
    legacy=W.validate_rules(policy())
    assert W.rules_hash(legacy)==W.digest({k:legacy.get(k) for k in ('schema_version','market','timing','buy','sell','avoid')})
    source=next(p for p in presets() if p['id']=='ma_golden_cross')
    payload=DraftIn(**policy(**{k:source[k] for k in ('buy','sell','avoid','config')})).model_dump()
    saved=W.save_draft(db,payload);active=W.activate_draft(db,saved['id'])
    assert active['config']['execution']['max_hold_days']==15
    changed={**saved['config'],'execution':{'max_hold_days':20}}
    assert W.rules_hash(W.validate_rules({**saved,'config':changed}))!=saved['rules_hash']

@pytest.mark.parametrize('template_id',list(TEMPLATES))
def test_all_imported_strategy_definitions_execute(template_id):
    cfg=next(s['config'] for s in presets() if s['id']==template_id)
    market,signals,score,known=execute({'600000':bars(),'600001':bars()},cfg)
    assert signals.entry.shape==(280,2) and known.shape==signals.exit.shape
    assert not known[:59].any()
    if template_id in ('active_limit_gene','consecutive_limit_ups','limit_up_momentum','near_limit_up','broken_board_recovery'):assert not known.any()

def test_historical_announcement_and_market_date_alignment(db):
    history=bars(5);d=history[3].date
    import_references(db,[{'symbol':'600000','field':'roe_latest','effective_date':history[1].date,'known_at':d,'value':12,'source':'公告1'},{'symbol':'600000','field':'turnover_rate','effective_date':d,'known_at':d,'value':6,'source':'交易所当日数据'}])
    for b in history:b.turnover_rate=None
    attach_references(db,'600000',history)
    assert not hasattr(history[2],'roe_latest')
    assert history[3].roe_latest==12 and history[4].roe_latest==12
    assert history[3].turnover_rate==6 and history[4].turnover_rate is None

def test_shared_cash_positions_fees_and_cancellation():
    history=bars(90);universe={s:list(history) for s in ('600000','600001','600002')}
    result=run_portfolio(universe,policy(),start=history[60].date,end=history[-1].date,max_positions=2,max_weight=.4)
    fills=[o for o in result['orders'] if o['status']=='filled']
    assert len(fills)==2 and all(o['side']=='buy' for o in fills)
    assert min(p['cash'] for p in result['curve'])>=0
    assert max(p['positions'] for p in result['curve'])<=2
    assert all(o['signal_date']<o['execution_date'] for o in fills)
    cancelled=Event();cancelled.set()
    with pytest.raises(Cancelled):run_portfolio(universe,policy(),start=history[60].date,end=history[-1].date,cancel=cancelled)

def test_risk_policy_closes_on_next_open_not_signal_close():
    history=bars(90)
    for i in range(65,90):history[i].close=9;history[i].open=9.05;history[i].high=9.1;history[i].low=8.9
    result=run_portfolio({'600000':history},policy(config={'execution':{'stop_loss_pct':-3}}),start=history[60].date,end=history[-1].date,max_positions=1,max_weight=1)
    sells=[o for o in result['orders'] if o['status']=='filled' and o['side']=='sell']
    assert sells and sells[0]['signal_date']<sells[0]['execution_date']
    assert any(c['ok'] for c in sells[0]['execution_checks'])
