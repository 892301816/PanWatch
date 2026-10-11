import { useCallback, useEffect, useMemo, useState } from 'react'
import { Newspaper, RefreshCw, Sparkles, Star, ExternalLink } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import StockInsightModal from '@panwatch/biz-ui/components/stock-insight-modal'

interface MessageItem {
  id: string
  source: string
  title: string
  content: string
  time: string
  url: string
  category: string
  importance: number
  symbols: string[]
  in_watchlist: boolean
}

interface FeedResponse {
  total: number
  returned: number
  hours: number
  watchlist_size: number
  watchlist_hits: number
  category_counts: Record<string, number>
  source_counts: Record<string, number>
  items: MessageItem[]
  brief: string
  disclaimer: string
}

const HOUR_OPTIONS = [
  { value: 6, label: '6小时' },
  { value: 24, label: '24小时' },
  { value: 72, label: '3天' },
  { value: 168, label: '7天' },
]

const CATEGORY_COLORS: Record<string, string> = {
  政策监管: 'bg-blue-500/15 text-blue-600 dark:text-blue-400',
  宏观数据: 'bg-violet-500/15 text-violet-600 dark:text-violet-400',
  公司事件: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400',
  行业催化: 'bg-amber-500/15 text-amber-600 dark:text-amber-400',
  海外地缘: 'bg-rose-500/15 text-rose-600 dark:text-rose-400',
  其他: 'bg-accent text-muted-foreground',
}

export default function MessagesPage() {
  const [data, setData] = useState<FeedResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [hours, setHours] = useState(24)
  const [category, setCategory] = useState<string>('')
  const [watchlistOnly, setWatchlistOnly] = useState(false)
  const [briefLoading, setBriefLoading] = useState(false)
  const [modal, setModal] = useState<{ open: boolean; symbol: string; name: string }>({
    open: false,
    symbol: '',
    name: '',
  })

  const load = useCallback(
    async (withBrief = false) => {
      try {
        setError('')
        const qs = new URLSearchParams({ hours: String(hours), limit: '300' })
        if (category) qs.set('category', category)
        if (watchlistOnly) qs.set('watchlist_only', 'true')
        if (withBrief) qs.set('with_brief', 'true')
        const res = await fetchAPI<FeedResponse>(`/messages/feed?${qs.toString()}`)
        setData(res)
      } catch (e) {
        setError(e instanceof Error ? e.message : '加载失败')
      } finally {
        setLoading(false)
        setBriefLoading(false)
      }
    },
    [hours, category, watchlistOnly],
  )

  useEffect(() => {
    setLoading(true)
    void load()
  }, [load])

  const categories = useMemo(() => {
    if (!data) return []
    // 保持与我给出的一致的展示顺序，而不是按数量
    const order = ['政策监管', '宏观数据', '公司事件', '行业催化', '海外地缘', '其他']
    return order.filter(c => (data.category_counts[c] || 0) > 0)
  }, [data])

  // 无筛选时按分类分组展示；选了某个分类就是平铺
  const grouped = useMemo(() => {
    if (!data) return []
    if (category) return [{ cat: category, items: data.items }]
    const buckets = new Map<string, MessageItem[]>()
    for (const m of data.items) {
      const list = buckets.get(m.category) || []
      list.push(m)
      buckets.set(m.category, list)
    }
    const order = ['政策监管', '宏观数据', '公司事件', '行业催化', '海外地缘', '其他']
    return order
      .filter(c => buckets.has(c))
      .map(c => ({ cat: c, items: buckets.get(c)! }))
  }, [data, category])

  return (
    <div className="mx-auto max-w-5xl space-y-3 p-3">
      <div className="card p-4">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <Newspaper className="h-4 w-4 text-primary" />
          <h1 className="text-sm font-semibold">消息</h1>
          <span className="text-[11px] text-muted-foreground">
            仅收集与分类，不做优劣判断 · 按关键词规则归类
          </span>
        </div>

        {/* 筛选器 */}
        <div className="flex flex-wrap items-center gap-2 text-[11px]">
          <div className="flex overflow-hidden rounded border border-border">
            {HOUR_OPTIONS.map(o => (
              <button
                key={o.value}
                type="button"
                onClick={() => setHours(o.value)}
                className={`px-2 py-0.5 transition-colors ${hours === o.value ? 'bg-primary text-primary-foreground' : 'hover:bg-accent'}`}
              >
                {o.label}
              </button>
            ))}
          </div>

          <button
            type="button"
            onClick={() => setWatchlistOnly(v => !v)}
            className={`inline-flex items-center gap-1 rounded border px-2 py-0.5 transition-colors ${
              watchlistOnly ? 'border-primary bg-primary/10 text-primary' : 'border-border hover:bg-accent'
            }`}
          >
            <Star className={`h-3 w-3 ${watchlistOnly ? 'fill-current' : ''}`} />
            只看自选股{data ? `（${data.watchlist_hits}）` : ''}
          </button>

          <div className="flex flex-wrap gap-1">
            <button
              type="button"
              onClick={() => setCategory('')}
              className={`rounded px-2 py-0.5 transition-colors ${!category ? 'bg-primary text-primary-foreground' : 'hover:bg-accent'}`}
            >
              全部{data ? ` ${data.total}` : ''}
            </button>
            {categories.map(c => (
              <button
                key={c}
                type="button"
                onClick={() => setCategory(c === category ? '' : c)}
                className={`rounded px-2 py-0.5 transition-colors ${
                  category === c ? 'bg-primary text-primary-foreground' : 'hover:bg-accent'
                }`}
              >
                {c} {data?.category_counts[c]}
              </button>
            ))}
          </div>

          <div className="ml-auto flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              className="h-6 text-[11px]"
              disabled={briefLoading}
              onClick={() => {
                setBriefLoading(true)
                void load(true)
              }}
            >
              <Sparkles className={`h-3 w-3 ${briefLoading ? 'animate-pulse' : ''}`} />
              {briefLoading ? '生成中…' : 'AI 汇总'}
            </Button>
            <button
              type="button"
              onClick={() => void load()}
              className="text-muted-foreground transition-colors hover:text-primary"
              title="刷新"
            >
              <RefreshCw className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>

        {data && (
          <div className="mt-2 text-[11px] text-muted-foreground">
            时间窗 {data.hours}h · 共 {data.total} 条 · 自选股 {data.watchlist_size} 只，命中 {data.watchlist_hits} 条
          </div>
        )}
      </div>

      {error && (
        <div className="rounded bg-destructive/10 px-3 py-2 text-[12px] text-destructive">{error}</div>
      )}

      {data?.brief && (
        <div className="card border-primary/30 p-3">
          <div className="mb-1 flex items-center gap-1.5 text-[11px] font-medium text-primary">
            <Sparkles className="h-3.5 w-3.5" />
            AI 汇总
          </div>
          <div className="whitespace-pre-wrap text-[12px] leading-relaxed">{data.brief}</div>
        </div>
      )}

      {loading ? (
        <div className="card py-10 text-center text-[12px] text-muted-foreground">加载中…</div>
      ) : !data || data.items.length === 0 ? (
        <div className="card py-10 text-center text-[12px] text-muted-foreground">
          该条件下暂无消息{watchlistOnly ? ' · 试试取消「只看自选股」' : ''}
        </div>
      ) : (
        grouped.map(({ cat, items }) => (
          <div key={cat} className="card p-4">
            <div className="mb-2 flex items-center gap-2">
              <span className={`rounded px-1.5 py-0.5 text-[10px] ${CATEGORY_COLORS[cat] || CATEGORY_COLORS['其他']}`}>
                {cat}
              </span>
              <span className="text-[11px] text-muted-foreground">{items.length} 条</span>
            </div>
            <div className="divide-y divide-border/40">
              {items.map(m => (
                <div key={m.id} className="flex items-start gap-2 py-1.5">
                  {m.in_watchlist && (
                    <Star className="mt-0.5 h-3 w-3 shrink-0 fill-amber-400 text-amber-400" />
                  )}
                  <span className="mt-0.5 shrink-0 font-mono text-[11px] text-muted-foreground">
                    {m.time}
                  </span>
                  <span className="mt-0.5 shrink-0 rounded bg-accent px-1 text-[9px] text-muted-foreground">
                    {m.source}
                  </span>
                  <div className="min-w-0 flex-1">
                    {m.url ? (
                      <a
                        href={m.url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-[12px] leading-snug hover:text-primary hover:underline"
                      >
                        {m.title}
                        <ExternalLink className="ml-1 inline h-3 w-3 opacity-50" />
                      </a>
                    ) : (
                      <span className="text-[12px] leading-snug">{m.title}</span>
                    )}
                    {m.symbols.length > 0 && (
                      <span className="ml-1 inline-flex flex-wrap gap-1 align-middle">
                        {m.symbols.slice(0, 4).map(s => (
                          <button
                            key={s}
                            type="button"
                            onClick={() => setModal({ open: true, symbol: s, name: '' })}
                            className="rounded bg-accent px-1 text-[9px] text-muted-foreground transition-colors hover:bg-primary hover:text-primary-foreground"
                          >
                            {s}
                          </button>
                        ))}
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))
      )}

      {data?.disclaimer && (
        <div className="px-1 text-[10px] leading-relaxed text-muted-foreground/70">{data.disclaimer}</div>
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
