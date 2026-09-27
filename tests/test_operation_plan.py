"""每日操作计划:情景归一化 + 价位→提醒条件映射。

这两个纯函数是整条链路里最容易出错、后果也最严重的地方 —— 它们把 AI 给的数字
变成真实的提醒阈值。数字错了,用户会被错误的价位叫醒或该叫醒时没叫醒,而这是个
用来约束交易纪律的功能,错价位的伤害大于没有价位。所以校验要严,用例要密。
"""
import pytest

from src.modules.automation.operation_plan import (
    QUANT_FIELDS,
    normalize_discipline,
    normalize_scenarios,
)


def _scenario(**over):
    """一份合法的情景基线,按需覆盖字段。"""
    base = {
        "title": "情景一：回踩7.45-7.60缩量企稳",
        "condition": "平开或小幅低开后回踩7.45-7.60区间，缩量止跌",
        "action_type": "buy",
        "action": "轻仓试仓，分批买入",
        "position": "总仓位1/3",
        "buy_price_range": "7.45-7.62元",
        "stop_loss": "7.20元（跌破7月低点平台）",
        "target1": "8.05-8.25元",
        "target2": "8.60-8.80元",
        "strategy": "盈亏比约3:1",
        "is_best": True,
        "entry_low": 7.45,
        "entry_high": 7.62,
        "stop_loss_num": 7.20,
        "target1_low": 8.05,
        "target1_high": 8.25,
        "target2_low": 8.60,
        "target2_high": 8.80,
    }
    base.update(over)
    return base


class TestNormalizeScenarios:
    def test_keeps_valid_scenario(self):
        out = normalize_scenarios([_scenario()])
        assert len(out) == 1
        s = out[0]
        assert s["title"] == "情景一：回踩7.45-7.60缩量企稳"
        assert s["action_type"] == "buy"
        assert s["is_best"] is True
        assert s["entry_low"] == 7.45 and s["entry_high"] == 7.62
        assert s["stop_loss_num"] == 7.20
        assert s["target1_low"] == 8.05 and s["target2_high"] == 8.80

    def test_non_list_and_garbage_return_empty(self):
        assert normalize_scenarios(None) == []
        assert normalize_scenarios("not a list") == []
        assert normalize_scenarios({}) == []
        assert normalize_scenarios([None, 1, "x"]) == []

    def test_string_numbers_are_coerced(self):
        """模型经常把数字写成字符串,必须能吃下。"""
        out = normalize_scenarios([_scenario(entry_low="7.45", stop_loss_num="7.2")])
        assert out[0]["entry_low"] == 7.45
        assert out[0]["stop_loss_num"] == 7.2

    def test_invalid_numbers_become_zero(self):
        """0 表示"该价位不适用",绝不能让 NaN/None/负值混进提醒阈值。"""
        out = normalize_scenarios(
            [_scenario(stop_loss_num="abc", target1_low=None, target2_high=-3)]
        )
        s = out[0]
        assert s["stop_loss_num"] == 0
        assert s["target1_low"] == 0
        assert s["target2_high"] == 0

    def test_scenario_without_any_price_is_dropped(self):
        """一个价位都没有的情景对"加提醒"毫无价值,丢弃而不是留个空壳。"""
        out = normalize_scenarios([_scenario(**{f: 0 for f in QUANT_FIELDS})])
        assert out == []

    def test_reversed_entry_range_is_swapped(self):
        """上下限写反会生成永远不成立的区间,自动纠正。"""
        out = normalize_scenarios([_scenario(entry_low=7.62, entry_high=7.45)])
        assert out[0]["entry_low"] == 7.45
        assert out[0]["entry_high"] == 7.62

    def test_reversed_target_range_is_swapped(self):
        out = normalize_scenarios(
            [_scenario(target1_low=8.25, target1_high=8.05, target2_low=8.8, target2_high=8.6)]
        )
        assert out[0]["target1_low"] == 8.05 and out[0]["target1_high"] == 8.25
        assert out[0]["target2_low"] == 8.60 and out[0]["target2_high"] == 8.80

    def test_unknown_action_type_falls_back_to_observe(self):
        out = normalize_scenarios([_scenario(action_type="随便买")])
        assert out[0]["action_type"] == "observe"

    def test_missing_title_gets_positional_name(self):
        out = normalize_scenarios([_scenario(title=""), _scenario(title="")])
        assert [s["title"] for s in out] == ["情景1", "情景2"]

    def test_unknown_fields_are_stripped(self):
        """模型的即兴字段不该透到前端/数据库。"""
        out = normalize_scenarios([_scenario(secret="x", confidence=0.9)])
        assert "secret" not in out[0] and "confidence" not in out[0]

    def test_capped_at_four_scenarios(self):
        out = normalize_scenarios([_scenario(title=f"情景{i}") for i in range(8)])
        assert len(out) == 4

    def test_mixed_valid_and_invalid_keeps_the_valid(self):
        out = normalize_scenarios(
            [_scenario(title="好的"), {"title": "空的"}, _scenario(title="也好的")]
        )
        assert [s["title"] for s in out] == ["好的", "也好的"]


class TestNormalizeDiscipline:
    def test_pairs(self):
        out = normalize_discipline(
            [{"principle": "仓位控制", "detail": "首次建仓不超过1/3"}]
        )
        assert out == [{"principle": "仓位控制", "detail": "首次建仓不超过1/3"}]

    def test_plain_strings_are_accepted(self):
        """模型有时只给一句话,兼容成 detail。"""
        out = normalize_discipline(["严格止损", "不追高"])
        assert out == [
            {"principle": "", "detail": "严格止损"},
            {"principle": "", "detail": "不追高"},
        ]

    def test_empty_entries_dropped(self):
        assert normalize_discipline([{}, {"principle": "", "detail": ""}, None]) == []
        assert normalize_discipline(None) == []
        assert normalize_discipline("nope") == []


# ---------------------------------------------------------------------------
# 价位 → 提醒条件
# ---------------------------------------------------------------------------

class TestPlanLevelCondition:
    """把情景里的价位转成 PriceAlertRule 的 condition_group。"""

    @staticmethod
    def _build(level, scenario):
        from src.modules.market.api.price_alerts import _plan_level_condition

        return _plan_level_condition(level, scenario)

    def test_stop_loss_is_less_or_equal(self):
        cond, desc, reason = self._build("stop_loss", {"stop_loss_num": 7.2})
        assert reason == ""
        assert cond == {"op": "and", "items": [{"type": "price", "op": "<=", "value": 7.2}]}
        assert desc == "7.2"

    def test_target_uses_lower_bound(self):
        """目标是个区间时,以"进入目标区"为判定 —— 取下沿,不是上沿。"""
        cond, desc, reason = self._build(
            "target1", {"target1_low": 8.05, "target1_high": 8.25}
        )
        assert reason == ""
        assert cond["items"][0] == {"type": "price", "op": ">=", "value": 8.05}
        assert desc == "8.05-8.25"

    def test_target_falls_back_to_upper_when_lower_missing(self):
        cond, desc, _ = self._build("target1", {"target1_low": 0, "target1_high": 8.25})
        assert cond["items"][0]["value"] == 8.25
        assert desc == "8.25"

    def test_entry_is_between(self):
        cond, desc, reason = self._build("entry", {"entry_low": 7.45, "entry_high": 7.62})
        assert reason == ""
        assert cond == {
            "op": "and",
            "items": [{"type": "price", "op": "between", "value": [7.45, 7.62]}],
        }
        assert desc == "7.45-7.62"

    @pytest.mark.parametrize(
        "level,scenario",
        [
            ("stop_loss", {"stop_loss_num": 0}),
            ("target1", {"target1_low": 0, "target1_high": 0}),
            ("target2", {"target2_low": 0, "target2_high": 0}),
            ("entry", {"entry_low": 0, "entry_high": 7.6}),
            ("entry", {"entry_low": 7.4, "entry_high": 0}),
        ],
    )
    def test_missing_price_is_rejected_with_reason(self, level, scenario):
        cond, _, reason = self._build(level, scenario)
        assert cond is None
        assert reason, "拒绝必须带原因,否则用户不知道哪一项被跳过了"


class TestConditionAlreadyTrue:
    """判定"提醒条件当前是否已成立"。

    提醒引擎是状态判定,条件一成立下一轮扫描就触发。这个标注让界面能提前告诉用户
    "这条加进去马上就会响",而不是等他被误报惊到才发现。
    """

    @staticmethod
    def _check(condition, price):
        from src.modules.market.api.price_alerts import _condition_already_true

        return _condition_already_true(condition, price)

    def test_stop_loss_below_price_is_not_true(self):
        """正常止损(低于现价):现在不成立,等跌下来才响。"""
        cond = {"op": "and", "items": [{"type": "price", "op": "<=", "value": 7.2}]}
        assert self._check(cond, 7.66) is False

    def test_stop_loss_above_price_is_already_true(self):
        """移动止损高于现价(突破后才生效):条件已然成立,加进去会立刻误报。"""
        cond = {"op": "and", "items": [{"type": "price", "op": "<=", "value": 8.25}]}
        assert self._check(cond, 7.66) is True

    def test_price_already_below_stop_is_true(self):
        """已跌穿止损:确实该被叫醒,标注为已成立是正确的。"""
        cond = {"op": "and", "items": [{"type": "price", "op": "<=", "value": 7.2}]}
        assert self._check(cond, 7.0) is True

    def test_target_above_price_is_not_true(self):
        cond = {"op": "and", "items": [{"type": "price", "op": ">=", "value": 8.05}]}
        assert self._check(cond, 7.66) is False

    def test_target_below_price_is_already_true(self):
        cond = {"op": "and", "items": [{"type": "price", "op": ">=", "value": 8.05}]}
        assert self._check(cond, 8.5) is True

    def test_between_band(self):
        cond = {"op": "and", "items": [{"type": "price", "op": "between", "value": [7.45, 7.62]}]}
        assert self._check(cond, 7.5) is True
        assert self._check(cond, 7.66) is False

    def test_unknown_price_returns_false(self):
        """取不到现价时不做判断,不能因为行情抖动就误标。"""
        cond = {"op": "and", "items": [{"type": "price", "op": "<=", "value": 7.2}]}
        assert self._check(cond, None) is False

    def test_malformed_condition_returns_false(self):
        assert self._check({}, 7.66) is False
        assert self._check({"items": []}, 7.66) is False
        assert self._check({"items": [{"op": "<=", "value": "abc"}]}, 7.66) is False
