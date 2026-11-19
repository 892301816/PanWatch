"""Read-only local coverage audit and point-in-time reference data import."""
from datetime import date
import math
from sqlalchemy import text
from src.modules.strategy.factor_engine import CATALOG
from src.platform.persistence.models import StrategyReferenceValue

def audit(db,symbol=''):
    out={'factor_version':CATALOG['version'],'factor_count':77,'template_count':26,'series':{},'requirements':{'daily':57,'amount':5,'turnover':6,'limit':2,'financial':7}}
    for table in ('daily_bars','daily_bars_qfq','screener_snapshots'):
        where=' WHERE symbol=:symbol' if symbol else ''
        row=db.execute(text(f'SELECT COUNT(*),COUNT(DISTINCT symbol),MIN(trade_date),MAX(trade_date),COUNT(DISTINCT trade_date) FROM {table}'+where),{'symbol':symbol}).first()
        out['series'][table]=dict(zip(('rows','symbols','start','end','trading_dates'),row))
    out['reference_rows']=db.query(StrategyReferenceValue).filter(StrategyReferenceValue.symbol==symbol).count() if symbol else db.query(StrategyReferenceValue).count()
    raw=out['series']['daily_bars']['end'];qfq=out['series']['daily_bars_qfq']['end']
    out['warnings']=[]
    if raw and (not qfq or qfq<raw):out['warnings'].append(f'原始日线截至 {raw}，前复权日线截至 {qfq or "缺失"}；请补齐或明确缩短回测区间')
    out['warnings'].append('换手、财务和历史交易状态按标的及日期检查；当前快照不能回填历史。')
    return out

def import_references(db,records):
    allowed={f['id'] for f in CATALOG['factors'] if f['dependency']=='financial'}|{'book_value_per_share','turnover_rate','price_limit_pct','limit_up_locked','consecutive_limit_ups'}
    if not isinstance(records,list) or not 1<=len(records)<=10000:raise ValueError('一次导入1至10000项历史数据')
    validated=[]
    for r in records:
        if not isinstance(r,dict) or r.get('field') not in allowed:raise ValueError('不支持的历史字段')
        symbol=str(r.get('symbol',''));source=str(r.get('source','')).strip()
        if len(symbol)!=6 or not symbol.isdigit() or not source:raise ValueError('需要6位证券代码和真实数据来源')
        effective=date.fromisoformat(r['effective_date']).isoformat();known=date.fromisoformat(r['known_at']).isoformat()
        v=r.get('value')
        if not isinstance(v,(int,float)) or not math.isfinite(v):raise ValueError('历史数据必须为有限数值')
        validated.append({'symbol':symbol,'field':r['field'],'effective_date':effective,'known_at':known,'value':float(v),'source':source[:200]})
    for r in validated:
        exists=db.query(StrategyReferenceValue).filter_by(**{k:v for k,v in r.items() if k!='value'}).first()
        if exists:
            if exists.value!=r['value']:raise ValueError('相同来源与时点的历史值不允许静默覆盖；请使用新的更正公告日期或来源版本')
        else:db.add(StrategyReferenceValue(**r))
    db.commit()
    return {'imported':len(validated)}

def attach_references(db,symbol,bars):
    records=db.query(StrategyReferenceValue).filter_by(symbol=symbol).order_by(StrategyReferenceValue.known_at,StrategyReferenceValue.effective_date,StrategyReferenceValue.id).all()
    financial={f['id'] for f in CATALOG['factors'] if f['dependency']=='financial'}-{'pb_latest'}
    for bar in bars:
        values={};provenance=[]
        for r in records:
            # Financials persist only after announcement. Market/valuation observations are date-exact.
            if r.known_at<=bar.date and r.effective_date<=bar.date and (r.field in financial|{'book_value_per_share'} or r.effective_date==bar.date):
                values[r.field]=r.value
                provenance.append({'field':r.field,'effective_date':r.effective_date,'known_at':r.known_at,'value':r.value,'source':r.source})
        if values.get('book_value_per_share',0)>0 and bar.raw_close:values['pb_latest']=bar.raw_close/values['book_value_per_share']
        for field,value in values.items():setattr(bar,field,value)
        if provenance:bar.reference_records=provenance
    return bars
