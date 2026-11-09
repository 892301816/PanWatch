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
import { CandleSpark, type SparkBar } from '@/components/CandleSpark'
import {
  fetchAPI,
  screenerApi,
  stocksApi,
  type BoardItem,
  type LadderItem,
  type LadderResponse,
  type ScreenerCondition,
  type ScreenerField,
  type ScreenerHistoryDetail,
  type ScreenerHistoryPerformance,
  type ScreenerItem,
  type ScreenerMetaResponse,
  type ScreenerRunSummary,
  type ScreenerStrategy,
} from '@panwatch/api'

type View = 'screen' | 'ladder' | 'boards' | 'history'

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
  const [candles, setCandles] = useState<Record<string, SparkBar[]>>({})
  const [candlesTried, setCandlesTried] = useState<Record<string, true>>({})
  const [watchlist, setWatchlist] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [adding, setAdding] = useState(false)
  const [addMsg, setAddMsg] = useState('')
  const [tradeDate, setTradeDate] = useState('')
  const [universe, setUniverse] = useState(0)
  const [total, setTotal] = useState(0)
  const [dataWarnings, setDataWarnings] = useState<string[]>([])
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
  const [historyRuns, setHistoryRuns] = useState<ScreenerRunSummary[]>([])
  const [historyTotal, setHistoryTotal] = useState(0)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyDetail, setHistoryDetail] = useState<ScreenerHistoryDetail | null>(null)
  const [historyPerf, setHistoryPerf] = useState<ScreenerHistoryPerformance | null>(null)
  const [autoSave, setAutoSave] = useState(
    () => (typeof localStorage === 'undefined' ? true : localStorage.getItem('screener.autoSave') !== '0')
  )
  const [builderOpen, setBuilderOpen] = useState(false)
  const [draftField, setDraftField] = useState('change_pct')
  const [draftOp, setDraftOp] = useState('gte')
  const [draftValue, setDraftValue] = useState('')
  const [conceptOptions, setConceptOptions] = useState<string[]>([])

  const [saving, setSaving] = useState(false)
  const [saveMode, setSaveMode] = useState(false)
  const [groupName, setGroupName] = useState('')
  const [groupMsg, setGroupMsg] = useState('')

  const [ladder, setLadder] = useState<LadderResponse | null>(null)
  const [ladderLoading, setLadderLoading] = useState(false)
  const [boards, setBoards] = useState<BoardItem[]>([])
  const [boardsLoading, setBoardsLoading] = useState(false)
  const [showStyleBoards, setShowStyleBoards] = useState(false)
  // 连涨天梯的板块筛选（只显示该板块的涨停股）
  const [ladderConcept, setLadderConcept] = useState<string | null>(null)
  const [ladderFiltering, setLadderFiltering] = useState(false)
  const [ladderDetailLoading, setLadderDetailLoading] = useState<string | null>(null)

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
    async (
      conds: ScreenerCondition[],
      opts?: { code?: string; logic?: 'all' | 'any'; exclude?: string[]; orderBy?: string; offset?: number; sector?: string | null }
    ) => {
      setRunning(true)
      setError('')
      setConditions(conds)
      if (opts?.logic) setLogic(opts.logic)
      if (opts?.exclude) setExclude(opts.exclude)
      if (opts?.orderBy) setOrderBy(opts.orderBy)
      if (opts?.code !== undefined) setActiveCode(opts.code)
      const sector = opts?.sector !== undefined ? opts.sector : sectorFilter
      const offset = Math.max(0, opts?.offset || 0)
      // 板块快捷筛选走服务端条件,否则分页后只能过滤当前页
      const finalConds = sector
        ? [...conds, { field: 'concept', op: 'in', value: [sector] } as ScreenerCondition]
        : conds
      try {
        const res = await screenerApi.run({
          conditions: finalConds,
          logic: opts?.logic || logic,
          exclude: opts?.exclude || exclude,
          order_by: opts?.orderBy || orderBy,
          limit: PAGE_SIZE,   // 服务端分页:一次只回一页
          offset,
          save: autoSave,     // 首屏请求写入历史
          code: opts?.code || activeCode,
          with_spark: false,  // 走势按页懒加载
        })
        setItems(res.items)
        setTotal(res.total ?? res.count)
        setTradeDate(res.trade_date)
        setUniverse(res.universe)
        setAppliedLogic(res.applied?.logic || opts?.logic || 'all')
        setDataWarnings(res.warnings || [])
        setPage(Math.floor(offset / PAGE_SIZE) + 1)
        setCandlesTried({}) // 保留已抓到的K线,只重置"已尝试"标记以便刷新
      } catch (e) {
        setError(e instanceof Error ? e.message : '选股失败')
        setItems([])
        setTotal(0)
      } finally {
        setRunning(false)
      }
    },
    [logic, exclude, orderBy, sectorFilter, autoSave, activeCode]
  )

  const applyStrategy = useCallback(
    (s: ScreenerStrategy) => {
      setSectorFilter(null)
      void run(s.conditions || [], {
        sector: null,
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

  // 点击天梯上方的板块卡片:在当前页把天梯过滤成该板块的涨停股
  const applyLadderConcept = useCallback(
    async (name: string | null) => {
      const next = name && name !== ladderConcept ? name : null
      setLadderConcept(next)
      setLadderFiltering(true)
      setError('')
      try {
        const data = await screenerApi.ladder(undefined, next)
        setLadder(data)
      } catch (e) {
        setError(e instanceof Error ? e.message : '天梯筛选失败')
      } finally {
        setLadderFiltering(false)
      }
    },
    [ladderConcept]
  )

  // 天梯里的个股卡片:直接打开个股详情,不再跳转选股页
  const openLadderStock = useCallback(
    async (it: LadderItem) => {
      if (ladderDetailLoading) return
      setLadderDetailLoading(it.symbol)
      setError('')
      try {
        const res = await screenerApi.run({
          conditions: [{ field: 'symbol', op: 'eq', value: it.symbol }],
          logic: 'all',
          exclude: [],
          order_by: 'change_pct desc',
          limit: 1,
          with_spark: false,
        })
        const found = res.items[0]
        if (found) setDetail(found)
        else setError(`未找到 ${it.name}（${it.symbol}）的行情数据`)
      } catch (e) {
        setError(e instanceof Error ? e.message : '个股详情加载失败')
      } finally {
        setLadderDetailLoading(null)
      }
    },
    [ladderDetailLoading]
  )

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
      setLadderConcept(null)
      setShowStyleBoards(false)
      await loadMeta()
      await run(conditions, { code: activeCode, offset: 0 })
    } catch (e) {
      setError(e instanceof Error ? e.message : '刷新失败')
    } finally {
      setRunning(false)
    }
  }

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const pageKey = items.map((i) => i.symbol).join(',')

  useEffect(() => {
    if (!builderOpen || draftField !== 'concept' || conceptOptions.length) return
    screenerApi
      .concepts()
      .then((r) => setConceptOptions(r.items.map((x) => x.name)))
      .catch(() => {})
  }, [builderOpen, draftField, conceptOptions.length])
  const pagedItems = items

  const loadHistory = useCallback(async () => {
    setHistoryLoading(true)
    try {
      const r = await screenerApi.history({ limit: 30 })
      setHistoryRuns(r.items)
      setHistoryTotal(r.total)
    } catch {
      setHistoryRuns([])
    } finally {
      setHistoryLoading(false)
    }
  }, [])

  useEffect(() => {
    if (view === 'history') void loadHistory()
  }, [view, loadHistory])

  const openHistory = useCallback(async (run: ScreenerRunSummary) => {
    try {
      const d = await screenerApi.historyDetail(run.id)
      setHistoryDetail(d)
      setHistoryPerf(d.performance)
      screenerApi
        .historyPerformance(run.id)
        .then((p) => setHistoryPerf(p))
        .catch(() => {})
    } catch (e) {
      setError(e instanceof Error ? e.message : '历史详情加载失败')
    }
  }, [])

  const rerunHistory = useCallback(() => {
    if (!historyDetail) return
    setView('screen')
    setHistoryDetail(null)
    void run(historyDetail.conditions || [], {
      code: 'custom',
      logic: (historyDetail.logic as 'all' | 'any') || 'all',
      exclude: historyDetail.exclude || ['st'],
      orderBy: historyDetail.order_by || 'change_pct desc',
    })
  }, [historyDetail, run])

  const removeHistory = useCallback(
    async (id: number) => {
      if (!window.confirm('删除这条历史记录？')) return
      await screenerApi.deleteHistory(id).catch(() => {})
      if (historyDetail?.id === id) setHistoryDetail(null)
      void loadHistory()
    },
    [historyDetail, loadHistory]
  )

  const goPage = useCallback(
    (p: number) => {
      void run(conditions, { code: activeCode, offset: (p - 1) * PAGE_SIZE })
    },
    [run, conditions, activeCode]
  )

  // 按页懒加载近 20 日K线(本地 daily_bars 优先,新股回退网络)
  useEffect(() => {
    if (!pageKey) return
    const need = pagedItems
      .filter((i) => !candlesTried[i.symbol])
      .map((i) => i.symbol)
      .slice(0, 120)
    if (!need.length) return
    let alive = true
    const mark = (symbols: string[]) =>
      setCandlesTried((prev) => {
        const next = { ...prev }
        symbols.forEach((s) => {
          next[s] = true
        })
        return next
      })
    fetchAPI<{ data: Record<string, SparkBar[]> }>(
      `/dataset/spark-ohlc?symbols=${need.join(',')}&days=20`
    )
      .then((r) => {
        if (!alive) return
        setCandles((prev) => ({ ...prev, ...(r.data || {}) }))
        mark(need)
      })
      .catch(() => {
        if (alive) mark(need)
      })
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pageKey])

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
              ['history', '历史'],
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
                    onChange={(e) => {
                      const next = e.target.value
                      setDraftField(next)
                      // 概念只有「属于 / 不属于 / 包含」有意义
                      if (next === 'concept') setDraftOp('in')
                    }}
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
                    {(draftField === 'concept'
                      ? ['in', 'not_in', 'contains']
                      : draftField === 'board'
                        ? ['eq', 'ne', 'in', 'not_in', 'contains']
                        : ['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'between', 'in', 'contains']
                    ).map((op) => (
                      <option key={op} value={op}>
                        {ops[op] || op}
                      </option>
                    ))}
                  </select>
                  <input
                    value={draftValue}
                    onChange={(e) => setDraftValue(e.target.value)}
                    list={
                      draftField === 'concept'
                        ? 'concept-options'
                        : draftField === 'board'
                          ? 'board-options'
                          : undefined
                    }
                    placeholder={
                      draftField === 'concept'
                        ? '选择或输入概念，多个用逗号分隔'
                        : draftField === 'board'
                          ? '如 创业板'
                          : draftOp === 'between'
                            ? '如 5,20'
                            : '条件值'
                    }
                    className="w-40 rounded border bg-background px-2 py-1"
                  />
                  {draftField === 'concept' && (
                    <datalist id="concept-options">
                      {conceptOptions.map((name) => (
                        <option key={name} value={name} />
                      ))}
                    </datalist>
                  )}
                  {draftField === 'board' && (
                    <datalist id="board-options">
                      {['主板', '创业板', '科创板', '北交所'].map((name) => (
                        <option key={name} value={name} />
                      ))}
                    </datalist>
                  )}
                  <button onClick={addCondition} className="rounded bg-primary px-2.5 py-1 text-primary-foreground">
                    添加
                  </button>
                </div>
              )}

              <div className="mt-2 flex flex-wrap items-center gap-3 text-[11px] text-muted-foreground">
                <span>
                  共 <b className="text-foreground">{total}</b> 只
                  {universe > 0 && <span className="ml-1 opacity-70">/ 全市场 {universe}</span>}
                </span>
                {conditions.length >= 2 && (
                  <span className={appliedLogic === 'any' ? 'text-amber-600 dark:text-amber-400' : ''}>
                    匹配方式：{appliedLogic === 'any' ? '任一（或）' : '全部（且）'}
                  </span>
                )}
                {sectorFilter && (
                  <button
                    onClick={() => {
                      setSectorFilter(null)
                      void run(conditions, { code: activeCode, sector: null })
                    }}
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

            {dataWarnings.length > 0 && (
              <div className="card border-amber-500/40 bg-amber-500/5 p-2 text-[11px] leading-relaxed text-amber-600 dark:text-amber-400">
                {dataWarnings.map((w) => (
                  <div key={w}>· {w}</div>
                ))}
              </div>
            )}

            {/* 列表 */}
            <div className="card overflow-hidden">
              <div className="hidden gap-3 border-b bg-muted/30 px-3 py-2 text-[10px] text-muted-foreground sm:grid sm:grid-cols-[28px_190px_200px_1fr_104px]">
                <div className="flex items-center justify-center">
                  <input
                    type="checkbox"
                    checked={pageAllSelected}
                    onChange={togglePage}
                    title="全选本页"
                    className="h-3.5 w-3.5 cursor-pointer"
                  />
                </div>
                <div>股票名称 / 涨跌幅</div>
                <div>近 20 日K线</div>
                <div>所属板块 · 概念</div>
                <div className="text-right">换手 / 成交额</div>
              </div>

              {running && !items.length ? (
                <div className="flex items-center justify-center gap-2 py-16 text-xs text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin" />
                  选股中…
                </div>
              ) : !items.length ? (
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
                      className="grid cursor-pointer gap-3 border-b px-3 py-3 last:border-0 hover:bg-accent/40 sm:grid-cols-[28px_190px_200px_1fr_104px] sm:items-center sm:gap-3"
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
                          <span
                            className={`shrink-0 text-xs font-semibold ${
                              up ? 'text-stock-up' : 'text-stock-down'
                            }`}
                          >
                            {pctText(it.change_pct)}
                          </span>
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
                        <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-muted-foreground">
                          <span>{it.symbol}</span>
                          <span className={up ? 'text-stock-up' : 'text-stock-down'}>{fmt(it.price)}</span>
                        </div>
                      </div>

                      <div className="flex items-center gap-3 sm:contents">
                        <div className="h-[92px] w-[200px] max-w-[60%] shrink-0 sm:h-[84px] sm:w-auto sm:max-w-none">
                          <CandleSpark
                            bars={candles[it.symbol]}
                            pending={!candlesTried[it.symbol]}
                          />
                        </div>
                        <div className="min-w-0 flex-1 text-[11px] leading-5 text-muted-foreground sm:hidden">
                          <div>换手 {fmt(it.turnover_rate)}%</div>
                          <div>{fmt(it.amount)} 亿</div>
                        </div>
                      </div>

                      <div className="flex flex-wrap gap-1">
                        {it.concepts.slice(0, 3).map((c) => (
                          <button
                            key={c.code}
                            onClick={(e) => {
                              e.stopPropagation()
                              const next = sectorFilter === c.name ? null : c.name
                              setSectorFilter(next)
                              void run(conditions, { code: activeCode, sector: next, offset: 0 })
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
                    </div>
                  )
                })
              )}

              {totalPages > 1 && (
                <div className="flex flex-wrap items-center justify-center gap-1 border-t px-3 py-2.5 text-[11px]">
                  <button
                    onClick={() => goPage(Math.max(1, page - 1))}
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
                          onClick={() => goPage(p)}
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
                    onClick={() => goPage(Math.min(totalPages, page + 1))}
                    disabled={page === totalPages}
                    className="rounded border px-2 py-1 text-muted-foreground hover:bg-accent disabled:opacity-40"
                  >
                    下一页
                  </button>
                  <span className="ml-2 text-muted-foreground">
                    共 {total} 只 · 每页 {PAGE_SIZE}
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
                  <div className="text-[10px] text-muted-foreground">
                    {ladderConcept ? `${ladderConcept} · 涨停` : '涨停'}
                  </div>
                  <div className="text-xl font-bold text-stock-up">
                    {ladderConcept ? (ladder.filter?.count ?? 0) : ladder.stats.limit_up}
                  </div>
                </div>
                {!ladderConcept && (
                  <div>
                    <div className="text-[10px] text-muted-foreground">跌停</div>
                    <div className="text-xl font-bold text-stock-down">{ladder.stats.limit_down}</div>
                  </div>
                )}
                <div>
                  <div className="text-[10px] text-muted-foreground">最高连板</div>
                  <div className="text-sm font-semibold">
                    {ladderConcept ? (ladder.filter?.max_days ?? 0) : ladder.stats.max_days} 板
                  </div>
                </div>
                {ladderFiltering && (
                  <div className="flex items-center gap-1 text-[11px] text-muted-foreground">
                    <Loader2 className="h-3 w-3 animate-spin" />
                    筛选中
                  </div>
                )}
                <div className="ml-auto text-[11px] text-muted-foreground">{ladder.trade_date}</div>
              </>
            )}
          </div>

          {boards.length > 0 && (
            <div className="card p-2 sm:p-3">
              <div className="mb-2 flex items-center gap-2 px-1">
                <div className="text-[11px] font-medium">今日强势板块</div>
                <div className="text-[10px] text-muted-foreground">点击板块只看该板块的涨停股</div>
              </div>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
                {boards.slice(0, 6).map((b) => {
                  const active = ladderConcept === b.name
                  return (
                    <button
                      key={b.code}
                      onClick={() => void applyLadderConcept(b.name)}
                      className={`rounded-lg border p-2 text-left transition ${
                        active
                          ? 'border-primary/60 bg-primary/8 ring-1 ring-primary/20'
                          : 'border-border/60 hover:border-primary/40'
                      }`}
                    >
                      <div className="flex items-center gap-1">
                        <span
                          className={`min-w-0 flex-1 truncate text-[11px] font-medium ${
                            active ? 'text-primary' : 'text-stock-up'
                          }`}
                        >
                          {b.name}
                        </span>
                        <span className="shrink-0 text-[10px] text-muted-foreground">{b.limit_up_count} 涨</span>
                      </div>
                      <div className="mt-1 flex items-center justify-between text-[10px]">
                        <span className={(b.change_pct || 0) >= 0 ? 'text-stock-up' : 'text-stock-down'}>
                          {pctText(b.change_pct)}
                        </span>
                        <span className="text-muted-foreground">强度 {Math.round(b.score)}</span>
                      </div>
                    </button>
                  )
                })}
              </div>
            </div>
          )}

          <div className="card p-2">
            <div className="mb-1 flex flex-wrap items-center gap-2 px-1">
              <div className="text-[11px] font-medium">涨停梯队</div>
              {ladderConcept ? (
                <button
                  onClick={() => void applyLadderConcept(null)}
                  className="flex items-center gap-1 rounded-full border border-primary/40 bg-primary/8 px-2 py-0.5 text-[10px] font-medium text-primary"
                  title="清除板块筛选"
                >
                  {ladderConcept}
                  {ladderFiltering && <Loader2 className="h-2.5 w-2.5 animate-spin" />}
                  <X className="h-2.5 w-2.5" />
                </button>
              ) : (
                <div className="text-[10px] text-muted-foreground">点击个股查看详情</div>
              )}
            </div>
            {ladder && ladderConcept && !ladder.groups.length && !ladderFiltering && (
              <div className="py-10 text-center text-xs text-muted-foreground">
                {ladder.concept_index_ready
                  ? `${ladderConcept} 今日没有涨停股`
                  : '概念索引尚未构建完成，稍后点一次刷新数据再试'}
              </div>
            )}
            {(ladder?.groups || []).map((g) => (
              <div key={g.level} className="grid grid-cols-[70px_1fr] gap-2 border-b py-2 last:border-0">
                <div className="flex items-center justify-center rounded bg-muted/50 text-[11px] font-medium text-amber-600 dark:text-amber-400">
                  {g.level}
                </div>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
                  {g.items.map((it) => (
                    <button
                      key={it.symbol}
                      onClick={() => void openLadderStock(it)}
                      className="relative rounded-md border bg-background p-2 text-left transition hover:border-primary/40 active:border-primary/60"
                    >
                      <span className="absolute right-2 top-1 text-[10px] text-muted-foreground">
                        {ladderDetailLoading === it.symbol ? (
                          <Loader2 className="h-3 w-3 animate-spin" />
                        ) : (
                          it.time
                        )}
                      </span>
                      <div className="pr-10 text-xs font-semibold">{it.name}</div>
                      <div className="mt-1 flex flex-wrap items-center gap-1">
                        {(it.concepts?.length ? it.concepts : [it.concept || '--']).map((c, ci) => (
                          <span
                            key={`${c}-${ci}`}
                            className={
                              ci === 0
                                ? 'text-[10px] font-medium text-stock-up'
                                : 'rounded bg-muted/50 px-1 text-[10px] text-muted-foreground'
                            }
                          >
                            {c}
                          </span>
                        ))}
                      </div>
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
          <div className="flex flex-wrap items-center gap-2 border-b bg-muted/20 px-3 py-2">
            <div className="text-[11px] font-medium">板块强弱</div>
            <div className="text-[10px] text-muted-foreground">默认只列主题/概念板块</div>
            <label className="ml-auto flex cursor-pointer items-center gap-1 text-[11px] text-muted-foreground">
              <input
                type="checkbox"
                checked={showStyleBoards}
                onChange={(e) => {
                  setShowStyleBoards(e.target.checked)
                  setBoardsLoading(true)
                  screenerApi
                    .boards(undefined, 30, e.target.checked)
                    .then((r) => setBoards(r.items))
                    .catch(() => {})
                    .finally(() => setBoardsLoading(false))
                }}
                className="h-3 w-3 cursor-pointer"
              />
              含风格板块（新高/破净/红利等）
            </label>
          </div>
          <div className="hidden grid-cols-[36px_1fr_80px_90px_70px_120px] gap-2 border-b bg-muted/30 px-3 py-2 text-[10px] text-muted-foreground sm:grid">
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
                  void run(conditions, { code: activeCode, sector: b.name, offset: 0 })
                }}
                className="grid w-full grid-cols-[24px_minmax(0,1fr)_auto] items-center gap-2 border-b px-3 py-2.5 text-left last:border-0 hover:bg-accent/40 sm:grid-cols-[36px_1fr_80px_90px_70px_120px] sm:py-2"
              >
                <div className="text-[11px] text-muted-foreground">{i + 1}</div>
                <div className="min-w-0">
                  <div className="truncate text-xs font-medium">{b.name}</div>
                  <div className="mt-1 flex items-center gap-2 text-[10px] text-muted-foreground sm:hidden">
                    <span className="shrink-0">涨停 {b.limit_up_count}</span>
                    <span className="shrink-0">最高 {b.max_days} 板</span>
                    <span className="shrink-0">强度 {Math.round(b.score)}</span>
                    <span className="block h-1 min-w-0 flex-1 overflow-hidden rounded-full bg-muted">
                      <span
                        className="block h-full rounded-full bg-stock-up/70"
                        style={{ width: `${Math.min(100, b.score)}%` }}
                      />
                    </span>
                  </div>
                  <div className="hidden text-[10px] text-muted-foreground sm:block">{b.members || '--'} 只成分股</div>
                </div>
                <div
                  className={`text-right text-[11px] ${
                    (b.change_pct || 0) >= 0 ? 'text-stock-up' : 'text-stock-down'
                  }`}
                >
                  {pctText(b.change_pct)}
                </div>
                <div className="hidden text-right text-[11px] sm:block">{b.limit_up_count}</div>
                <div className="hidden text-right text-[11px] sm:block">{b.max_days}</div>
                <div className="hidden h-1.5 overflow-hidden rounded-full bg-muted sm:block">
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

      {view === 'history' && (
        <div className="space-y-3">
          <div className="card overflow-hidden">
            <div className="flex flex-wrap items-center gap-2 border-b bg-muted/20 px-3 py-2">
              <div className="text-[11px] font-medium">选股历史</div>
              <div className="text-[10px] text-muted-foreground">
                共 {historyTotal} 条 · 明细保留 90 天
              </div>
              <label className="ml-auto flex cursor-pointer items-center gap-1 text-[11px] text-muted-foreground">
                <input
                  type="checkbox"
                  checked={autoSave}
                  onChange={(e) => {
                    setAutoSave(e.target.checked)
                    localStorage.setItem('screener.autoSave', e.target.checked ? '1' : '0')
                  }}
                  className="h-3 w-3 cursor-pointer"
                />
                自动记录
              </label>
              <button
                onClick={() => void loadHistory()}
                className="rounded border px-2 py-0.5 text-[11px] text-muted-foreground hover:bg-accent"
              >
                刷新
              </button>
            </div>

            {historyLoading ? (
              <div className="flex items-center justify-center gap-2 py-12 text-xs text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                加载历史…
              </div>
            ) : !historyRuns.length ? (
              <div className="py-12 text-center text-xs text-muted-foreground">
                还没有记录，运行一次选股就会自动保存
              </div>
            ) : (
              <div className="divide-y">
                {historyRuns.map((r) => (
                  <button
                    key={r.id}
                    onClick={() => void openHistory(r)}
                    className={`flex w-full items-start gap-3 px-3 py-2.5 text-left hover:bg-accent/40 ${
                      historyDetail?.id === r.id ? 'bg-accent/30' : ''
                    }`}
                  >
                    <div className="w-[92px] shrink-0 text-[10px] leading-4 text-muted-foreground">
                      {r.created_at?.slice(5, 16) || '--'}
                      <div>{r.trade_date || '--'}</div>
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-xs font-medium">
                        {r.rule_name ||
                          (r.conditions.length
                            ? r.conditions
                                .slice(0, 2)
                                .map((c) => conditionLabel(c, fields, ops))
                                .join(' · ')
                            : '全部（不限条件）')}
                      </div>
                      <div className="mt-0.5 text-[10px] text-muted-foreground">
                        命中 {r.matched_count} 只 / 全市场 {r.universe}
                        {r.conditions.length > 2 ? ` · 共 ${r.conditions.length} 个条件` : ''}
                      </div>
                    </div>
                    <div className="shrink-0 text-[10px] text-muted-foreground">
                      {r.source === 'rule' ? '分组' : r.source === 'preset' ? '预设' : '自定义'}
                    </div>
                  </button>
                ))}
              </div>
            )}
          </div>

          {historyDetail && (
            <div className="card p-3">
              <div className="flex flex-wrap items-center gap-2">
                <div className="text-[11px] font-medium">历史详情</div>
                <div className="text-[10px] text-muted-foreground">
                  {historyDetail.created_at} · 数据日 {historyDetail.trade_date} · 命中{' '}
                  {historyDetail.matched_count} 只
                </div>
                <div className="ml-auto flex items-center gap-1.5">
                  <button
                    onClick={rerunHistory}
                    className="rounded border px-2 py-0.5 text-[11px] hover:bg-accent"
                  >
                    重新运行
                  </button>
                  <button
                    onClick={() => void removeHistory(historyDetail.id)}
                    className="rounded border px-2 py-0.5 text-[11px] text-destructive hover:bg-destructive/10"
                  >
                    删除
                  </button>
                  <button
                    onClick={() => setHistoryDetail(null)}
                    className="rounded border px-2 py-0.5 text-[11px] text-muted-foreground hover:bg-accent"
                  >
                    关闭
                  </button>
                </div>
              </div>

              <div className="mt-2 flex flex-wrap gap-1">
                {historyDetail.conditions.length === 0 && (
                  <span className="text-[11px] text-muted-foreground">全部（不限条件）</span>
                )}
                {historyDetail.conditions.map((c, i) => (
                  <span
                    key={`${c.field}-${c.op}-${i}`}
                    className="rounded-full border bg-muted/40 px-2 py-0.5 text-[11px]"
                  >
                    {conditionLabel(c, fields, ops)}
                  </span>
                ))}
              </div>

              <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
                {(['1', '3', '5', '10'] as const).map((h) => {
                  const stat = historyPerf?.horizons?.[h]
                  const color =
                    stat?.avg == null
                      ? 'text-muted-foreground'
                      : stat.avg >= 0
                        ? 'text-stock-up'
                        : 'text-stock-down'
                  return (
                    <div key={h} className="rounded-md border bg-muted/20 px-2 py-1.5">
                      <div className="text-[10px] text-muted-foreground">T+{h} 等权收益</div>
                      <div className={`text-sm font-medium ${color}`}>
                        {stat?.avg == null ? '--' : `${stat.avg > 0 ? '+' : ''}${stat.avg}%`}
                      </div>
                      <div className="text-[10px] text-muted-foreground">
                        {stat?.count ? `胜率 ${stat.win_rate}% · ${stat.count} 只` : '数据不足'}
                      </div>
                    </div>
                  )
                })}
              </div>

              <div className="mt-2 overflow-x-auto">
                <table className="w-full text-left text-[11px]">
                  <thead className="text-[10px] text-muted-foreground">
                    <tr className="border-b">
                      <th className="py-1 pr-2 font-normal">#</th>
                      <th className="py-1 pr-2 font-normal">代码</th>
                      <th className="py-1 pr-2 font-normal">名称</th>
                      <th className="py-1 pr-2 text-right font-normal">涨跌幅</th>
                      <th className="hidden py-1 pr-2 text-right font-normal sm:table-cell">换手</th>
                      <th className="hidden py-1 font-normal sm:table-cell">概念</th>
                    </tr>
                  </thead>
                  <tbody>
                    {historyDetail.items.map((it) => (
                      <tr key={it.symbol} className="border-b last:border-0">
                        <td className="py-1 pr-2 text-muted-foreground">{it.rank}</td>
                        <td className="py-1 pr-2 font-mono">{it.symbol}</td>
                        <td className="py-1 pr-2">{it.name}</td>
                        <td
                          className={`py-1 pr-2 text-right ${
                            (it.change_pct || 0) >= 0 ? 'text-stock-up' : 'text-stock-down'
                          }`}
                        >
                          {pctText(it.change_pct)}
                        </td>
                        <td className="hidden py-1 pr-2 text-right sm:table-cell">
                          {it.turnover_rate == null ? '--' : `${it.turnover_rate.toFixed(2)}%`}
                        </td>
                        <td className="hidden py-1 text-muted-foreground sm:table-cell">
                          {it.concepts.slice(0, 3).join(' / ') || '--'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {historyDetail.matched_count > historyDetail.items.length && (
                  <div className="py-2 text-center text-[10px] text-muted-foreground">
                    明细仅保存前 {historyDetail.items.length} 条，其余请用「重新运行」查看
                  </div>
                )}
              </div>
            </div>
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
                          void run(conditions, { code: activeCode, sector: c.name, offset: 0 })
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
