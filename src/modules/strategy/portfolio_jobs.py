"""User-started portfolio research jobs. No broker calls, no automatic activation."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from datetime import date
import hashlib
import json
import time
from sqlalchemy import text
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import BacktestRun,StrategyDraft,StrategyRevision,Stock
from src.modules.strategy import workflow
from src.modules.strategy.backtest.single import load_bars
from src.modules.strategy.backtest.portfolio import run_portfolio,Cancelled

_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='strategy-portfolio')
_lock=Lock();_jobs={}

def start(db,payload):
    draft=db.get(StrategyDraft,payload['draft_id'])
    if not draft or draft.archived_at or draft.questions:raise ValueError('草稿不存在或还有待确认问题')
    policy=workflow.validate_rules(draft.payload)
    if policy['market']!='CN' or policy['timing']!='completed_daily':raise ValueError('组合研究当前支持 A 股完整日线策略')
    start_date=date.fromisoformat(payload['start_date']);end_date=date.fromisoformat(payload['end_date'])
    if start_date>end_date or (end_date-start_date).days>3650:raise ValueError('回测日期范围无效或超过10年')
    if payload['universe']=='watchlist':symbols=[s.symbol for s in db.query(Stock).filter(Stock.market=='CN')]
    elif payload['universe']=='all':symbols=[r[0] for r in db.execute(text('SELECT DISTINCT symbol FROM daily_bars_qfq'))]
    else:symbols=payload.get('symbols',[])
    ordinary=('000','001','002','003','300','301','600','601','603','605','688','689')
    symbols=sorted({s for s in symbols if len(s)==6 and s.isdigit() and s.startswith(ordinary)})
    if policy['scope']=='stock':symbols=[s for s in symbols if s==policy['symbol']]
    if not symbols:raise ValueError('所选范围没有可研究的沪深普通 A 股；ETF、北交所等执行规则尚未适配')
    with _lock:
        if len(_jobs)>=2:raise ValueError('已有组合任务运行或排队，请等待或取消')
        row=BacktestRun(status='running',strategy_code='strategy_portfolio',market='CN',start_date=str(start_date),end_date=str(end_date),config={**payload,'symbols':symbols,'progress':{'stage':'排队','done':0,'total':len(symbols)}},input_snapshot={'draft_id':draft.id,'rules':policy,'rules_hash':workflow.rules_hash(policy),'universe':symbols},started_at=workflow.now())
        db.add(row);db.commit();db.refresh(row)
        event=Event();_jobs[row.id]=event
        _pool.submit(_run,row.id,event)
    return {'id':row.id,'status':row.status}

def _run(run_id,event):
    db=SessionLocal();last_update=0
    def progress(stage,done,total):
        nonlocal last_update
        if time.monotonic()-last_update<1 and done!=total:return
        last_update=time.monotonic();row=db.get(BacktestRun,run_id)
        row.config={**row.config,'progress':{'stage':stage,'done':done,'total':total}};db.commit()
    try:
        row=db.get(BacktestRun,run_id);payload=dict(row.config);policy=row.input_snapshot['rules'];db.commit()
        histories={};fingerprint=hashlib.sha256();skipped=[]
        # Load local only. A portfolio run must not issue one network fallback per ticker.
        needed=min(5000,(date.today()-date.fromisoformat(payload['start_date'])).days+520)
        for i,symbol in enumerate(payload['symbols']):
            if event.is_set():raise Cancelled('用户取消')
            bars=[b for b in load_bars(symbol,days=needed,allow_network=False) if b.date<=payload['end_date']]
            if len(bars)>=60:histories[symbol]=bars
            else:skipped.append(symbol)
            for b in bars:fingerprint.update(json.dumps([symbol,b.__dict__],sort_keys=True,default=str).encode())
            progress('读取本地历史',i+1,len(payload['symbols']))
        result=run_portfolio(histories,policy,start=payload['start_date'],end=payload['end_date'],initial_capital=payload['initial_capital'],max_positions=payload['max_positions'],max_weight=payload['max_weight'],cancel=event,progress=progress)
        result.update(data_hash=fingerprint.hexdigest(),skipped_symbols=skipped,rules_hash=workflow.rules_hash(policy),backtest_id=run_id)
        row=db.get(BacktestRun,run_id);row.result=result;row.status='succeeded';row.finished_at=workflow.now();db.commit()
    except Exception as exc:
        db.rollback();row=db.get(BacktestRun,run_id)
        if row:row.status='cancelled' if isinstance(exc,Cancelled) else 'failed';row.error_message=str(exc)[:1000];row.finished_at=workflow.now();db.commit()
    finally:
        db.close()
        with _lock:_jobs.pop(run_id,None)

def cancel(run_id):
    with _lock:
        event=_jobs.get(run_id)
        if event:event.set()
    return {'requested':bool(event)}

def read(db,run_id):
    row=db.get(BacktestRun,run_id)
    if not row or row.strategy_code!='strategy_portfolio':raise ValueError('组合报告不存在')
    if row.status=='running' and run_id not in _jobs:
        row.status='interrupted';row.error_message='服务重启，任务已中断；请重新运行';db.commit()
    return {'id':row.id,'status':row.status,'progress':row.config.get('progress'),'error':row.error_message,'result':row.result,'input_snapshot':row.input_snapshot}
