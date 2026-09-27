"""AI 每日操作计划 Agent。

按股生成"多情景操作方案":总体判断 + 3~4 个情景(每个情景同时带人读文本与量化
价位) + 操作纪律 + 一句话总结 + 风险提示。

产出落到两处:

1. ``operation_plans`` 表 —— 结构化留档。情景里的量化价位是"一键加入提醒"的数据源,
   提醒通过 ``source_plan_id`` 回指计划,便于按计划分组与清理。
2. ``analysis_history`` —— 让它在既有「报告」页签里按常规方式出现。

为什么要求 AI 同时给"文本 + 数值"两套价位:报告里写的是
``"8.05-8.25元（MA20/前高密集区，减仓1/2）"``,而程序要的是 ``8.05`` / ``8.25``。
靠正则从这种文本里抠数字早晚出错,且一旦抠错就会生成错误的提醒 —— 对一个用来
约束交易纪律的功能来说,错误价位比没有价位更糟。所以价位由模型显式给出,程序只做
合法性校验;数值为 0 表示该情景不监控此项。
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

from src.modules.automation.base import AgentContext, AnalysisResult, BaseAgent
from src.modules.research.analysis_history import save_analysis
from src.modules.research.context_builder import ContextBuilder
from src.modules.research.signals import SignalPackBuilder
from src.modules.research.signals.structured_output import (
    strip_tagged_json,
    try_extract_tagged_json,
)
from src.platform.persistence.json_safe import to_jsonable
from src.platform.persistence.models import OperationPlan, Stock

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "operation_plan.txt"

# 情景里的量化价位字段(单位:元)。0 或缺失表示该情景不监控此项。
QUANT_FIELDS: tuple[str, ...] = (
    "entry_low",
    "entry_high",
    "stop_loss_num",
    "target1_low",
    "target1_high",
    "target2_low",
    "target2_high",
)

ACTION_TYPES = {"buy", "add", "reduce", "stop_loss", "observe"}

# 情景允许的文本字段,不在表里的一律丢弃(避免把模型的即兴字段透到前端)。
TEXT_FIELDS: tuple[str, ...] = (
    "title",
    "condition",
    "action",
    "position",
    "buy_price_range",
    "stop_loss",
    "target1",
    "target2",
    "strategy",
)

MAX_SCENARIOS = 4


def _num(value) -> float:
    """宽松转正数;None/非法/<=0 一律 0(0 = 该价位不适用,不监控)。"""
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if f != f or f <= 0:  # NaN 或非正
        return 0.0
    return f


def _text(value) -> str:
    return str(value or "").strip()


def normalize_scenarios(raw) -> list[dict]:
    """校验并归一化 AI 给出的情景数组。

    丢弃无法使用的条目,而不是抛错 —— 一个情景格式不对不该让整份计划作废。
    但会把丢弃情况记日志,便于排查 prompt 漂移。
    """
    if not isinstance(raw, list):
        return []

    out: list[dict] = []
    for idx, item in enumerate(raw[: MAX_SCENARIOS * 2]):
        if not isinstance(item, dict):
            logger.warning(f"[操作计划] 情景 {idx} 不是对象,已丢弃")
            continue
        scenario = {field: _text(item.get(field)) for field in TEXT_FIELDS}
        if not scenario["title"]:
            scenario["title"] = f"情景{len(out) + 1}"

        action_type = _text(item.get("action_type")).lower()
        scenario["action_type"] = action_type if action_type in ACTION_TYPES else "observe"
        scenario["is_best"] = bool(item.get("is_best"))

        for field in QUANT_FIELDS:
            scenario[field] = _num(item.get(field))

        # 买入区间上下限颠倒时自动交换,避免生成 [高, 低] 这种永远不成立的区间
        if scenario["entry_low"] and scenario["entry_high"]:
            if scenario["entry_low"] > scenario["entry_high"]:
                scenario["entry_low"], scenario["entry_high"] = (
                    scenario["entry_high"],
                    scenario["entry_low"],
                )
        # 目标区间同理
        for lo, hi in (("target1_low", "target1_high"), ("target2_low", "target2_high")):
            if scenario[lo] and scenario[hi] and scenario[lo] > scenario[hi]:
                scenario[lo], scenario[hi] = scenario[hi], scenario[lo]

        # 至少要有一个可执行价位,否则该情景对"加提醒"没有价值
        if not any(scenario[f] for f in QUANT_FIELDS):
            logger.warning(f"[操作计划] 情景「{scenario['title']}」没有任何量化价位,已丢弃")
            continue
        out.append(scenario)

    if len(out) > MAX_SCENARIOS:
        out = out[:MAX_SCENARIOS]
    return out


def normalize_discipline(raw) -> list[dict]:
    """操作纪律:[{principle, detail}]。兼容模型给出纯字符串数组的写法。"""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw[:10]:
        if isinstance(item, dict):
            principle, detail = _text(item.get("principle")), _text(item.get("detail"))
        else:
            principle, detail = "", _text(item)
        if principle or detail:
            out.append({"principle": principle, "detail": detail})
    return out


class OperationPlanAgent(BaseAgent):
    """每日操作计划(按股执行)。"""

    name = "operation_plan"
    display_name = "操作计划"
    description = "按股生成多情景操作方案（含止损/目标价位），可一键加入提醒"

    async def collect(self, context: AgentContext) -> dict:
        """采集目标股票的结构化数据包(行情/技术/资金/新闻/公告/持仓)。"""
        sym_list = [(s.symbol, s.market, s.name) for s in context.watchlist]
        if not sym_list:
            return {}

        packs = await SignalPackBuilder().build_for_symbols(
            symbols=sym_list,
            include_news=True,
            news_hours=72,
            portfolio=context.portfolio,
            include_technical=True,
            include_capital_flow=True,
            include_events=True,
            events_days=14,
        )
        context_pack = await ContextBuilder().build_symbol_contexts(
            agent_name=self.name,
            context=context,
            packs=packs,
            realtime_hours=24,
            extended_hours=72,
            history_days=30,
            kline_days=120,
            persist_snapshot=True,
        )
        return {
            "signal_packs": packs,
            "symbol_contexts": context_pack.get("symbols", {}),
            "quality_overview": context_pack.get("quality_overview", {}),
            "timestamp": datetime.now().isoformat(),
        }

    def build_prompt(self, data: dict, context: AgentContext) -> tuple[str, str]:
        system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
        packs = data.get("signal_packs") or {}
        symbol_contexts = data.get("symbol_contexts") or {}

        lines: list[str] = [f"## 计划日期：{date.today().strftime('%Y-%m-%d')}"]

        for stock in context.watchlist:
            pack = packs.get(stock.symbol)
            quote = getattr(pack, "quote", None)
            name = (stock.name or getattr(quote, "name", "") or stock.symbol).strip()
            lines.append(f"\n## 标的：{name}（{stock.symbol}，{stock.market}）")

            if quote:
                price = quote.current_price or 0
                lines.append(
                    f"- 最新价：{price:.2f}　涨跌幅：{(quote.change_pct or 0):+.2f}%"
                    f"　昨收：{(quote.prev_close or 0):.2f}"
                )
                lines.append(
                    f"- 今日：开{(quote.open_price or 0):.2f} 高{(quote.high_price or 0):.2f}"
                    f" 低{(quote.low_price or 0):.2f}　成交额：{(quote.turnover or 0) / 1e8:.2f}亿"
                )
            else:
                lines.append("- 行情数据缺失")

            tech = (getattr(pack, "technical", None) or {}) if pack else {}
            if tech and not tech.get("error"):
                lines.append(
                    f"- 均线：MA5={tech.get('ma5') or 0:.2f} MA10={tech.get('ma10') or 0:.2f}"
                    f" MA20={tech.get('ma20') or 0:.2f} MA60={tech.get('ma60') or 0:.2f}"
                )
                lines.append(
                    f"- 支撑：短期{tech.get('support_s') or 0:.2f} 中期{tech.get('support_m') or 0:.2f}"
                    f" 长期{tech.get('support_l') or 0:.2f}"
                )
                lines.append(
                    f"- 压力：短期{tech.get('resistance_s') or 0:.2f} 中期{tech.get('resistance_m') or 0:.2f}"
                    f" 长期{tech.get('resistance_l') or 0:.2f}"
                )
                lines.append(
                    f"- 趋势：{tech.get('trend') or '未知'}　MACD：{tech.get('macd_status') or '未知'}"
                    f"　量能：{tech.get('volume_trend') or '未知'}"
                )
                extras = []
                if tech.get("rsi6") is not None:
                    extras.append(f"RSI6={tech['rsi6']:.1f}（{tech.get('rsi_status') or ''}）")
                if tech.get("change_5d") is not None:
                    extras.append(f"5日{tech['change_5d']:+.1f}%")
                if tech.get("change_20d") is not None:
                    extras.append(f"20日{tech['change_20d']:+.1f}%")
                if extras:
                    lines.append("- " + "　".join(extras))
            else:
                lines.append("- 技术指标缺失")

            flow = (getattr(pack, "capital_flow", None) or {}) if pack else {}
            if flow and not flow.get("error"):
                lines.append(
                    f"- 资金：主力净流入 {(flow.get('main_net_inflow') or 0) / 1e8:.2f}亿"
                    f"（占比 {(flow.get('main_net_inflow_pct') or 0):+.2f}%）"
                )

            pos = getattr(pack, "position", None) if pack else None
            if pos is not None:
                lines.append(
                    f"- 持仓：{getattr(pos, 'quantity', 0)}股　成本 {getattr(pos, 'cost_price', 0) or 0:.2f}"
                    f"　风格 {getattr(pos, 'trading_style', '') or '未设置'}"
                )
            else:
                lines.append("- 持仓：未持有")

            # 近期新闻标题(只要标题,正文太长且噪声大)
            news = getattr(pack, "news", None) if pack else None
            items = list(getattr(news, "items", None) or [])[:6]
            if items:
                lines.append("- 近期新闻：")
                for item in items:
                    lines.append(f"    - {_text(getattr(item, 'title', ''))[:60]}")

            ctx = symbol_contexts.get(stock.symbol) or {}
            summary = ctx.get("summary") or ctx.get("context_summary")
            if summary:
                lines.append(f"- 历史脉络：{_text(summary)[:300]}")

        lines.append(
            "\n请严格按系统提示的结构输出：先给正文（总体判断/具体操作方案/操作纪律/"
            "一句话总结/风险提示），最后追加 <!--PANWATCH_JSON--> 包裹的结构化 JSON。"
            "量化价位必须是纯数字、与正文一致、且基于上面的真实价格。"
        )
        return system_prompt, "\n".join(lines)

    async def analyze(self, context: AgentContext, data: dict) -> AnalysisResult:
        system_prompt, user_content = self.build_prompt(data, context)
        content = await context.ai_client.chat(system_prompt, user_content)

        structured = try_extract_tagged_json(content) or {}
        display_content = strip_tagged_json(content)

        scenarios = normalize_scenarios(structured.get("scenarios"))
        discipline = normalize_discipline(structured.get("discipline"))
        plan_date = date.today().strftime("%Y-%m-%d")

        stocks = list(context.watchlist)
        primary = stocks[0] if stocks else None
        name = (primary.name if primary else "") or ""

        if not scenarios:
            logger.warning(
                f"[操作计划] {primary.symbol if primary else '?'} 未能解析出任何有效情景,"
                f"仅留档文本(常见原因:模型未按 JSON schema 输出)"
            )

        # 逐只落库。single 模式下 watchlist 只有一只,这里循环是为了兼容将来批量。
        for stock in stocks:
            self._persist_plan(
                stock=stock,
                plan_date=plan_date,
                overall_judgment=_text(structured.get("overall_judgment")),
                scenarios=scenarios,
                discipline=discipline,
                summary=_text(structured.get("summary")),
                risk_warning=_text(structured.get("risk_warning")),
                content=display_content,
            )
            save_analysis(
                agent_name=self.name,
                stock_symbol=stock.symbol,
                content=display_content,
                title=f"【{self.display_name}】{stock.name or stock.symbol} {plan_date}",
                raw_data={"structured": structured, "scenarios": scenarios},
            )

        title = f"【{self.display_name}】{name}（{primary.symbol if primary else ''}）"
        return AnalysisResult(
            agent_name=self.name,
            title=title,
            content=display_content,
            raw_data={
                **data,
                "structured": structured,
                "scenarios": scenarios,
                "plan_date": plan_date,
            },
        )

    @staticmethod
    def _persist_plan(
        *,
        stock,
        plan_date: str,
        overall_judgment: str,
        scenarios: list[dict],
        discipline: list[dict],
        summary: str,
        risk_warning: str,
        content: str,
    ) -> None:
        """按 (market, symbol, plan_date) upsert 一条计划。

        同一天重复生成会覆盖当天那条(计划本就该以最新一次为准);历史日期原样保留,
        "当时是怎么计划的"可回溯。
        """
        from src.platform.persistence.database import SessionLocal

        db = SessionLocal()
        try:
            stock_row = (
                db.query(Stock)
                .filter(Stock.symbol == stock.symbol, Stock.market == stock.market)
                .first()
            )
            row = (
                db.query(OperationPlan)
                .filter(
                    OperationPlan.market == stock.market,
                    OperationPlan.stock_symbol == stock.symbol,
                    OperationPlan.plan_date == plan_date,
                )
                .first()
            )
            if row is None:
                row = OperationPlan(
                    market=stock.market,
                    stock_symbol=stock.symbol,
                    plan_date=plan_date,
                )
                db.add(row)

            row.stock_id = stock_row.id if stock_row else None
            row.stock_name = stock.name or (stock_row.name if stock_row else "") or stock.symbol
            row.agent_name = OperationPlanAgent.name
            row.overall_judgment = overall_judgment
            row.scenarios = to_jsonable(scenarios)
            row.discipline = to_jsonable(discipline)
            row.summary = summary
            row.risk_warning = risk_warning
            row.content = content
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"[操作计划] 落库失败 {stock.symbol} {plan_date}: {e}", exc_info=True)
        finally:
            db.close()

    async def run_single(
        self, context: AgentContext, stock_symbol: str
    ) -> AnalysisResult | None:
        """单只模式:只分析指定股票。"""
        original = context.config.watchlist
        context.config.watchlist = [s for s in original if s.symbol == stock_symbol]
        if not context.config.watchlist:
            return None
        try:
            data = await self.collect(context)
            if not data.get("signal_packs"):
                logger.warning(f"[操作计划] {stock_symbol} 无可用数据,跳过")
                return None
            result = await self.analyze(context, data)
            if getattr(context, "suppress_notify", False):
                result.raw_data["notified"] = False
                result.raw_data["notify_skipped"] = "suppressed"
                return result
            notify_result = await context.notifier.notify_with_result(
                result.title, result.content, result.images
            )
            result.raw_data["notified"] = bool(notify_result.get("success"))
            return result
        finally:
            context.config.watchlist = original


__all__ = [
    "OperationPlanAgent",
    "normalize_scenarios",
    "normalize_discipline",
    "QUANT_FIELDS",
    "ACTION_TYPES",
]
