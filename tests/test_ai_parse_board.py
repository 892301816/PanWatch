"""AI 解析:上市板块字段与「概念里写板块名」的自动纠正。"""
from src.modules.screener.ai_parse import _validate_condition


def test_concept_board_name_is_converted_to_board_field():
    warnings: list[str] = []
    cond = _validate_condition(
        {"field": "concept", "op": "not_in", "value": ["创业板"]}, warnings
    )
    assert cond == {"field": "board", "op": "not_in", "value": "创业板"}
    assert warnings == []


def test_board_field_accepts_text_value():
    warnings: list[str] = []
    cond = _validate_condition(
        {"field": "board", "op": "ne", "value": "创业板"}, warnings
    )
    assert cond == {"field": "board", "op": "ne", "value": "创业板"}
    assert warnings == []
