import { Activity, AlertTriangle, ChevronRight, HelpCircle, Loader2, RefreshCw } from 'lucide-react'
import type { MarketSentimentResponse } from '@panwatch/api'
import { SENTIMENT_TIPS } from './sentiment-help'

const TIER_STYLE: Record<string, string> = {
  T0: 'bg-stock-down/15 text-stock-down',
  T1: 'bg-stock-down/15 text-stock-down',
  T2: 'bg-amber-500/15 text-amber-600',
  T3: 'bg-yellow-500/15 text-yellow-700',
  T4: 'bg-blue-500/15 text-blue-600',
  T5: 'bg-violet-500/15 text-violet-600',
}

function pct(value?: number | null, digits = 0): string {
  if (value == null || !Number.isFinite(value)) return '--'
  return `${(value * 100).toFixed(digits)}%`
}

function l3Label(l3?: string): string {
  if (l3 === 'confirm') return '确认走弱'
  if (l3 === 'warning') return '断板预警'
  return '未断板'
}

function Sparkline({ values }: { values: Array<number | null> }) {
  const nums = values.filter((v): v is number => v != null && Number.isFinite(v))
  if (nums.length < 2) return null
  const w = 92
  const h = 28
  const step = w / (nums.length - 1)
  const points = nums.map((v, i) => `${(i * step).toFixed(1)},${(h - (Math.max(0, Math.min(13, v)) / 13) * h).toFixed(1)}`)
  const line = `M${points.join(' L')}`
  const area = `${line} L${w},${h} L0,${h} Z`
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width={w} height={h} aria-label="近60日L2趋势">
      <defs>
        <linearGradient id="sent-spark" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0" stopColor="currentColor" stopOpacity="0.22" />
          <stop offset="1" stopColor="currentColor" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill="url(#sent-spark)" className="text-primary" />
      <path d={line} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" className="text-primary" />
      <circle cx={w} cy={h - (Math.max(0, Math.min(13, nums[nums.length - 1])) / 13) * h} r="2.2" fill="currentColor" className="text-primary" />
    </svg>
  )
}

export default function MarketSentimentCard({
  data,
  loading,
  error,
  busy,
  onOpen,
  onOpenHelp,
  onRefresh,
  className = '',
}: {
  data: MarketSentimentResponse | null
  loading: boolean
  error: string
  busy?: boolean
  onOpen: () => void
  onOpenHelp?: () => void
  onRefresh: () => void
  className?: string
}) {
  const today = data?.today || null
  const partial = Boolean(today?.partial || today?.tier == null)
  const stale = Boolean(data?.stale)

  return (
    <div className={`card relative flex min-w-0 flex-col gap-1 p-3 ${className}`}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <Activity className="h-4 w-4 shrink-0 text-primary" />
        <span className="whitespace-nowrap text-[12px] font-medium text-foreground">市场情绪</span>
        <span className="rounded bg-primary px-1.5 py-0.5 text-[9px] font-bold leading-none text-primary-foreground">NEW</span>
        <button
          type="button"
          onClick={onOpenHelp ?? onOpen}
          title="每个指标分别是什么意思"
          aria-label="查看市场情绪指标说明"
          className="rounded p-0.5 text-muted-foreground hover:bg-accent/40 hover:text-foreground"
        >
          <HelpCircle className="h-3.5 w-3.5" />
        </button>
        {today?.tier && !partial && (
          <span
            title={SENTIMENT_TIPS.tier}
            className={`cursor-help rounded px-1.5 py-0.5 text-[11px] font-bold leading-none ${TIER_STYLE[today.tier] || 'bg-accent text-muted-foreground'}`}
          >
            {today.tier}
          </span>
        )}
        {today?.total_position != null && !partial && (
          <span className="whitespace-nowrap text-[10.5px] text-muted-foreground">
            单股≤{pct(today.single_position)} · 总仓≤{pct(today.total_position)}
          </span>
        )}
        {today && (
          <span
            title={SENTIMENT_TIPS.ebb}
            className={`cursor-help whitespace-nowrap rounded-full px-1.5 py-0.5 text-[10px] ${
              today.ebb_confirmed ? 'bg-stock-down/15 text-stock-down' : 'bg-emerald-500/15 text-emerald-600'
            }`}
          >
            {today.ebb_confirmed ? '退潮已确认' : '退潮未确认'}
          </span>
        )}
        <button
          type="button"
          onClick={onOpen}
          disabled={!today}
          className="ml-auto inline-flex items-center gap-0.5 whitespace-nowrap text-[10.5px] text-primary disabled:opacity-40"
        >
          详情 <ChevronRight className="h-3 w-3" />
        </button>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 py-2 text-[11px] text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> 情绪数据加载中…
        </div>
      ) : error ? (
        <div className="flex flex-wrap items-center gap-2 py-1.5 text-[11px] text-amber-600">
          <AlertTriangle className="h-3.5 w-3.5" /> 情绪数据加载失败
          <button type="button" onClick={onRefresh} className="text-primary">重试</button>
        </div>
      ) : !today ? (
        <div className="flex flex-wrap items-center gap-2 py-1.5 text-[11px] text-muted-foreground">
          今日情绪尚未生成（收盘后生成）
          <button type="button" onClick={onRefresh} disabled={busy} className="inline-flex items-center gap-1 text-primary disabled:opacity-50">
            {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
            {busy ? '生成中…' : '生成'}
          </button>
        </div>
      ) : partial ? (
        <div className="py-1 text-[11px] leading-relaxed text-muted-foreground">
          <b className="text-amber-600">数据覆盖 {pct(today.coverage, 1)}</b>，未达到 90%，今日不生成仓位档。
          <button type="button" onClick={onRefresh} disabled={busy} className="ml-2 inline-flex items-center gap-1 text-primary disabled:opacity-50">
            {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
            重新生成
          </button>
        </div>
      ) : (
        <>
          <div className="flex items-end gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px]">
                <span title={SENTIMENT_TIPS.l1} className="cursor-help text-muted-foreground">L1 <b className="text-foreground">{today.l1.toFixed(1)}</b><span className="text-[9px]">/10</span></span>
                <span title={SENTIMENT_TIPS.l2} className="cursor-help text-muted-foreground">L2 <b className="text-foreground">{today.l2?.toFixed(1) ?? '--'}</b><span className="text-[9px]">/13</span></span>
                <span title={SENTIMENT_TIPS.l3} className="cursor-help text-muted-foreground">L3 <b className="text-foreground">{l3Label(today.l3)}</b></span>
              </div>
              <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[10.5px] text-muted-foreground">
                <span title={SENTIMENT_TIPS.main_sector} className="cursor-help">主线 <b className="text-foreground">{today.main_sector || '无主线'}</b>{today.main_sector_hl ? ` · ${today.main_sector_hl} 只` : ''}</span>
                <span title={SENTIMENT_TIPS.leader} className="cursor-help">龙头 <b className="text-foreground">{today.leader || '--'}</b></span>
              </div>
              <div className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-muted-foreground">
                {today.fry_rate == null ? (
                  <span title={SENTIMENT_TIPS.sample} className="cursor-help">
                    炸板 全市场{pct(today.global_fry_rate)}（主线样本不足）
                  </span>
                ) : (
                  <span title={SENTIMENT_TIPS.fry} className="cursor-help">
                    炸板 {pct(today.fry_rate)}{' '}
                    <span className={today.fry_rate <= (today.fry_baseline ?? 0.3) ? 'text-emerald-600' : 'text-stock-down'}>
                      {today.fry_rate <= (today.fry_baseline ?? 0.3) ? '↓' : '↑'} 基线{pct(today.fry_baseline)}
                    </span>
                  </span>
                )}
                <span title={SENTIMENT_TIPS.promote} className="cursor-help">
                  晋级 {pct(today.promote_rate)}{' '}
                  <span className={(today.promote_rate ?? 0) >= (today.promote_baseline ?? 0.3) ? 'text-stock-up' : 'text-emerald-600'}>
                    {(today.promote_rate ?? 0) >= (today.promote_baseline ?? 0.3) ? '↑' : '↓'} 基线{pct(today.promote_baseline)}
                  </span>
                </span>
                <span title={SENTIMENT_TIPS.high_face} className="cursor-help">大面 {today.high_face}</span>
                <span title={SENTIMENT_TIPS.sky_floor} className="cursor-help">天地板 {today.sky_floor}</span>
              </div>
            </div>
            <div className="hidden shrink-0 text-right sm:block">
              <Sparkline values={(data?.history || []).map((row) => row.l2)} />
              <div className="text-[9px] text-muted-foreground/70">近60日 L2</div>
            </div>
          </div>
          {stale && (
            <div className="flex flex-wrap items-center gap-1 text-[10px] text-amber-600">
              <AlertTriangle className="h-3 w-3" /> 数据截至 {today.date}，今日尚未更新
              <button type="button" onClick={onRefresh} disabled={busy} className="text-primary disabled:opacity-50">
                {busy ? '更新中…' : '更新'}
              </button>
            </div>
          )}
          {today.warnings?.length > 0 && (
            <div className="truncate text-[10px] text-amber-600" title={today.warnings.join('；')}>
              {today.warnings[0]}
            </div>
          )}
        </>
      )}
    </div>
  )
}
