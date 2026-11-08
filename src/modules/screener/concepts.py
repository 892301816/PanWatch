"""概念板块与个股 → 概念反查索引。

数据源主用东财,东财(两个主机)都不可用时降级到通达信板块文件,见文末
``_fetch_boards_tdx`` / ``tdx_blocks``。

东财 ``push2`` 的 clist 在本机/容器网络会被 reset(见 snapshot.py 文档),
但 ``data.eastmoney.com/dataapi/bkzj/getbkzj`` 可用。bkzj 的语义是
「key 决定返回哪个指标、code 决定范围」:

- ``code=m:90+t:3`` → 全部概念板块(约 516 个),``key=f3`` 给涨跌幅、``key=f62`` 给主力净流入;
- ``code=b:BKxxxx`` → 该板块成分股(实测单个概念可返回 200+,上限约 600)。

因此建索引 = 1 次板块榜(f3) + 1 次资金榜(f62) + N 次成分股。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
from sqlalchemy import delete, func, text

from src.modules.screener import tdx_blocks
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import ScreenerConceptMember

logger = logging.getLogger(__name__)

_BKZJ_URL = "https://data.eastmoney.com/dataapi/bkzj/getbkzj"
_CONCEPT_SCOPE = "m:90+t:3"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://data.eastmoney.com/",
}

_MAX_BOARDS = 300
_DEFAULT_BOARDS = 255
THEME_BOARD_LIMIT = 240
STYLE_BOARD_LIMIT = 15
# 一次完整构建大约每板块 120~180 条归属;低于这个数说明大概率构建不完整
_MIN_GOOD_ROWS = 15000
_MEM: dict = {"trade_date": "", "map": None}
_BOARDS_MEM: dict = {"ts": 0.0, "items": [], "source": ""}
# 东财可用时的源名,用于判断是否需要从通达信兜底切回来
_EASTMONEY_SOURCES = ("eastmoney", "push2")
_BOARDS_TTL_S = 300.0
_LOCK = threading.Lock()
_BUILDING = threading.Event()
_LAST_BUILD: dict = {}
_LAST_ATTEMPT = 0.0


def _num(value) -> float | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bkzj(key: str, code: str, *, timeout: float = 20.0) -> list[dict]:
    resp = httpx.get(
        _BKZJ_URL, params={"key": key, "code": code}, headers=_HEADERS, timeout=timeout
    )
    resp.raise_for_status()
    data = (resp.json() or {}).get("data") or {}
    diff = data.get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    return [x for x in diff if isinstance(x, dict)]


def _fetch_boards_bkzj() -> list[dict]:
    """data.eastmoney.com/bkzj:三次请求(f3 涨幅 / f62 净流入 / f6 成交额)。"""
    try:
        pct_rows = _bkzj("f3", _CONCEPT_SCOPE)
        flow_rows = _bkzj("f62", _CONCEPT_SCOPE)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[选股] 概念板块(bkzj)获取失败: {exc}")
        return []
    try:
        amount_rows = _bkzj("f6", _CONCEPT_SCOPE)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[选股] 板块成交额获取失败(排序降级): {exc}")
        amount_rows = []

    flow = {str(r.get("f12") or ""): _num(r.get("f62")) for r in flow_rows}
    amount = {str(r.get("f12") or ""): _num(r.get("f6")) for r in amount_rows}
    boards = []
    for r in pct_rows:
        code = str(r.get("f12") or "").strip()
        if not code:
            continue
        boards.append(
            {
                "code": code,
                "name": str(r.get("f14") or "").strip(),
                "change_pct": round((_num(r.get("f3")) or 0.0) / 100, 2),
                "main_net_inflow": flow.get(code),
                "turnover": amount.get(code),
            }
        )
    return boards


def _fetch_boards_push2() -> list[dict]:
    """push2.eastmoney.com clist:一次请求拿全字段。bkzj 不可用时的备用主机。"""
    try:
        from marketdata.vendors.theme import ThemeVendor

        rows = ThemeVendor().concept_boards(limit=500)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[选股] 概念板块(push2)获取失败: {exc}")
        return []
    return [
        {
            "code": r.code,
            "name": r.name,
            "change_pct": r.change_pct or 0.0,
            "main_net_inflow": r.main_net_inflow,
            "turnover": r.turnover,
        }
        for r in rows
        if r.code
    ]


def _fetch_boards_tdx() -> list[dict]:
    """通达信兜底:板块清单自带概念/风格/指数三类,数值用本地快照现算。

    通达信只给成分股,所以涨跌幅取成分股中位数、成交额取成分股合计;
    主力净流入没有对应数据,留 None(前端不展示该字段)。
    """
    try:
        groups = tdx_blocks.fetch_groups()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[选股] 通达信板块获取失败: {exc}")
        return []
    if not groups.get("theme"):
        return []

    try:
        from src.modules.screener import snapshot as snapshot_mod

        quotes = {
            r["symbol"]: r
            for r in snapshot_mod.get_snapshot()
            if not r.get("is_suspended")
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[选股] 通达信板块强度计算失败(无快照): {exc}")
        quotes = {}

    # 通达信自身放在「概念」文件里、但实际是标签/宽基的板块
    tags = {"通达信88", "ST板块", "含H股", "含B股", "含GDR", "含可转债"}

    boards: list[dict] = []
    for kind, label in (("theme", "概念"), ("style", "风格"), ("index", "指数")):
        for name, codes in (groups.get(kind) or {}).items():
            if name in tags:
                continue
            pcts: list[float] = []
            amount = 0.0
            for code in codes:
                row = quotes.get(code)
                if not row:
                    continue
                pct = row.get("change_pct")
                if pct is not None:
                    pcts.append(float(pct))
                amount += float(row.get("amount") or 0.0)
            pcts.sort()
            change = round(pcts[len(pcts) // 2], 2) if pcts else 0.0
            boards.append(
                {
                    "code": f"tdx:{name}",
                    "name": name,
                    "change_pct": change,
                    "main_net_inflow": None,
                    "turnover": amount,
                    "kind": "theme" if kind == "theme" else kind,
                    "group": label,
                }
            )
    if boards:
        logger.info(f"[选股] 板块源降级为通达信: {len(boards)} 个板块(本地快照计算强度)")
    return boards


def _tdx_lookup(name: str) -> list[str]:
    """按板块名查通达信成分股。

    东财与通达信命名不完全一致(「光伏概念」vs「光伏」),只做后缀剥离后的
    精确匹配,不做模糊匹配 —— 宁可没有,也不要挂错板块。
    """
    try:
        groups = tdx_blocks.fetch_groups()
    except Exception:  # noqa: BLE001
        return []
    for kind in ("theme", "style", "index"):
        codes = (groups.get(kind) or {}).get(name)
        if codes:
            return codes
    stripped = name.strip()
    for suffix in ("概念", "板块", "指数"):
        if stripped.endswith(suffix) and len(stripped) > len(suffix) + 1:
            stripped = stripped[: -len(suffix)]
            break
    if stripped != name.strip():
        for kind in ("theme", "style", "index"):
            codes = (groups.get(kind) or {}).get(stripped)
            if codes:
                return codes
    return []


def fetch_concept_boards() -> list[dict]:
    """概念板块榜(涨幅 + 主力净流入),按主力净流入降序。

    bkzj 一次只返回一个指标,所以涨跌幅和资金要各取一次再按代码合并。
    结果在进程内缓存 5 分钟(天梯/板块榜/索引三处共用)。

    数据源顺序:东财 bkzj → 东财 push2 → 通达信。
    """
    import time

    if _BOARDS_MEM["items"] and time.monotonic() - _BOARDS_MEM["ts"] <= _BOARDS_TTL_S:
        return _BOARDS_MEM["items"]

    source = "eastmoney"
    boards = _fetch_boards_bkzj()
    if not boards:
        source = "push2"
        boards = _fetch_boards_push2()
    if not boards:
        source = "tdx"
        boards = _fetch_boards_tdx()
    if not boards:
        logger.warning("[选股] 概念板块获取失败(东财与通达信均不可用)")
        return []
    if source == "tdx":
        # 没有主力净流入,按成交额排序
        boards.sort(key=lambda b: (b.get("turnover") or 0.0), reverse=True)
    else:
        boards.sort(key=lambda b: (b.get("main_net_inflow") or 0.0), reverse=True)
    _BOARDS_MEM.update(ts=time.monotonic(), items=boards, source=source)
    if source != "tdx":
        # 东财正常时也悄悄预热通达信板块名,给「风格/主题」判定多一份判据
        tdx_blocks.warm_async()
    return boards


# ── 板块分层 ─────────────────────────────────────────────────────────────
# 东财 531 个「概念板块」里混着大量选股条件式板块(新高/破净/红利/昨日连板…),
# 它们资金体量天然比单个题材大,按净流入排序会长期霸榜,把真正的题材挤出索引。
# 这里按名称把它们分成 主题 / 风格 / 指数 三类:主题进个股归属,风格只做风格榜,
# 指数类(宽基成分)两边都不进。
_INDEX_PATTERNS = (
    "中证", "上证50", "上证180", "上证380", "深证", "深成", "创业板综", "科创50",
    "沪深300", "HS300", "标准普尔", "标普", "MSCI", "富时", "AH股", "GDR", "成份", "成分",
)

_STYLE_WORDS = (
    "新高", "新低", "涨停", "跌停", "连板", "打板", "打二板", "破净", "破发",
    "破增发", "红利", "高股息", "低市净率", "高市盈率", "低市盈率", "市净率",
    "小盘", "中盘", "大盘", "微盘", "低价", "高价", "百元", "次新", "ST", "摘帽",
    "预增", "预减", "预亏", "扭亏", "亏损", "送转", "填权", "举牌", "重仓",
    "融资融券", "沪股通", "深股通", "昨日", "今日", "股权激励", "增持", "回购",
    "减持", "解禁", "市值", "市盈率", "价值股", "题材股", "证金持股", "基金",
    "保险", "社保", "QFII", "风格", "龙虎榜", "游资", "机构",
    "微利", "首亏", "中报", "年报", "季报", "预盈",
)


def classify_board(name: str) -> str:
    """板块类型: ``theme`` / ``style`` / ``index``。

    先判指数(宽基成分),再判风格(状态/资金/估值标签),其余算主题。
    """
    text = (name or "").strip()
    if not text:
        return "style"
    if any(k in text for k in _INDEX_PATTERNS):
        return "index"
    # 通达信自带风格分类,数据在内存时优先用它(词表容易漏词)
    if text in tdx_blocks.cached_style_names():
        return "style"
    if any(k in text for k in _STYLE_WORDS):
        return "style"
    return "theme"


def select_boards(boards: list[dict]) -> list[dict]:
    """挑出要建索引的板块:主题按成交额取前 N,风格按净流入取前 M,指数类丢弃。

    主题按成交额而不是净流入排序 —— 资金流出时(比如今天科技板块)净流入为负,
    按它排序会把光伏/半导体/机器人全挤到最后面。
    """
    themes: list[dict] = []
    styles: list[dict] = []
    for b in boards:
        # 通达信源自带分类,东财源按名称规则分类
        kind = b.get("kind") or classify_board(b.get("name") or "")
        if kind == "theme":
            themes.append(b)
        elif kind == "style":
            styles.append(b)
    themes.sort(key=lambda b: (b.get("turnover") or 0.0), reverse=True)
    styles.sort(key=lambda b: (b.get("main_net_inflow") or 0.0), reverse=True)
    return themes[:THEME_BOARD_LIMIT] + styles[:STYLE_BOARD_LIMIT]


def board_source() -> str:
    """当前板块源:``eastmoney`` / ``push2`` / ``tdx``(空表示还没取过)。"""
    return _BOARDS_MEM.get("source") or ""


def _count_rows(trade_date: str) -> int:
    db = SessionLocal()
    try:
        return int(
            db.query(func.count(ScreenerConceptMember.id))
            .filter(ScreenerConceptMember.trade_date == trade_date)
            .scalar()
            or 0
        )
    finally:
        db.close()


def _store(trade_date: str, rows: list[dict]) -> None:
    db = SessionLocal()
    try:
        db.execute(
            delete(ScreenerConceptMember).where(
                ScreenerConceptMember.trade_date == trade_date
            )
        )
        for start in range(0, len(rows), 1000):
            db.bulk_insert_mappings(ScreenerConceptMember, rows[start:start + 1000])
        db.commit()
    finally:
        db.close()


def build_concept_index(trade_date: str, *, board_limit: int = _DEFAULT_BOARDS) -> int:
    """拉概念榜 + 每个概念的成分股,落成个股→概念反查。返回写入条数。"""
    from src.modules.market.theme_service import is_excluded_concept

    fetch_concept_boards()
    source = _BOARDS_MEM.get("source") or ""
    boards = [
        b for b in (_BOARDS_MEM.get("items") or []) if not is_excluded_concept(b["name"])
    ]
    boards = select_boards(boards)
    if board_limit:
        boards = boards[: max(1, min(int(board_limit), _MAX_BOARDS))]
    if not boards:
        return 0

    def _fetch_members(board: dict) -> list[dict]:
        """单个板块成分股:bkzj 重试 1 次 → push2 主机 → 通达信同名兜底。"""
        if source == "tdx":
            # 通达信源:三个文件一次下载后全在内存,不需要逐板块请求
            return [{"f12": c, "f14": ""} for c in _tdx_lookup(board["name"])]
        code = board["code"]
        for attempt in (1, 2):
            try:
                rows = _bkzj("f3", f"b:{code}")
                if rows:
                    return [r for r in rows if str(r.get("f12") or "").strip()]
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[选股] 概念成分股失败 {code}(bkzj {attempt}): {exc}")
        try:
            from marketdata.vendors.theme import ThemeVendor

            rows = ThemeVendor().concept_stocks(board_code=code, limit=600)
            return [{"f12": r.get("code"), "f14": r.get("name")} for r in rows if r.get("code")]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[选股] 概念成分股失败 {code}(push2): {exc}")
        codes = _tdx_lookup(board["name"])
        if codes:
            logger.debug(f"[选股] 概念成分股改用通达信: {board['name']}")
            return [{"f12": c, "f14": ""} for c in codes]
        return []

    def _members(board: dict) -> tuple[dict, list[dict]]:
        return board, _fetch_members(board)

    out: list[dict] = []
    ok_boards = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for rank, (board, members) in enumerate(pool.map(_members, boards), start=1):
            if members:
                ok_boards += 1
            for m in members:
                out.append(
                    {
                        "trade_date": trade_date,
                        "symbol": str(m.get("f12") or "").strip(),
                        "name": str(m.get("f14") or "").strip(),
                        "concept_code": board["code"],
                        "concept_name": board["name"],
                        "concept_change_pct": board["change_pct"],
                        "concept_rank": rank,
                    }
                )

    _LAST_BUILD.update(
        trade_date=trade_date,
        boards=len(boards),
        ok_boards=ok_boards,
        rows=len(out),
        source=source or "unknown",
        ts=time.time(),
    )
    if ok_boards < len(boards) * 0.6:
        logger.warning(
            f"[选股] 概念索引不完整: 仅 {ok_boards}/{len(boards)} 个板块取到成分股"
        )
    if out:
        _store(trade_date, out)
        _MEM.update(trade_date=trade_date, map=None)
        logger.info(
            f"[选股] 概念索引完成: {ok_boards}/{len(boards)} 个板块 / {len(out)} 条归属"
        )
    return len(out)


def ensure_concept_index(trade_date: str, *, board_limit: int = 0, force: bool = False) -> bool:
    """索引不存在或明显不完整时就地构建(首次打开页面会多花几十秒)。

    ``trade_date`` 当天还没有索引时,会回退到最近一个有索引的交易日继续可用
    (由 get_concept_map 处理),这里只负责"把当天的建出来"。
    """
    global _LAST_ATTEMPT
    import time as _time

    # 先确定当前板块源(有 5 分钟缓存,正常不会额外打网络)
    fetch_concept_boards()
    cur_src = _BOARDS_MEM.get("source") or ""
    last_src = _LAST_BUILD.get("source") or ""
    rows = _count_rows(trade_date)

    # 板块源在「东财」与「通达信」之间切换过 → 板块代码口径不一致,重建
    if (
        not force
        and rows >= _MIN_GOOD_ROWS
        and last_src
        and cur_src
        and _LAST_BUILD.get("trade_date") == trade_date
        and (cur_src in _EASTMONEY_SOURCES) != (last_src in _EASTMONEY_SOURCES)
        and _time.monotonic() - _LAST_ATTEMPT >= 600
    ):
        logger.warning(
            f"[选股] 板块源由 {last_src} 切换为 {cur_src},重建概念索引"
        )
        force = True

    if force:
        pass
    elif rows >= _MIN_GOOD_ROWS:
        return True
    elif rows > 0 and _time.monotonic() - _LAST_ATTEMPT < 600:
        # 不完整但刚试过:10 分钟内不重复构建,先用手上的
        return False
    if _BUILDING.is_set() or not _LOCK.acquire(blocking=False):
        return False
    try:
        rows = _count_rows(trade_date)
        if not force and rows >= _MIN_GOOD_ROWS:
            return True
        _BUILDING.set()
        _LAST_ATTEMPT = _time.monotonic()
        build_concept_index(trade_date, board_limit=board_limit)
        return _count_rows(trade_date) > 0
    finally:
        _BUILDING.clear()
        _LOCK.release()


def latest_index_date(trade_date: str) -> str | None:
    """<= trade_date 的最近一个有概念索引的交易日。"""
    db = SessionLocal()
    try:
        value = db.execute(
            text(
                "SELECT MAX(trade_date) FROM screener_concept_members "
                "WHERE trade_date <= :d"
            ),
            {"d": trade_date},
        ).scalar()
        return str(value) if value else None
    finally:
        db.close()


def get_concept_map(trade_date: str) -> dict[str, list[dict]]:
    """返回 {symbol: [{code,name,change_pct,rank}, ...]},按板块强度排序。

    当天索引缺失(东财挂了、还没构建完)时回退到最近一个交易日的归属,
    避免个股直接变成"没有板块";实际用到的日期见 ``concept_source_date()``。
    """
    if _MEM["map"] is not None and _MEM["trade_date"] == trade_date:
        return _MEM["map"]

    used_date = trade_date
    db = SessionLocal()
    try:
        rows = (
            db.query(ScreenerConceptMember)
            .filter(ScreenerConceptMember.trade_date == trade_date)
            .order_by(ScreenerConceptMember.concept_rank.asc())
            .all()
        )
        if not rows:
            fallback = latest_index_date(trade_date)
            if fallback and fallback != trade_date:
                used_date = fallback
                rows = (
                    db.query(ScreenerConceptMember)
                    .filter(ScreenerConceptMember.trade_date == fallback)
                    .order_by(ScreenerConceptMember.concept_rank.asc())
                    .all()
                )
                if rows:
                    logger.warning(f"[选股] {trade_date} 概念索引为空,沿用 {fallback} 的板块归属")
    finally:
        db.close()

    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r.symbol, []).append(
            {
                "code": r.concept_code,
                "name": r.concept_name,
                "change_pct": r.concept_change_pct,
                "rank": r.concept_rank,
            }
        )
    _MEM.update(trade_date=trade_date, map=out, source_date=used_date)
    return out


def concept_index_status(trade_date: str) -> dict:
    db = SessionLocal()
    try:
        rows = int(
            db.query(func.count(ScreenerConceptMember.id))
            .filter(ScreenerConceptMember.trade_date == trade_date)
            .scalar()
            or 0
        )
        concepts = int(
            db.query(func.count(func.distinct(ScreenerConceptMember.concept_code)))
            .filter(ScreenerConceptMember.trade_date == trade_date)
            .scalar()
            or 0
        )
    finally:
        db.close()
    last = _LAST_BUILD.get("trade_date")
    return {
        "rows": rows,
        "concepts": concepts,
        "source_date": _MEM.get("source_date") or trade_date,
        "source": (_LAST_BUILD.get("source") or _BOARDS_MEM.get("source") or ""),
        "last_build": (
            {
                "trade_date": last,
                "boards": _LAST_BUILD.get("boards", 0),
                "ok_boards": _LAST_BUILD.get("ok_boards", 0),
                "rows": _LAST_BUILD.get("rows", 0),
                "source": _LAST_BUILD.get("source", ""),
            }
            if last
            else None
        ),
    }
