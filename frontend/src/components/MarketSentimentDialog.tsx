import { useEffect, useMemo, useState } from 'react'
import { Activity, AlertTriangle, ChevronDown, HelpCircle, X } from 'lucide-react'
import type { MarketSentimentResponse, MarketSentimentSnapshot } from '@panwatch/api'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import { SENTIMENT_HELP, SENTIMENT_TIPS, TIER_TABLE } from './sentiment-help'

const METRIC_TIPS: Record<string, string> = {
  '涨停家数': '收盘封住涨停的股票数，只统计当天有日K的 A 股。',
  '跌停家数': '收盘封住跌停的股票数。',
  '跌停/涨停': '跌停家数 ÷ 涨停家数。大于 1.2 代表亏钱效应占优，是 L1 的扣分项。',
  '高位大面': SENTIMENT_TIPS.high_face,
  '天地板': SENTIMENT_TIPS.sky_floor,
  '炸板率': SENTIMENT_TIPS.fry,
  '晋级率': SENTIMENT_TIPS.promote,
  '主线家数': SENTIMENT_TIPS.main_sector,
}

const TIER_RANGE: Record<string, string> = {
  T0: '系统性风险 → 空仓',
  T1: 'L2 < 2 → 空仓',
  T2: 'L2 2–5 → 单股 5% / 总仓 10%',
  T3: 'L2 5–8 → 单股 10% / 总仓 30%',
  T4: 'L2 8–11 → 单股 20% / 总仓 70%',
  T5: 'L2 ≥ 11 → 单股 25% / 总仓 90%',
}

function pct(value?: number | null, digits = 0): string {
  if (value == null || !Number.isFinite(value)) return '--'
  return `${(value * 100).toFixed(digits)}%`
}

function num(value?: number | null, digits = 1): string {
  if (value == null || !Number.isFinite(value)) return '--'
  return value.toFixed(digits)
}

function L2Chart({ history }: { history: MarketSentimentSnapshot[] }) {
  const points = useMemo(() => history.filter((row) => row.l2 != null), [history])
  if (points.length < 2) {
    return <div className="py-8 text-center text-[11px] text-muted-foreground">历史数据不足，至少需要两个交易日</div>
  }
  const W = 820
  const H = 220
  const pad = { l: 38, r: 58, t: 14, b: 26 }
  const x = (index: number) => pad.l + index * (W - pad.l - pad.r) / (points.length - 1)
  const y = (value: number) => pad.t + (13 - value) * (H - pad.t - pad.b) / 13
  const bands = [
    { from: 11, to: 13, color: 'rgba(124,58,237,.10)', label: 'T5' },
    { from: 8, to: 11, color: 'rgba(37,99,235,.12)', label: 'T4' },
    { from: 5, to: 8, color: 'rgba(217,119,6,.08)', label: 'T3' },
    { from: 2, to: 5, color: 'rgba(225,29,72,.06)', label: 'T2' },
    { from: 0, to: 2, color: 'rgba(107,114,128,.05)', label: 'T1' },
  ]
  const line = points.map((row, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(row.l2 as number).toFixed(1)}`).join(' ')
  const area = `${line} L${x(points.length - 1)},${H - pad.b} L${pad.l},${H - pad.b} Z`
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height="220" role="img" aria-label="近60日L2走势">
      <defs>
        <linearGradient id="sent-chart-area" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0" stopColor="#2563eb" stopOpacity="0.16" />
          <stop offset="1" stopColor="#2563eb" stopOpacity="0" />
        </linearGradient>
      </defs>
      {bands.map((band) => (
        <g key={band.label}>
          <rect x={pad.l} y={y(band.to)} width={W - pad.l - pad.r} height={Math.max(1, y(band.from) - y(band.to))} fill={band.color} />
          <text x={W - pad.r + 8} y={(y(band.to) + y(band.from)) / 2 + 3} fontSize="9" className="fill-muted-foreground">{band.label}</text>
        </g>
      ))}
      {[0, 2, 5, 8, 11, 13].map((value) => (
        <g key={value}>
          <line x1={pad.l} y1={y(value)} x2={W - pad.r} y2={y(value)} stroke="currentColor" strokeWidth="0.8" strokeDasharray="3 3" className="text-border" />
          <text x={pad.l - 7} y={y(value) + 3} textAnchor="end" fontSize="9" className="fill-muted-foreground">{value}</text>
        </g>
      ))}
      <path d={area} fill="url(#sent-chart-area)" />
      <path d={line} fill="none" stroke="#2563eb" strokeWidth="1.7" strokeLinejoin="round" strokeLinecap="round" />
      {points.map((row, i) => row.ebb_confirmed ? (
        <circle key={`ebb-${row.date}`} cx={x(i)} cy={y(row.l2 as number)} r="3" fill="#e11d48" stroke="white" strokeWidth="1.3" />
      ) : null)}
      <circle cx={x(points.length - 1)} cy={y(points[points.length - 1].l2 as number)} r="3" fill="#2563eb" stroke="white" strokeWidth="1.3" />
      {[0, Math.floor((points.length - 1) / 2), points.length - 1].map((index) => (
        <text key={`x-${index}`} x={x(index)} y={H - 8} textAnchor="middle" fontSize="9" className="fill-muted-foreground">
          {points[index].date.slice(5)}
        </text>
      ))}
    </svg>
  )
}

export default function MarketSentimentDialog({
  open,
  onOpenChange,
  data,
  focusHelp = false,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  data: MarketSentimentResponse | null
  focusHelp?: boolean
}) {
  const today = data?.today || null
  const partial = !today || today.partial || today.tier == null || today.l2 == null
  const parts = today?.l2_parts || {}
  const l2 = today?.l2
  const [helpOpen, setHelpOpen] = useState(false)

  useEffect(() => {
    if (!open) {
      setHelpOpen(false)
      return
    }
    if (!focusHelp) return
    setHelpOpen(true)
    const timer = window.setTimeout(() => {
      document.getElementById('sentiment-help')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }, 100)
    return () => window.clearTimeout(timer)
  }, [open, focusHelp])

  const toggleHelp = () => {
    setHelpOpen((prev) => {
      const next = !prev
      if (next) {
        window.setTimeout(() => {
          document.getElementById('sentiment-help')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
        }, 60)
      }
      return next
    })
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-3xl overflow-y-auto p-0">
        <DialogHeader className="sticky top-0 z-10 mb-0 border-b border-border/60 bg-card px-5 py-4 pr-12">
          <DialogTitle className="flex flex-wrap items-center gap-2 text-[15px]">
            <Activity className="h-4 w-4 text-primary" />
            市场情绪
            {today?.tier && !partial && (
              <span title={SENTIMENT_TIPS.tier} className="cursor-help rounded bg-blue-500/15 px-1.5 py-0.5 text-[12px] text-blue-600">{today.tier}</span>
            )}
            {today && <span className="text-[10.5px] font-normal text-muted-foreground">{today.date}</span>}
            <button
              type="button"
              onClick={toggleHelp}
              className="inline-flex items-center gap-0.5 text-[10px] font-normal text-primary hover:underline"
            >
              指标说明
              <ChevronDown className={`h-3 w-3 transition-transform ${helpOpen ? 'rotate-180' : ''}`} />
            </button>
            <button
              type="button"
              onClick={() => onOpenChange(false)}
              className="ml-auto rounded p-1 text-muted-foreground hover:bg-accent/40 hover:text-foreground"
              aria-label="关闭"
            >
              <X className="h-4 w-4" />
            </button>
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-4 px-5 py-4">
          {partial ? (
            <div className="rounded-lg border border-dashed border-amber-500/50 bg-amber-500/10 p-4 text-[12px] leading-relaxed">
              <div className="font-semibold text-amber-600">今日不生成情绪档位</div>
              <p className="mt-2 text-muted-foreground">
                {!today
                  ? '今天还没有生成情绪快照。收盘后数据同步完成再刷新。'
                  : `全市场日K覆盖率 ${pct(today.coverage, 1)}，低于 90% 的最低要求。系统只记录原始指标，不给 T 档，也不推进跨日状态。`}
              </p>
              {today?.warnings?.length ? (
                <p className="mt-2 text-[11px] text-amber-700">{today.warnings.join('；')}</p>
              ) : null}
            </div>
          ) : (
            <>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                <div title={SENTIMENT_TIPS.l1} className="cursor-help rounded-lg border border-border/60 bg-muted/10 p-3">
                  <div className="text-[10px] text-muted-foreground">L1 全局</div>
                  <div className="mt-0.5 text-[20px] font-bold">{num(today.l1)} <span className="text-[11px] font-normal text-muted-foreground">/10</span></div>
                  <div className="mt-0.5 text-[10px] text-muted-foreground">
                    {today.mood === 'panic' ? '恐慌' : today.mood === 'euphoric' ? '亢奋' : '中性'}
                    {today.systemic_risk ? ' · 系统性风险' : today.drop_trigger ? ' · 跌停潮' : ''}
                  </div>
                </div>
                <div title={SENTIMENT_TIPS.l2} className="cursor-help rounded-lg border border-border/60 bg-muted/10 p-3">
                  <div className="text-[10px] text-muted-foreground">L2 主线</div>
                  <div className="mt-0.5 text-[20px] font-bold">{num(l2)} <span className="text-[11px] font-normal text-muted-foreground">/13</span></div>
                  <div className="mt-0.5 text-[10px] text-muted-foreground">{today.main_sector || '无主线'}{today.main_sector_hl ? ` · ${today.main_sector_hl} 只` : ''}</div>
                </div>
                <div title={SENTIMENT_TIPS.l3} className="cursor-help rounded-lg border border-border/60 bg-muted/10 p-3">
                  <div className="text-[10px] text-muted-foreground">L3 龙头</div>
                  <div className="mt-0.5 text-[17px] font-bold text-emerald-600">
                    {today.l3 === 'confirm' ? '确认走弱' : today.l3 === 'warning' ? '断板预警' : '未断板'}
                  </div>
                  <div className="mt-0.5 text-[10px] text-muted-foreground">{today.leader || '--'} · {today.leader_break_days} 天未封</div>
                </div>
              </div>

              <section>
                <h3 className="mb-2 text-[11px] font-medium text-muted-foreground">L2 主线分拆 · 合计 {num(l2)}</h3>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  {[
                    { label: '梯队完整度', value: parts.tier ?? 0, max: 4, tip: '主线内 4 板以上家数：≥3 家得 4 分，≥1 家得 2 分。' },
                    { label: '炸板率偏离', value: parts.fry ?? 0, max: 4, tip: SENTIMENT_TIPS.fry },
                    { label: '晋级率偏离', value: parts.promote ?? 0, max: 2, tip: SENTIMENT_TIPS.promote },
                    { label: '高位大面', value: parts.face ?? 0, max: 3, tip: SENTIMENT_TIPS.high_face },
                  ].map((item) => (
                    <div key={item.label} title={item.tip} className="cursor-help rounded-lg border border-border/60 p-2.5">
                      <div className="flex justify-between text-[10.5px] text-muted-foreground">
                        <span>{item.label}</span>
                        <b className="text-foreground">{item.value} / {item.max}</b>
                      </div>
                      <div className="mt-1.5 h-1 overflow-hidden rounded bg-border">
                        <div className="h-1 rounded bg-primary" style={{ width: `${Math.max(0, Math.min(100, (item.value / item.max) * 100))}%` }} />
                      </div>
                    </div>
                  ))}
                </div>
              </section>

              <section>
                <h3 className="mb-2 text-[11px] font-medium text-muted-foreground">盘面指标</h3>
                <div className="grid grid-cols-2 gap-x-5 gap-y-0 text-[10.5px] sm:grid-cols-4">
                  {[
                    ['涨停家数', today.limit_up],
                    ['跌停家数', today.limit_down],
                    ['跌停/涨停', today.limit_up > 0 ? (today.limit_down / today.limit_up).toFixed(2) : '--'],
                    ['高位大面', today.high_face],
                    ['天地板', today.sky_floor],
                    ['炸板率', today.fry_rate == null
                      ? `全市场 ${pct(today.global_fry_rate)} / 主线样本不足`
                      : `${pct(today.fry_rate)} / 基线${pct(today.fry_baseline)}`],
                    ['晋级率', `${pct(today.promote_rate)} / 基线${pct(today.promote_baseline)}`],
                    ['主线家数', today.main_sector_hl ? `${today.main_sector_hl} 只` : '--'],
                  ].map(([label, value]) => (
                    <div key={String(label)} title={METRIC_TIPS[String(label)] || ''} className="flex cursor-help justify-between gap-2 border-b border-dashed border-border/60 py-1.5">
                      <span className="text-muted-foreground">{label}</span>
                      <span className="font-medium">{value}</span>
                    </div>
                  ))}
                </div>
              </section>

              <section>
                <h3 className="mb-2 text-[11px] font-medium text-muted-foreground">近 60 日 L2 与情绪档位</h3>
                <div className="rounded-lg border border-border/60 bg-muted/10 p-2">
                  <div className="mb-1 flex flex-wrap gap-3 px-1 text-[10px] text-muted-foreground">
                    <span><span className="mr-1 inline-block h-2 w-2 rounded-sm bg-blue-600" />L2 主线分</span>
                    <span><span className="mr-1 inline-block h-2 w-2 rounded-sm bg-blue-500/20" />T4 区间</span>
                    <span><span className="mr-1 inline-block h-2 w-2 rounded-sm bg-rose-500/30" />退潮确认</span>
                  </div>
                  <L2Chart history={data?.history || []} />
                  <div className="px-1 pt-1 text-[9.5px] text-muted-foreground">
                    仅显示有 L2 的交易日（{(data?.history || []).filter((row) => row.l2 != null).length} 个）；部分日期因日K覆盖或行业数据不足未生成档位。
                  </div>
                </div>
              </section>

              <section>
                <h3 className="mb-2 text-[11px] font-medium text-muted-foreground">为什么是 {today.tier}</h3>
                <div className="rounded-r-lg border-l-[3px] border-primary bg-primary/5 px-3 py-2 text-[11px] leading-relaxed">
                  <div><b>L2 = {num(l2)}</b>，{TIER_RANGE[today.tier || 'T1']}。</div>
                  <div className="mt-1 text-muted-foreground">
                    主线 {today.main_sector || '--'} 有 {today.main_sector_hl} 只涨停/炸板（{parts.tier ?? 0} 分），
                    {today.fry_rate == null
                      ? `主线 3 板以上不足 2 只，炸板率取中性 ${parts.fry ?? 2} 分（全市场 ${pct(today.global_fry_rate)} 仅供对照），`
                      : `炸板率 ${pct(today.fry_rate)} vs 基线 ${pct(today.fry_baseline)}（${parts.fry ?? 0} 分），`}
                    晋级率 {pct(today.promote_rate)} vs 基线 {pct(today.promote_baseline)}（{parts.promote ?? 0} 分），
                    大面 {today.high_face} 家（{parts.face ?? 0} 分）。
                  </div>
                  <div className="mt-1 text-muted-foreground">
                    <b className="text-foreground">L1 = {num(today.l1)}</b>
                    {today.l1 <= 4
                      ? `，恐慌区间（规则上限 T2）；当前档位 ${today.tier}。`
                      : today.l1 <= 7
                        ? `，偏弱区间（规则上限 T4）；当前档位 ${today.tier} 由 L2 等分项决定。`
                        : '，不额外限制仓位档。'}
                  </div>
                </div>
              </section>

              <details
                id="sentiment-help"
                open={helpOpen}
                onToggle={(event) => setHelpOpen(event.currentTarget.open)}
                className="rounded-lg border border-border/60 bg-muted/10 p-3"
              >
                <summary className="flex cursor-pointer list-none items-center gap-1.5 text-[11px] font-medium text-foreground [&::-webkit-details-marker]:hidden">
                  <HelpCircle className="h-3.5 w-3.5 text-primary" />
                  指标说明 · 每个位置怎么读
                  <span className="ml-auto text-[10px] font-normal text-muted-foreground">
                    {helpOpen ? '收起' : '展开'}
                  </span>
                </summary>
                <div className="mt-2 grid grid-cols-1 gap-x-4 gap-y-2 sm:grid-cols-2">
                  {SENTIMENT_HELP.map((item) => (
                    <div key={item.term} className="text-[10.5px] leading-relaxed">
                      <span className="font-medium text-foreground">{item.term}：</span>
                      <span className="text-muted-foreground">{item.desc}</span>
                    </div>
                  ))}
                </div>
                <div className="mt-3 overflow-hidden rounded border border-border/60">
                  <table className="w-full text-[10.5px]">
                    <thead className="bg-muted/40 text-muted-foreground">
                      <tr>
                        <th className="px-2 py-1 text-left font-normal">档位</th>
                        <th className="px-2 py-1 text-left font-normal">L2</th>
                        <th className="px-2 py-1 text-right font-normal">单股上限</th>
                        <th className="px-2 py-1 text-right font-normal">总仓上限</th>
                      </tr>
                    </thead>
                    <tbody>
                      {TIER_TABLE.map((row) => (
                        <tr key={row.tier} className="border-t border-border/40">
                          <td className="px-2 py-1 font-medium">{row.tier}</td>
                          <td className="px-2 py-1 text-muted-foreground">{row.l2}</td>
                          <td className="px-2 py-1 text-right">{row.single}</td>
                          <td className="px-2 py-1 text-right">{row.total}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <p className="mt-2 text-[10px] leading-relaxed text-muted-foreground">
                  档位是“总仓上限”，不是建议仓位；T0/T1 表示空仓。L1 只做上限裁剪：L1≤4 最高 T2，L1 5–7 最高 T4。炸板率低于基线用绿色 ↓（改善），晋级率高于基线用红色 ↑（改善）；A 股红涨绿跌只用于价格。
                </p>
              </details>

              <div className="flex flex-wrap gap-x-3 gap-y-1 border-t border-border/60 pt-2 text-[10px] text-muted-foreground">
                <span>数据覆盖 {pct(today.coverage, 1)}</span>
                <span>来源：{today.source}</span>
                <span className={today.ebb_confirmed ? 'text-stock-down' : 'text-emerald-600'}>
                  {today.ebb_confirmed ? '退潮已确认' : '退潮未确认'}
                </span>
                {today.warnings?.map((warning) => (
                  <span key={warning} className="flex items-center gap-1 text-amber-600">
                    <AlertTriangle className="h-3 w-3" />{warning}
                  </span>
                ))}
              </div>
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
