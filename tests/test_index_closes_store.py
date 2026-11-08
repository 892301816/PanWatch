"""指数收盘留存:同日同价重复写入不落盘。"""
import json
import time

import src.modules.market.api.market as mkt


def test_merge_closes_skips_unchanged(tmp_path, monkeypatch):
    """指数接口每分钟刷新一次,8 个指数不能每次都重写文件。"""
    path = tmp_path / "index_closes.json"
    monkeypatch.setattr(mkt, "_closes_path", lambda: path)

    mkt._merge_closes("N225", [("2026-11-09", 100.0)])
    first = path.stat().st_mtime_ns
    time.sleep(0.01)
    mkt._merge_closes("N225", [("2026-11-09", 100.0)])
    assert path.stat().st_mtime_ns == first

    mkt._merge_closes("N225", [("2026-11-09", 101.0)])
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["N225"] == [["2026-11-09", 101.0]]
