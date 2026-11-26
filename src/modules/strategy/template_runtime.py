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
    if 'chart_script' in config:
        from src.modules.strategy.chart_script import compile_script
        allowed = {'chart_script', 'engine', 'inputs'}
        if set(config) - allowed:
            raise ValueError('脚本与模板配置不能混用')
        compile_script(config['chart_script'])
        out = {'chart_script': config['chart_script'].strip()}
        if config.get('engine'):
            out['engine'] = str(config['engine'])
        inputs = config.get('inputs')
        if inputs is not None:
            if not isinstance(inputs, dict) or len(inputs) > 64:
                raise ValueError('脚本参数格式错误')
            clean_inputs = {}
            for key, value in inputs.items():
                if not isinstance(key, str) or len(key) > 120:
                    raise ValueError('脚本参数名过长')
                if isinstance(value, bool) or isinstance(value, (int, float)):
                    clean_inputs[key] = value
                elif isinstance(value, str) and len(value) <= 200:
                    clean_inputs[key] = value
                else:
                    raise ValueError('脚本参数只支持数字、布尔与短字符串')
            if clean_inputs:
                out['inputs'] = clean_inputs
        return out
    if set(config)-{'template_id','template_ref','mode','legacy_script','params','execution','scoring','directions','factor_version'}:raise ValueError('策略配置包含不支持的字段')
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

_GATE_PREFIX = ('use_', 'enable_', 'require_', 'is_')

_UNIT_RULES = (
    ('_pct', '%'), ('pct', '%'), ('gap', '%'), ('range', '%'), ('turnover', '%'),
    ('days', '天'), ('window', '天'), ('period', '天'),
    ('vol_ratio', '倍'), ('ratio', '倍'), ('count', '次'), ('boards', '次'), ('limit_up', '次'),
    ('price', '元'), ('amount', '元'),
)


def _unit_of(pid: str, label: str = '') -> str:
    # 标签里带单位符号时优先采用（'涨停次数' -> 次，'涨幅%' -> %）
    if '次' in label:
        return '次'
    if '%' in label:
        return '%'
    if '天' in label:
        return '天'
    low = pid.lower()
    for token, unit in _UNIT_RULES:
        if token in low:
            return unit
    return ''


def _op_of(pid: str) -> str:
    low = pid.lower()
    if low.startswith('min_') or low.startswith('least_'):
        return '≥'
    if low.startswith('max_') or low.endswith('_max'):
        return '≤'
    if 'above' in low or 'at_least' in low:
        return '≥'
    return '≥'



# 参数 -> 条件字段映射：过滤类参数翻译成统一条件行；窗口类保留为「算法窗口」
FILTER_FIELDS = {
    'min_pct': ('change_pct', 'gte', '%'), 'min_change': ('change_pct', 'gte', '%'),
    'max_change': ('change_pct', 'lte', '%'), 'min_open_gap': ('open_gap', 'gte', '%'),
    'limit_gap': ('open_gap', 'lte', '%'),
    'vol_ratio_max': ('volume_ratio_5d', 'lte', '倍'), 'min_vol_ratio': ('volume_ratio_5d', 'gte', '倍'),
    'min_turnover': ('turnover_rate', 'gte', '%'), 'max_turnover': ('turnover_rate', 'lte', '%'),
    'min_boards': ('consecutive_limit_ups', 'gte', '次'), 'boards': ('consecutive_limit_ups', 'gte', '次'),
    'min_limit_count': ('limit_up_count_60d', 'gte', '次'),
    'rsi_max': ('rsi_14', 'lte', ''), 'rsi_min': ('rsi_14', 'gte', ''),
    'vol_max': ('annual_vol_20d', 'lte', '%'),
    'ma_proximity': ('ma20_dev', 'abs_lte', '%'), 'proximity': ('ma20_dev', 'abs_lte', '%'),
    'range_pct_max': ('boll_width', 'lte', '%'),
    'require_limit_up': ('limit_up_locked', 'eq', '是'),
    'require_boll_breakout': ('boll_breakout', 'eq', '是'),
    'require_ma_golden': ('ma_golden_cross', 'eq', '是'),
    'require_above_ma5': ('above_ma5', 'eq', '是'),
    'require_above_ma20': ('above_ma20', 'eq', '是'),
    'require_above_ma60': ('above_ma60', 'eq', '是'),
    'require_ma_alignment': ('ma_bull_alignment', 'eq', '是'),
    'require_positive_momentum': ('momentum_20d', 'gt', '%'),
    'require_positive_change': ('change_pct', 'gt', '%'),
    'require_n_day_high': ('high20_gap', 'gte', '%'),
    'require_n_day_low': ('n_day_low', 'eq', '是'),
    'require_bullish_candle': ('body_pct', 'gt', '%'),
    'require_recovery': ('close_above_prev', 'eq', '是'),
    'require_close_above_open': ('body_pct', 'gt', '%'),
    'require_ma20_breakout': ('ma20_breakout', 'eq', '是'),
    'require_macd_golden': ('macd_golden_cross', 'eq', '是'),
}

WINDOW_HINT = ('days', 'window', 'period', 'platform_days', 'squeeze_days')


def _is_window(pid: str) -> bool:
    low = pid.lower()
    return any(token in low for token in WINDOW_HINT) and not low.startswith(('min_', 'max_'))


def template_conditions(spec: dict) -> dict:
    """把模板的过滤参数翻译成 buy 条件行；窗口参数保留在 params（算法窗口）。"""
    params = spec.get('params') or []
    buy, windows = [], []
    for index, param in enumerate(params):
        pid = str(param.get('id', ''))
        default = param.get('default')
        if _is_window(pid):
            windows.append(param)
            continue
        if isinstance(default, bool):
            # 开关：默认关闭的参数不进入条件，打开时由映射决定阈值
            continue
        mapped = FILTER_FIELDS.get(pid)
        if not mapped:
            continue
        field, op, unit = mapped
        buy.append({'field': field, 'op': op, 'value': default})
    # 布尔条件：require_* 为真的默认开启
    for param in params:
        pid = str(param.get('id', ''))
        if isinstance(param.get('default'), bool) and param.get('default'):
            mapped = FILTER_FIELDS.get(pid)
            if mapped and mapped[0] not in {row['field'] for row in buy}:
                buy.append({'field': mapped[0], 'op': mapped[1], 'value': 1})
    return {'buy': buy, 'windows': windows}


def param_rows(spec: dict) -> list[dict]:
    """把模板参数声明归一成统一的条件行。

    规则：
    - `use_/enable_/require_/is_*` 且默认值为布尔 -> 视为"开关"；
      若紧跟一个非布尔参数，则两者合并成一行（开关控制这行是否生效）。
    - 其余数值参数 -> 一行"字段 ≥/≤ 数值 单位"，比较符由 id 前缀推断。
    - 所有模板共用同一套行结构，前端只需要一种渲染。
    """
    params = spec.get('params') or []
    rows: list[dict] = []
    index = 0
    while index < len(params):
        param = params[index]
        pid = str(param.get('id', ''))
        default = param.get('default')
        is_bool = isinstance(default, bool)
        is_gate = is_bool and pid.lower().startswith(_GATE_PREFIX)
        if is_gate:
            nxt = params[index + 1] if index + 1 < len(params) else None
            paired = nxt is not None and not isinstance(nxt.get('default'), bool)
            if paired:
                qkey = str(nxt.get('id', ''))
                rows.append({
                    'key': qkey,
                    'gate': pid,
                    'label': nxt.get('label') or qkey,
                    'control': 'number',
                    'op': _op_of(qkey),
                    'unit': _unit_of(qkey, str(nxt.get('label') or '')),
                    'default': nxt.get('default'),
                    'min': nxt.get('min'),
                    'max': nxt.get('max'),
                    'step': nxt.get('step'),
                    'hint': param.get('label') or '',
                })
                index += 2
                continue
        if is_bool:
            rows.append({
                'key': pid,
                'label': param.get('label') or pid,
                'control': 'toggle',
                'op': '',
                'unit': '',
                'default': default,
                'hint': '',
            })
        else:
            rows.append({
                'key': pid,
                'label': param.get('label') or pid,
                'control': 'number',
                'op': _op_of(pid),
                'unit': _unit_of(pid, str(param.get('label') or '')),
                'default': default,
                'min': param.get('min'),
                'max': param.get('max'),
                'step': param.get('step'),
                'hint': '',
            })
        index += 1
    return rows


def presets():
    return [{**s,'note':s.get('description',''),'paramRows':[r for r in param_rows(s) if _is_window(str(r.get('key','')))],'buy':template_conditions(s)['buy'], 'sell':[], 'avoid':[], 'config':{'mode':'conditions','template_ref':s['id'],'params':{p['id']:p['default'] for p in s.get('params',[]) if _is_window(str(p.get('id','')))},'execution':{'stop_loss_pct':(s.get('stop_loss') or -.06)*100,'max_hold_days':s.get('max_hold_days') or 20},'scoring':s.get('scoring') or ({'momentum_20d':1} if s['id']=='factor_rank_research' else {}),'directions':{}},'source':'tick-stock-panel','formula_version':CATALOG['version']} for s in TEMPLATES.values()]

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
    if 'chart_script' in config:
        from src.modules.strategy.chart_script import series
        points = series(bars, config['chart_script'], symbol=symbol, inputs=config.get('inputs'))
        for row, point in zip(rows, points):
            row.update(point)
        return rows
    if not config.get('template_id'):return rows
    market,signals,score,known=execute({symbol:bars},config)
    for i,row in enumerate(rows):
        row['template_entry']=int(signals.entry[i,0]) if known[i,0] else None
        row['template_exit']=int(signals.exit[i,0]) if known[i,0] else None
    return rows
