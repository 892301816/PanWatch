import { useCallback, useEffect, useState } from 'react'
import { Flame, RefreshCw, TrendingUp, ExternalLink, ChevronDown, ChevronRight } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import StockInsightModal from '@panwatch/biz-ui/components/stock-insight-modal'

interface ThemeStock {
  code: string
  name: string
  change_pct?: number | null
  limit_up_days?: number
  industry?: string
}

interface ThemeNews {
  source: string
  title: string
  time: string
  url: string
  importance?: number
}

interface ThemeItem {
  code: string
  name: string
  rank: number
  score: number
  limit_up_count: number
  limit_up_ratio: number
  max_limit_up_days: number
  member_count: number
  main_net_inflow?: number | null
  change_pct?: number | null
  up_count: number
  leader_name: string
  leader_code: string
  limit_up_stocks: ThemeStock[]
  news: ThemeNews[]
  ai_comment: string
}

interface DailyResponse {
  trade_date: string
  count: number
  items: ThemeItem[]
  disclaimer: string
}

interface ThemeDetail {
  code: string
  name: string
  flash_news: ThemeNews[]
  stock_news: ThemeNews[]
  limit_up_stocks: ThemeStock[]
  ai_comment: string
}

interface EvolutionSeries {
  code: string
  name: string
  ranks: (number | null)[]
  days_on: number
}

interface EvolutionResponse {
  dates: string[]
  series: EvolutionSeries[]
  new: { code: string; name: string }[]
  dropped: { code: string; name: string }[]
  persistent: EvolutionSeries[]
}

function fmtYi(v?: number | null) {
  if (v == null) return '—'
  return `${(v / 1e8).toFixed(2)}亿`
}

function fmtPct(v?: number | null) {
  if (v == null) return '—'
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
}

export default function ThemePanel() {
  const [data, setData] = useState<DailyResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [detail, setDetail] = useState<ThemeDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [tab, setTab] = useState<'daily' | 'evolution'>('daily')
  const [evolution, setEvolution] = useState<EvolutionResponse | null>(null)
  // 个股详情用弹窗展示 —— 与首页「今日要紧事」同一套交互。
  // 注意不能 navigate('/stocks?symbol=…'):个股页的真实路由是 /portfolio,且它不读 URL 参数。
  const [modal, setModal] = useState<{ open: boolean; symbol: string; name: string }>({
    open: false,
    symbol: '',
    name: '',
  })

  const load = useCallback(async () => {
    try {
      setError('')
      const res = await fetchAPI<DailyResponse>('/themes/daily')
      setData(res)
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (tab !== 'evolution' || evolution) return
    fetchAPI<EvolutionResponse>('/themes/evolution?days=5')
      .then(setEvolution)
      .catch(() => setEvolution({ dates: [], series: [], new: [], dropped: [], persistent: [] }))
  }, [tab, evolution])

  const openTheme = async (code: string) => {
    if (expanded === code) {
      setExpanded(null)
      return
    }
    setExpanded(code)
    setDetail(null)
    setDetailLoading(true)
    try {
      const d = await fetchAPI<ThemeDetail>(`/themes/${code}/news`)
      setDetail(d)
    } catch {
      setDetail(null)
    } finally {
      setDetailLoading(false)
    }
  }

  const doRefresh = async () => {
    setRefreshing(true)
    try {
      await fetchAPI('/themes/refresh', { method: 'POST' })
      setEvolution(null)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : '刷新失败')
    } finally {
      setRefreshing(false)
    }
  }

  const openStock = (symbol: string, name = '') => {
    if (symbol) setModal({ open: true, symbol, name })
  }

  return (
    <div className="card p-4 lg:col-span-12">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Flame className="h-4 w-4 text-orange-500" />
        <h2 className="text-sm font-semibold">每日炒作题材</h2>
        {data && (
          <span className="text-[11px] text-muted-foreground">
            {data.trade_date}（涨停池 + 概念板块 + 快讯 综合得出）
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          <div className="flex overflow-hidden rounded border border-border text-[11px]">
            {(['daily', 'evolution'] as const).map(t => (
              <button
                key={t}
                type="button"
                onClick={() => setTab(t)}
                className={`px-2 py-0.5 transition-colors ${tab === t ? 'bg-primary text-primary-foreground' : 'hover:bg-accent'}`}
              >
                {t === 'daily' ? '当日榜' : '5日演进'}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={doRefresh}
            disabled={refreshing}
            className="inline-flex items-center gap-1 text-[11px] text-muted-foreground transition-colors hover:text-primary disabled:opacity-50"
            title="重算并落库（约 20 秒）"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${refreshing ? 'animate-spin' : ''}`} />
            重算
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-2 rounded bg-destructive/10 px-2 py-1 text-[11px] text-destructive">{error}</div>
      )}

      {loading ? (
        <div className="py-8 text-center text-[12px] text-muted-foreground">加载中…</div>
      ) : tab === 'daily' ? (
        !data || data.items.length === 0 ? (
          <div className="py-8 text-center text-[12px] text-muted-foreground">
            暂无题材数据 · 点右上「重算」生成
          </div>
        ) : (
          <div className="divide-y divide-border/40">
            {data.items.map(t => {
              const isOpen = expanded === t.code
              return (
                <div key={t.code}>
                  <div
                    className="flex cursor-pointer items-center gap-3 py-2 hover:bg-accent/30"
                    onClick={() => void openTheme(t.code)}
                  >
                    {isOpen ? (
                      <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                    ) : (
                      <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                    )}
                    <span className="w-5 shrink-0 text-center text-[11px] font-semibold text-muted-foreground">
                      {t.rank}
                    </span>
                    <span className="w-28 shrink-0 truncate text-[13px] font-medium" title={t.name}>
                      {t.name}
                    </span>
                    <span className="w-12 shrink-0 text-right text-[12px] font-semibold text-orange-500">
                      {t.score.toFixed(0)}
                    </span>
                    <span className="shrink-0 rounded bg-red-500/15 px-1.5 text-[10px] text-red-600 dark:text-red-400">
                      涨停 {t.limit_up_count}
                    </span>
                    {t.max_limit_up_days > 1 && (
                      <span className="shrink-0 rounded bg-amber-500/15 px-1.5 text-[10px] text-amber-600 dark:text-amber-400">
                        {t.max_limit_up_days}板
                      </span>
                    )}
                    <span className="hidden shrink-0 text-[11px] text-muted-foreground sm:inline">
                      {fmtPct(t.change_pct)}
                    </span>
                    <span className="hidden shrink-0 text-[11px] text-muted-foreground md:inline">
                      净流入 {fmtYi(t.main_net_inflow)}
                    </span>
                    <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">
                      龙头 {t.leader_name || '—'}
                    </span>
                  </div>

                  {t.ai_comment && (
                    <div className="pb-2 pl-9 pr-2 text-[11px] leading-relaxed text-muted-foreground">
                      <TrendingUp className="mr-1 inline h-3 w-3 text-primary" />
                      {t.ai_comment}
                    </div>
                  )}

                  {isOpen && (
                    <div className="mb-2 ml-9 rounded border border-border/60 bg-accent/20 p-2.5">
                      {detailLoading ? (
                        <div className="py-3 text-center text-[11px] text-muted-foreground">加载中…</div>
                      ) : !detail ? (
                        <div className="py-3 text-center text-[11px] text-muted-foreground">详情加载失败</div>
                      ) : (
                        <div className="space-y-2.5">
                          <div>
                            <div className="mb-1 text-[11px] font-medium text-muted-foreground">
                              涨停股（{detail.limit_up_stocks.length}）
                            </div>
                            <div className="flex flex-wrap gap-1">
                              {detail.limit_up_stocks.map(s => (
                                <button
                                  key={s.code}
                                  type="button"
                                  onClick={() => openStock(s.code, s.name)}
                                  className="rounded bg-background px-1.5 py-0.5 text-[11px] transition-colors hover:bg-primary hover:text-primary-foreground"
                                  title={s.industry}
                                >
                                  {s.name}
                                  {s.limit_up_days && s.limit_up_days > 1 ? ` ${s.limit_up_days}板` : ''}
                                </button>
                              ))}
                            </div>
                          </div>

                          {detail.flash_news.length > 0 && (
                            <div>
                              <div className="mb-1 text-[11px] font-medium text-muted-foreground">
                                相关快讯（{detail.flash_news.length}）
                              </div>
                              <div className="space-y-1">
                                {detail.flash_news.map((n, i) => (
                                  <div key={i} className="flex items-start gap-2 text-[11px]">
                                    <span className="shrink-0 text-muted-foreground">{n.time}</span>
                                    <span className="shrink-0 rounded bg-accent px-1 text-[9px] text-muted-foreground">
                                      {n.source}
                                    </span>
                                    {n.url ? (
                                      <a
                                        href={n.url}
                                        target="_blank"
                                        rel="noreferrer"
                                        className="min-w-0 flex-1 hover:text-primary hover:underline"
                                      >
                                        {n.title}
                                        <ExternalLink className="ml-1 inline h-3 w-3 opacity-60" />
                                      </a>
                                    ) : (
                                      <span className="min-w-0 flex-1">{n.title}</span>
                                    )}
                                  </div>
                                ))}
                              </div>
                            </div>
                          )}

                          {detail.stock_news.length > 0 && (
                            <div>
                              <div className="mb-1 text-[11px] font-medium text-muted-foreground">
                                涨停股个股新闻（{detail.stock_news.length}）
                              </div>
                              <div className="space-y-1">
                                {detail.stock_news.slice(0, 8).map((n, i) => (
                                  <div key={i} className="flex items-start gap-2 text-[11px]">
                                    <span className="shrink-0 text-muted-foreground">{n.time}</span>
                                    <span className="shrink-0 rounded bg-accent px-1 text-[9px] text-muted-foreground">
                                      {n.source}
                                    </span>
                                    {n.url ? (
                                      <a
                                        href={n.url}
                                        target="_blank"
                                        rel="noreferrer"
                                        className="min-w-0 flex-1 hover:text-primary hover:underline"
                                      >
                                        {n.title}
                                      </a>
                                    ) : (
                                      <span className="min-w-0 flex-1">{n.title}</span>
                                    )}
                                  </div>
                                ))}
                              </div>
                            </div>
                          )}

                          {detail.flash_news.length === 0 && detail.stock_news.length === 0 && (
                            <div className="text-[11px] text-muted-foreground">该题材暂无匹配的新闻快讯</div>
                          )}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )
      ) : (
        <EvolutionView data={evolution} />
      )}

      {data?.disclaimer && (
        <div className="mt-2 text-[10px] leading-relaxed text-muted-foreground/70">{data.disclaimer}</div>
      )}

      <StockInsightModal
        open={modal.open}
        onOpenChange={o => setModal(m => ({ ...m, open: o }))}
        symbol={modal.symbol}
        market="CN"
        stockName={modal.name}
      />
    </div>
  )
}

function EvolutionView({ data }: { data: EvolutionResponse | null }) {
  if (!data) return <div className="py-8 text-center text-[12px] text-muted-foreground">加载中…</div>
  if (data.dates.length < 2) {
    return (
      <div className="py-8 text-center text-[12px] text-muted-foreground">
        只有 {data.dates.length} 天数据，演进需要至少 2 个交易日 · 先让定时任务跑几天
      </div>
    )
  }
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-3 text-[11px]">
        {data.persistent.length > 0 && (
          <div>
            <span className="font-medium text-orange-500">持续发酵：</span>
            {data.persistent.slice(0, 6).map(s => (
              <span key={s.code} className="mr-2">
                {s.name}（{s.days_on}天）
              </span>
            ))}
          </div>
        )}
        {data.new.length > 0 && (
          <div>
            <span className="font-medium text-red-500">新上榜：</span>
            {data.new.slice(0, 8).map(s => (
              <span key={s.code} className="mr-2">
                {s.name}
              </span>
            ))}
          </div>
        )}
        {data.dropped.length > 0 && (
          <div>
            <span className="font-medium text-muted-foreground">掉榜：</span>
            {data.dropped.slice(0, 8).map(s => (
              <span key={s.code} className="mr-2">
                {s.name}
              </span>
            ))}
          </div>
        )}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-[11px]">
          <thead>
            <tr className="text-muted-foreground">
              <th className="py-1 pr-3 text-left font-medium">题材</th>
              {data.dates.map(d => (
                <th key={d} className="px-2 py-1 text-center font-medium">
                  {d.slice(5)}
                </th>
              ))}
              <th className="px-2 py-1 text-center font-medium">上榜</th>
            </tr>
          </thead>
          <tbody>
            {data.series.slice(0, 12).map(s => (
              <tr key={s.code} className="border-t border-border/40">
                <td className="py-1 pr-3">{s.name}</td>
                {s.ranks.map((r, i) => (
                  <td key={i} className="px-2 py-1 text-center">
                    {r == null ? (
                      <span className="text-muted-foreground/40">·</span>
                    ) : (
                      <span className={r <= 3 ? 'font-semibold text-orange-500' : ''}>{r}</span>
                    )}
                  </td>
                ))}
                <td className="px-2 py-1 text-center text-muted-foreground">{s.days_on}天</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
