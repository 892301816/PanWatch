"""Bounded condition trees with three-valued logic and causal offsets."""
import math

OPS={'eq','ne','gt','gte','lt','lte','between','in','not_in'}

def validate_condition(item, fields, depth=0):
    if not isinstance(item,dict) or depth>4:raise ValueError('条件组最多嵌套4层')
    if item.get('children') is not None:
        children=item['children'];logic=item.get('logic','all')
        if logic not in ('all','any') or not isinstance(children,list) or not 1<=len(children)<=10:raise ValueError('条件组需包含1至10项，并选择全部或任一')
        return {'field':'__group__','op':'eq','value':1,'logic':logic,'children':[validate_condition(c,fields,depth+1) for c in children]}
    if item.get('field') not in fields:raise ValueError('含未知字段，须人工确认，不能作为已执行规则')
    op=item.get('op','eq');value=item.get('value')
    if op not in OPS:raise ValueError('不支持的比较方式')
    values=value if op in ('between','in','not_in') else [value]
    if not isinstance(values,list) or not 1<=len(values)<=30 or (op=='between' and len(values)!=2):raise ValueError('比较范围或集合不能为空')
    try:numbers=[float(x) for x in values]
    except (TypeError,ValueError):raise ValueError('条件阈值必须是数字') from None
    if any(not math.isfinite(x) for x in numbers):raise ValueError('条件阈值必须是有限数字')
    if op=='between' and numbers[0]>numbers[1]:raise ValueError('范围下限不能大于上限')
    out={'field':item['field'],'op':op,'value':numbers if op in ('between','in','not_in') else numbers[0]}
    for key,default,lo,hi in (('offset',0,0,240),('consecutive',1,1,60)):
        v=item.get(key,default)
        if isinstance(v,bool) or not isinstance(v,int) or not lo<=v<=hi:raise ValueError('历史偏移或连续天数超出范围')
        if v!=default:out[key]=v
    return out

def combine(values,logic='all'):
    if not values:return False
    if logic=='all':return False if False in values else None if None in values else True
    return True if True in values else None if None in values else False

def evaluate(rows,idx,condition):
    if 'children' in condition:return combine([evaluate(rows,idx,c) for c in condition['children']],condition.get('logic','all'))
    outcomes=[]
    for lag in range(condition.get('consecutive',1)):
        j=idx-condition.get('offset',0)-lag
        actual=rows[j].get(condition['field']) if 0<=j<len(rows) else None
        if actual is None or not isinstance(actual,(int,float)) or not math.isfinite(actual):outcomes.append(None);continue
        value=condition['value'];op=condition['op']
        outcomes.append({'eq':lambda:actual==value,'ne':lambda:actual!=value,'gt':lambda:actual>value,'gte':lambda:actual>=value,'lt':lambda:actual<value,'lte':lambda:actual<=value,'between':lambda:value[0]<=actual<=value[1],'in':lambda:actual in value,'not_in':lambda:actual not in value}[op]())
    return combine(outcomes)

def execution_check(execution,price,cost,holding_days=None):
    checks=[]
    for key,op,label in (('stop_loss_pct','lte','策略止损'),('take_profit_pct','gte','策略止盈')):
        if execution.get(key) is not None:
            actual=(price/cost-1)*100 if price and cost and cost>0 else None
            checks.append({'field':key,'label':label,'actual':actual,'expected':execution[key],'known':actual is not None,'ok':None if actual is None else actual<=execution[key] if op=='lte' else actual>=execution[key]})
    if execution.get('max_hold_days'):
        checks.append({'field':'max_hold_days','label':'最长持有交易日','actual':holding_days,'expected':execution['max_hold_days'],'known':holding_days is not None,'ok':None if holding_days is None else holding_days>=execution['max_hold_days']})
    return checks
