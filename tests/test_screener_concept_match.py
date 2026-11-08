"""选股「所属概念」条件的名称归一与未命中提示。"""
from src.modules.screener.engine import _concept_warnings, _match, _norm_concept


def _row(*names):
    return {"concepts": [{"name": n} for n in names]}


def test_norm_strips_source_suffix():
    assert _norm_concept("光伏概念") == "光伏"
    assert _norm_concept("光伏") == "光伏"
    assert _norm_concept("储能板块") == "储能"
    assert _norm_concept("  ") == ""


def test_in_matches_across_source_naming():
    # 用户在通达信口径里选「光伏」,索引是东财「光伏概念」
    assert _match(_row("光伏概念"), {"field": "concept", "op": "in", "value": ["光伏"]})
    assert _match(_row("光伏"), {"field": "concept", "op": "in", "value": ["光伏概念"]})
    # 不同板块不能误匹配
    assert not _match(_row("储能概念"), {"field": "concept", "op": "in", "value": ["光伏"]})


def test_not_in_and_contains():
    cond = {"field": "concept", "op": "not_in", "value": ["光伏"]}
    assert _match(_row("储能概念"), cond)
    assert not _match(_row("光伏概念"), cond)
    assert _match(_row("光伏概念"), {"field": "concept", "op": "contains", "value": "光伏"})
    assert _match(_row("光伏概念"), {"field": "concept", "op": "contains", "value": "光伏概念"})


def test_warnings_only_for_unknown_names():
    concept_map = {"000001": [{"name": "光伏概念"}], "000002": [{"name": "储能概念"}]}

    assert _concept_warnings([], concept_map) == []
    # 后缀差异已归一,不应告警
    assert _concept_warnings(
        [{"field": "concept", "op": "in", "value": ["光伏"]}], concept_map
    ) == []

    warnings = _concept_warnings(
        [{"field": "concept", "op": "in", "value": ["半导体"]}], concept_map
    )
    assert len(warnings) == 1
    assert "半导体" in warnings[0]
