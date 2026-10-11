"""消息分类的纯函数单测。

分类是关键词规则,词表会随上游措辞变化而需要维护 —— 这些用例同时充当
"规则意图"的文档:哪些消息应该落在哪个桶。
"""
from __future__ import annotations

from src.modules.market.message_service import (
    CATEGORY_OTHER,
    classify_category,
    compute_importance,
)


def test_classify_geopolitics_beats_policy_keyword():
    """「美联储降息」同时含政策词「降息」,但讲的是海外的事 —— 地理优先级更高,不能归错桶。"""
    assert classify_category("美联储官员称可能降息") == "海外地缘"
    assert classify_category("央行宣布降准0.5个百分点") == "政策监管"


def test_classify_core_buckets():
    assert classify_category("国务院印发关于促进消费的意见") == "政策监管"
    assert classify_category("9月CPI同比上涨0.4%") == "宏观数据"
    assert classify_category("某公司：股东拟减持不超过2%股份") == "公司事件"
    assert classify_category("某公司固态电池产品实现量产") == "行业催化"


def test_classify_falls_back_to_other():
    """自然灾害/事故类不该被硬塞进任何投资分类。"""
    assert classify_category("尼泊尔中北部地区突发洪水 5人失踪") == CATEGORY_OTHER
    assert classify_category("广东汕头通报一厂房起火：明火基本扑灭") == CATEGORY_OTHER
    assert classify_category("") == CATEGORY_OTHER


def test_classify_matches_content_not_only_title():
    assert classify_category("午后异动", "该公司公告称拟收购某标的") == "公司事件"


def test_importance_keyword_boost():
    # 高影响词直接顶到 3
    assert compute_importance("重磅：某政策落地") == 3
    assert compute_importance("突发：某公司被立案") == 3
    # 中影响词至少 1
    assert compute_importance("某公司发布业绩公告") >= 1
    # 源自带的重要性要保留
    assert compute_importance("普通消息", 2) == 2
    # 源自带的更高时不被关键词压低
    assert compute_importance("重磅消息", 3) == 3
    # 都没有则为 0
    assert compute_importance("平平无奇的一条消息") == 0


def test_importance_clamped():
    assert compute_importance("普通", 99) == 3
    assert compute_importance("普通", -5) == 0
