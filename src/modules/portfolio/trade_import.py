"""交割单/对账单文件解析（CSV / TSV / HTML 伪装 xls / xlsx / xls）。

只做「表格 → 统一字段」这一步，不碰数据库：

- 自动识别分隔符、编码（UTF-8 / GBK）与表头行（券商导出常带几行账户信息）；
- 按中文列名同义词映射到交易字段；
- 撤单/废单/申购/分红等非成交行标记为跳过，交给上层决定是否展示；
- xlsx/xls 依赖 openpyxl/xlrd，缺库时给出可读错误，不阻塞 CSV。

统一的交易字段::

    {action, symbol, name, quantity, price, trade_date, time, fees, external_id, amount}
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date as date_cls
from html.parser import HTMLParser

# 列名同义词（顺序有意义：越具体的放前面）
_HEADER_SYNONYMS: dict[str, list[str]] = {
    "external_id": ["成交编号", "合同编号", "流水号", "申请编号", "委托编号", "成交序号"],
    "trade_date": ["成交日期", "交易日期", "发生日期", "成交日", "日期"],
    "trade_time": ["成交时间", "委托时间", "时间"],
    "symbol": ["证券代码", "股票代码", "证券编码", "代码"],
    "name": ["证券名称", "股票名称", "证券简称", "名称", "简称"],
    "action": ["买卖标志", "买卖方向", "操作", "交易类别", "业务名称", "方向", "摘要"],
    "quantity": ["成交数量", "成交股数", "成交量", "股数", "数量"],
    "price": ["成交价格", "成交均价", "成交价", "价格"],
    "amount": ["成交金额", "发生金额", "成交额", "金额"],
    "commission": ["佣金", "净佣金", "手续费"],
    "stamp": ["印花税"],
    "transfer": ["过户费"],
    "other": ["规费", "其他费用", "经手费", "证管费"],
    "fee_total": ["费用合计", "总费用", "手续费合计"],
}

_SKIP_WORDS = ("撤单", "废单", "已撤", "撤废", "申购", "赎回", "中签", "配号", "分红", "红利", "送股", "转增", "缴款", "新股")

_BUY_WORDS = ("证券买入", "担保品买入", "融资买入", "普通买入", "买入", "买")
_SELL_WORDS = ("证券卖出", "担保品卖出", "融券卖出", "普通卖出", "卖出", "卖")


class _TableParser(HTMLParser):
    """把 HTML 表格收成二维数组（兼容券商导出的「伪装 xls」）。"""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._rows: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._rows = []
        elif tag == "tr" and self._rows is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None and self._rows is not None:
            if any(c for c in self._row):
                self._rows.append(self._row)
            self._row = None
        elif tag == "table" and self._rows is not None:
            if self._rows:
                self.tables.append(self._rows)
            self._rows = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _decode(content: bytes | str) -> str:
    if isinstance(content, str):
        return content
    for encoding in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _split_table(text: str) -> list[list[str]]:
    """CSV/TSV → 二维数组；失败时按行拆。"""
    sample = text[:4096]
    delimiter = ","
    for candidate in (",", "\t", ";"):
        if sample.count(candidate) > sample.count(delimiter):
            delimiter = candidate
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error:
        rows = [line.split(delimiter) for line in text.splitlines()]
    return [[cell.strip() for cell in row] for row in rows]


def _read_xlsx(content: bytes) -> list[list[str]]:
    """xlsx = zip + XML，优先用标准库解析（部署镜像不一定装 openpyxl）。

    兼容共享字符串（t="s"）与内联字符串（t="inlineStr"）；只取单元格值，
    样式/公式不展开——券商导出的成交表通常都是值。
    """
    try:
        return _read_xlsx_stdlib(content)
    except Exception:  # noqa: BLE001 — 标准库失败再试 openpyxl
        pass
    try:
        from openpyxl import load_workbook  # type: ignore
    except ImportError as exc:  # pragma: no cover - 部署环境可能没装
        raise ValueError("无法解析 xlsx（标准库解析失败且缺少 openpyxl），请导出 CSV") from exc
    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    rows: list[list[str]] = []
    for sheet in wb.worksheets:
        for row in sheet.iter_rows(values_only=True):
            rows.append(["" if v is None else str(v).strip() for v in row])
    return rows


def _read_xlsx_stdlib(content: bytes) -> list[list[str]]:
    import xml.etree.ElementTree as ET
    import zipfile

    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = set(archive.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall(f"{ns}si"):
                shared.append("".join(node.text or "" for node in item.iter(f"{ns}t")))
        sheets = sorted(
            name
            for name in names
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        )
        rows: list[list[str]] = []
        for name in sheets:
            root = ET.fromstring(archive.read(name))
            for row in root.findall(f".//{ns}sheetData/{ns}row"):
                values: list[str] = []
                for cell in row.findall(f"{ns}c"):
                    kind = cell.get("t")
                    value = cell.find(f"{ns}v")
                    text = value.text if value is not None else ""
                    if kind == "s" and text:
                        try:
                            text = shared[int(text)]
                        except (ValueError, IndexError):
                            text = ""
                    elif kind == "inlineStr":
                        text = "".join(node.text or "" for node in cell.iter(f"{ns}t"))
                    values.append(str(text or ""))
                rows.append(values)
        return rows


def _read_xls(content: bytes) -> list[list[str]]:
    try:
        import xlrd  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise ValueError("当前环境缺少 xlrd，无法解析 xls，请导出 CSV") from exc
    book = xlrd.open_workbook(file_contents=content)
    rows: list[list[str]] = []
    for sheet in book.sheets():
        for index in range(sheet.nrows):
            rows.append([str(v).strip() for v in sheet.row_values(index)])
    return rows


def _normalize_header(value: str) -> str:
    return re.sub(r"[\s*:：()（）]", "", str(value or ""))


def _find_header(rows: list[list[str]]) -> tuple[int, dict[str, int]] | None:
    """在前 30 行里找表头：至少命中 3 个已知列名。"""
    for index, row in enumerate(rows[:30]):
        mapping: dict[str, int] = {}
        normalized = [_normalize_header(cell) for cell in row]
        for key, synonyms in _HEADER_SYNONYMS.items():
            for col, header in enumerate(normalized):
                if not header:
                    continue
                if any(header == syn or syn in header for syn in synonyms):
                    mapping.setdefault(key, col)
                    break
        if len(mapping) >= 3 and ("symbol" in mapping or "name" in mapping):
            return index, mapping
    return None


def _cell(row: list[str], mapping: dict[str, int], key: str) -> str:
    col = mapping.get(key)
    if col is None or col >= len(row):
        return ""
    return str(row[col] or "").strip()


def _to_float(value: str) -> float | None:
    if not value:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", str(value))
    try:
        return float(cleaned)
    except ValueError:
        return None


def _normalize_date(value: str, fallback: date_cls) -> str:
    text = str(value or "").strip()
    m = re.search(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})", text)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"^(20\d{2})(\d{2})(\d{2})$", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # Excel 日期序列号（1900 起），常见于 xlsx 导出
    if re.fullmatch(r"\d{5}(?:\.\d+)?", text):
        serial = float(text)
        if 20000 <= serial <= 80000:
            from datetime import timedelta

            base = date_cls(1899, 12, 30)
            return (base + timedelta(days=int(serial))).isoformat()
    return fallback.isoformat()


def _normalize_time(value: str) -> str:
    m = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", str(value or ""))
    if not m:
        return ""
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return ""
    return f"{hour:02d}:{minute:02d}:{int(m.group(3) or 0):02d}"


def _normalize_symbol(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return ""
    # Excel 会把 002943 存成 2943；按 A 股 6 位补零（港股 5 位另算）
    if len(digits) <= 6:
        return digits.zfill(6)
    return digits[-6:]


def _normalize_action(value: str, quantity: float | None) -> tuple[str | None, str]:
    text = str(value or "")
    if any(word in text for word in _SKIP_WORDS):
        return None, "非成交记录（撤单/申购/分红等）"
    for word in _SELL_WORDS:
        if word in text:
            return "sell", ""
    for word in _BUY_WORDS:
        if word in text:
            return "buy", ""
    if quantity is not None and quantity < 0:
        return "sell", ""
    return None, "未识别买卖方向"


def parse_statement(
    content: bytes | str,
    *,
    filename: str = "",
    today: date_cls | None = None,
) -> list[dict]:
    """解析交割单，返回逐行结果（ok=False 的行带 reason）。"""
    today = today or date_cls.today()
    text = _decode(content)

    if isinstance(content, bytes) and content[:2] == b"PK":
        rows = _read_xlsx(content)
    elif isinstance(content, bytes) and content[:4] == b"\xd0\xcf\x11\xe0":
        rows = _read_xls(content)
    elif "<table" in text.lower():
        parser = _TableParser()
        parser.feed(text)
        rows = parser.tables[0] if parser.tables else []
    else:
        rows = _split_table(text)

    if not rows:
        return []

    found = _find_header(rows)
    if found is None:
        raise ValueError("没有识别到表头（需要包含证券代码/证券名称/买卖标志/成交数量/成交价格等列）")
    header_index, mapping = found

    results: list[dict] = []
    for offset, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        if not any(str(cell).strip() for cell in row):
            continue
        if any("合计" in str(cell) for cell in row):
            continue

        action, reason = _normalize_action(_cell(row, mapping, "action"), _to_float(_cell(row, mapping, "quantity")))
        quantity_raw = _to_float(_cell(row, mapping, "quantity"))
        price = _to_float(_cell(row, mapping, "price"))
        amount = _to_float(_cell(row, mapping, "amount"))
        quantity = int(abs(quantity_raw)) if quantity_raw is not None else 0
        if price is None and amount is not None and quantity > 0:
            price = round(abs(amount) / quantity, 4)

        fees = {
            "commission": abs(_to_float(_cell(row, mapping, "commission")) or 0.0),
            "stamp_duty": abs(_to_float(_cell(row, mapping, "stamp")) or 0.0),
            "transfer_fee": abs(_to_float(_cell(row, mapping, "transfer")) or 0.0),
            "other_fee": abs(_to_float(_cell(row, mapping, "other")) or 0.0),
        }
        fee_total = abs(_to_float(_cell(row, mapping, "fee_total")) or 0.0)
        fees["total"] = round(fee_total if fee_total > 0 else sum(fees.values()), 2)

        item = {
            "line": offset,
            "raw": " | ".join(str(cell) for cell in row if str(cell).strip()),
            "ok": False,
            "reason": "",
            "action": action,
            "symbol": _normalize_symbol(_cell(row, mapping, "symbol")),
            "name": _cell(row, mapping, "name"),
            "quantity": quantity,
            "price": float(price or 0.0),
            "amount": abs(amount) if amount is not None else round(quantity * float(price or 0.0), 2),
            "trade_date": _normalize_date(
                _cell(row, mapping, "trade_date") or _cell(row, mapping, "trade_time"), today
            ),
            "time": _normalize_time(_cell(row, mapping, "trade_time")),
            "fees": {**fees, "total": round(fees["total"], 2)},
            "external_id": _cell(row, mapping, "external_id"),
            "warnings": [],
        }
        if action is None:
            item["reason"] = reason
            results.append(item)
            continue
        if not item["symbol"]:
            item["reason"] = "缺少证券代码"
            results.append(item)
            continue
        if item["quantity"] <= 0:
            item["reason"] = "缺少成交数量"
            results.append(item)
            continue
        if item["price"] <= 0:
            item["reason"] = "缺少成交价格"
            results.append(item)
            continue
        item["ok"] = True
        results.append(item)

    return results
