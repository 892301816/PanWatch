"""概念索引来源持久化:落盘/读取/裁剪/口径判断。"""
import json

import src.modules.screener.concepts as concepts


def _use_tmp(monkeypatch, tmp_path):
    path = tmp_path / "concept_index_source.json"
    monkeypatch.setattr(concepts, "_source_path", lambda: path)
    return path


def test_source_roundtrip(tmp_path, monkeypatch):
    path = _use_tmp(monkeypatch, tmp_path)
    concepts._write_index_source("2026-11-07", "tdx", 255, 255, 43274)

    assert concepts._read_index_source("2026-11-07") == "tdx"
    assert concepts._read_index_source("2026-11-08") == ""

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["2026-11-07"]["rows"] == 43274
    assert data["2026-11-07"]["ok_boards"] == 255


def test_source_keeps_only_recent_days(tmp_path, monkeypatch):
    path = _use_tmp(monkeypatch, tmp_path)
    for day in range(1, 13):
        concepts._write_index_source(f"2026-11-{day:02d}", "eastmoney", 255, 255, 38000)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data) == concepts._SOURCE_KEEP_DAYS
    assert "2026-11-01" not in data
    assert "2026-11-12" in data


def test_source_ignores_broken_file(tmp_path, monkeypatch):
    path = _use_tmp(monkeypatch, tmp_path)
    path.write_text("{not json", encoding="utf-8")

    assert concepts._read_index_source("2026-11-07") == ""
    # 写入仍能覆盖坏文件
    concepts._write_index_source("2026-11-07", "push2", 1, 1, 1)
    assert concepts._read_index_source("2026-11-07") == "push2"


def test_source_family_mismatch():
    # 东财两主机之间切换不算换族
    assert not concepts._source_family_mismatch("eastmoney", "push2")
    assert not concepts._source_family_mismatch("push2", "eastmoney")
    # 东财 <-> 通达信 必须重建
    assert concepts._source_family_mismatch("eastmoney", "tdx")
    assert concepts._source_family_mismatch("tdx", "push2")
    # 任一边没有记录时不贸然重建
    assert not concepts._source_family_mismatch("", "tdx")
    assert not concepts._source_family_mismatch("tdx", "")
