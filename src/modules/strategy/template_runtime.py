"""Audited builtin definitions with validated parameters and explicit data masks."""
from importlib import import_module
import ast
from pathlib import Path
import math
import numpy as np
from src.modules.strategy.factor_engine import CATALOG
from src.modules.strategy.matrix_adapter import from_histories, build_matrix_score

TEMPLATES={s['id']:s for s in CATALOG['strategies']}

def normalize_config(config):
    if not isinstance(config or {},dict):raise ValueError('策略配置格式错误')
    config=config or {};out={}
    if set(config)-{'template_id','params','execution','scoring','directions','factor_version'}:raise ValueError('策略配置包含不支持的字段')
    if config.get('factor_version') not in (None,CATALOG['version']):raise ValueError('公式版本不受支持，请保留旧报告并重新核对策略')
    if config.get('factor_version'):out['factor_version']=config['factor_version']
    template=config.get('template_id')
    if template:
        if template not in TEMPLATES:raise ValueError('未知策略模板')
        spec=TEMPLATES[template];params={}
        if set(config.get('params',{}))-{p['id'] for p in spec.get('params',[])}:raise ValueError('模板参数包含未知字段')
        for p in spec.get('params',[]):
            value=config.get('params',{}).get(p['id'],p['default'])
            if p['type']=='bool':
                if not isinstance(value,bool):raise ValueError(f"{p['label']}必须为是/否")
            else:
                try:value=float(value)
                except (TypeError,ValueError):raise ValueError('模板参数必须是数字') from None
                if not math.isfinite(value) or not p.get('min',-math.inf)<=value<=p.get('max',math.inf):raise ValueError(f"{p['label']}超出范围")
                if p['type']=='int':
                    if int(value)!=value:raise ValueError('模板参数必须为整数')
                    value=int(value)
            params[p['id']]=value
        if template=='factor_rank_research' or config.get('scoring') or spec.get('scoring'):
            scoring=config.get('scoring',spec.get('scoring',{}));allowed={f['id'] for f in CATALOG['factors']}|{'amount','consecutive_limit_ups'}
            if not isinstance(scoring,dict) or not 1<=len(scoring)<=4 or any(k not in allowed or not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0 for k,v in scoring.items()):raise ValueError('评分需要1至4个已注册因子及正权重')
            out['scoring']=scoring;out['directions']=config.get('directions',{})
            if any(k not in scoring or v not in ('high','low') for k,v in out['directions'].items()):raise ValueError('无效评分方向')
        out.update(template_id=template,params=params,factor_version=CATALOG['version'])
    clean={}
    if set(config.get('execution',{}))-{'stop_loss_pct','take_profit_pct','max_hold_days'}:raise ValueError('不支持的执行规则')
    for key,lo,hi in (('stop_loss_pct',-99,-.01),('take_profit_pct',.01,1000),('max_hold_days',1,5000)):
        value=config.get('execution',{}).get(key)
        if value is not None:
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not lo<=value<=hi:raise ValueError('止损、止盈或持有期限超出范围')
            if key=='max_hold_days' and int(value)!=value:raise ValueError('持有期限必须是整数')
            clean[key]=value
    if clean:out['execution']=clean
    return out

def presets():
    return [{**s,'note':s.get('description',''),'buy':[{'field':'template_entry','op':'eq','value':1}], 'sell':[{'field':'template_exit','op':'eq','value':1}], 'avoid':[], 'config':{'template_id':s['id'],'params':{p['id']:p['default'] for p in s.get('params',[])},'execution':{'stop_loss_pct':(s.get('stop_loss') or -.06)*100,'max_hold_days':s.get('max_hold_days') or 20},'scoring':s.get('scoring') or ({'momentum_20d':1} if s['id']=='factor_rank_research' else {}),'directions':{}},'source':'tick-stock-panel','formula_version':CATALOG['version']} for s in TEMPLATES.values()]

def execute(histories,config):
    config=normalize_config(config)
    spec=TEMPLATES[config['template_id']]
    module=import_module('src.modules.strategy.imported_tick.'+spec['module']);strategy=module.MATRIX_STRATEGY
    tree=ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
    fields={n.args[1].value for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='matrix_feature' and len(n.args)>1 and isinstance(n.args[1],ast.Constant)}
    fields |= set(spec.get('scoring',{}))|set(config.get('scoring',{}))
    if spec['id']=='rsi_midline_pullback':fields.add('rsi_'+str(config['params'].get('rsi_period',14)))
    if spec['id']=='broken_board_recovery':fields.add('limit_up_locked')
    market=from_histories(histories,fields)
    if spec['id']=='factor_rank_research':
        if len(histories)<2:raise ValueError('因子排名需要至少两个标的的同日截面，请使用组合回测')
        strategy=module.FactorRankResearchMatrixStrategy(config['scoring'],config.get('directions'))
    signals=strategy.compute_signals(market,config['params']);score=signals.score
    if config.get('scoring') and len(histories)>1:score=build_matrix_score(market,np.isfinite(market.close),config['scoring'],'score',True,fallback=score,directions=config.get('directions'))
    known=np.isfinite(market.close)
    for field in market.accessed:known &= np.isfinite(market.features.get(field,np.full(market.shape,np.nan)))
    warmup=max(60,strategy.required_warmup_bars(config['params']),max((f['warmup'] for f in CATALOG['factors'] if f['id'] in market.accessed),default=1))
    for col in range(len(market.symbols)):
        count=0
        for i in range(len(market.dates)):
            if np.isfinite(market.close[i,col]):count+=1
            known[i,col] &= count>=warmup
    return market,signals,score,known

def attach(rows,bars,config,symbol='single'):
    if not config.get('template_id'):return rows
    market,signals,score,known=execute({symbol:bars},config)
    for i,row in enumerate(rows):
        row['template_entry']=int(signals.entry[i,0]) if known[i,0] else None
        row['template_exit']=int(signals.exit[i,0]) if known[i,0] else None
    return rows
