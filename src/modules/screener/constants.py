"""选股字段目录、算子和预设策略。

字段清单同时服务于三处:前端条件构建器、后端校验、AI 自然语言解析。
金额类字段在对外接口里统一换算成「亿元」,避免前端再做一次除法。
"""

from __future__ import annotations

# 金额字段的存储单位 → 对外单位(亿元)
FIELD_SCALE: dict[str, float] = {
    "amount": 1e8,
    "float_mv": 1e8,
    "total_mv": 1e8,
    "main_net_inflow": 1e8,
}

# 前端条件构建器与 AI prompt 共用的字段目录。
FIELD_CATALOG: list[dict] = [
    {"key": "change_pct", "label": "涨跌幅", "unit": "%", "group": "行情", "type": "number"},
    {"key": "price", "label": "现价", "unit": "元", "group": "行情", "type": "number"},
    {"key": "amplitude", "label": "振幅", "unit": "%", "group": "行情", "type": "number"},
    {"key": "turnover_rate", "label": "换手率", "unit": "%", "group": "行情", "type": "number"},
    {"key": "volume_ratio", "label": "量比", "unit": "", "group": "行情", "type": "number", "available": False},
    {"key": "amount", "label": "成交额", "unit": "亿元", "group": "行情", "type": "number"},
    {"key": "float_mv", "label": "流通市值", "unit": "亿元", "group": "行情", "type": "number"},
    {"key": "total_mv", "label": "总市值", "unit": "亿元", "group": "行情", "type": "number"},
    {"key": "pe_ttm", "label": "市盈率(动)", "unit": "", "group": "行情", "type": "number"},
    {"key": "pb", "label": "市净率", "unit": "", "group": "行情", "type": "number"},
    {"key": "speed", "label": "涨速", "unit": "%", "group": "行情", "type": "number", "available": False},
    {"key": "main_net_inflow", "label": "主力净流入", "unit": "亿元", "group": "资金", "type": "number", "available": False},
    {"key": "limit_up_days", "label": "连板数", "unit": "板", "group": "涨停", "type": "number"},
    {"key": "limit_up_count_5d", "label": "近5日涨停次数", "unit": "次", "group": "涨停", "type": "number"},
    {"key": "limit_up_days_5d", "label": "近5日最高连板", "unit": "板", "group": "涨停", "type": "number"},
    {"key": "limit_up_last_days_ago", "label": "最近涨停距今天数", "unit": "天", "group": "涨停", "type": "number"},
    {"key": "limit_up_stat_days", "label": "今日涨停统计窗口", "unit": "天", "group": "涨停", "type": "number"},
    {"key": "limit_up_stat_count", "label": "今日池内涨停次数", "unit": "次", "group": "涨停", "type": "number"},
    {"key": "seal_fund", "label": "封板资金", "unit": "亿元", "group": "涨停", "type": "number"},
    {"key": "break_count", "label": "炸板次数", "unit": "次", "group": "涨停", "type": "number"},
    {"key": "board", "label": "上市板块", "unit": "", "group": "板块", "type": "string"},
    {"key": "industry", "label": "所属行业", "unit": "", "group": "板块", "type": "string", "available": False},
    {"key": "concept", "label": "所属概念", "unit": "", "group": "板块", "type": "list"},
    {"key": "concept_rank", "label": "最强概念的板块排名", "unit": "", "group": "板块", "type": "number"},
    {"key": "is_st", "label": "是否 ST", "unit": "", "group": "其他", "type": "boolean"},
]

FIELD_KEYS = {f["key"] for f in FIELD_CATALOG}

# 封板资金也按亿元展示,但存储时来自涨停池(元)
FIELD_SCALE["seal_fund"] = 1e8

OPS: dict[str, str] = {
    "eq": "等于",
    "ne": "不等于",
    "gt": "大于",
    "gte": "大于等于",
    "lt": "小于",
    "lte": "小于等于",
    "between": "介于",
    "in": "属于",
    "not_in": "不属于",
    "contains": "包含",
}

# 预设策略（同花顺的「分组条件」）
PRESETS: list[dict] = [
    {
        "code": "first_board",
        "name": "首板候选",
        "description": "今日首板，换手活跃，市值适中",
        "conditions": [
            {"field": "limit_up_days", "op": "eq", "value": 1},
            {"field": "turnover_rate", "op": "between", "value": [3, 20]},
            {"field": "float_mv", "op": "between", "value": [20, 300]},
        ],
        "exclude": ["st"],
        "order_by": "change_pct desc",
    },
    {
        "code": "ladder",
        "name": "连板梯队",
        "description": "2 板及以上，按连板高度排序",
        "conditions": [
            {"field": "limit_up_days", "op": "gte", "value": 2},
        ],
        "exclude": ["st"],
        "order_by": "limit_up_days desc",
    },
    {
        "code": "volume_attack",
        "name": "放量上攻",
        "description": "换手放大且涨幅居前，未封板",
        "conditions": [
            {"field": "turnover_rate", "op": "gte", "value": 5},
            {"field": "change_pct", "op": "between", "value": [3, 9.5]},
            {"field": "amount", "op": "gte", "value": 3},
        ],
        "exclude": ["st"],
        "order_by": "change_pct desc",
    },
    {
        "code": "pullback",
        "name": "强势回踩",
        "description": "前期涨停过的个股今日回调，换手仍在",
        "conditions": [
            {"field": "change_pct", "op": "between", "value": [-6, 2]},
            {"field": "limit_up_count_5d", "op": "gte", "value": 1},
            {"field": "turnover_rate", "op": "gte", "value": 4},
        ],
        "exclude": ["st"],
        "order_by": "change_pct desc",
    },
    {
        "code": "small_active",
        "name": "小市值活跃",
        "description": "流通市值 100 亿以内，换手与成交额同时放量",
        "conditions": [
            {"field": "float_mv", "op": "lte", "value": 100},
            {"field": "turnover_rate", "op": "gte", "value": 5},
            {"field": "amount", "op": "gte", "value": 2},
        ],
        "exclude": ["st"],
        "order_by": "amount desc",
    },
]

DISCLAIMER = "选股结果由规则计算得出，仅供研究参考，不构成投资建议。"
