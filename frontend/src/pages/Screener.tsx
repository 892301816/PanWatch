import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  ExternalLink,
  Loader2,
  Plus,
  RefreshCw,
  Save,
  Sparkles,
  Star,
  Trash2,
  X,
} from 'lucide-react'
import StockInsightModal from '@panwatch/biz-ui/components/stock-insight-modal'
import {
  fetchAPI,
  screenerApi,
  stocksApi,
  type BoardItem,
  type LadderResponse,
  type ScreenerCondition,
  type ScreenerField,
  type ScreenerItem,
  type ScreenerMetaResponse,
  type ScreenerStrategy,
} from '@panwatch/api'

type View = 'screen' | 'ladder' | 'boards'

const PAGE_SIZE = 80

interface KlineBar {
  date: string
  open: number
  close: number
  high: number
  low: number
  volume: number
}

const fmt = (v: number | null | undefined, digits = 2) =>
  v == null || Number.isNaN(v) ? '--' : v.toFixed(digits)

const pctText = (v: number | null | undefined) =>
  v == null || Number.isNaN(v) ? '--' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`

function Sparkline({ data, positive }: { data: number[]; positive: boolean }) {
  const color = positive ? '#E53935' : '#43A047'
  if (!data || data.length === 0) {
    return (
      <div className="flex h-9 w-full items-center justify-center rounded bg-muted/30 text-[10px] text-muted-foreground">
        暂无走势
      </div>
    )
  }
  if (data.length === 1) {
    // 新股只有 1 个交易日:画一条基准线 + 点,避免整块空白
    return (
      <svg viewBox="0 0 132 36" className="h-9 w-full" preserveAspectRatio="none">
        <line x1="0" y1="18" x2="132" y2="18" stroke="currentColor" className="text-border" strokeDasharray="3 3" strokeWidth="0.8" />
        <circle cx="66" cy="18" r="2.6" fill={color} />
        <text x="72" y="15" fontSize="8" fill="#8b929c">上市首日</text>
      </svg>
    )
  }
  const w = 132
  const h = 36
  const min = Math.min(...data)
  const max = Math.max(...data)
  const span = max - min || 1
  const pts = data.map((v, i) => {
    const x = (i / (data.length - 1)) * w
    const y = h - ((v - min) / span) * (h - 4) - 2
    return `${x.toFixed(1)},${y.toFixed(1)}`
  })
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="h-9 w-full" preserveAspectRatio="none">
      <line x1="0" y1={h / 2} x2={w} y2={h / 2} stroke="currentColor" className="text-border" strokeDasharray="3 3" strokeWidth="0.8" />
      <polyline points={pts.join(' ')} fill="none" stroke={color} strokeWidth="1.4" strokeLinejoin="round" />
    </svg>
  )
}

function KlineChart({ bars }: { bars: KlineBar[] }) {
  if (!bars.length) {
    return <div className="flex h-[200px] items-center justify-center text-xs text-muted-foreground">K 线加载中…</div>
  }
  const W = 420
  const H = 210
  const padR = 44
  const volH = 38
  const plotH = H - volH - 16
  const hi = Math.max(...bars.map((b) => b.high))
  const lo = Math.min(...bars.map((b) => b.low))
  const span = hi - lo || 1
  const bw = (W - padR) / bars.length
  const cw = Math.max(1.4, bw * 0.58)
  const y = (v: number) => ((hi - v) / span) * plotH + 4

  const ma = (n: number) => {
    const out: Array<{ x: number; y: number }> = []
    for (let i = n - 1; i < bars.length; i++) {
      let sum = 0
      for (let j = 0; j < n; j++) sum += bars[i - j].close
      out.push({ x: i * bw + bw / 2, y: y(sum / n) })
    }
    return out
  }
  const ma5 = ma(5)
  const ma10 = ma(10)
  const maxVol = Math.max(...bars.map((b) => b.volume || 0)) || 1

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
      {[0, 1, 2, 3].map((i) => {
        const gy = 4 + (plotH * i) / 3
        return (
          <g key={i}>
            <line x1="0" y1={gy} x2={W - padR} y2={gy} stroke="currentColor" className="text-border" strokeWidth="0.5" />
            <text x={W - padR + 4} y={gy + 3} fontSize="9" className="fill-muted-foreground">
              {(hi - (span * i) / 3).toFixed(2)}
            </text>
          </g>
        )
      })}
      {bars.map((b, i) => {
        const x = i * bw + bw / 2
        const rise = b.close >= b.open
        const color = rise ? '#E53935' : '#43A047'
        const top = y(Math.max(b.open, b.close))
        const height = Math.max(1, Math.abs(y(b.open) - y(b.close)))
        return (
          <g key={b.date}>
            <line x1={x} y1={y(b.high)} x2={x} y2={y(b.low)} stroke={color} strokeWidth="1" />
            <rect x={x - cw / 2} y={top} width={cw} height={height} fill={color} />
            <rect
              x={x - cw / 2}
              y={H - 8 - ((b.volume || 0) / maxVol) * volH}
              width={cw}
              height={((b.volume || 0) / maxVol) * volH}
              fill={color}
              opacity="0.45"
            />
          </g>
        )
      })}
      <polyline points={ma5.map((p) => `${p.x},${p.y}`).join(' ')} fill="none" stroke="#e8b339" strokeWidth="1" />
      <polyline points={ma10.map((p) => `${p.x},${p.y}`).join(' ')} fill="none" stroke="#5b9bd5" strokeWidth="1" />
      <text x="4" y="10" fontSize="9" fill="#e8b339">MA5</text>
      <text x="32" y="10" fontSize="9" fill="#5b9bd5">MA10</text>
    </svg>
  )
}

function conditionLabel(cond: ScreenerCondition, fields: ScreenerField[], ops: Record<string, string>) {
  const field = fields.find((f) => f.key === cond.field)
  const label = field?.label || cond.field
  const unit = field?.unit || ''
  const op = ops[cond.op] || cond.op
  const value = cond.value
  if (Array.isArray(value)) {
    if (cond.op === 'between') return `${label} ${value[0]} ~ ${value[1]}${unit}`
    return `${label} ${op} ${value.join(' / ')}`
  }
  if (field?.type === 'boolean') return `${label} ${op} ${value ? '是' : '否'}`
  return `${label} ${op} ${value}${unit}`
}

export default function ScreenerPage() {
  const [meta, setMeta] = useState<ScreenerMetaResponse | null>(null)
  const [view, setView] = useState<View>('screen')

  const [conditions, setConditions] = useState<ScreenerCondition[]>([])
  const [logic, setLogic] = useState<'all' | 'any'>('all')
  const [exclude, setExclude] = useState<string[]>(['st'])
  const [orderBy, setOrderBy] = useState('change_pct desc')
  const [activeCode, setActiveCode] = useState('')
  const [items, setItems] = useState<ScreenerItem[]>([])
  const [page, setPage] = useState(1)
  const [sparks, setSparks] = useState<Record<string, number[]>>({})
  const [watchlist, setWatchlist] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [adding, setAdding] = useState(false)
  const [addMsg, setAddMsg] = useState('')
  const [tradeDate, setTradeDate] = useState('')
  const [universe, setUniverse] = useState(0)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')
  const [appliedLogic, setAppliedLogic] = useState<'all' | 'any'>('all')

  const [aiText, setAiText] = useState('')
  const [aiOpen, setAiOpen] = useState(false)
  const [parsing, setParsing] = useState(false)
  const [aiNote, setAiNote] = useState('')
  const [watchGroups, setWatchGroups] = useState<Array<{ name: string; count: number }>>([])
  const [targetGroup, setTargetGroup] = useState('')
  const [newGroup, setNewGroup] = useState('')

  const [sectorFilter, setSectorFilter] = useState<string | null>(null)
  const [builderOpen, setBuilderOpen] = useState(false)
  const [draftField, setDraftField] = useState('change_pct')
  const [draftOp, setDraftOp] = useState('gte')
  const [draftValue, setDraftValue] = useState('')

  const [saving, setSaving] = useState(false)
  const [saveMode, setSaveMode] = useState(false)
  const [groupName, setGroupName] = useState('')
  const [groupMsg, setGroupMsg] = useState('')

  const [ladder, setLadder] = useState<LadderResponse | null>(null)
  const [ladderLoading, setLadderLoading] = useState(false)
  const [boards, setBoards] = useState<BoardItem[]>([])
  const [boardsLoading, setBoardsLoading] = useState(false)

  const [detail, setDetail] = useState<ScreenerItem | null>(null)
  const [insightOpen, setInsightOpen] = useState(false)
  const [kline, setKline] = useState<KlineBar[]>([])

  const fields = meta?.fields || []
  const ops = meta?.ops || {}
  const presets = meta?.presets || []
  const rules = meta?.rules || []
  const groups = useMemo(() => [...presets, ...rules], [presets, rules])

  const loadMeta = useCallback(async () => {
    try {
      const data = await screenerApi.meta()
      setMeta(data)
      return data
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载配置失败')
      return null
    }
  }, [])

  const run = useCallback(
    async (conds: ScreenerCondition[], opts?: { code?: string; logic?: 'all' | 'any'; exclude?: string[]; orderBy?: string }) => {
      setRunning(true)
      setError('')
      setConditions(conds)
      if (opts?.logic) setLogic(opts.logic)
      if (opts?.exclude) setExclude(opts.exclude)
      if (opts?.orderBy) setOrderBy(opts.orderBy)
      if (opts?.code !== undefined) setActiveCode(opts.code)
      try {
        const res = await screenerApi.run({
          conditions: conds,
          logic: opts?.logic || logic,
          exclude: opts?.exclude || exclude,
          order_by: opts?.orderBy || orderBy,
          limit: 0,           // 0 = 返回全部，前端分页
          with_spark: false,  // 走势按页懒加载
        })
        setItems(res.items)
        setTradeDate(res.trade_date)
        setUniverse(res.universe)
        setAppliedLogic(res.applied?.logic || opts?.logic || 'all')
        setPage(1)
        setSparks({})
      } catch (e) {
        setError(e instanceof Error ? e.message : '选股失败')
        setItems([])
      } finally {
        setRunning(false)
      }
    },
    [logic, exclude, orderBy]
  )

  const applyStrategy = useCallback(
    (s: ScreenerStrategy) => {
      setSectorFilter(null)
      void run(s.conditions || [], {
        code: s.code,
        logic: (s.logic as 'all' | 'any') || 'all',
        exclude: s.exclude || ['st'],
        orderBy: s.order_by || 'change_pct desc',
      })
    },
    [run]
  )

  useEffect(() => {
    void (async () => {
      const data = await loadMeta()
      const first = data?.presets?.[0]
      if (first) applyStrategy(first)
      else void run([], { code: 'all' })
    })()
    stocksApi
      .list()
      .then((rows) => setWatchlist(new Set(rows.map((r) => r.symbol))))
      .catch(() => {})
    stocksApi
      .groups()
      .then(setWatchGroups)
      .catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (view === 'ladder' && !ladder) {
      setLadderLoading(true)
      screenerApi
        .ladder()
        .then(setLadder)
        .catch((e) => setError(e instanceof Error ? e.message : '天梯加载失败'))
        .finally(() => setLadderLoading(false))
    }
    if ((view === 'boards' || view === 'ladder') && !boards.length) {
      setBoardsLoading(true)
      screenerApi
        .boards(undefined, 30)
        .then((r) => setBoards(r.items))
        .catch((e) => setError(e instanceof Error ? e.message : '板块加载失败'))
        .finally(() => setBoardsLoading(false))
    }
  }, [view, ladder, boards.length])

  useEffect(() => {
    if (!detail) {
      setKline([])
      return
    }
    let alive = true
    setKline([])
    fetchAPI<{ klines: KlineBar[] }>(`/klines/${detail.symbol}?market=CN&days=60`)
      .then((r) => alive && setKline(r.klines || []))
      .catch(() => alive && setKline([]))
    return () => {
      alive = false
    }
  }, [detail])

  const onParse = async () => {
    const text = aiText.trim()
    if (!text) return
    setParsing(true)
    setAiNote('')
    try {
      const res = await screenerApi.parse(text)
      const note = [res.explanation, ...(res.warnings || [])].filter(Boolean).join(' · ')
      setAiNote(note || (res.ok ? '' : '没有识别出有效条件'))
      if (res.conditions?.length) {
        // 先把条件落到状态里(即使随后的执行失败,条件 chips 仍然保留)
        setConditions(res.conditions)
        setLogic(res.logic)
        setExclude(res.exclude)
        setOrderBy(res.order_by)
        await run(res.conditions, {
          code: 'ai',
          logic: res.logic,
          exclude: res.exclude,
          orderBy: res.order_by,
        })
      }
    } catch (e) {
      setAiNote(e instanceof Error ? e.message : 'AI 解析失败')
    } finally {
      setParsing(false)
    }
  }

  const addCondition = () => {
    const field = fields.find((f) => f.key === draftField)
    let value: unknown = draftValue.trim()
    if (field?.type === 'number') {
      if (draftOp === 'between') {
        const nums = draftValue
          .split(/[,，~-]/)
          .map((x) => Number(x.trim()))
          .filter((n) => !Number.isNaN(n))
        if (nums.length !== 2) {
          setError('区间条件需要两个数字，例如 5,20')
          return
        }
        value = [Math.min(...nums), Math.max(...nums)]
      } else {
        const n = Number(draftValue)
        if (Number.isNaN(n)) {
          setError('请输入数字')
          return
        }
        value = n
      }
    } else if (field?.type === 'list') {
      value = draftValue.split(/[,，]/).map((x) => x.trim()).filter(Boolean)
    }
    if (value === '' || (Array.isArray(value) && !value.length)) {
      setError('请填写条件值')
      return
    }
    const next = [...conditions, { field: draftField, op: draftOp, value }]
    setBuilderOpen(false)
    setDraftValue('')
    setError('')
    void run(next, { code: 'custom' })
  }

  const removeCondition = (index: number) => {
    const next = conditions.filter((_, i) => i !== index)
    void run(next, { code: 'custom' })
  }

  const saveGroup = async () => {
    const name = groupName.trim() || aiNote.slice(0, 20) || '我的分组'
    setSaving(true)
    try {
      await screenerApi.createRule({
        name,
        conditions,
        logic,
        exclude,
        order_by: orderBy,
        limit: 120,
      })
      setGroupMsg(`已保存「${name}」`)
      setGroupName('')
      setSaveMode(false)
      await loadMeta()
      setTimeout(() => setGroupMsg(''), 2500)
    } catch (e) {
      setGroupMsg(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const deleteRule = async (rule: ScreenerStrategy) => {
    if (!rule.id) return
    try {
      await screenerApi.deleteRule(rule.id)
      await loadMeta()
    } catch (e) {
      setError(e instanceof Error ? e.message : '删除失败')
    }
  }

  const refreshAll = async () => {
    setRunning(true)
    setError('')
    try {
      await screenerApi.refresh()
      setLadder(null)
      setBoards([])
      await loadMeta()
      await run(conditions, { code: activeCode })
    } catch (e) {
      setError(e instanceof Error ? e.message : '刷新失败')
    } finally {
      setRunning(false)
    }
  }

  const visibleItems = useMemo(() => {
    if (!sectorFilter) return items
    return items.filter((it) => it.concepts.some((c) => c.name === sectorFilter))
  }, [items, sectorFilter])

  const totalPages = Math.max(1, Math.ceil(visibleItems.length / PAGE_SIZE))
  const pagedItems = useMemo(
    () => visibleItems.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE),
    [visibleItems, page]
  )
  const sparkKey = pagedItems.map((i) => i.symbol).join(',')

  useEffect(() => {
    setPage(1)
  }, [sectorFilter])

  useEffect(() => {
    if (page > totalPages) setPage(totalPages)
  }, [page, totalPages])

  // 按页懒加载迷你走势:本地库优先,新股等缺失的回退 K 线接口
  useEffect(() => {
    if (!sparkKey) return
    const need = pagedItems.filter((i) => !sparks[i.symbol]).map((i) => i.symbol)
    if (!need.length) return
    let alive = true
    fetchAPI<{ data: Record<string, number[]> }>(
      `/dataset/spark?symbols=${need.join(',')}&days=20`
    )
      .then((r) => {
        if (alive) setSparks((prev) => ({ ...prev, ...(r.data || {}) }))
      })
      .catch(() => {})
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sparkKey])

  const toggleSelect = (symbol: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(symbol)) next.delete(symbol)
      else next.add(symbol)
      return next
    })
  }

  const pageAllSelected =
    pagedItems.length > 0 && pagedItems.every((i) => selected.has(i.symbol))

  const togglePage = () => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (pageAllSelected) pagedItems.forEach((i) => next.delete(i.symbol))
      else pagedItems.forEach((i) => next.add(i.symbol))
      return next
    })
  }

  const addToWatchlist = async (list: ScreenerItem[]) => {
    const group = (newGroup.trim() || targetGroup || '').trim()
    const targets = list
      .filter((i) => !watchlist.has(i.symbol))
      .map((i) => ({ symbol: i.symbol, name: i.name, market: 'CN', group_name: group }))
    if (!targets.length) {
      setAddMsg('所选股票都已在自选里')
      setTimeout(() => setAddMsg(''), 2500)
      return
    }
    setAdding(true)
    try {
      const res = await stocksApi.createBatch(targets)
      setWatchlist((prev) => {
        const next = new Set(prev)
        res.added.forEach((a) => next.add(a.symbol))
        return next
      })
      setSelected(new Set())
      setNewGroup('')
      stocksApi.groups().then(setWatchGroups).catch(() => {})
      setAddMsg(
        `已加入自选 ${res.added_count} 只${group ? `（${group}）` : ''}${res.skipped_count ? `，跳过 ${res.skipped_count} 只` : ''}`
      )
    } catch (e) {
      setAddMsg(e instanceof Error ? e.message : '加入自选失败')
    } finally {
      setAdding(false)
      setTimeout(() => setAddMsg(''), 3000)
    }
  }

  return (
    <div className="mx-auto max-w-[1400px] space-y-3 p-3">
      {/* 顶部 */}
      <div className="card p-3">
        <div className="flex flex-wrap items-center gap-2">
          <Sparkles className="h-4 w-4 text-primary" />
          <h1 className="text-sm font-semibold">选股工作台</h1>
          <div className="ml-2 flex rounded-lg bg-muted p-0.5 text-xs">
            {([
              ['screen', '选股'],
              ['ladder', '连涨天梯'],
              ['boards', '板块强弱'],
            ] as Array<[View, string]>).map(([key, label]) => (
              <button
                key={key}
                onClick={() => setView(key)}
                className={`rounded-md px-3 py-1 transition ${
                  view === key ? 'bg-background font-medium text-foreground shadow-sm' : 'text-muted-foreground'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="ml-auto flex items-center gap-2 text-[11px] text-muted-foreground">
            {tradeDate && <span>数据日 {tradeDate}</span>}
            <button
              onClick={refreshAll}
              disabled={running}
              className="flex items-center gap-1 rounded-md border px-2 py-1 hover:bg-accent disabled:opacity-50"
            >
              <RefreshCw className={`h-3 w-3 ${running ? 'animate-spin' : ''}`} />
              刷新数据
            </button>
          </div>
        </div>

      </div>

      {view === 'screen' && (
        <div className="grid gap-3 lg:grid-cols-[190px_1fr]">
          {/* 分组 */}
          <div className="card h-fit p-2">
            <div className="px-2 pb-1 pt-1 text-[10px] tracking-wider text-muted-foreground">我的分组</div>
            {groups.map((g) => (
              <div key={g.code} className="group flex items-center">
                <button
                  onClick={() => applyStrategy(g)}
                  className={`flex-1 rounded-md px-2 py-1.5 text-left text-xs transition ${
                    activeCode === g.code ? 'bg-primary/10 font-medium text-primary' : 'hover:bg-accent'
                  }`}
                >
                  {g.name}
                </button>
                {g.id != null && (
                  <button
                    onClick={() => deleteRule(g)}
                    className="hidden rounded p-1 text-muted-foreground hover:text-destructive group-hover:block"
                    title="删除分组"
                  >
                    <Trash2 className="h-3 w-3" />
                  </button>
                )}
              </div>
            ))}
            <button
              onClick={() => applyStrategy({ code: 'all', name: '全部', conditions: [] })}
              className={`mt-1 w-full rounded-md px-2 py-1.5 text-left text-xs transition ${
                activeCode === 'all' ? 'bg-primary/10 font-medium text-primary' : 'hover:bg-accent text-muted-foreground'
              }`}
            >
              全部（不限条件）
            </button>
          </div>

          <div className="space-y-3">
            {/* 条件栏 */}
            <div className="card p-3">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="mr-1 text-[10px] tracking-wider text-muted-foreground">当前条件</span>
                {conditions.length === 0 && (
                  <span className="text-[11px] text-muted-foreground">未设置条件，显示全市场按涨幅排序</span>
                )}
                {conditions.map((c, i) => (
                  <span
                    key={`${c.field}-${c.op}-${i}`}
                    className="flex items-center gap-1 rounded-full border bg-muted/40 px-2 py-0.5 text-[11px]"
                  >
                    {conditionLabel(c, fields, ops)}
                    <button onClick={() => removeCondition(i)} className="text-muted-foreground hover:text-destructive">
                      <X className="h-3 w-3" />
                    </button>
                  </span>
                ))}
                {conditions.length >= 2 && (
                  <span className="ml-1 flex items-center gap-0.5 rounded-full border px-1 py-0.5 text-[11px]">
                    <span className="px-1 text-muted-foreground">满足</span>
                    <button
                      onClick={() => run(conditions, { logic: 'all', code: 'custom' })}
                      className={`rounded-full px-1.5 py-0.5 ${
                        logic === 'all' ? 'bg-primary/15 text-primary' : 'text-muted-foreground'
                      }`}
                    >
                      全部(且)
                    </button>
                    <button
                      onClick={() => run(conditions, { logic: 'any', code: 'custom' })}
                      className={`rounded-full px-1.5 py-0.5 ${
                        logic === 'any' ? 'bg-amber-500/15 text-amber-600 dark:text-amber-400' : 'text-muted-foreground'
                      }`}
                    >
                      任一(或)
                    </button>
                  </span>
                )}
                <button
                  onClick={() => setBuilderOpen((v) => !v)}
                  className="rounded-full border border-dashed px-2 py-0.5 text-[11px] text-muted-foreground hover:bg-accent"
                >
                  <Plus className="mr-0.5 inline h-3 w-3" />
                  添加条件
                </button>
                <button
                  onClick={() => setAiOpen((v) => !v)}
                  className={`rounded-full border px-2 py-0.5 text-[11px] ${
                    aiOpen
                      ? 'border-primary/40 bg-primary/10 text-primary'
                      : 'border-primary/30 text-primary hover:bg-primary/5'
                  }`}
                >
                  <Sparkles className="mr-0.5 inline h-3 w-3" />
                  AI 一句话
                </button>
              </div>

              {aiOpen && (
                <div className="mt-2 flex flex-wrap items-center gap-2 rounded-lg border border-primary/20 bg-primary/5 p-2 text-xs">
                  <Sparkles className="h-3.5 w-3.5 shrink-0 text-primary" />
                  <input
                    value={aiText}
                    onChange={(e) => setAiText(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && !parsing && onParse()}
                    placeholder="例如：最近5日有过涨停，股价小于30，不属于创业板和ST"
                    autoFocus
                    className="min-w-[240px] flex-1 rounded border bg-background px-2 py-1 outline-none"
                  />
                  <button
                    onClick={onParse}
                    disabled={parsing || !aiText.trim()}
                    className="flex items-center gap-1 rounded bg-primary px-2.5 py-1 text-primary-foreground disabled:opacity-50"
                  >
                    {parsing ? <Loader2 className="h-3 w-3 animate-spin" /> : <Sparkles className="h-3 w-3" />}
                    生成条件
                  </button>
                  <button
                    onClick={() => setAiOpen(false)}
                    className="rounded border px-2 py-1 text-muted-foreground hover:bg-accent"
                  >
                    收起
                  </button>
                </div>
              )}
              {aiNote && <div className="mt-1 text-[11px] text-muted-foreground">{aiNote}</div>}

              {builderOpen && (
                <div className="mt-2 flex flex-wrap items-center gap-2 rounded-lg border bg-muted/20 p-2 text-xs">
                  <select
                    value={draftField}
                    onChange={(e) => setDraftField(e.target.value)}
                    className="rounded border bg-background px-2 py-1"
                  >
                    {fields.filter((f) => f.available !== false).map((f) => (
                      <option key={f.key} value={f.key}>
                        {f.label}
                      </option>
                    ))}
                  </select>
                  <select
                    value={draftOp}
                    onChange={(e) => setDraftOp(e.target.value)}
                    className="rounded border bg-background px-2 py-1"
                  >
                    {['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'between', 'in', 'contains'].map((op) => (
                      <option key={op} value={op}>
                        {ops[op] || op}
                      </option>
                    ))}
                  </select>
                  <input
                    value={draftValue}
                    onChange={(e) => setDraftValue(e.target.value)}
                    placeholder={draftOp === 'between' ? '如 5,20' : '条件值'}
                    className="w-28 rounded border bg-background px-2 py-1"
                  />
                  <button onClick={addCondition} className="rounded bg-primary px-2.5 py-1 text-primary-foreground">
                    添加
                  </button>
                </div>
              )}

              <div className="mt-2 flex flex-wrap items-center gap-3 text-[11px] text-muted-foreground">
                <span>
                  共 <b className="text-foreground">{visibleItems.length}</b> 只
                  {universe > 0 && <span className="ml-1 opacity-70">/ 全市场 {universe}</span>}
                </span>
                {conditions.length >= 2 && (
                  <span className={appliedLogic === 'any' ? 'text-amber-600 dark:text-amber-400' : ''}>
                    匹配方式：{appliedLogic === 'any' ? '任一（或）' : '全部（且）'}
                  </span>
                )}
                {sectorFilter && (
                  <button
                    onClick={() => setSectorFilter(null)}
                    className="flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-primary"
                  >
                    板块：{sectorFilter}
                    <X className="h-3 w-3" />
                  </button>
                )}
                {groupMsg && <span className="text-primary">{groupMsg}</span>}
                {selected.size > 0 && (
                  <span className="flex flex-wrap items-center gap-1">
                    <select
                      value={targetGroup}
                      onChange={(e) => setTargetGroup(e.target.value)}
                      className="rounded border bg-background px-1.5 py-1 text-[11px]"
                      title="目标分组"
                    >
                      <option value="">未分组</option>
                      {watchGroups
                        .filter((g) => g.name)
                        .map((g) => (
                          <option key={g.name} value={g.name}>
                            {g.name} ({g.count})
                          </option>
                        ))}
                    </select>
                    <input
                      value={newGroup}
                      onChange={(e) => setNewGroup(e.target.value)}
                      placeholder="或新建分组"
                      className="w-24 rounded border bg-background px-1.5 py-1 text-[11px]"
                    />
                    <button
                      onClick={() => addToWatchlist(items.filter((i) => selected.has(i.symbol)))}
                      disabled={adding}
                      className="flex items-center gap-1 rounded bg-primary/10 px-2 py-1 text-primary hover:bg-primary/15 disabled:opacity-50"
                    >
                      {adding ? <Loader2 className="h-3 w-3 animate-spin" /> : <Star className="h-3 w-3" />}
                      加自选 ({selected.size})
                    </button>
                  </span>
                )}
                {addMsg && <span className="text-primary">{addMsg}</span>}
                <div className="ml-auto flex items-center gap-2">
                  {saveMode ? (
                    <>
                      <input
                        value={groupName}
                        onChange={(e) => setGroupName(e.target.value)}
                        onKeyDown={(e) => e.key === 'Enter' && !saving && saveGroup()}
                        placeholder="分组名称"
                        autoFocus
                        className="w-28 rounded border bg-background px-2 py-1"
                      />
                      <button
                        onClick={saveGroup}
                        disabled={saving}
                        className="flex items-center gap-1 rounded border px-2 py-1 hover:bg-accent disabled:opacity-50"
                      >
                        {saving ? <Loader2 className="h-3 w-3 animate-spin" /> : <Save className="h-3 w-3" />}
                        保存
                      </button>
                      <button
                        onClick={() => {
                          setSaveMode(false)
                          setGroupName('')
                        }}
                        className="rounded border px-2 py-1 text-muted-foreground hover:bg-accent"
                      >
                        取消
                      </button>
                    </>
                  ) : (
                    <button
                      onClick={() => setSaveMode(true)}
                      className="flex items-center gap-1 rounded border px-2 py-1 hover:bg-accent"
                    >
                      <Save className="h-3 w-3" />
                      存为分组
                    </button>
                  )}
                  <button
                    onClick={() => run(conditions, { code: activeCode })}
                    disabled={running}
                    className="flex items-center gap-1 rounded bg-primary px-3 py-1 font-medium text-primary-foreground disabled:opacity-50"
                  >
                    {running ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
                    运行选股
                  </button>
                </div>
              </div>
            </div>

            {error && (
              <div className="card border-destructive/40 p-2 text-xs text-destructive">{error}</div>
            )}

            {/* 列表 */}
            <div className="card overflow-hidden">
              <div className="hidden gap-3 border-b bg-muted/30 px-3 py-2 text-[10px] text-muted-foreground sm:grid sm:grid-cols-[28px_190px_140px_1fr_110px_96px]">
                <div className="flex items-center justify-center">
                  <input
                    type="checkbox"
                    checked={pageAllSelected}
                    onChange={togglePage}
                    title="全选本页"
                    className="h-3.5 w-3.5 cursor-pointer"
                  />
                </div>
                <div>股票名称 / 代码</div>
                <div>近 20 日走势</div>
                <div>所属板块 · 概念</div>
                <div className="text-right">换手 / 成交额</div>
                <div className="text-right">涨幅 / 现价</div>
              </div>

              {running && !visibleItems.length ? (
                <div className="flex items-center justify-center gap-2 py-16 text-xs text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin" />
                  选股中…
                </div>
              ) : !visibleItems.length ? (
                <div className="py-16 text-center text-xs text-muted-foreground">
                  没有符合条件的个股，试着放宽条件
                </div>
              ) : (
                pagedItems.map((it) => {
                  const up = (it.change_pct || 0) >= 0
                  return (
                    <div
                      key={it.symbol}
                      onClick={() => setDetail(it)}
                      className="grid cursor-pointer gap-3 border-b px-3 py-2 last:border-0 hover:bg-accent/40 sm:grid-cols-[28px_190px_140px_1fr_110px_96px] sm:items-center"
                    >
                      <div
                        className="hidden items-center justify-center sm:flex"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <input
                          type="checkbox"
                          checked={selected.has(it.symbol)}
                          onChange={() => toggleSelect(it.symbol)}
                          className="h-3.5 w-3.5 cursor-pointer"
                        />
                      </div>
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-1">
                          <span className="truncate text-sm font-semibold">{it.name}</span>
                          {watchlist.has(it.symbol) && (
                            <span className="rounded bg-muted px-1 py-0.5 text-[10px] text-muted-foreground">
                              已自选
                            </span>
                          )}
                          {it.tags.map((t) => (
                            <span key={t} className="rounded bg-stock-up/10 px-1 py-0.5 text-[10px] text-stock-up">
                              {t}
                            </span>
                          ))}
                        </div>
                        <div className="mt-0.5 text-[11px] text-muted-foreground">{it.symbol}</div>
                      </div>

                      <div className="hidden sm:block">
                        <Sparkline data={sparks[it.symbol] || it.spark || []} positive={up} />
                      </div>

                      <div className="flex flex-wrap gap-1">
                        {it.concepts.slice(0, 3).map((c) => (
                          <button
                            key={c.code}
                            onClick={(e) => {
                              e.stopPropagation()
                              setSectorFilter((v) => (v === c.name ? null : c.name))
                            }}
                            className={`rounded-full border px-2 py-0.5 text-[10px] transition hover:border-primary/40 hover:text-primary ${
                              sectorFilter === c.name ? 'border-primary/50 bg-primary/10 text-primary' : 'text-muted-foreground'
                            }`}
                          >
                            {c.name}
                          </button>
                        ))}
                        {!it.concepts.length && <span className="text-[10px] text-muted-foreground">--</span>}
                      </div>

                      <div className="hidden text-right text-[11px] leading-5 text-muted-foreground sm:block">
                        <div>换手 {fmt(it.turnover_rate)}%</div>
                        <div>{fmt(it.amount)} 亿</div>
                      </div>

                      <div
                        className={`rounded-md px-1 py-1 text-center ${
                          up ? 'bg-stock-up/10 text-stock-up' : 'bg-stock-down/10 text-stock-down'
                        }`}
                      >
                        <div className="text-sm font-bold">{pctText(it.change_pct)}</div>
                        <div className="text-[10px] text-muted-foreground">{fmt(it.price)}</div>
                      </div>
                    </div>
                  )
                })
              )}

              {totalPages > 1 && (
                <div className="flex flex-wrap items-center justify-center gap-1 border-t px-3 py-2.5 text-[11px]">
                  <button
                    onClick={() => setPage((p) => Math.max(1, p - 1))}
                    disabled={page === 1}
                    className="rounded border px-2 py-1 text-muted-foreground hover:bg-accent disabled:opacity-40"
                  >
                    上一页
                  </button>
                  {Array.from({ length: totalPages }, (_, i) => i + 1)
                    .filter((p) => p === 1 || p === totalPages || Math.abs(p - page) <= 2)
                    .map((p, idx, arr) => (
                      <span key={p} className="flex items-center gap-1">
                        {idx > 0 && p - arr[idx - 1] > 1 && (
                          <span className="px-1 text-muted-foreground">…</span>
                        )}
                        <button
                          onClick={() => setPage(p)}
                          className={`min-w-[26px] rounded border px-1.5 py-1 ${
                            p === page
                              ? 'border-primary/40 bg-primary/10 font-medium text-primary'
                              : 'text-muted-foreground hover:bg-accent'
                          }`}
                        >
                          {p}
                        </button>
                      </span>
                    ))}
                  <button
                    onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                    disabled={page === totalPages}
                    className="rounded border px-2 py-1 text-muted-foreground hover:bg-accent disabled:opacity-40"
                  >
                    下一页
                  </button>
                  <span className="ml-2 text-muted-foreground">
                    共 {visibleItems.length} 只 · 每页 {PAGE_SIZE}
                  </span>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {view === 'ladder' && (
        <div className="space-y-3">
          <div className="card flex flex-wrap items-center gap-6 p-3">
            {ladderLoading || !ladder ? (
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                加载天梯…
              </div>
            ) : (
              <>
                <div>
                  <div className="text-[10px] text-muted-foreground">涨停</div>
                  <div className="text-xl font-bold text-stock-up">{ladder.stats.limit_up}</div>
                </div>
                <div>
                  <div className="text-[10px] text-muted-foreground">跌停</div>
                  <div className="text-xl font-bold text-stock-down">{ladder.stats.limit_down}</div>
                </div>
                <div>
                  <div className="text-[10px] text-muted-foreground">最高连板</div>
                  <div className="text-sm font-semibold">{ladder.stats.max_days} 板</div>
                </div>
                <div className="ml-auto text-[11px] text-muted-foreground">{ladder.trade_date}</div>
              </>
            )}
          </div>

          <div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-6">
            {boards.slice(0, 6).map((b) => (
              <button
                key={b.code}
                onClick={() => {
                  setSectorFilter(b.name)
                  setView('screen')
                  run(conditions, { code: activeCode })
                }}
                className="card p-2 text-left transition hover:border-primary/40"
              >
                <div className="text-xs font-medium text-stock-up">
                  {b.name} <span className="font-normal opacity-70">({b.limit_up_count})</span>
                </div>
                <div className="mt-1 text-[10px] text-muted-foreground">
                  {b.change_pct != null ? `${b.change_pct > 0 ? '+' : ''}${b.change_pct.toFixed(2)}%` : '--'}
                </div>
              </button>
            ))}
          </div>

          <div className="card p-2">
            {(ladder?.groups || []).map((g) => (
              <div key={g.level} className="grid grid-cols-[70px_1fr] gap-2 border-b py-2 last:border-0">
                <div className="flex items-center justify-center rounded bg-muted/50 text-[11px] font-medium text-amber-600 dark:text-amber-400">
                  {g.level}
                </div>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
                  {g.items.map((it) => (
                    <button
                      key={it.symbol}
                      onClick={() => {
                        setView('screen')
                        setSectorFilter(null)
                        const conds: ScreenerCondition[] = it.concept
                          ? [{ field: 'concept', op: 'contains', value: it.concept }]
                          : [{ field: 'change_pct', op: 'gte', value: 0 }]
                        run(conds, { code: 'custom' })
                      }}
                      className="relative rounded-md border bg-background p-2 text-left transition hover:border-primary/40"
                    >
                      <span className="absolute right-2 top-1 text-[10px] text-muted-foreground">{it.time}</span>
                      <div className="pr-10 text-xs font-semibold">{it.name}</div>
                      <div className="mt-1 text-[10px] text-stock-up">{it.concept || '--'}</div>
                      {it.stat && <div className="mt-0.5 text-[10px] text-muted-foreground">{it.stat}</div>}
                    </button>
                  ))}
                </div>
              </div>
            ))}
            {ladder && !ladder.groups.length && (
              <div className="py-16 text-center text-xs text-muted-foreground">暂无涨停数据</div>
            )}
          </div>
        </div>
      )}

      {view === 'boards' && (
        <div className="card overflow-hidden">
          <div className="grid grid-cols-[36px_1fr_80px_90px_70px_120px] gap-2 border-b bg-muted/30 px-3 py-2 text-[10px] text-muted-foreground">
            <div>#</div>
            <div>板块 / 概念</div>
            <div className="text-right">涨跌幅</div>
            <div className="text-right">涨停数</div>
            <div className="text-right">最高板</div>
            <div>强度</div>
          </div>
          {boardsLoading ? (
            <div className="flex items-center justify-center gap-2 py-16 text-xs text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              加载板块…
            </div>
          ) : (
            boards.map((b, i) => (
              <button
                key={b.code}
                onClick={() => {
                  setSectorFilter(b.name)
                  setView('screen')
                  run(conditions, { code: activeCode })
                }}
                className="grid w-full grid-cols-[36px_1fr_80px_90px_70px_120px] items-center gap-2 border-b px-3 py-2 text-left last:border-0 hover:bg-accent/40"
              >
                <div className="text-[11px] text-muted-foreground">{i + 1}</div>
                <div className="min-w-0">
                  <div className="truncate text-xs font-medium">{b.name}</div>
                  <div className="text-[10px] text-muted-foreground">{b.members || '--'} 只成分股</div>
                </div>
                <div
                  className={`text-right text-[11px] ${
                    (b.change_pct || 0) >= 0 ? 'text-stock-up' : 'text-stock-down'
                  }`}
                >
                  {pctText(b.change_pct)}
                </div>
                <div className="text-right text-[11px]">{b.limit_up_count}</div>
                <div className="text-right text-[11px]">{b.max_days}</div>
                <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                  <div
                    className="h-full rounded-full bg-stock-up/70"
                    style={{ width: `${Math.min(100, b.score)}%` }}
                  />
                </div>
              </button>
            ))
          )}
        </div>
      )}

      {/* 详情抽屉 */}
      {detail && (
        <>
          <div className="fixed inset-0 z-40 bg-black/40" onClick={() => setDetail(null)} />
          <aside className="fixed right-0 top-0 z-50 h-full w-[440px] max-w-[94vw] overflow-auto border-l bg-background shadow-2xl">
            <div className="flex items-start gap-3 border-b p-4">
              <div>
                <div className="text-lg font-bold">{detail.name}</div>
                <div className="mt-0.5 text-[11px] text-muted-foreground">
                  {detail.symbol}
                  {detail.is_st ? ' · ST' : ''}
                </div>
              </div>
              <div className="ml-auto text-right">
                <div className={`text-xl font-bold ${(detail.change_pct || 0) >= 0 ? 'text-stock-up' : 'text-stock-down'}`}>
                  {fmt(detail.price)}
                </div>
                <div className={`text-[11px] ${(detail.change_pct || 0) >= 0 ? 'text-stock-up' : 'text-stock-down'}`}>
                  {pctText(detail.change_pct)}
                </div>
              </div>
              <button onClick={() => setDetail(null)} className="text-muted-foreground hover:text-foreground">
                <X className="h-5 w-5" />
              </button>
            </div>

            <div className="space-y-4 p-4">
              <div>
                <div className="mb-1 text-[10px] tracking-wider text-muted-foreground">日 K · 近 60 日</div>
                <KlineChart bars={kline} />
              </div>

              <div className="grid grid-cols-3 gap-2">
                {[
                  ['换手率', `${fmt(detail.turnover_rate)}%`],
                  ['振幅', `${fmt(detail.amplitude)}%`],
                  ['成交额', `${fmt(detail.amount)} 亿`],
                  ['流通市值', `${fmt(detail.float_mv)} 亿`],
                  ['市盈率', fmt(detail.pe_ttm)],
                  ['封板资金', detail.seal_fund == null ? '--' : `${fmt(detail.seal_fund)} 亿`],
                  ['连板数', detail.limit_up_days || '--'],
                  ['涨停统计', detail.limit_up_stat_count ? `${detail.limit_up_stat_days}天${detail.limit_up_stat_count}板` : '--'],
                  ['炸板次数', detail.break_count || '--'],
                ].map(([k, v]) => (
                  <div key={k} className="rounded-md border bg-muted/20 p-2">
                    <div className="text-[10px] text-muted-foreground">{k}</div>
                    <div className="mt-0.5 text-xs font-medium">{v}</div>
                  </div>
                ))}
              </div>

              <div>
                <div className="mb-1 text-[10px] tracking-wider text-muted-foreground">所属板块 / 概念</div>
                <div className="flex flex-wrap gap-1.5">
                  {detail.concepts.length ? (
                    detail.concepts.map((c) => (
                      <button
                        key={c.code}
                        onClick={() => {
                          setSectorFilter(c.name)
                          setDetail(null)
                          setView('screen')
                        }}
                        className="rounded-full border px-2.5 py-1 text-[11px] hover:border-primary/40 hover:text-primary"
                      >
                        {c.name}
                        {c.change_pct != null && (
                          <span className={(c.change_pct || 0) >= 0 ? 'ml-1 text-stock-up' : 'ml-1 text-stock-down'}>
                            {c.change_pct.toFixed(2)}%
                          </span>
                        )}
                      </button>
                    ))
                  ) : (
                    <span className="text-[11px] text-muted-foreground">暂无概念归属（索引未覆盖该股）</span>
                  )}
                </div>
              </div>

              <div className="flex items-center gap-2 border-t pt-3">
                <button
                  onClick={() => addToWatchlist([detail])}
                  disabled={adding || watchlist.has(detail.symbol)}
                  className="flex items-center gap-1 rounded border px-3 py-1.5 text-[11px] hover:bg-accent disabled:opacity-50"
                >
                  <Star className="h-3 w-3" />
                  {watchlist.has(detail.symbol) ? '已在自选' : '加入自选'}
                </button>
                <button
                  onClick={() => setInsightOpen(true)}
                  className="flex items-center gap-1 rounded border px-3 py-1.5 text-[11px] hover:bg-accent"
                >
                  <ExternalLink className="h-3 w-3" />
                  打开个股概览
                </button>
                {addMsg && <span className="text-[11px] text-primary">{addMsg}</span>}
              </div>

              <div className="border-t pt-3 text-[10px] leading-relaxed text-muted-foreground">
                概念标签来自每日构建的个股→概念反查索引，仅覆盖按资金排序的前 160 个概念。
                数据仅供研究参考，不构成投资建议。
              </div>
            </div>
          </aside>
        </>
      )}

      {detail && (
        <StockInsightModal
          open={insightOpen}
          onOpenChange={setInsightOpen}
          symbol={detail.symbol}
          market="CN"
          stockName={detail.name}
        />
      )}
    </div>
  )
}
