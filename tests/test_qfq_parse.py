"""前复权日K解析与代码映射。"""
from src.platform.dataset.qfq import parse_qfq_payload, tencent_code


def test_tencent_code_mapping():
    assert tencent_code("600519") == "sh600519"
    assert tencent_code("000001") == "sz000001"
    assert tencent_code("300750") == "sz300750"
    assert tencent_code("688981") == "sh688981"
    assert tencent_code("920085") == "bj920085"
    assert tencent_code("") == ""


def test_parse_qfq_payload_orders_and_skips_bad_rows():
    payload = {
        "data": {
            "sh600519": {
                "qfqday": [
                    ["2026-11-05", "1440.0", "1450.0", "1460.0", "1430.0", "40000"],
                    ["2026-11-06", "1450.0", "1453.0", "1457.7", "1436.0", "42848"],
                    ["bad-date", "1", "2", "3", "4", "5"],
                    ["2026-11-04", "1", "2"],
                ]
            }
        }
    }
    bars = parse_qfq_payload(payload, "sh600519")
    assert len(bars) == 2
    assert bars[-1]["trade_date"] == "2026-11-06"
    assert bars[-1]["close"] == 1453.0
    assert bars[-1]["volume"] == 42848.0


def test_parse_qfq_payload_missing_symbol():
    assert parse_qfq_payload({"data": {}}, "sh600519") == []
