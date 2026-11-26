import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import {
  fetchAPI,
  strategyApi,
  type StudioBar,
  type BacktestResult,
  type BacktestOrder,
  type StudioStats,
  type StrategyLibraryItem,
  type ChartCompileResult,
  type ChartPreviewResult,
} from '@panwatch/api'
import { ResultChart } from '@panwatch/biz-ui/components/StrategyBacktest'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@panwatch/base-ui/components/ui/dialog'
import {
  ScriptGallery,
  ScriptWriter,
  type StudioDraft as Draft,
} from '@/components/StudioScriptDialogs'
import StudioChart from '@/components/StudioChart'
import StudioMobileWorkbench from '@/components/StudioMobileWorkbench'
import StudioPineInputs, {
  type PineInputValue,
} from '@/components/StudioPineInputs'
import StudioTopbar, {
  type StudioChartType,
  type StudioScriptKind,
} from '@/components/StudioTopbar'
import StudioScriptBar from '@/components/StudioScriptBar'
import StudioBacktestDock, {
  DOCK_DEFAULT_HEIGHT,
  DOCK_HEIGHT_KEY,
  DOCK_MIN_HEIGHT,
} from '@/components/StudioBacktestDock'
import StudioStatusBar from '@/components/StudioStatusBar'
import { readStudies } from '@/lib/studio-indicators'
import './strategy-studio.css'

/** Pine 脚本是工作台唯一主线：结构化条件区保留代码，但默认不展示。 */
const SHOW_STRUCTURED = false
const STUDIO_INTERVALS = ['1d', '1w', '1m']
/** 图表区高度：与回测 dock 各自独立记忆 */
const CHART_HEIGHT_KEY = 'pw:studio:chart-height'
const CHART_MIN_HEIGHT = 320
const STAR_KEY = 'pw:studio:stars'

type Stock = { symbol: string; market: string; name: string }
type Target = Stock & { kind: 'stock' | 'index' }
type History = { result: BacktestResult; name: string; source?: string }
export type GalleryView = 'templates' | 'mine' | null
type Props = {
  stocks: Stock[]
  items: StrategyLibraryItem[]
  initialSymbol: string
  market: string
  selected?: StrategyLibraryItem
  historical?: BacktestResult
  gallery: GalleryView
  setGallery: (view: GalleryView) => void
  onSaved: () => Promise<void>
  onRules: (item: StrategyLibraryItem, symbol: string) => void
  /** 打开「我的策略」整页弹窗（由 StrategyCenter 提供） */
  onOpenLibrary?: (symbol: string, market: string) => void
  /** 打开工作台时直接弹出脚本编辑器（Pine 主线：从策略列表进来） */
  autoEdit?: boolean
  /** 调用方显式指定：本条策略按脚本打开（不再做类型推断） */
  forceScript?: boolean
}
/**
 * 工作台只走 Pine：模板内置的都是 Pine 策略脚本，
 * 这样回测结果一定带完整统计（夏普/索提诺/卡玛/手续费/期望值…），
 * 不再出现老四语句脚本那套缺字段的报告。
 */
const source = (
  kind: 'sma' | 'ema',
  fast: number,
  slow: number,
  reverse = false,
) =>
  [
    '//@version=5',
    'strategy("' +
      (reverse ? '反向交叉' : kind === 'ema' ? '指数均线交叉' : '双均线交叉') +
      '", overlay=true)',
    'fast = ta.' + kind + '(close, ' + fast + ')',
    'slow = ta.' + kind + '(close, ' + slow + ')',
    'longCond = ta.' + (reverse ? 'crossunder' : 'crossover') + '(fast, slow)',
    'shortCond = ta.' + (reverse ? 'crossover' : 'crossunder') + '(fast, slow)',
    'if longCond',
    '    strategy.entry("L", strategy.long)',
    'if shortCond',
    '    strategy.close("L")',
  ].join('\n')
const templates: Draft[] = [
  { name: '双均线交叉', source: source('sma', 5, 20) },
  { name: '指数均线交叉', source: source('ema', 8, 21) },
  { name: '反向交叉', source: source('sma', 6, 18, true) },
]
const indices: Target[] = [
  { kind: 'index', market: 'CN', symbol: '000001', name: '上证指数' },
  { kind: 'index', market: 'CN', symbol: '000300', name: '沪深300' },
]
const emptyOrders: BacktestOrder[] = []
const emptyBars: StudioBar[] = []
/** 稳定的空参数/空绘图引用：避免默认值每次渲染换新对象触发图表重建。 */
const emptyParams: Record<string, PineInputValue> = {}
const message = (e: unknown) => (e instanceof Error ? e.message : '请求失败')
const keyOf = (t: Target) => [t.kind, t.market, t.symbol].join(':')
function savedDraft(): Draft {
  try {
    const d = JSON.parse(sessionStorage.getItem('pw:studio:editor') || 'null')
    // 只接受 Pine 脚本；旧的四语句草稿自动迁移到默认模板
    if (
      typeof d?.source === 'string' &&
      typeof d.name === 'string' &&
      d.source.includes('//@version')
    )
      return d
  } catch {}
  return templates[0]
}
function scriptOnly(
  item?: StrategyLibraryItem,
): item is StrategyLibraryItem & { config: { chart_script: string } } {
  const matches = (rows: StrategyLibraryItem['buy'], field: string) =>
    rows.length === 1 &&
    rows[0].field === field &&
    rows[0].op === 'eq' &&
    rows[0].value === 1 &&
    !rows[0].offset &&
    (!rows[0].consecutive || rows[0].consecutive === 1)
  // 脚本策略以 config.mode 为准；老数据（只有合成 template_entry/exit 行）仍按脚本处理
  const config = item?.config as { mode?: string; chart_script?: string; legacy_script?: string } | undefined
  const hasScript = !!(config?.chart_script || config?.legacy_script)
  if (!hasScript || item?.questions?.length) return false
  const realBuy = (item?.buy || []).filter(row => row.field !== 'template_entry')
  const realSell = (item?.sell || []).filter(row => row.field !== 'template_exit')
  if (realBuy.length || realSell.length || (item?.avoid?.length || 0) > 0) return false
  if (config?.mode === 'script') return true
  if (config?.mode === 'conditions') return true   // 只剩合成行：仍是脚本策略
  return matches(item!.buy, 'template_entry') || matches(item!.sell, 'template_exit')
}
function snapshot(result: BacktestResult): History {
  const rules = result.rules_snapshot as
    | { name?: string; config?: { chart_script?: string } }
    | undefined
  return {
    result,
    name: rules?.name || '策略回测',
    source: rules?.config?.chart_script,
  }
}
export default function StrategyStudio({
  stocks,
  items,
  initialSymbol,
  market,
  selected,
  historical,
  gallery,
  setGallery,
  onSaved,
  onRules,
  onOpenLibrary,
  autoEdit = false,
  forceScript = false,
}: Props) {
  const [draft, setDraft] = useState<Draft>(() =>
    scriptOnly(selected)
      ? {
          id: selected.id,
          name: selected.name,
          source: selected.config.chart_script,
        }
      : savedDraft(),
  )
  // 支持从列表直接进入脚本编辑（?script=1），绕开类型推断
  const urlMode = typeof window !== 'undefined' ? new URLSearchParams(window.location.search).get('mode') : null
  const forceScriptFromQuery = urlMode === 'script' || (typeof window !== 'undefined' && new URLSearchParams(window.location.search).get('script') === '1')
  const forceConditions = urlMode === 'conditions'
  // selected 是异步到达的：到了再校正一次，脚本策略一律回到脚本模式（不依赖首帧）
  useEffect(() => {
    const cfg = selected?.config as { chart_script?: string; legacy_script?: string } | undefined
    if (cfg?.chart_script || cfg?.legacy_script) setStructured(undefined)
  }, [selected?.id])
  // 打开过一次就把 edit/script 从地址栏去掉，刷新时不应再次弹窗
  useEffect(() => {
    if (typeof window === 'undefined') return
    const params = new URLSearchParams(window.location.search)
    if (params.get('edit') !== '1' && !params.has('script')) return
    params.delete('edit')
    params.delete('script')
    window.history.replaceState(null, '', `${window.location.pathname}?${params.toString()}`)
  }, [])
  const [structured, setStructured] = useState<StrategyLibraryItem | undefined>(
    forceConditions ? selected : (scriptOnly(selected) || forceScript || forceScriptFromQuery ? undefined : selected),
  )
  const [applied, setApplied] = useState<Draft>(() => {
    if (scriptOnly(selected)) return draft
    try {
      const previous = JSON.parse(
        sessionStorage.getItem('pw:studio:applied') || 'null',
      )
      if (typeof previous?.source === 'string') return previous
    } catch {}
    return templates[0]
  })
  const [manual, setManual] = useState(''),
    [manualMarket, setManualMarket] = useState('CN')
  const [lookupOpen, setLookupOpen] = useState(false)
  const [custom, setCustom] = useState<Target[]>([])
  const appliedCache = useRef<{
    key: string
    data: ChartPreviewResult
  }>()
  const [instrument, setInstrument] = useState<Target>(() => {
    const s =
      historical?.symbol ||
      initialSymbol ||
      (selected?.scope === 'stock' ? selected.symbol : '')
    if (s)
      return {
        symbol: s,
        name:
          stocks.find((x) => x.symbol === s && x.market === market)?.name || s,
        market,
        kind: 'stock',
      }
    return stocks.length ? { ...stocks[0], kind: 'stock' } : indices[0]
  })
  const options = [
    ...indices,
    ...stocks.map((s) => ({ ...s, kind: 'stock' as const })),
    ...custom.filter(
      (t) =>
        !stocks.some((s) => s.symbol === t.symbol && s.market === t.market),
    ),
  ]
  if (!options.some((s) => keyOf(s) === keyOf(instrument)))
    options.push(instrument)
  const [period, setPeriod] = useState('1d'),
    [tab, setTab] = useState<'editor' | 'report'>(
      historical ? 'report' : 'editor',
    )
  const [writer, setWriter] = useState(false)
  const [collapsed, setCollapsed] = useState(false),
    [notice, setNotice] = useState(''),
    [busy, setBusy] = useState(false)
  const [report, setReport] = useState<History | undefined>(
    historical ? snapshot(historical) : undefined,
  )
  const [overlayReport, setOverlayReport] = useState(!!historical)
  const [preview, setPreview] = useState<ChartPreviewResult>()
  const [failure, setFailure] = useState(''),
    [loading, setLoading] = useState(false),
    [reload, setReload] = useState(0)
  // Pine 脚本参数：换标的/换周期都保留，只有脚本自己声明过的参数才会被使用。
  const [params, setParams] = useState<Record<string, PineInputValue>>(emptyParams)
  const [compile, setCompile] = useState<ChartCompileResult>()
  const [compiling, setCompiling] = useState(false)
  const [range, setRange] = useState({
    start: '',
    end: '',
    capital: '100000',
    commission: '0.025',
    minFee: '5',
    stamp: '0.05',
    transfer: '0.001',
    slippage: '5',
  })
  const [focus, setFocus] = useState<{ date: string }>(),
    [picked, setPicked] = useState(''),
    [order, setOrder] = useState<number | null>(null)
  const [shelf, setShelf] = useState<Draft[]>(() => {
    try {
      const a = JSON.parse(sessionStorage.getItem('pw:studio:shelf') || '[]')
      return Array.isArray(a) ? a : []
    } catch {
      return []
    }
  })
  // —— TideView 布局新增状态：只影响工作台外观，不改变原有数据流 ——
  const [editorOpen, setEditorOpen] = useState(false)
  // 关闭脚本编辑器：若从「我的策略」进来，则返回并重新打开弹窗
  const editorWasOpen = useRef(false)
  useEffect(() => {
    if (editorOpen) { editorWasOpen.current = true; return }
    if (!editorWasOpen.current || typeof window === 'undefined') return
    if (sessionStorage.getItem('pw:returnToLibrary') !== '1') return
    sessionStorage.removeItem('pw:returnToLibrary')
    const params = new URLSearchParams(window.location.search)
    params.set('view', 'my')
    params.set('library', '1')
    window.location.href = `${window.location.pathname}?${params.toString()}`
  }, [editorOpen])
  useEffect(() => {
    if (autoEdit && !structured) setEditorOpen(true)
  }, [autoEdit, structured])
  const [optionsOpen, setOptionsOpen] = useState(false)
  const [reportOpen, setReportOpen] = useState(false)
  const [layout, setLayout] = useState<'default' | 'compact'>('default')
  const [chartType, setChartType] = useState<StudioChartType>('candle')
  const [statusState, setStatusState] = useState<'ready' | 'running' | 'error'>(
    'ready',
  )
  const [cursorLine, setCursorLine] = useState(1)
  const [cursorColumn, setCursorColumn] = useState(1)
  // 回测结果 dock 的默认高度 280px，拖拽后写回 localStorage。
  const [dockHeight, setDockHeight] = useState(() => {
    try {
      const saved = Number(localStorage.getItem(DOCK_HEIGHT_KEY))
      if (Number.isFinite(saved) && saved >= DOCK_MIN_HEIGHT)
        return Math.min(
          saved,
          Math.round(
            (typeof window === 'undefined' ? 1200 : window.innerHeight) * 0.6,
          ),
        )
    } catch {}
    // 手机上空闲高度紧张，dock 默认只留一条「回测结果」的抬头
    const compact =
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(max-width: 700px)').matches
    return compact ? 190 : DOCK_DEFAULT_HEIGHT
  })
  // 图表区高度独立于回测区：0 = 自适应（吃满剩余空间），>0 = 用户拖出的固定高度
  const [chartHeight, setChartHeight] = useState(() => {
    try {
      const saved = Number(localStorage.getItem(CHART_HEIGHT_KEY))
      if (Number.isFinite(saved) && saved >= CHART_MIN_HEIGHT) return saved
    } catch {}
    return 0
  })
  function resizeChart(next: number) {
    // 不设上限：K 线区可以一直拖高，由外层滑轨承接
    const value = next <= 0 ? 0 : Math.max(Math.round(next), CHART_MIN_HEIGHT)
    setChartHeight(value)
    try {
      if (value) localStorage.setItem(CHART_HEIGHT_KEY, String(value))
      else localStorage.removeItem(CHART_HEIGHT_KEY)
    } catch {}
  }
  function startChartDrag(event: { preventDefault: () => void }) {
    event.preventDefault()
    const stage = canvasSection.current
    if (!stage) return
    const top = stage.getBoundingClientRect().top
    const move = (moveEvent: PointerEvent) =>
      resizeChart(moveEvent.clientY - top)
    const up = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      document.body.classList.remove('st-dock-dragging')
    }
    document.body.classList.add('st-dock-dragging')
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }
  // 手机端走独立骨架（同状态、不同 DOM）：<= 700px 判定
  const [isMobile, setIsMobile] = useState(
    () =>
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(max-width: 700px)').matches,
  )
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const query = window.matchMedia('(max-width: 700px)')
    const onChange = (event: MediaQueryListEvent) => setIsMobile(event.matches)
    query.addEventListener('change', onChange)
    return () => query.removeEventListener('change', onChange)
  }, [])
  const [stars, setStars] = useState<string[]>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(STAR_KEY) || '[]')
      return Array.isArray(saved)
        ? saved.filter((key): key is string => typeof key === 'string')
        : []
    } catch {
      return []
    }
  })
  const seq = useRef(0),
    canvasSection = useRef<HTMLDivElement>(null)
  const previewSeq = useRef(0)
  const editor = useRef<HTMLTextAreaElement>(null),
    gutter = useRef<HTMLPreElement>(null)
  // 上次预览的标的+周期：只有上下文变化才清空图表，脚本报错时保留旧图。
  const previewContext = useRef('')
  const lastSaved = items.find((i) => i.id === draft.id)
  const plainStock =
    instrument.kind === 'stock' &&
    instrument.market === 'CN' &&
    /^(000|001|002|003|300|301|600|601|603|605|688|689)\d{3}$/.test(
      instrument.symbol,
    )
  const selectedStrategy = structured || lastSaved
  const compatible =
    !selectedStrategy ||
    (selectedStrategy.market === instrument.market &&
      (selectedStrategy.scope !== 'stock' ||
        selectedStrategy.symbol === instrument.symbol))
  const useSnapshot = overlayReport && report && period === '1d'
  const bars =
    (useSnapshot ? report.result.chart_bars : preview?.bars) || emptyBars
  const orders = useSnapshot ? report.result.orders || emptyOrders : emptyOrders
  const actual = bars.find((b) => b.date === picked)
  const dirty = draft.source !== applied.source || draft.name !== applied.name
  const fullName = structured?.name || applied.name
  const instrumentKey = keyOf(instrument)
  const starred = stars.includes(instrumentKey)
  const favorite = shelf.some(
    (item) => item.name === draft.name && item.source === draft.source,
  )
  // 脚本类型沿用原有 engine / kind 判定，只做一次归一化给顶部和脚本条共用。
  const sourceEngine = compile?.engine || preview?.engine
  const sourceKind =
    compile?.engine === 'pine' ? compile.kind : preview?.script_kind
  // ---- 观潮式编辑器状态：脏标记、行数、错误行 ----

  const lineCount = draft.source.split('\n').length
  const errorLines = new Set((compile?.diagnostics || []).map((row) => row.line))
  const errorCount = compile?.diagnostics.length || 0
  const scriptKind: StudioScriptKind =
    sourceEngine === 'pine'
      ? sourceKind === 'strategy'
        ? 'strategy'
        : 'indicator'
      : 'v1'
  // 指标角标 = 图表内置指标 + 当前 Pine 脚本声明的绘图。
  const indicatorCount = readStudies().length + (preview?.plots?.length || 0)
  // Pine 预览统计：回测快照里没有统计字段时继续保留预览结果，避免面板空掉。
  const pineStats = preview?.engine === 'pine' ? preview.stats : undefined
  const pineTrades = preview?.engine === 'pine' ? preview.trades : undefined
  const pineEquity = preview?.engine === 'pine' ? preview.equity_curve : undefined
  const pineDates =
    preview?.engine === 'pine' ? preview.bars.map((bar) => bar.date) : undefined
  // 回测报告没有 Pine 统计时，用现有 BacktestResult.metrics 组装同一套指标网格。
  const reportStats: StudioStats | undefined = report
    ? {
        total_return_pct: report.result.total_return,
        annualized_return_pct: report.result.metrics?.annual_return,
        max_drawdown_pct: report.result.metrics?.max_drawdown,
        win_rate_pct: report.result.win_rate,
        trades: report.result.trade_count,
        profit_factor: report.result.metrics?.profit_factor ?? undefined,
        avg_holding_bars: report.result.metrics?.avg_hold_days,
      }
    : undefined
  const dockStats = pineStats ?? reportStats
  const dockState: 'idle' | 'running' | 'done' | 'error' =
    statusState === 'running'
      ? 'running'
      : statusState === 'error'
        ? 'error'
        : report
          ? 'done'
          : 'idle'
  const statusDetail =
    notice ||
    (!compatible
      ? '所选策略与当前标的范围不符'
      : !plainStock
        ? '仅看图研究 · 单票回测支持沪深普通 A 股'
        : dirty
          ? '草稿已修改，尚未应用到图表'
          : useSnapshot
            ? '回测结果已归档'
            : '使用真实历史行情')
  // 旧 footer 里的数据源说明并入状态栏，鼠标悬停仍可看到完整文本。
  const statusSource = useSnapshot
    ? report.result.data_info?.source
    : preview?.source
  const statusText = statusSource
    ? statusDetail + ' · ' + statusSource
    : statusDetail
  useEffect(() => {
    try {
      sessionStorage.setItem('pw:studio:editor', JSON.stringify(draft))
      sessionStorage.setItem('pw:studio:shelf', JSON.stringify(shelf))
    } catch {}
  }, [draft, shelf])
  useEffect(() => {
    try {
      sessionStorage.setItem('pw:studio:applied', JSON.stringify(applied))
    } catch {}
  }, [applied])
  useEffect(
    () => () => {
      seq.current++
      previewSeq.current++
    },
    [],
  )
  useEffect(() => {
    if (useSnapshot) return
    const context = keyOf(instrument) + '|' + period
    const sameContext = previewContext.current === context
    previewContext.current = context
    const cacheKey = [
      keyOf(instrument),
      period,
      applied.source,
      JSON.stringify(params),
    ].join('|')
    if (!structured && appliedCache.current?.key === cacheKey) {
      setPreview(appliedCache.current.data)
      appliedCache.current = undefined
      setLoading(false)
      setFailure('')
      return
    }
    const controller = new AbortController(),
      n = ++previewSeq.current
    setLoading(true)
    setFailure('')
    // 换标的/换周期才清空旧图；同一上下文里脚本报错时保留上一次的图。
    if (!sameContext) setPreview(undefined)
    // 沿用既有 fetchAPI 调用点：该接口的返回类型已扩展为 ChartPreviewResult
    fetchAPI<ChartPreviewResult>('/strategy/chart-preview', {
      method: 'POST',
      signal: controller.signal,
      timeoutMs: 90000,
      body: JSON.stringify({
        symbol: instrument.symbol,
        market: instrument.market,
        instrument: instrument.kind,
        interval: period,
        script: applied.source,
        price_only: !!structured,
        ...(Object.keys(params).length ? { inputs: params } : {}),
        draft_id:
          compatible &&
          structured &&
          !structured.questions?.length &&
          structured.timing !== 'intraday_preview' &&
          period === '1d' &&
          instrument.kind === 'stock'
            ? structured.id
            : undefined,
      }),
    })
      .then((data) => {
        if (n === previewSeq.current) setPreview(data)
      })
      .catch((e) => {
        if (!controller.signal.aborted && n === previewSeq.current)
          setFailure(message(e))
      })
      .finally(() => {
        if (n === previewSeq.current) setLoading(false)
      })
    return () => {
      controller.abort()
      previewSeq.current++
    }
  }, [
    instrument,
    period,
    applied,
    structured,
    compatible,
    reload,
    !!useSnapshot,
    params,
  ])
  // Ctrl+Enter 全局快捷键：抽屉打开时也生效，preventDefault 避免 textarea 换行。
  const shortcut = useRef<() => void>(() => {})
  shortcut.current = runBacktest
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        event.preventDefault()
        shortcut.current()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])
  function saveScript(value: Draft, original?: StrategyLibraryItem) {
    return strategyApi.saveDraft({
      draft_id: value.id,
      name: value.name,
      scope: original?.scope || 'global',
      symbol: original?.symbol || '',
      market: original?.market || instrument.market,
      config: { chart_script: value.source },
      source_text: value.source,
      buy: [{ field: 'template_entry', op: 'eq', value: 1 }],
      sell: [{ field: 'template_exit', op: 'eq', value: 1 }],
      avoid: [],
      note: original?.note || '',
      timing: 'completed_daily',
    })
  }
  function writeDraft(next: Draft) {
    seq.current++
    setBusy(false)
    setStatusState('ready')
    setDraft(next)
  }
  function loadDraft(next: Draft, item?: StrategyLibraryItem) {
    if (
      (draft.source.trim() || draft.name) &&
      !shelf.some((s) => s.name === draft.name && s.source === draft.source)
    )
      setShelf((prev) => [draft, ...prev].slice(0, 20))
    writeDraft(next)
    setStructured(item)
    setTab('editor')
    setCollapsed(false)
    // 旧布局在这里展开编辑面板；新布局等价地打开脚本编辑抽屉。
    setEditorOpen(true)
    setStatusState('ready')
    setNotice('已载入编辑器')
    if (item) setOverlayReport(false)
  }
  function switchContext() {
    seq.current++
    setBusy(false)
    setPicked('')
    setFocus(undefined)
    setOrder(null)
    setOverlayReport(false)
    setStatusState('ready')
    setNotice('')
  }
  /** 顶栏切换标的：沿用原 select 的 manual 分支和上下文清理。 */
  function changeInstrument(value: string) {
    if (value === 'manual') {
      setLookupOpen(true)
      return
    }
    const target = options.find((row) => keyOf(row) === value)
    if (!target) return
    switchContext()
    setReport(undefined)
    setInstrument(target)
  }
  /** 顶栏切换周期：沿用原按钮的上下文清理逻辑。 */
  function changePeriod(value: string) {
    if (value === period) return
    switchContext()
    setPeriod(value)
  }
  /** 从 textarea 的 selectionStart 计算行/列，供状态栏显示。 */
  function trackCursor(node: HTMLTextAreaElement) {
    const position = node.selectionStart ?? 0
    const before = node.value.slice(0, position)
    setCursorLine(before.split('\n').length)
    setCursorColumn(position - before.lastIndexOf('\n'))
  }
  function toggleStar() {
    setStars((prev) => {
      const next = prev.includes(instrumentKey)
        ? prev.filter((key) => key !== instrumentKey)
        : [...prev, instrumentKey]
      try {
        localStorage.setItem(STAR_KEY, JSON.stringify(next))
      } catch {}
      return next
    })
  }
  /** 收藏复用现有草稿架：收藏 = 当前脚本进入 sessionStorage 草稿架。 */
  function toggleFavorite() {
    setShelf((prev) => {
      const exists = prev.some(
        (item) => item.name === draft.name && item.source === draft.source,
      )
      const next = exists
        ? prev.filter(
            (item) =>
              !(item.name === draft.name && item.source === draft.source),
          )
        : [draft, ...prev].slice(0, 20)
      try {
        sessionStorage.setItem('pw:studio:shelf', JSON.stringify(next))
      } catch {}
      return next
    })
  }
  /**
   * 函数知识库随前端静态资源发布，保持和观潮同一份 Pine 知识。
   * TODO(P1): 后端提供下载接口后，删除 public/pine-functions.txt 静态副本。
   */
  function downloadKnowledge() {
    const link = document.createElement('a')
    link.href = '/pine-functions.txt'
    link.download = 'TideView-Pine函数知识库.txt'
    document.body.appendChild(link)
    link.click()
    link.remove()
    setNotice('函数知识库已开始下载')
  }
  /**
   * 指标菜单仍由 StudioChart 内部持有；这里桥接图例上的「指标」按钮，
   * 不修改图表内核。TODO(P1): 后续可把指标状态提升为受控 props。
   */
  function openIndicators() {
    const button = canvasSection.current?.querySelector<HTMLButtonElement>(
      '.st-legend button[aria-expanded]',
    )
    if (button) {
      button.click()
      return
    }
    setNotice('指标入口在图表工具栏')
  }
  /** 我的策略：优先打开整页弹窗，没有回调时退回弹窗式脚本列表 */
  function openLibrary() {
    if (onOpenLibrary) onOpenLibrary(instrument.symbol, instrument.market)
    else setGallery('mine')
  }
  function changeChartType(next: StudioChartType) {
    setChartType(next)
    // TODO(P1): 图表内核本轮未改，线图先只记录选择，下一轮接入 series 类型。
    setNotice(next === 'candle' ? '' : '线图模式待图表内核接入（P1）')
  }
  /** 所有回测入口共用同一套前置校验，避免顶栏菜单绕过禁用条件。 */
  function runBacktest() {
    if (busy) return
    if (!plainStock) {
      setNotice('仅沪深普通 A 股支持单票日线回测')
      return
    }
    if (!compatible) {
      setNotice('所选策略与当前标的范围不符')
      return
    }
    void action('run')
  }
  async function action(mode: 'apply' | 'save' | 'run') {
    const n = ++seq.current
    setBusy(true)
    setNotice('')
    setStatusState('running')
    let ok = false
    try {
      if (mode === 'run') {
        const values = [
          range.capital,
          range.commission,
          range.minFee,
          range.stamp,
          range.transfer,
          range.slippage,
        ]
        if (
          values.some(
            (v) =>
              v.trim() === '' || !Number.isFinite(Number(v)) || Number(v) < 0,
          ) ||
          +range.capital <= 0 ||
          (range.start && range.end && range.start > range.end)
        )
          throw Error('请检查回测日期、初始资金与费用。')
        const matched =
          structured ||
          (lastSaved?.config?.chart_script === draft.source &&
          lastSaved.name === draft.name
            ? lastSaved
            : undefined)
        const result = await fetchAPI<BacktestResult>('/strategy/backtest', {
          method: 'POST',
          timeoutMs: 180000,
          body: JSON.stringify({
            symbol: instrument.symbol,
            market: instrument.market,
            ...(matched
              ? { draft_id: matched.id }
              : { chart_script: draft.source, script_name: draft.name }),
            days: 500,
            start_date: range.start || null,
            end_date: range.end || null,
            initial_capital: +range.capital,
            commission_rate: +range.commission / 100,
            min_commission: +range.minFee,
            stamp_duty_rate: +range.stamp / 100,
            transfer_fee_rate: +range.transfer / 100,
            slippage_bps: +range.slippage,
            ...(Object.keys(params).length ? { inputs: params } : {}),
          }),
        })
        if (n !== seq.current) return
        if (result.error) throw Error(result.error)
        setReport(snapshot(result))
        setApplied(draft)
        setOverlayReport(true)
        setPeriod('1d')
        setTab('report')
        setCollapsed(false)
        setOrder(null)
        setPicked('')
        setFocus(undefined)
        setNotice('回测完成 · 已显示本次日线快照')
      } else if (mode === 'save') {
        if (lastSaved && !scriptOnly(lastSaved))
          throw Error(
            '该草稿包含其他规则，请从「我的策略」编辑，或复制成新脚本。',
          )
        const result = await saveScript(draft, lastSaved)
        onSaved()
        if (n !== seq.current) return
        setDraft({ ...draft, id: result.id })
        setNotice('草稿已保存到我的策略')
      } else {
        // Keep the previous chart mounted until the replacement is valid.
        const data = await fetchAPI<ChartPreviewResult>(
          '/strategy/chart-preview',
          {
            method: 'POST',
            timeoutMs: 90000,
            body: JSON.stringify({
              symbol: instrument.symbol,
              market: instrument.market,
              instrument: instrument.kind,
              interval: period,
              script: draft.source,
              ...(Object.keys(params).length ? { inputs: params } : {}),
            }),
          },
        )
        if (n !== seq.current) return
        appliedCache.current = {
          key: [
            keyOf(instrument),
            period,
            draft.source,
            JSON.stringify(params),
          ].join('|'),
          data,
        }
        setApplied({ ...draft })
        setOverlayReport(false)
        setPreview(data)
        setFailure('')
        setNotice('策略已绘制到图表')
      }
      ok = true
    } catch (e) {
      if (n === seq.current) {
        setNotice(message(e))
        // 脚本被后端拒绝（例如 Pine 400）时把原文留在编辑器下方，旧图表保持不动
        if (mode === 'apply') setFailure(message(e))
      }
    } finally {
      if (n === seq.current) {
        setBusy(false)
        setStatusState(ok ? 'ready' : 'error')
      }
    }
  }
  function locate(date: string) {
    setFocus({ date })
    setPicked(date)
    canvasSection.current?.scrollIntoView({ block: 'nearest' })
  }
  /** 只做语法检查：诊断列表可点击定位到编辑器对应行。 */
  async function checkSyntax() {
    setCompiling(true)
    try {
      const data = await strategyApi.chartCompile({ script: draft.source })
      setCompile(data)
      setNotice(data.ok ? '语法检查通过' : '脚本存在语法问题，请查看诊断列表')
    } catch (e) {
      setCompile(undefined)
      setNotice(message(e))
    } finally {
      setCompiling(false)
    }
  }
  /** 选中诊断行整行，把编辑器滚动到可视区。 */
  function goToLine(line: number, column = 1) {
    const node = editor.current
    if (!node) return
    const rows = draft.source.split('\n')
    const target = Math.min(Math.max(1, Math.floor(line) || 1), rows.length)
    let offset = 0
    for (let i = 0; i < target - 1; i++) offset += rows[i].length + 1
    const text = rows[target - 1]
    const start = offset + Math.min(Math.max(column - 1, 0), text.length)
    node.focus()
    node.setSelectionRange(start, offset + text.length)
    setCursorLine(target)
    setCursorColumn(Math.max(1, column))
  }
  function selectOrder(i: number) {
    setOrder(i)
    setOverlayReport(true)
    setPeriod('1d')
    const o = report?.result.orders?.[i]
    if (o) locate(o.execution_date || o.signal_date)
  }
  function inspect(date: string) {
    setPicked(date)
    const i = orders.findIndex(
      (o) => o.execution_date === date || o.signal_date === date,
    )
    if (i >= 0) setOrder(i)
  }
  const currentOrder =
    order === null ? undefined : report?.result.orders?.[order]
  const indicatorSource = useSnapshot
    ? report.source
    : structured
      ? undefined
      : applied.source
  const pineInputs = useSnapshot ? [] : preview?.inputs || []
  const warnings = compile?.warnings?.length
    ? compile.warnings
    : useSnapshot
      ? []
      : preview?.warnings || []
  // 现有回测结果区搬到 dock 的「收益」tab，结构、按钮和回调保持不变。
  const resultNode: ReactNode = report ? (
    <>
      <div className="st-edithead st-dock-report-head">
        <h3>
          {report.name} · 报告 #{report.result.backtest_id}
        </h3>
        <div className="st-dock-report-actions">
          {!useSnapshot && (
            <button
              type="button"
              onClick={() => {
                setOverlayReport(true)
                setPeriod('1d')
                setPicked('')
              }}
            >
              查看本次 K 线
            </button>
          )}
          <button type="button" onClick={() => setOptionsOpen(true)}>
            回测参数
          </button>
        </div>
      </div>
      <p>
        {report.result.start} — {report.result.end} · 策略{' '}
        {report.result.total_return}% · 买入持有{' '}
        {report.result.buy_hold_return}% · 最大回撤{' '}
        {report.result.metrics?.max_drawdown ?? '—'}%
      </p>
      {report.source && report.source !== draft.source && (
        <p>本报告对应另一版脚本。</p>
      )}
      <details>
        <summary>本次回测口径</summary>
        <p>
          资金 {report.result.initial_capital} 元 · 数据{' '}
          {report.result.data_info?.source || '未记录'} · 复权{' '}
          {report.result.data_info?.adjustment || '未知'} · 预热{' '}
          {report.result.data_info?.warmup_bars ?? '—'} 根
        </p>
        <p>{report.result.explain}</p>
        {report.source && <pre>{report.source}</pre>}
      </details>
      <ResultChart
        result={report.result}
        orders={report.result.orders || emptyOrders}
        selectedOrder={order}
        onSelectOrder={selectOrder}
      />
      <details>
        <summary>
          信号与订单 · {report.result.orders?.length || 0} 笔
        </summary>
        {report.result.orders?.map((o, i) => (
          <div className="st-row" key={i}>
            <button
              type="button"
              onClick={() => {
                setOverlayReport(true)
                setPeriod('1d')
                setOrder(i)
                locate(o.signal_date)
              }}
            >
              {o.signal_date} {o.side === 'buy' ? '买入' : '卖出'}
              信号
            </button>
            <button type="button" onClick={() => selectOrder(i)}>
              {o.execution_date || '区间结束'} ·{' '}
              {o.status === 'filled'
                ? '成交 ' + o.fill_price + ' × ' + o.quantity
                : o.reason || '未成交'}
            </button>
          </div>
        ))}
      </details>
    </>
  ) : null
  if (isMobile) {
    return (
      <StudioMobileWorkbench
        instrument={{
          name: instrument.name,
          symbol: instrument.symbol,
          market: instrument.market,
        }}
        symbolOptions={options.map((target) => ({
          value: keyOf(target),
          label:
            target.name === target.symbol
              ? target.symbol
              : target.name + ' ' + target.symbol,
        }))}
        onSymbol={changeInstrument}
        period={period}
        periods={STUDIO_INTERVALS.map((item) => ({
          value: item,
          label:
            item === '1d'
              ? '日线'
              : item === '1w'
                ? '周线'
                : item === '1m'
                  ? '月线'
                  : item,
        }))}
        onPeriod={changePeriod}
        bars={bars}
        orders={orders}
        plots={useSnapshot ? undefined : preview?.plots}
        focus={focus}
        onPick={inspect}
        storageKey={'pw:lines:' + keyOf(instrument) + period}
        loading={loading}
        failure={failure}
        onReload={() => setReload((n) => n + 1)}
        scriptName={fullName}
        scriptKind={scriptKind}
        busy={busy}
        canBacktest={plainStock && compatible}
        backtestHint={
          !plainStock
            ? '仅沪深普通 A 股支持单票日线回测'
            : '所选策略与当前标的范围不符'
        }
        favorite={favorite}
        indicatorCount={indicatorCount}
        onBacktest={runBacktest}
        onFavorite={toggleFavorite}
        onGallery={openLibrary}
        onKnowledge={downloadKnowledge}
        onIndicators={openIndicators}
        draft={{ name: draft.name, source: draft.source }}
        onDraft={writeDraft}
        inputs={(preview?.inputs ?? []) as never}
        params={params as never}
        onParams={setParams as never}
        compile={compile}
        compiling={compiling}
        onCompile={checkSyntax}
        onGoToLine={goToLine}
        onSave={() => void action('save')}
        onApply={() => void action('apply')}
        dockState={dockState}
        stats={dockStats as never}
        trades={pineTrades}
        equity={pineEquity}
        dates={pineDates}
        result={resultNode}
        notice={notice}
        preview={preview}
        onTemplate={() => setGallery('templates')}
      />
    )
  }
  return (
    <section
      className={'st st-workbench' + (layout === 'compact' ? ' st-compact' : '')}
      aria-label="K线策略工作台"
      data-panel-tab={tab}
      data-panel-collapsed={collapsed ? 'true' : 'false'}
    >
      <h2 className="st-visually-hidden">策略工作台</h2>
      <StudioTopbar
        symbol={instrument.symbol}
        name={instrument.name}
        starred={starred}
        onStar={toggleStar}
        chartType={chartType}
        onChartType={changeChartType}
        interval={period}
        intervals={STUDIO_INTERVALS}
        onInterval={changePeriod}
        indicatorCount={indicatorCount}
        onIndicators={openIndicators}
        strategyName={fullName}
        scriptKind={scriptKind}
        onScriptEdit={() => setEditorOpen(true)}
        onBacktest={runBacktest}
        onSettings={() => setOptionsOpen(true)}
        instrumentValue={instrumentKey}
        instrumentOptions={options.map((target) => ({
          value: keyOf(target),
          label:
            (target.name === target.symbol
              ? target.symbol
              : target.name + ' · ' + target.symbol) +
            ' ' +
            (target.kind === 'index' ? '指数' : target.market),
        }))}
        onInstrumentChange={changeInstrument}
        onGallery={(view) => setGallery(view)}
        layout={layout}
        onLayout={setLayout}
      />
      {lookupOpen && (
        <div className="st-lookup">
          <form
            onSubmit={(e) => {
              e.preventDefault()
              const code = manual.trim().toUpperCase()
              if (!code) return
              const target: Target = {
                kind: 'stock',
                market: manualMarket,
                symbol: code,
                name:
                  stocks.find(
                    (s) => s.symbol === code && s.market === manualMarket,
                  )?.name || code,
              }
              setCustom((rows) => [
                ...rows.filter((r) => keyOf(r) !== keyOf(target)),
                target,
              ])
              switchContext()
              setReport(undefined)
              setInstrument(target)
              setManual('')
              setLookupOpen(false)
            }}
          >
            <select
              aria-label="标的市场"
              value={manualMarket}
              onChange={(e) => setManualMarket(e.target.value)}
            >
              <option value="CN">A股</option>
              <option value="HK">港股</option>
              <option value="US">美股</option>
            </select>
            <input
              required
              maxLength={20}
              aria-label="证券代码"
              placeholder="证券代码"
              value={manual}
              onChange={(e) => setManual(e.target.value)}
            />
            <button>打开</button>
            <button type="button" onClick={() => setLookupOpen(false)}>
              取消
            </button>
          </form>
        </div>
      )}
      <div
        ref={canvasSection}
        className={'st-stage' + (chartHeight ? ' st-stage-fixed' : '')}
        style={chartHeight ? { height: chartHeight } : undefined}
      >
        <div className="st-caption st-visually-hidden">
          {useSnapshot ? report.name : fullName}
          <small>
            {indicatorSource
              ? indicatorSource
                  .split(/\r?\n/)
                  .slice(0, 2)
                  .map((s) => s.split('=')[1]?.trim())
                  .join(' / ')
              : '规则策略'}{' '}
            ·{' '}
            {useSnapshot
              ? '报告 #' + report.result.backtest_id
              : period === '1d'
                ? '日线'
                : period === '1w'
                  ? '周线'
                  : '月线'}
          </small>
        </div>
        {bars.length > 0 ? (
          <StudioChart
            key={keyOf(instrument) + period}
            bars={bars}
            orders={orders}
            storageKey={'pw:lines:' + keyOf(instrument) + period}
            focus={focus}
            onPick={inspect}
            plots={useSnapshot ? undefined : preview?.plots}
          />
        ) : (
          <div className="st-empty" role="status">
            {useSnapshot
              ? '这份旧报告未记录原始 K 线，可查看原收益与逐笔订单。'
              : loading
                ? '正在读取行情…'
                : failure || '暂无可显示的 K 线'}
            {!useSnapshot && !loading && (
              <button type="button" onClick={() => setReload((n) => n + 1)}>
                重新读取
              </button>
            )}
          </div>
        )}
        {picked && actual && (
          <p className="st-detail">
            {picked} · 收 {actual.close.toFixed(2)}
            {actual.template_entry
              ? ' · 买入交叉'
              : actual.template_exit
                ? ' · 卖出交叉'
                : ''}
            {actual.fast != null ? ' · 快线 ' + actual.fast.toFixed(2) : ''}
            {actual.slow != null ? ' / 慢线 ' + actual.slow.toFixed(2) : ''}
            {currentOrder &&
              (picked === currentOrder.execution_date ||
                picked === currentOrder.signal_date) &&
              ' · ' +
                (currentOrder.side === 'buy' ? '买入' : '卖出') +
                ' · ' +
                (currentOrder.status === 'filled'
                  ? '成交 ' +
                    currentOrder.fill_price +
                    ' × ' +
                    currentOrder.quantity +
                    (typeof currentOrder.fees === 'object' && currentOrder.fees
                      ? ' · 费用 ' +
                        Number(currentOrder.fees.explicit_fees || 0).toFixed(
                          2,
                        ) +
                        ' 元 · 滑点成本 ' +
                        Number(currentOrder.fees.slippage_cost || 0).toFixed(
                          2,
                        ) +
                        ' 元'
                      : '')
                  : currentOrder.reason)}
          </p>
        )}
        <div
          className="st-stage-resize"
          role="separator"
          aria-label="调整图表区高度"
          aria-orientation="horizontal"
          title="拖动调整图表区高度，双击恢复自适应"
          onPointerDown={startChartDrag}
          onDoubleClick={() => resizeChart(0)}
        />
      </div>
      <StudioScriptBar
        strategyName={fullName}
        scriptKind={scriptKind}
        busy={busy}
        onEdit={() => setEditorOpen(true)}
        onBacktest={runBacktest}
        onBacktestOptions={() => setOptionsOpen(true)}
        onFavorite={toggleFavorite}
        onGallery={openLibrary}
        onKnowledge={downloadKnowledge}
        favorite={favorite}
        onReport={() => setReportOpen(true)}
        reportState={dockState}
        strategies={items.map(item => ({
          id: item.id,
          name: item.name,
          kind: ((item.config as { chart_script?: string; legacy_script?: string } | undefined)?.chart_script
            || (item.config as { chart_script?: string; legacy_script?: string } | undefined)?.legacy_script)
            ? 'script' as const : 'conditions' as const,
        }))}
        strategyValue={selected?.id}
        onStrategyChange={(id) => {
          const item = items.find(row => row.id === id)
          if (!item) return
          const cfg = item.config as { chart_script?: string; legacy_script?: string } | undefined
          const kind = cfg?.chart_script || cfg?.legacy_script ? 'script' : 'conditions'
          const params = new URLSearchParams(window.location.search)
          params.set('view', 'studio')
          params.set('strategy', String(id))
          params.set('mode', kind)
          window.location.href = `${window.location.pathname}?${params.toString()}`
        }}
        canBacktest={plainStock && compatible}
        backtestHint={
          !plainStock
            ? '仅沪深普通 A 股支持单票日线回测'
            : '所选策略与当前标的范围不符'
        }
      />
      {reportOpen && (
        <div className="st-modal" role="dialog" aria-label="回测结果">
          <button
            type="button"
            className="st-modal-mask"
            aria-label="关闭回测结果"
            onClick={() => setReportOpen(false)}
          />
          <div className="st-modal-panel">
            <header className="st-modal-head">
              <b>回测结果</b>
              <button
                type="button"
                className="st-modal-close"
                aria-label="关闭"
                onClick={() => setReportOpen(false)}
              >
                ✕
              </button>
            </header>
            <div className="st-modal-body">
              {dockStats && (
                <div className="st-report-kpi" aria-label="回测关键指标">
                  {(
                    [
                      ['策略收益', dockStats.total_return_pct, 'pct'],
                      ['最大回撤', dockStats.max_drawdown_pct, 'neg'],
                      ['胜率', dockStats.win_rate_pct, 'pct'],
                      ['交易笔数', dockStats.trades, 'plain'],
                    ] as Array<[string, number | undefined, 'pct' | 'neg' | 'plain']>
                  ).map(([label, value, kind]) => (
                    <div key={label} className="st-report-kpi-card">
                      <span>{label}</span>
                      <b
                        className={
                          kind === 'plain'
                            ? ''
                            : kind === 'neg'
                              ? 'st-down'
                              : (value ?? 0) < 0
                                ? 'st-down'
                                : 'st-up'
                        }
                      >
                        {value == null
                          ? '—'
                          : kind === 'plain'
                            ? String(value)
                            : value.toFixed(2) + '%'}
                      </b>
                    </div>
                  ))}
                </div>
              )}
              <StudioBacktestDock
                state={dockState}
                error={statusState === 'error' ? notice : null}
                stats={dockStats}
                statsHeading={pineStats ? undefined : '回测统计'}
                statsNote={
                  pineStats
                    ? undefined
                    : '来自本次 A 股日线回测 · 历史结果不代表未来表现'
                }
                trades={pineTrades}
                equity={pineEquity}
                dates={pineDates}
                result={resultNode}
                onEdit={() => setEditorOpen(true)}
                onTemplate={() => setGallery('templates')}
                height={Math.max(
                  dockHeight,
                  Math.round(
                    (typeof window === 'undefined' ? 900 : window.innerHeight) * 0.6,
                  ),
                )}
                onHeight={setDockHeight}
                onBacktest={runBacktest}
              />
            </div>
          </div>
        </div>
      )}
      <StudioStatusBar
        state={statusState}
        detail={statusText}
        line={cursorLine}
        column={cursorColumn}
      />

      {editorOpen && (
        <Dialog
          open
          onOpenChange={(open) => {
            if (!open) {
              if (
                dirty &&
                !window.confirm(
                  '当前脚本有未保存的修改，关闭后修改会丢失。确定关闭吗？',
                )
              )
                return
              setEditorOpen(false)
            }
          }}
        >
          <DialogContent className="strategy-dialog studio-modal st-editor-drawer">
            <DialogHeader>
              <DialogTitle>脚本编辑</DialogTitle>
              <DialogDescription>
                Pine 脚本是工作台主线；按 Ctrl+Enter 可直接开始回测。
              </DialogDescription>
            </DialogHeader>
            {structured && !SHOW_STRUCTURED ? (
              // 结构化条件区按主线约定隐藏，代码保留：把 SHOW_STRUCTURED 改回 true 即可恢复。
              <div className="st-structured-note">
                <h3>{structured.name}</h3>
                <p>
                  结构化条件编辑区已按工作台主线隐藏；如需调整规则，请打开规则编辑器。
                </p>
                <button
                  type="button"
                  onClick={() => onRules(structured, instrument.symbol)}
                >
                  打开规则编辑器
                </button>
              </div>
            ) : structured ? (
              <>
                <h3>{structured.name}</h3>
                <p>结构化策略 · 回测沿用原买卖、禁买和风险规则。</p>
                <button
                  type="button"
                  onClick={() => onRules(structured, instrument.symbol)}
                >
                  打开规则编辑器
                </button>
              </>
            ) : (
              <>
                <div className="st-edithead">
                  <input
                    aria-label="脚本名称"
                    maxLength={100}
                    value={draft.name}
                    onChange={(e) =>
                      writeDraft({ ...draft, name: e.target.value })
                    }
                  />
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => setWriter(true)}
                  >
                    AI 编写
                  </button>
                  <button type="button" onClick={() => setGallery('mine')}>
                    我的策略
                  </button>
                  <button
                    type="button"
                    disabled={busy || !draft.name.trim()}
                    onClick={() => void action('save')}
                  >
                    保存草稿
                  </button>
                  <button
                    type="button"
                    className="st-accent"
                    disabled={busy || !compatible}
                    onClick={() => void action('apply')}
                  >
                    应用到图表 ↗
                  </button>
                </div>
                <div className="st-editor-bar">
                  <span
                    className={
                      'st-badge' + (scriptKind === 'v1' ? '' : ' pine')
                    }
                  >
                    {scriptKind === 'strategy'
                      ? 'Pine 策略'
                      : scriptKind === 'indicator'
                        ? 'Pine 指标'
                        : '四语句脚本'}
                  </span>
                  <button
                    type="button"
                    disabled={compiling || !draft.source.trim()}
                    onClick={() => void checkSyntax()}
                  >
                    {compiling ? '检查中…' : '检查语法'}
                  </button>
                  {compile && (
                    <small>
                      {compile.ok
                        ? '语法检查通过'
                        : '发现 ' + compile.diagnostics.length + ' 个问题'}
                    </small>
                  )}
                </div>
                <div className="st-editor-status" aria-label="编辑器状态">
                  <span data-dirty={dirty ? 'true' : 'false'}>
                    {dirty ? '已修改，未保存' : '已保存'}
                  </span>
                  <span>{lineCount} 行</span>
                  <span data-error={errorCount > 0 ? 'true' : 'false'}>
                    {errorCount > 0 ? errorCount + ' 个错误' : '无错误'}
                  </span>
                </div>
                <div className="st-code">
                  <pre className="st-gutter" ref={gutter} aria-hidden="true">
                    {Array.from({ length: lineCount }, (_, i) => (
                      <span
                        key={i}
                        className={errorLines.has(i + 1) ? 'is-error' : undefined}
                      >
                        {i + 1}
                      </span>
                    ))}
                  </pre>
                  <textarea
                    ref={editor}
                    aria-label="策略脚本"
                    spellCheck={false}
                    wrap="off"
                    maxLength={20000}
                    value={draft.source}
                    onScroll={(e) => {
                      // 行号栏跟着纵向滚动，编号始终与代码行对齐
                      if (gutter.current)
                        gutter.current.scrollTop = e.currentTarget.scrollTop
                    }}
                    onSelect={(e) => trackCursor(e.currentTarget)}
                    onKeyUp={(e) => trackCursor(e.currentTarget)}
                    onClick={(e) => trackCursor(e.currentTarget)}
                    onChange={(e) => {
                      writeDraft({ ...draft, source: e.target.value })
                      // 脚本已改动，旧的检查结果不再可信
                      if (compile) setCompile(undefined)
                      trackCursor(e.currentTarget)
                    }}
                  />
                </div>
                {!!compile?.diagnostics.length && (
                  <ul className="st-diagnostics" aria-label="语法诊断">
                    {compile.diagnostics.map((row, i) => (
                      <li key={i}>
                        <button
                          type="button"
                          onClick={() => goToLine(row.line, row.column)}
                        >
                          第 {row.line} 行第 {row.column} 列 · {row.message}
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
                {!!warnings.length && (
                  <ul className="st-warnings" aria-label="脚本告警">
                    {warnings.map((text, i) => (
                      <li key={i}>{text}</li>
                    ))}
                  </ul>
                )}
                {!!failure && (
                  <p className="st-script-error" role="alert">
                    {failure}
                  </p>
                )}
                <details>
                  <summary>脚本语法与当前信号</summary>
                  <p>
                    快慢线支持 MA(n)、EMA(n)，周期 2–60。买卖条件支持
                    CROSSUP、CROSSDOWN。选择日期可定位交叉信号。
                  </p>
                  <div className="st-actions">
                    {bars
                      .filter((b) => b.template_entry || b.template_exit)
                      .slice(-6)
                      .reverse()
                      .map((b) => (
                        <button
                          key={b.date}
                          type="button"
                          onClick={() => locate(b.date)}
                        >
                          {b.date} · {b.template_entry ? '买' : '卖'}
                        </button>
                      ))}
                  </div>
                </details>
                {pineInputs.length > 0 && (
                  <StudioPineInputs
                    inputs={pineInputs}
                    values={params}
                    onChange={setParams}
                  />
                )}
                {lastSaved && (
                  <button
                    type="button"
                    onClick={() => onRules(lastSaved, instrument.symbol)}
                  >
                    管理应用范围
                  </button>
                )}
              </>
            )}
          </DialogContent>
        </Dialog>
      )}

      {optionsOpen && (
        <Dialog
          open
          onOpenChange={(open) => {
            if (!open) setOptionsOpen(false)
          }}
        >
          <DialogContent className="strategy-dialog studio-modal st-options-dialog">
            <DialogHeader>
              <DialogTitle>回测参数</DialogTitle>
              <DialogDescription>
                参数只影响本次回测，不会写入策略草稿；回测固定按日线运行。
              </DialogDescription>
            </DialogHeader>
            {/* st-fields 只保留在回测参数弹层；结构化条件区由 SHOW_STRUCTURED 控制。 */}
            <div className="st-fields">
              {(
                [
                  ['start', '起始日期'],
                  ['end', '结束日期'],
                  ['capital', '初始资金（元）'],
                  ['commission', '佣金（%）'],
                  ['minFee', '最低佣金（元）'],
                  ['stamp', '印花税（%）'],
                  ['transfer', '过户费（%）'],
                  ['slippage', '滑点（基点）'],
                ] as const
              ).map(([key, label]) => (
                <label key={key}>
                  {label}
                  <input
                    type={key === 'start' || key === 'end' ? 'date' : 'number'}
                    step="any"
                    min={key === 'start' || key === 'end' ? undefined : '0'}
                    value={range[key]}
                    onChange={(e) =>
                      setRange({ ...range, [key]: e.target.value })
                    }
                  />
                </label>
              ))}
            </div>
            {/* 现有回测内核固定满仓、次日开盘；可编辑参数待后端支持（P1）。 */}
            <div className="st-fields st-fields-locked">
              <label>
                仓位（%）
                <input value="100" disabled readOnly />
              </label>
              <label>
                成交时点
                <input value="次日开盘" disabled readOnly />
              </label>
            </div>
            <p className="st-pine-hint">
              仓位与成交时点沿用现有回测内核（满仓、次日开盘）；单票回测支持沪深普通
              A 股，指数与其他市场仅做图表研究。
            </p>
            <div className="st-dialog-actions">
              <button type="button" onClick={() => setOptionsOpen(false)}>
                关闭
              </button>
              <button
                type="button"
                className="st-accent"
                disabled={busy || !plainStock || !compatible}
                onClick={() => {
                  setOptionsOpen(false)
                  void action('run')
                }}
              >
                按当前参数回测
              </button>
            </div>
          </DialogContent>
        </Dialog>
      )}

      {writer && (
        <ScriptWriter
          draft={draft}
          close={() => setWriter(false)}
          accept={(next) => {
            loadDraft(next)
            setWriter(false)
            setNotice('AI 脚本已载入草稿，可应用到图表或保存')
          }}
        />
      )}
      {gallery && (
        <ScriptGallery
          initial={gallery}
          templates={templates}
          items={items}
          shelf={shelf}
          market={instrument.market}
          editRules={(item) => {
            onRules(item, item.symbol || instrument.symbol)
            setGallery(null)
          }}
          close={() => setGallery(null)}
          choose={(next) => {
            loadDraft(next)
            setGallery(null)
          }}
          openItem={(item) => {
            if (scriptOnly(item))
              loadDraft({
                id: item.id,
                name: item.name,
                source: item.config.chart_script,
              })
            else loadDraft(draft, item)
            setGallery(null)
          }}
          copy={async (next) => {
            await saveScript(next)
            await onSaved()
          }}
        />
      )}
    </section>
  )
}
