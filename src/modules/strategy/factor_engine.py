"""Versioned, causal Tick-compatible daily factors. No IO or forward filling."""
from __future__ import annotations

import json
from pathlib import Path
import numpy as np
import pandas as pd

CATALOG = json.loads(Path(__file__).with_name('tick_catalog.json').read_text(encoding='utf-8'))
ALIASES={'change_pct':('change_pct',100),'momentum_5d':('runup_5d',100),'momentum_10d':('runup_10d',100),'momentum_20d':('runup_20d',100),'ma5_bias':('ma5_dev',100),'ma20_bias':('ma20_dev',100),'vol_ratio_5d':('volume_ratio_5d',1),'gap_return':('open_gap',100),'intraday_return':('body_pct',100),'close_position':('close_pos',100)}
FACTOR_FIELDS = [{**f, 'source_id':f['id'], 'key':ALIASES.get(f['id'],(f['key'],1))[0], 'unit':'%' if f['id'] in ALIASES and ALIASES[f['id']][1]==100 else f['unit'], 'description':f['formula']} for f in CATALOG['factors']]
DEPENDENCY_HINTS = {'daily': '需要完整日线和指标预热', 'amount': '需要同日原始价格与成交额', 'turnover': '需要对应日期的历史换手率', 'limit': '需要历史涨跌停和上市/ST状态', 'financial': '需要公告日不晚于当日的财务历史'}


def compute_frame(bars,extra_fields=()) -> pd.DataFrame:
    """Original factor units are retained (ratios are explicitly labelled)."""
    n = len(bars)
    def col(name, default=np.nan):
        return pd.Series([getattr(b, name, default) if getattr(b, name, None) is not None else default for b in bars], dtype=float)
    c, o, h, l, v = (col(k) for k in ('close','open','high','low','volume'))
    raw = col('raw_close'); raw = raw.where(raw > 0, c)
    a, turnover = col('amount'), col('turnover_rate')
    def ratio(x,y): return x / y.where(y != 0)
    def rel(x,y): return ratio(x,y)-1
    r = c.pct_change(fill_method=None)
    f = {'close':c,'open':o,'high':h,'low':l,'volume':v,'amount':a,'turnover_rate':turnover,'raw_close':raw,'prev_close':c.shift(1),'change_pct':r,'amplitude':ratio(h-l,c.shift(1))}
    for p in (5,10,20,30,60):
        f[f'ma{p}']=c.rolling(p).mean();f[f'ema{p}']=c.ewm(span=p,adjust=False).mean().where(c.expanding().count()>=p)
        f[f'ma{p}_bias']=rel(c,f[f'ma{p}']);f[f'ema{p}_bias']=rel(c,f[f'ema{p}'])
        f[f'momentum_{p}d']=rel(c,c.shift(p))
    dif=c.ewm(span=12,adjust=False).mean()-c.ewm(span=26,adjust=False).mean()
    dea=dif.ewm(span=9,adjust=False).mean()
    for key,value in {'macd_dif':dif,'macd_dea':dea,'macd_hist':(dif-dea)*2}.items():
        f[key]=value.where(c.expanding().count()>=35);f[key+'_pct']=ratio(f[key],c)
    delta=c.diff();gain=delta.clip(lower=0).fillna(0);loss=(-delta).clip(lower=0).fillna(0)
    periods={6,14,24}|{int(k[4:]) for k in extra_fields if k.startswith('rsi_') and k[4:].isdigit() and 2<=int(k[4:])<=60}
    for p in periods:
        ag=gain.ewm(alpha=1/p,adjust=False).mean();al=loss.ewm(alpha=1/p,adjust=False).mean()
        f[f'rsi_{p}']=(100-100/(1+ag/al.where(al!=0,1e-12))).where(c.expanding().count()>=p+1)
    rsv=ratio(c-l.rolling(9).min(),h.rolling(9).max()-l.rolling(9).min())*100
    k=rsv.ewm(alpha=1/3,adjust=False,ignore_na=True).mean().where(rsv.notna());d=k.ewm(alpha=1/3,adjust=False,ignore_na=True).mean().where(k.notna())
    f.update(kdj_k=k,kdj_d=d,kdj_j=3*k-2*d,kdj_kd_diff=k-d)
    mid=c.rolling(20).mean();std=c.rolling(20).std(ddof=1)
    f.update(boll_upper=mid+2*std,boll_lower=mid-2*std,boll_width=ratio(4*std,mid),boll_position=ratio(c-(mid-2*std),4*std))
    tr=pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    f['atr_14']=tr.ewm(alpha=1/14,adjust=False).mean();f['atr_pct']=ratio(f['atr_14'],c);f['annual_vol_20d']=r.rolling(20).std()*np.sqrt(252)
    for p in (5,10):f[f'vol_ma{p}']=v.rolling(p).mean();f[f'vol_ratio_{p}d']=ratio(v,v.shift().rolling(p).mean())
    f.update(vol_trend_5_10=rel(v.rolling(5).mean(),v.rolling(10).mean()),vol_trend_5_60=rel(v.rolling(5).mean(),v.rolling(60).mean()),turnover_ratio_5d=rel(turnover,turnover.shift().rolling(5).mean()),log_amount=np.log1p(a.where(a>=0)),amount_ratio_5d=rel(a,a.shift().rolling(5).mean()),gap_return=rel(o,c.shift()),intraday_return=rel(c,o),close_position=ratio(c-l,h-l),high_60d=c.rolling(60).max(),low_60d=c.rolling(60).min())
    f.update(distance_to_high_60d=rel(c,f['high_60d']),distance_from_low_60d=rel(c,f['low_60d']))
    # Amount and volume refer to raw trading prices. Never compare qfq close with raw VWAP.
    f.update(vwap_bias=rel(raw,ratio(a,v*100)),max_ret_20d=r.rolling(20).max(),ret_skew_20d=r.rolling(20).apply(lambda x: 0.0 if np.std(x)==0 else np.mean(((x-np.mean(x))/np.std(x))**3),raw=True),up_days_20d=(r>0).astype(float).where(r.notna()).rolling(20).sum(),amihud_20d=ratio(r.abs(),a/1e8).rolling(20).mean(),turnover_z_60d=ratio(turnover-turnover.shift().rolling(60).mean(),turnover.shift().rolling(60).std()),vol_price_corr_20d=r.rolling(20).corr(v))
    for p in (20,60):
        locked=col('limit_up_locked');f[f'limit_up_count_{p}d']=locked.rolling(p).sum()
    f.update(log_float_mv=np.log((raw*v/turnover).where((turnover>0)&(v>0))),momentum_120d=rel(c,c.shift(120)),mom_accel_20_60=f['momentum_20d']-f['momentum_60d'],rsi_14_delta_5d=f['rsi_14']-f['rsi_14'].shift(5),overnight_ret_20d=rel(o,c.shift()).rolling(20).sum(),intraday_ret_20d=rel(c,o).rolling(20).sum(),downside_vol_20d=np.sqrt(r.clip(upper=0).pow(2).rolling(20).mean()),vol_regime_5_60=ratio(r.rolling(5).std(),r.rolling(60).std()),amplitude_trend_20_60=rel(f['amplitude'].rolling(20).mean(),f['amplitude'].rolling(60).mean()),obv_trend_20d=ratio((np.sign(r)*v).rolling(20).sum(),v.rolling(20).mean()*20),amount_mean_20d=(a/1e8).rolling(20).mean(),turnover_mean_20d=turnover.rolling(20).mean(),turnover_std_20d=ratio(turnover.rolling(20).std(),turnover.rolling(20).mean()),position_240d=ratio(c-c.rolling(240).min(),c.rolling(240).max()-c.rolling(240).min()),distance_to_high_240d=rel(c,c.rolling(240).max()))
    # ---- 模板条件所需的派生字段（全部来自已有列，不引入新数据源） ----
    f['above_ma5'] = (c > f['ma5']).astype(float).where(f['ma5'].notna())
    f['above_ma20'] = (c > f['ma20']).astype(float).where(f['ma20'].notna())
    f['above_ma60'] = (c > f['ma60']).astype(float).where(f['ma60'].notna())
    f['boll_breakout'] = (c > f['boll_upper']).astype(float).where(f['boll_upper'].notna())
    f['ma_golden_cross'] = ((f['ma5'] > f['ma20']) & (f['ma5'].shift(1) <= f['ma20'].shift(1))).astype(float).where(f['ma20'].notna())
    f['ma_bull_alignment'] = ((f['ma5'] > f['ma20']) & (f['ma20'] > f['ma60'])).astype(float).where(f['ma60'].notna())
    f['ma20_breakout'] = ((c > f['ma20']) & (c.shift(1) <= f['ma20'].shift(1))).astype(float).where(f['ma20'].notna())
    f['macd_golden_cross'] = ((dif > dea) & (dif.shift(1) <= dea.shift(1))).astype(float)
    f['close_above_prev'] = (c > f['prev_close']).astype(float)
    for w in sorted({60} | {int(k[9:]) for k in extra_fields if k.startswith('n_day_low_') and k[9:].isdigit() and 5 <= int(k[9:]) <= 250}):
        low_n = l.rolling(w).min()
        f[f'n_day_low_{w}'] = (l <= low_n).astype(float).where(low_n.notna())
    f.setdefault('n_day_low', f['n_day_low_60'])
    for name in ('price_limit_pct','limit_up_locked','consecutive_limit_ups'):
        f[name] = col(name)

    # 行情源未提供涨停/连板列时，按 A 股涨跌停规则推导（主板 10%、创业/科创 20%、ST 5%）
    if f['limit_up_locked'].isna().all():
        limit_col = col('price_limit_pct')
        limit_ratio = limit_col.where(limit_col > 0, np.nan)
        # price_limit_pct 可能是百分数(10)或小数(0.1)，统一成小数阈值
        limit_ratio = limit_ratio.where(limit_ratio < 1, limit_ratio / 100.0)
        threshold = (limit_ratio - 0.002).fillna(0.098)
        locked = (r >= threshold).astype(float)
        f['limit_up_locked'] = locked
        group = (~locked.astype(bool)).cumsum()
        f['consecutive_limit_ups'] = locked.groupby(group).cumsum()
        for span in (20, 60):
            f[f'limit_up_count_{span}d'] = locked.rolling(span).sum()


    # 行情源未提供涨停/连板列时，按 A 股涨跌停规则推导
    if f['limit_up_locked'].isna().all():
        limit_col = col('price_limit_pct')
        limit_ratio = limit_col.where(limit_col > 0, np.nan)
        limit_ratio = limit_ratio.where(limit_ratio < 1, limit_ratio / 100.0)
        threshold = (limit_ratio - 0.002).fillna(0.098)
        locked = (r >= threshold).astype(float)
        f['limit_up_locked'] = locked
        group = (~locked.astype(bool)).cumsum()
        f['consecutive_limit_ups'] = locked.groupby(group).cumsum()
        for span in (20, 60):
            f[f'limit_up_count_{span}d'] = locked.rolling(span).sum()
    for spec in CATALOG['factors']:
        if spec['id'] not in f:f[spec['id']]=col(spec['id'])
    return pd.DataFrame(f,index=range(n)).replace([np.inf,-np.inf],np.nan)


def field_rows(bars):
    frame=compute_frame(bars)
    return [{f['key']: (float(row[f['id']])*ALIASES.get(f['id'],('',1))[1] if i+1>=f['warmup'] and pd.notna(row[f['id']]) else None) for f in FACTOR_FIELDS} for i,row in enumerate(frame.to_dict('records'))]


def required_warmup(conditions) -> int:
    specs={f['key']:f['warmup'] for f in FACTOR_FIELDS}
    def visit(row):
        if 'children' in row:return max((visit(c) for c in row['children']),default=1)
        return specs.get(row.get('field'),60)+int(row.get('offset',0))+int(row.get('consecutive',1))-1
    return max((visit(c) for c in conditions),default=60)
