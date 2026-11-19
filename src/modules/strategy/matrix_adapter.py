"""Small NumPy contract for vendored MIT strategy definitions; no market IO."""
from __future__ import annotations
from dataclasses import dataclass
from types import SimpleNamespace
import numpy as np
import pandas as pd
from src.modules.strategy.factor_engine import compute_frame

SignalMatrix = SimpleNamespace

@dataclass
class MarketDataMatrix:
    features: dict
    dates: list
    symbols: list

    def __post_init__(self):
        self.accessed=set()
        self.close=self.features['close'];self.open=self.features['open'];self.high=self.features['high'];self.low=self.features['low']
        self.shape=self.close.shape

    @property
    def limit_up_locked(self):
        return matrix_feature(self,'limit_up_locked')


def from_histories(histories: dict,fields=None) -> MarketDataMatrix:
    dates=sorted({b.date for bars in histories.values() for b in bars})
    symbols=sorted(histories)
    requested=set(fields or ())|{'open','high','low','close'}
    result={};index={d:i for i,d in enumerate(dates)}
    for col,s in enumerate(symbols):
        frame=compute_frame(histories[s],requested)
        names=requested if fields is not None else frame.columns
        idx=[index[b.date] for b in histories[s]]
        for name in names:
            if name not in result:result[name]=np.full((len(dates),len(symbols)),np.nan)
            if name in frame:result[name][idx,col]=frame[name].to_numpy(dtype=float)
    return MarketDataMatrix(result,dates,symbols)


def matrix_feature(market,name):
    market.accessed.add(name)
    return market.features.get(name,np.full(market.shape,np.nan))


def valid_shift(values, periods=1, valid_mask=None, **_kwargs):
    values=np.asarray(values,dtype=float);out=np.full_like(values,np.nan)
    valid=np.isfinite(values) if valid_mask is None else np.asarray(valid_mask,dtype=bool)
    for col in range(values.shape[1]):
        idx=np.flatnonzero(valid[:,col]);out[idx,col]=pd.Series(values[idx,col]).shift(periods).to_numpy()
    return out


def valid_ewm_adjust_false(values,valid_mask=None,*,span=None,alpha=None,**_kwargs):
    values=np.asarray(values,dtype=float);out=np.full_like(values,np.nan)
    valid=np.isfinite(values) if valid_mask is None else np.asarray(valid_mask,dtype=bool)
    for col in range(values.shape[1]):
        idx=np.flatnonzero(valid[:,col]);out[idx,col]=pd.Series(values[idx,col]).ewm(alpha=alpha if alpha is not None else 2/(span+1),adjust=False).mean().to_numpy()
    return out


def make_signal_matrix(shape,*,entry=None,exit=None,score=None,**kwargs):
    return SignalMatrix(entry=np.zeros(shape,dtype=bool) if entry is None else np.asarray(entry,dtype=bool),exit=np.zeros(shape,dtype=bool) if exit is None else np.asarray(exit,dtype=bool),score=np.zeros(shape) if score is None else score,**kwargs)


def build_matrix_score(market,universe,scoring,order_by,descending,*,fallback,directions=None):
    if not scoring:return fallback
    score=np.zeros(market.shape);known=universe.copy();total=sum(scoring.values())
    for name,weight in scoring.items():
        x=matrix_feature(market,name);valid=np.isfinite(x)&universe;known &= valid
        for i in range(len(x)):
            if not valid[i].any():continue
            lo,hi=x[i,valid[i]].min(),x[i,valid[i]].max()
            z=(x[i]-lo)/(hi-lo) if hi>lo else np.full(x.shape[1],.5)
            if (directions or {}).get(name)=='low':z=1-z
            score[i]+=np.where(valid[i],z,0)*weight/total*100
    return np.where(known,score,np.nan)
