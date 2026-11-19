"""One cash account and bounded positions over a fixed, recorded universe."""
from __future__ import annotations
from threading import Event
import math
from src.modules.strategy import template_runtime
from src.modules.strategy.factor_engine import field_rows
from src.modules.strategy.patterns import compute_features
from src.modules.strategy.rule_engine import evaluate,combine,execution_check
from src.modules.strategy.backtest.single import _affordable_fill,_execution_failure,limit_threshold
from src.modules.strategy.backtest.cost_model import CostModel

class Cancelled(RuntimeError):pass

def run_portfolio(histories,policy,*,start,end,initial_capital=100000,max_positions=5,max_weight=.2,cost=None,cancel=None,progress=None):
    if not 1<=max_positions<=100 or not 0<max_weight<=1 or initial_capital<=0 or not math.isfinite(initial_capital):raise ValueError('无效的组合资金或仓位参数')
    cost=cost or CostModel();cancel=cancel or Event();config=policy.get('config',{})
    if not histories:raise ValueError('股票池没有可用本地日线')
    universe=sorted(histories);days=sorted({b.date for bars in histories.values() for b in bars if start<=b.date<=end})
    maps={s:{b.date:(i,b) for i,b in enumerate(bars)} for s,bars in histories.items()}
    features={}
    for k,s in enumerate(universe):
        if cancel.is_set():raise Cancelled('用户取消')
        bars=histories[s];rows=[compute_features(bars,i,False) for i in range(len(bars))]
        if any('tf_' in str(c) for c in policy['buy']+policy['sell']+policy['avoid']):
            for row,extra in zip(rows,field_rows(bars)):
                for key,value in extra.items():
                    if key.startswith('tf_'):row[key]=value
        features[s]=rows
        if progress:progress('计算因子',k+1,len(universe))
    scores={}
    if config.get('template_id'):
        market,signals,score,known=template_runtime.execute(histories,config)
        for col,s in enumerate(market.symbols):
            for t,d in enumerate(market.dates):
                if d not in maps[s]:continue
                i,_=maps[s][d];features[s][i].update(template_entry=int(signals.entry[t,col]) if known[t,col] else None,template_exit=int(signals.exit[t,col]) if known[t,col] else None)
                scores[(s,d)]=float(score[t,col]) if math.isfinite(score[t,col]) else None
    cash=float(initial_capital);positions={};pending=[];orders=[];curve=[];last_prices={};coverage=0
    for day_index,day in enumerate(days):
        if cancel.is_set():raise Cancelled('用户取消')
        # Only quotes already reached in the simulation may value the account.
        for s in universe:
            if day in maps[s]:last_prices[s]=maps[s][day][1].open
        for order in sorted(pending,key=lambda x:x['side']!='sell'):
            s=order['symbol'];record={**order,'execution_date':day,'status':'unfilled'}
            pair=maps[s].get(day)
            if not pair:record['reason']='当日缺少日线，未成交';orders.append(record);continue
            i,bar=pair;bars=histories[s];failure=_execution_failure(bar,bars[i-1].close if i else 0,order['side'],limit_threshold(s))
            fill=None
            if not failure:
                if order['side']=='sell' and s in positions and positions[s]['entry_day']<day:
                    fill=cost.fill('sell',bar.open,positions[s]['quantity'])
                elif order['side']=='buy' and s not in positions and len(positions)<max_positions:
                    equity=cash+sum(p['quantity']*last_prices.get(sym,p['entry_price']) for sym,p in positions.items())
                    budget=min(cash,equity*max_weight)
                    fill=_affordable_fill(cost,budget,bar.open,200 if s.startswith(('688','689')) else 100)
            if fill:
                cash+=fill.cash_delta
                record.update(status='filled',price=fill.fill_price,quantity=fill.quantity,cash_after=cash,fees=fill.explicit_fees)
                if order['side']=='buy':positions[s]={'quantity':fill.quantity,'entry_price':fill.fill_price,'entry_day':day,'entry_day_index':day_index}
                else:positions.pop(s)
            else:record['reason']=failure[1] if failure else '资金、持仓上限、整手或T+1约束未满足'
            orders.append(record)
        pending=[];buys=[]
        for s in universe:
            if day not in maps[s]:continue
            i,bar=maps[s][day];last_prices[s]=bar.close
            if i<59 or bar.volume<=0:continue
            rows=features[s]
            ready=all(evaluate(rows,i,c) is not None for c in policy['buy']+policy['avoid'])
            coverage+=int(ready)
            if s in positions:
                p=positions[s];risk=execution_check(config.get('execution',{}),bar.close,p['entry_price'],day_index-p['entry_day_index']+1)
                if combine([evaluate(rows,i,c) for c in policy['sell']],'any') is True or any(c['ok'] for c in risk):pending.append({'symbol':s,'side':'sell','signal_date':day,'execution_checks':risk})
            elif policy['buy'] and ready and combine([evaluate(rows,i,c) for c in policy['buy']]) is True and combine([evaluate(rows,i,c) for c in policy['avoid']],'any') is not True:
                value=scores.get((s,day))
                if config.get('template_id') and len(universe)>1 and value is None:continue
                buys.append({'symbol':s,'side':'buy','signal_date':day,'score':value or 0})
        pending+=sorted(buys,key=lambda o:(-o['score'],o['symbol']))[:max_positions]
        equity=cash+sum(p['quantity']*last_prices.get(s,p['entry_price']) for s,p in positions.items())
        curve.append({'date':day,'equity':round(equity,4),'cash':round(cash,4),'equity_pct':round((equity/initial_capital-1)*100,3),'positions':len(positions)})
        if progress:progress('模拟组合交易',day_index+1,len(days))
    for o in pending:orders.append({**o,'execution_date':None,'status':'unfilled','reason':'区间结束，无下一交易日日线'})
    if not days:raise ValueError('所选区间没有本地日线')
    if policy['buy'] and coverage==0:raise ValueError('所选股票池与区间没有可完整判断策略的日期，检查历史输入或预热长度')
    peak=initial_capital;drawdown=0
    for point in curve:peak=max(peak,point['equity']);drawdown=max(drawdown,(peak-point['equity'])/peak*100)
    return {'mode':'portfolio','start':days[0],'end':days[-1],'curve':curve,'orders':orders,'open_positions':positions,'initial_capital':initial_capital,'final_equity':curve[-1]['equity'],'total_return':curve[-1]['equity_pct'],'metrics':{'max_drawdown':drawdown},'universe':universe,'coverage':coverage,'rules_snapshot':policy,'limitations':['固定股票池研究，未使用历史退市股全集，存在幸存者偏差。','仅在有对应日线时模拟成交；缺失、涨跌停与T+1约束不伪造成交。','技术回测按输入价格口径计价；前复权价格不等于当时实际成交价。','风险条件收盘确认，次日开盘尝试执行，不模拟盘中触价成交。']}
