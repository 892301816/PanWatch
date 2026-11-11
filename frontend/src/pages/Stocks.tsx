import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import WealthActionMenu from '@/components/WealthActionMenu'
import { Search, X, Bot, Play, RefreshCw, Building2, ChevronDown, ChevronRight, Cpu, Bell, Clock, Newspaper, ExternalLink } from 'lucide-react'
import { fetchAPI, stocksApi, portfolioApi, strategyApi, type WealthOverview, type AIService, type NotifyChannel, type StrategyGate } from '@panwatch/api'
import { klinesApi } from '@panwatch/api/klines'
import { useLocalStorage } from '@/lib/utils'
import {
  buildPortfolioStockKeys,
  loadPortfolioPageBackgroundData,
  loadPortfolioPageCoreData,
  loadPortfolioPageQuoteData,
} from '@/lib/portfolio-page-data'
import { SuggestionBadge, type SuggestionInfo, type KlineSummary } from '@panwatch/biz-ui/components/suggestion-badge'
import { buildKlineSuggestion } from '@/lib/kline-scorer'
import { KlineSummaryDialog } from '@panwatch/biz-ui/components/kline-summary-dialog'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { Switch } from '@panwatch/base-ui/components/ui/switch'
import { Badge } from '@panwatch/base-ui/components/ui/badge'
import { Skeleton } from '@panwatch/base-ui/components/ui/skeleton'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@panwatch/base-ui/components/ui/dialog'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectGroup, SelectLabel, SelectItem } from '@panwatch/base-ui/components/ui/select'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import StockInsightModal from '@panwatch/biz-ui/components/stock-insight-modal'
import TradeGate, { isGateBlocked } from '@panwatch/biz-ui/components/TradeGate'
import { DeepAnalysisModal } from '@panwatch/biz-ui/components/deep-analysis-modal'
import WealthPanel from '@/components/WealthPanel'
import WealthSummary from '@/components/WealthSummary'
import '@/styles/wealth-workspace.css'

interface AgentResult {
  success?: boolean
  message?: string
  title: string
  content: string
  should_alert: boolean
  notified: boolean
  skipped?: boolean
}

interface StockAgentInfo {
  agent_name: string
  display_name?: string
  schedule: string
  ai_model_id: number | null
  notify_channel_ids: number[]
}

interface Stock {
  id: number
  symbol: string
  name: string
  market: string
  sort_order?: number
  group_name?: string
  agents: StockAgentInfo[]
}

interface Account {
  id: number
  name: string
  available_funds: number
  enabled: boolean
  strategy_type?: string
  risk_policy?: Record<string, number>
  fee_policy?: Record<string, number>
  dividend_reinvest?: boolean
}

interface Position {
  id: number
  stock_id: number
  sort_order?: number
  symbol: string
  name: string
  market: string
  cost_price: number
  quantity: number
  invested_amount: number | null
  trading_style: string  // short: 短线, swing: 波段, long: 长线
  stop_loss_price?: number | null
  take_profit_price?: number | null
  current_price: number | null
  current_price_cny: number | null  // 人民币价格（港股换算后）
  change_pct: number | null
  market_value: number | null
  market_value_cny: number | null  // 人民币市值
  pnl: number | null
  pnl_pct: number | null
  daily_pnl: number | null
  daily_pnl_pct: number | null
  exchange_rate: number | null  // 汇率（仅港股）
}

interface AccountSummary {
  id: number
  name: string
  available_funds: number
  total_market_value: number
  total_cost: number
  total_pnl: number
  total_pnl_pct: number
  total_daily_pnl: number
  total_assets: number
  strategy_type?: string
  risk_policy?: Record<string, number>
  fee_policy?: Record<string, number>
  dividend_reinvest?: boolean
  positions: Position[]
}

interface PortfolioSummary {
  accounts: AccountSummary[]
  total: {
    total_market_value: number
    total_cost: number
    total_pnl: number
    total_pnl_pct: number
    total_daily_pnl: number
    available_funds: number
    total_assets: number
  }
  exchange_rates?: {
    HKD_CNY: number
    USD_CNY?: number
  }
  quotes?: Record<string, { current_price: number | null; change_pct: number | null }>
}

interface AgentConfig {
  name: string
  display_name: string
  description: string
  enabled: boolean
  schedule: string
  execution_mode: string  // batch: 批量分析, single: 逐只分析
}

interface SchedulePreview {
  schedule: string
  timezone: string
  next_runs: string[]
}

interface SearchResult {
  symbol: string
  name: string
  market: string
}

interface QuoteRequestItem {
  symbol: string
  market: string
}

interface QuoteResponse {
  symbol: string
  market: string
  current_price: number | null
  change_pct: number | null
}

interface StockForm {
  symbol: string
  name: string
  market: string
}

interface AccountForm {
  name: string
  available_funds: string
}

interface PositionForm {
  account_id: number
  stock_id: number
  cost_price: string
  quantity: string
  invested_amount: string
  trading_style: string
  // 搜索选中的股票信息（新增持仓时用）
  stock_symbol: string
  stock_name: string
  stock_market: string
  record_trade: boolean
  deduct_cash: boolean
}

// 股票建议信息（来自盘中监控 API）
interface StockSuggestionData {
  symbol: string
  suggestion: SuggestionInfo | null
  kline: KlineSummary | null
}

// 建议池中的建议（包含来源和时间信息）
interface PoolSuggestion {
  id: number
  stock_symbol: string
  stock_market?: string
  stock_name: string
  action: string
  action_label: string
  signal: string
  reason: string
  agent_name: string
  agent_label: string
  created_at: string
  expires_at: string | null
  is_expired: boolean
  prompt_context: string
  ai_response: string
  meta?: Record<string, any>
  should_alert?: boolean
}

interface MarketStatus {
  code: string
  name: string
  status: string
  status_text: string
  is_trading: boolean
  sessions: string[]
  local_time: string
}

interface NewsItem {
  source: string
  source_label: string
  external_id: string
  title: string
  content: string
  publish_time: string
  symbols: string[]
  importance: number
  url: string
}

interface PriceAlertRuleSummary {
  stock_symbol: string
  market: string
  enabled: boolean
}

const emptyStockForm: StockForm = { symbol: '', name: '', market: 'CN' }
const emptyAccountForm: AccountForm = { name: '', available_funds: '0' }

const buildQuoteItemsFrom = (stockList: Stock[], portfolio: PortfolioSummary | null): QuoteRequestItem[] => {
  const items: QuoteRequestItem[] = []
  const seen = new Set<string>()
  const add = (symbol: string, market: string) => {
    const key = `${market}:${symbol}`
    if (seen.has(key)) return
    seen.add(key)
    items.push({ symbol, market })
  }

  for (const stock of stockList) add(stock.symbol, stock.market)
  for (const account of portfolio?.accounts || []) {
    for (const pos of account.positions) add(pos.symbol, pos.market)
  }
  return items
}

const toQuoteMap = (rows: QuoteResponse[]): Record<string, { current_price: number | null; change_pct: number | null }> => {
  const map: Record<string, { current_price: number | null; change_pct: number | null }> = {}
  for (const item of rows || []) {
    map[`${item.market}:${item.symbol}`] = {
      current_price: item.current_price ?? null,
      change_pct: item.change_pct ?? null,
    }
  }
  return map
}

const round2 = (value: number) => Math.round(value * 100) / 100

const mergePortfolioQuotes = (
  portfolio: PortfolioSummary | null,
  quotes: Record<string, { current_price: number | null; change_pct: number | null }>
): PortfolioSummary | null => {
  if (!portfolio) return null

  const hkdRate = portfolio.exchange_rates?.HKD_CNY ?? 0.92
  const usdRate = portfolio.exchange_rates?.USD_CNY ?? 7.25

  let grandMarketValue = 0
  let grandCost = 0
  let grandAvailable = 0
  let grandDailyPnl = 0

  const accounts = portfolio.accounts.map(account => {
    let accMarketValue = 0
    let accCost = 0
    let accDailyPnl = 0

    const positions = account.positions.map(pos => {
      const quote = quotes[`${pos.market}:${pos.symbol}`]
      const current_price = quote?.current_price ?? pos.current_price ?? null
      const change_pct = quote?.change_pct ?? pos.change_pct ?? null
      const rate = pos.market === 'HK' ? hkdRate : pos.market === 'US' ? usdRate : 1

      const cost = pos.cost_price * pos.quantity * rate
      accCost += cost

      let market_value: number | null = null
      let market_value_cny: number | null = null
      let pnl: number | null = null
      let pnl_pct: number | null = null
      let daily_pnl: number | null = null
      let daily_pnl_pct: number | null = null

      if (current_price != null) {
        market_value = current_price * pos.quantity
        market_value_cny = market_value * rate
        accMarketValue += market_value_cny
        pnl = market_value_cny - cost
        pnl_pct = cost > 0 ? (pnl / cost * 100) : 0
      }

      if (current_price != null && change_pct != null && change_pct !== -100) {
        const prev = current_price / (1 + change_pct / 100)
        if (isFinite(prev) && prev > 0) {
          daily_pnl = round2((current_price - prev) * pos.quantity * rate)
          daily_pnl_pct = round2(change_pct)
          accDailyPnl += daily_pnl
        }
      }

      return {
        ...pos,
        current_price,
        current_price_cny: current_price != null ? current_price * rate : null,
        change_pct,
        market_value,
        market_value_cny,
        pnl,
        pnl_pct,
        daily_pnl,
        daily_pnl_pct,
        exchange_rate: pos.market === 'HK' || pos.market === 'US' ? rate : null,
      }
    })

    const accPnl = accMarketValue - accCost
    const accPnlPct = accCost > 0 ? (accPnl / accCost * 100) : 0
    const accTotalAssets = accMarketValue + account.available_funds

    grandMarketValue += accMarketValue
    grandCost += accCost
    grandAvailable += account.available_funds
    grandDailyPnl += accDailyPnl

    return {
      ...account,
      total_market_value: round2(accMarketValue),
      total_cost: round2(accCost),
      total_pnl: round2(accPnl),
      total_pnl_pct: round2(accPnlPct),
      total_daily_pnl: round2(accDailyPnl),
      total_assets: round2(accTotalAssets),
      positions,
    }
  })

  const grandPnl = grandMarketValue - grandCost
  const grandPnlPct = grandCost > 0 ? (grandPnl / grandCost * 100) : 0
  const grandTotalAssets = grandMarketValue + grandAvailable

  return {
    ...portfolio,
    accounts,
    total: {
      total_market_value: round2(grandMarketValue),
      total_cost: round2(grandCost),
      total_pnl: round2(grandPnl),
      total_pnl_pct: round2(grandPnlPct),
      total_daily_pnl: round2(grandDailyPnl),
      available_funds: round2(grandAvailable),
      total_assets: round2(grandTotalAssets),
    },
  }
}

export default function StocksPage() {
  const [stocks, setStocks] = useState<Stock[]>([])
  const [accounts, setAccounts] = useState<Account[]>([])
  const [agents, setAgents] = useState<AgentConfig[]>([])
  const [services, setServices] = useState<AIService[]>([])
  const [channels, setChannels] = useState<NotifyChannel[]>([])
  const [loading, setLoading] = useState(true)

  // Portfolio
  const [portfolio, setPortfolio] = useState<PortfolioSummary | null>(null)
  const [portfolioRaw, setPortfolioRaw] = useState<PortfolioSummary | null>(null)
  const [portfolioLoading, setPortfolioLoading] = useState(false)
  const [expandedAccounts, setExpandedAccounts] = useState<Set<number>>(new Set())

  // Quotes for all stocks (used in stock list)
  const [quotes, setQuotes] = useState<Record<string, { current_price: number | null; change_pct: number | null }>>({})
  const [quotesLoading, setQuotesLoading] = useState(false)
  // Keyed by `${market}:${symbol}` to avoid cross-market symbol collisions
  const [klineSummaries, setKlineSummaries] = useState<Record<string, KlineSummary>>({})

  // Auto-refresh (持久化到 localStorage)
  const [autoRefresh, setAutoRefresh] = useLocalStorage('panwatch_stocks_autoRefresh', false)
  const [refreshInterval, setRefreshInterval] = useLocalStorage('panwatch_stocks_refreshInterval', 30)
  const [lastRefreshTime, setLastRefreshTime] = useState<Date | null>(null)
  const refreshTimerRef = useRef<ReturnType<typeof setInterval>>()

  // Alerts / Scanning
  const [scanning, setScanning] = useState(false)

  type ViewTab = 'positions' | 'watchlist' | 'plans'
  const [savedView, setSavedView] = useLocalStorage<string>('panwatch_stocks_viewTab', 'positions')
  const [searchParams, setSearchParams] = useSearchParams()
  const resolveView = (value: string | null): ViewTab => value === 'watchlist' ? 'watchlist' : ['plans', 'dca', 'goals'].includes(value || '') ? 'plans' : 'positions'
  const viewTab = resolveView(searchParams.get('view') || savedView)
  const setViewTab = (view: ViewTab) => {
    setSavedView(view)
    setSearchParams(previous => { const next = new URLSearchParams(previous); next.set('view', view); return next })
  }
  const [accountFilter, setAccountFilter] = useState<number | null>(null)
  const [recordPickerOpen, setRecordPickerOpen] = useState(false)
  const [privateMode, setPrivateMode] = useLocalStorage('panwatch_wealth_private', false)
  const [workspacePanel, setWorkspacePanel] = useState<'analysis' | 'records' | 'accounts' | null>(null)
  const [panelAccountId, setPanelAccountId] = useState<number | null>(null)
  const [duePlans, setDuePlans] = useState<WealthOverview['dca_due']>([])
  const [dueError, setDueError] = useState(false)
  const [dueRetry, setDueRetry] = useState(0)
  const [focusPlanId, setFocusPlanId] = useState<number | null>(null)
  const [tasksExpanded, setTasksExpanded] = useState(false)
  const [positionSearch, setPositionSearch] = useState('')
  const [watchSearch, setWatchSearch] = useState('')
  const [detailedColumns, setDetailedColumns] = useLocalStorage('panwatch_wealth_detailed_columns', false)
  const panelFallbackRef = useRef<HTMLButtonElement>(null)
  const workspaceRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const normalized = resolveView(savedView)
    if (savedView !== normalized) setSavedView(normalized)
  }, [savedView])


  // 股票 AI 建议（来自盘中监控 API）
  const [suggestions] = useState<Record<string, StockSuggestionData>>({})
  // 建议池建议（来自 /suggestions API）
  const [poolSuggestions, setPoolSuggestions] = useState<Record<string, PoolSuggestion>>({})
  const [poolSuggestionsLoading, setPoolSuggestionsLoading] = useState(false)

  // News Dialog
  const [newsDialogOpen, setNewsDialogOpen] = useState(false)
  const [newsDialogSymbol, setNewsDialogSymbol] = useState<string>('')  // 空=全部, 否则=指定股票
  const [news, setNews] = useState<NewsItem[]>([])
  const [newsLoading, setNewsLoading] = useState(false)

  // Kline Dialog
  const [klineDialogOpen, setKlineDialogOpen] = useState(false)
  const [klineDialogSymbol, setKlineDialogSymbol] = useState('')
  const [klineDialogMarket, setKlineDialogMarket] = useState('CN')
  const [klineDialogName, setKlineDialogName] = useState<string | undefined>(undefined)
  const [klineDialogHasPosition, setKlineDialogHasPosition] = useState<boolean>(false)
  const [klineDialogInitialSummary, setKlineDialogInitialSummary] = useState<KlineSummary | null>(null)
  const [insightOpen, setInsightOpen] = useState(false)
  const [insightSymbol, setInsightSymbol] = useState('')
  const [insightMarket, setInsightMarket] = useState('CN')
  const [insightName, setInsightName] = useState<string | undefined>(undefined)
  const [insightHasPosition, setInsightHasPosition] = useState(false)

  // Market status
  const [marketStatus, setMarketStatus] = useState<MarketStatus[]>([])
  // Guard to prevent overlapping K线刷新任务导致实际并发超限
  const klineRefreshInFlight = useRef<Promise<void> | null>(null)
  const initialLoadPromiseRef = useRef<Promise<void> | null>(null)
  const configLoadPromiseRef = useRef<Promise<void> | null>(null)
  const configLoadedRef = useRef(false)

  // Stock form
  const [showStockForm, setShowStockForm] = useState(false)
  const [stockForm, setStockForm] = useState<StockForm>(emptyStockForm)
  const [searchQuery, setSearchQuery] = useState('')
  const [searchMarket, setSearchMarket] = useState('')  // 搜索市场筛选
  const [searchResults, setSearchResults] = useState<SearchResult[]>([])
  const [showDropdown, setShowDropdown] = useState(false)
  const [searching, setSearching] = useState(false)
  const [refreshingStockList, setRefreshingStockList] = useState(false)

  // Account form
  const [accountDialogOpen, setAccountDialogOpen] = useState(false)
  const [accountForm, setAccountForm] = useState<AccountForm>(emptyAccountForm)
  const [editAccountId, setEditAccountId] = useState<number | null>(null)

  // 策略状态:对当前持仓批量检查(失败静默)
  useEffect(() => {
    const symbols = Array.from(
      new Set(
        (portfolio?.accounts || [])
          .flatMap((a) => (a.positions || []).map((p) => p.symbol))
          .filter(Boolean)
      )
    ) as string[]
    if (!symbols.length) {
      setStrategyMap({})
      return
    }
    let alive = true
    strategyApi
      .board(symbols)
      .then((r) => {
        if (!alive) return
        const map: Record<string, string> = {}
        for (const item of r.items || []) {
          if (['符合买入', '符合卖出', '接近买入', '禁止买入'].includes(item.verdict)) {
            map[item.symbol] = item.verdict
          }
        }
        setStrategyMap(map)
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [portfolio])

  // Position form
  const [positionDialogOpen, setPositionDialogOpen] = useState(false)
  const [positionForm, setPositionForm] = useState<PositionForm>({ account_id: 0, stock_id: 0, cost_price: '', quantity: '', invested_amount: '', trading_style: '', stock_symbol: '', stock_name: '', stock_market: 'CN', record_trade: true, deduct_cash: false })
  const [editPositionId, setEditPositionId] = useState<number | null>(null)
  // 持仓行的策略状态（符合买入/卖出/接近买入），批量检查一次
  const [strategyMap, setStrategyMap] = useState<Record<string, string>>({})
  const [positionDialogAccountId, setPositionDialogAccountId] = useState<number | null>(null)
  const [positionSearchQuery, setPositionSearchQuery] = useState('')
  const [positionSearchMarket, setPositionSearchMarket] = useState('')  // 搜索市场筛选
  const [positionSearchResults, setPositionSearchResults] = useState<SearchResult[]>([])
  const [positionSearching, setPositionSearching] = useState(false)
  const [showPositionDropdown, setShowPositionDropdown] = useState(false)
  const positionSearchTimer = useRef<ReturnType<typeof setTimeout>>()
  const positionDropdownRef = useRef<HTMLDivElement>(null)

  // 交易 / 风控弹窗（理财化）
  const [tradeTarget, setTradeTarget] = useState<{
    account: AccountSummary
    pos: Position
    action: 'buy' | 'sell'
  } | null>(null)
  const [tradeForm, setTradeForm] = useState({
    quantity: '100',
    price: '',
    date: new Date().toISOString().slice(0, 10),
    note: '',
    fee: '',
    cost: '',
  })
  const [tradeBusy, setTradeBusy] = useState(false)
  // 交易前体检：结论为「不建议」时需要勾选确认才能继续记录
  const [tradeGate, setTradeGate] = useState<StrategyGate | null>(null)
  const [tradeGateAck, setTradeGateAck] = useState(false)
  const [positionGate, setPositionGate] = useState<StrategyGate | null>(null)
  const [positionGateAck, setPositionGateAck] = useState(false)
  const [riskTarget, setRiskTarget] = useState<{
    account: AccountSummary
    pos: Position
  } | null>(null)
  const [riskForm, setRiskForm] = useState({ stop: '', take: '' })
  const [riskBusy, setRiskBusy] = useState(false)
  const [riskHint, setRiskHint] = useState('')
  const [wealthRefreshKey, setWealthRefreshKey] = useState(0)
  useEffect(() => {
    if (viewTab !== 'positions') return
    let cancelled = false
    setDueError(false)
    portfolioApi.dcaDue(1).then(rows => {
      if (!cancelled) setDuePlans(rows)
    }).catch(() => {
      if (!cancelled) { setDuePlans([]); setDueError(true) }
    })
    return () => { cancelled = true }
  }, [viewTab, wealthRefreshKey, dueRetry])


  // Agent dialog
  const [agentDialogStock, setAgentDialogStock] = useState<Stock | null>(null)

  // 深度分析(TradingAgents)弹窗
  const [deepAnalysisTarget, setDeepAnalysisTarget] = useState<{
    stockId: number
    symbol: string
    name: string
  } | null>(null)
  const openDeepAnalysis = useCallback((stockId: number, symbol: string, name: string) => {
    setDeepAnalysisTarget({ stockId, symbol, name })
  }, [])
  const [triggeringAgent, setTriggeringAgent] = useState<string | null>(null)
  const [schedulePreviewCache, setSchedulePreviewCache] = useState<Record<string, SchedulePreview | { error: string }>>({})
  const [schedulePreviewLoading, setSchedulePreviewLoading] = useState<Record<string, boolean>>({})
  // 运行中的单只股票 Agent（按股票标记具体 Agent 名称）
  const [runningAgents, setRunningAgents] = useState<Record<number, string | null>>({})
  const [agentResultDialog, setAgentResultDialog] = useState<{ title: string; content: string; should_alert: boolean; notified: boolean } | null>(null)

  // Stock list filter
  const [stockListFilter, setStockListFilter] = useState('')  // '' = 全部, 'CN' = A股, 'HK' = 港股, 'US' = 美股
  const [watchGroupFilter, setWatchGroupFilter] = useState('__all__')  // 自选分组筛选
  const [watchlistOnlyAlerts, setWatchlistOnlyAlerts] = useLocalStorage<boolean>('panwatch_watchlist_only_alerts', false)

  // Remove watchlist modal
  const [removeWatchStock, setRemoveWatchStock] = useState<Stock | null>(null)
  const [removingWatchStock, setRemovingWatchStock] = useState(false)
  const [draggingWatchStockId, setDraggingWatchStockId] = useState<number | null>(null)
  const [draggingPositionId, setDraggingPositionId] = useState<number | null>(null)
  const [draggingPositionAccountId, setDraggingPositionAccountId] = useState<number | null>(null)
  const watchDragSnapshotRef = useRef<Stock[] | null>(null)
  const positionDragSnapshotRef = useRef<PortfolioSummary | null>(null)

  const { toast } = useToast()

  const moveById = <T extends { id: number }>(list: T[], fromId: number, toId: number): T[] => {
    const fromIdx = list.findIndex(x => x.id === fromId)
    const toIdx = list.findIndex(x => x.id === toId)
    if (fromIdx < 0 || toIdx < 0 || fromIdx === toIdx) return list
    const next = [...list]
    const [moved] = next.splice(fromIdx, 1)
    next.splice(toIdx, 0, moved)
    return next
  }

  const persistWatchlistOrder = useCallback(async (ordered: Stock[]) => {
    const payload = ordered.map((s, idx) => ({ id: s.id, sort_order: idx + 1 }))
    await fetchAPI('/stocks/reorder', {
      method: 'PUT',
      body: JSON.stringify({ items: payload }),
    })
  }, [])

  const previewWatchlistReorder = useCallback((fromId: number, toId: number) => {
    if (fromId === toId) return
    setStocks(prev => {
      const ordered = [...prev].sort((a, b) => Number(a.sort_order || 0) - Number(b.sort_order || 0) || a.id - b.id)
      const moved = moveById(ordered, fromId, toId)
      return moved.map((s, idx) => ({ ...s, sort_order: idx + 1 }))
    })
  }, [])

  const commitWatchlistReorder = useCallback(async () => {
    const current = stocks
    if (!current || current.length === 0) return
    try {
      await persistWatchlistOrder(current)
    } catch (e) {
      if (watchDragSnapshotRef.current) setStocks(watchDragSnapshotRef.current)
      toast(e instanceof Error ? e.message : '保存关注排序失败', 'error')
    }
  }, [persistWatchlistOrder, stocks, toast])

  const persistPositionOrder = useCallback(async (ordered: Position[]) => {
    const payload = ordered.map((p, idx) => ({ id: p.id, sort_order: idx + 1 }))
    await fetchAPI('/positions/reorder/batch', {
      method: 'PUT',
      body: JSON.stringify({ items: payload }),
    })
  }, [])

  const previewPositionReorder = useCallback((accountId: number, fromId: number, toId: number) => {
    if (fromId === toId) return
    setPortfolioRaw(prev => {
      if (!prev) return prev
      const accountsNext = prev.accounts.map(acc => {
        if (acc.id !== accountId) return acc
        const moved = moveById(acc.positions || [], fromId, toId).map((p, idx) => ({ ...p, sort_order: idx + 1 }))
        return { ...acc, positions: moved }
      })
      return { ...prev, accounts: accountsNext }
    })
  }, [])

  const commitPositionReorder = useCallback(async (accountId: number) => {
    const acc = portfolioRaw?.accounts?.find(a => a.id === accountId)
    const ordered = acc?.positions || []
    if (!ordered.length) return
    try {
      await persistPositionOrder(ordered)
    } catch (e) {
      if (positionDragSnapshotRef.current) setPortfolioRaw(positionDragSnapshotRef.current)
      toast(e instanceof Error ? e.message : '保存持仓排序失败', 'error')
    }
  }, [persistPositionOrder, portfolioRaw, toast])

  const isSuppressCardClick = () => {
    try {
      const until = (window as any).__panwatch_suppress_card_click_until
      return typeof until === 'number' && Date.now() < until
    } catch {
      return false
    }
  }
  const searchTimer = useRef<ReturnType<typeof setTimeout>>()
  const dropdownRef = useRef<HTMLDivElement>(null)

  const buildQuoteItems = useCallback((): QuoteRequestItem[] => {
    return buildQuoteItemsFrom(stocks, portfolioRaw)
  }, [stocks, portfolioRaw])

  const requestQuotes = useCallback(async (items: QuoteRequestItem[], signal?: AbortSignal): Promise<QuoteResponse[]> => {
    if (items.length === 0) return []
    try {
      return await fetchAPI<QuoteResponse[]>('/quotes/batch', {
        method: 'POST',
        body: JSON.stringify({ items }),
        signal,
      })
    } catch (e) {
      console.warn('刷新行情失败:', e)
      return []
    }
  }, [])

  const requestSuggestions = useCallback(async (items: QuoteRequestItem[], signal?: AbortSignal): Promise<Record<string, PoolSuggestion>> => {
    if (items.length === 0) return {}
    try {
      const params = new URLSearchParams({
        include_expired: 'true',
        stock_keys: buildPortfolioStockKeys(items),
      })
      return await fetchAPI<Record<string, PoolSuggestion>>(`/suggestions?${params.toString()}`, { signal })
    } catch (e) {
      console.warn('加载建议池失败:', e)
      return {}
    }
  }, [])

  const requestPriceAlerts = useCallback(async (items: QuoteRequestItem[], signal?: AbortSignal): Promise<PriceAlertRuleSummary[]> => {
    if (items.length === 0) return []
    try {
      return await fetchAPI<PriceAlertRuleSummary[]>('/price-alerts', { signal })
    } catch (e) {
      console.warn('加载提醒摘要失败:', e)
      return []
    }
  }, [])

  const requestKlineSummaries = useCallback(async (items: QuoteRequestItem[], signal?: AbortSignal): Promise<Record<string, KlineSummary>> => {
    if (items.length === 0) return {}
    try {
      const data = await klinesApi.summaryBatch(items, signal)
      const map: Record<string, KlineSummary> = {}
      for (const item of data || []) {
        if (item && item.summary && !('error' in item.summary)) {
          map[`${item.market}:${item.symbol}`] = item.summary as unknown as KlineSummary
        }
      }
      return map
    } catch {
      // 批量请求失败时保留旧摘要，避免技术徽章整体闪断。
      return {}
    }
  }, [])

  const refreshQuotes = useCallback(async () => {
    const items = buildQuoteItems()
    if (items.length === 0) return

    setQuotesLoading(true)
    try {
      const data = await requestQuotes(items)
      if (data.length > 0) {
        setQuotes(toQuoteMap(data))
        setLastRefreshTime(new Date())
      }
    } finally {
      setQuotesLoading(false)
    }
  }, [buildQuoteItems, requestQuotes])

  useEffect(() => {
    if (!portfolioRaw) return
    setPortfolio(mergePortfolioQuotes(portfolioRaw, quotes))
  }, [portfolioRaw, quotes])

  // 刷新 K 线摘要（批量接口）；并防止重入
  const refreshKlines = useCallback(async () => {
    if (klineRefreshInFlight.current) return klineRefreshInFlight.current
    const run = (async () => {
      const items = buildQuoteItems()
      if (items.length === 0) return
      const map = await requestKlineSummaries(items)
      // 增量合并：本轮单只失败时保留旧值，避免技术徽章闪断/消失
      setKlineSummaries(prev => ({ ...prev, ...map }))
    })()
    klineRefreshInFlight.current = run
    try { await run } finally { klineRefreshInFlight.current = null }
  }, [buildQuoteItems, requestKlineSummaries])

  // 从建议池加载建议（包含历史建议和多来源建议）
  const loadPoolSuggestions = useCallback(async (itemsOverride?: QuoteRequestItem[]) => {
    setPoolSuggestionsLoading(true)
    try {
      const data = await requestSuggestions(itemsOverride || buildQuoteItems())
      setPoolSuggestions(data)
    } finally {
      setPoolSuggestionsLoading(false)
    }
  }, [buildQuoteItems, requestSuggestions])

  const loadConfigAsync = useCallback((signal?: AbortSignal): Promise<void> => {
    if (configLoadedRef.current) return Promise.resolve()
    if (configLoadPromiseRef.current) return configLoadPromiseRef.current

    const run = (async () => {
      try {
        const [agentData, servicesData, channelsData] = await Promise.all([
          fetchAPI<AgentConfig[]>('/agents', { signal }),
          fetchAPI<AIService[]>('/providers/services', { signal }),
          fetchAPI<NotifyChannel[]>('/channels', { signal }),
        ])
        setAgents(agentData)
        setServices(servicesData)
        setChannels(channelsData)
        configLoadedRef.current = true
      } catch (e) {
        console.warn('加载配置数据失败:', e)
      }
    })()
    configLoadPromiseRef.current = run
    void run.finally(() => {
      configLoadPromiseRef.current = null
    })
    return run
  }, [])

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const stockData = await fetchAPI<Stock[]>('/stocks', { signal })
      setStocks(stockData)
    } catch (e) {
      if (!signal?.aborted) console.error(e)
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [])

  const loadPortfolio = useCallback(async () => {
    setPortfolioLoading(true)
    try {
      const portfolioData = await fetchAPI<PortfolioSummary>('/portfolio/summary?include_quotes=false')
      const items = buildQuoteItemsFrom(stocks, portfolioData)
      const [quoteRows, klineMap] = await Promise.all([
        requestQuotes(items),
        requestKlineSummaries(items),
      ])
      const quoteMap = toQuoteMap(quoteRows)
      setPortfolioRaw(portfolioData)
      setAccounts(portfolioData.accounts.map(account => ({
        id: account.id,
        name: account.name,
        available_funds: account.available_funds,
        enabled: true,
      })))
      setExpandedAccounts(new Set(portfolioData.accounts.map(account => account.id)))
      setQuotes(prev => ({ ...prev, ...quoteMap }))
      setKlineSummaries(prev => ({ ...prev, ...klineMap }))
      setPortfolio(mergePortfolioQuotes(portfolioData, { ...quotes, ...quoteMap }))
    } catch (e) {
      console.error(e)
    } finally {
      setPortfolioLoading(false)
    }
  }, [requestKlineSummaries, requestQuotes, quotes, stocks])

  const openTradeDialog = (account: AccountSummary, pos: Position, action: 'buy' | 'sell') => {
    setTradeTarget({ account, pos, action })
    setTradeGate(null)
    setTradeGateAck(false)
    setTradeForm({
      quantity: action === 'sell' ? String(Math.min(pos.quantity, 100)) : '100',
      price: pos.current_price != null ? pos.current_price.toFixed(2) : '',
      date: new Date().toISOString().slice(0, 10),
      note: '',
      fee: '',
      cost: '',
    })
  }

  const tradeFeePreview = useMemo(() => {
    if (!tradeTarget) return null
    const qty = Number(tradeForm.quantity) || 0
    const price = Number(tradeForm.price) || 0
    const gross = qty * price
    const p = {
      commission_rate: 0.00025,
      min_commission: 5,
      stamp_duty_rate: 0.0005,
      transfer_fee_rate: 0.00001,
      ...(tradeTarget.account.fee_policy || {}),
    }
    const commission = gross > 0 ? Math.max(gross * Number(p.commission_rate), Number(p.min_commission)) : 0
    const stamp = tradeTarget.action === 'sell' ? gross * Number(p.stamp_duty_rate) : 0
    const transfer = gross * Number(p.transfer_fee_rate)
    const manualFee = tradeForm.fee !== '' ? Number(tradeForm.fee) : null
    return {
      gross,
      commission,
      stamp,
      transfer,
      manualFee,
      total: manualFee != null && Number.isFinite(manualFee) ? manualFee : commission + stamp + transfer,
    }
  }, [tradeTarget, tradeForm.quantity, tradeForm.price, tradeForm.fee])

  const submitTrade = async () => {
    if (!tradeTarget) return
    const quantity = Number(tradeForm.quantity)
    const price = Number(tradeForm.price)
    if (!Number.isFinite(quantity) || quantity <= 0 || !Number.isFinite(price) || price <= 0) {
      toast('请填写有效的数量和价格', 'error')
      return
    }
    if (isGateBlocked(tradeGate) && !tradeGateAck) {
      toast('策略体检不建议这笔交易，勾选「已了解风险」后再记录', 'error')
      return
    }
    setTradeBusy(true)
    try {
      const res = await portfolioApi.createTrade({
        account_id: tradeTarget.account.id,
        stock_id: tradeTarget.pos.stock_id,
        action: tradeTarget.action,
        quantity,
        price,
        trade_date: tradeForm.date || undefined,
        note: tradeForm.note,
        fee_override: tradeForm.fee !== '' ? Number(tradeForm.fee) : undefined,
        cost_override: tradeForm.cost !== '' ? Number(tradeForm.cost) : undefined,
      })
      toast(
        `${tradeTarget.action === 'buy' ? '加仓' : '减仓'}已记录，费用 ¥${res.fees.total.toFixed(2)}${res.warning ? '（资金不足）' : ''}`,
        'success',
      )
      setTradeTarget(null)
      await loadPortfolio()
      setWealthRefreshKey((k) => k + 1)
    } catch (e) {
      toast(e instanceof Error ? e.message : '交易记录失败', 'error')
    } finally {
      setTradeBusy(false)
    }
  }

  const openRiskDialog = (account: AccountSummary, pos: Position) => {
    setRiskTarget({ account, pos })
    setRiskForm({
      stop: pos.stop_loss_price != null ? String(pos.stop_loss_price) : '',
      take: pos.take_profit_price != null ? String(pos.take_profit_price) : '',
    })
    setRiskHint('')
  }

  const submitRisk = async () => {
    if (!riskTarget) return
    setRiskBusy(true)
    try {
      const res = await portfolioApi.updatePositionRisk(riskTarget.pos.id, {
        stop_loss_price: riskForm.stop ? Number(riskForm.stop) : null,
        take_profit_price: riskForm.take ? Number(riskForm.take) : null,
      })
      setRiskHint(res.hint)
      await loadPortfolio()
      setWealthRefreshKey((k) => k + 1)
    } catch (e) {
      toast(e instanceof Error ? e.message : '保存失败', 'error')
    } finally {
      setRiskBusy(false)
    }
  }

  const loadInitialData = useCallback((signal: AbortSignal): Promise<void> => {
    if (initialLoadPromiseRef.current) return initialLoadPromiseRef.current
    setLoading(true)
    setPortfolioLoading(true)

    const run = (async () => {
      const coreData = await loadPortfolioPageCoreData({
        loadStocks: requestSignal => fetchAPI<Stock[]>('/stocks', { signal: requestSignal }),
        loadPortfolio: requestSignal => fetchAPI<PortfolioSummary>('/portfolio/summary?include_quotes=false', { signal: requestSignal }),
      }, signal)

      if (signal.aborted) return

      const quoteData = await loadPortfolioPageQuoteData({
        buildQuoteItems: buildQuoteItemsFrom,
        loadQuotes: requestQuotes,
      }, coreData.stocks, coreData.portfolio, signal)

      if (signal.aborted) return

      const quoteMap = toQuoteMap(quoteData.quotes)
      setStocks(coreData.stocks)
      setPortfolioRaw(coreData.portfolio)
      setQuotes(quoteMap)
      setKlineSummaries({})
      setPoolSuggestions({})
      setPortfolio(mergePortfolioQuotes(coreData.portfolio, quoteMap))
      const nextAccounts = coreData.portfolio.accounts.map(account => ({
        id: account.id,
        name: account.name,
        available_funds: account.available_funds,
        enabled: true,
      }))
      setAccounts(nextAccounts)
      setExpandedAccounts(new Set(nextAccounts.map(account => account.id)))
      if (quoteData.quotes.length > 0) setLastRefreshTime(new Date())
      setLoading(false)
      setPortfolioLoading(false)

      void loadPortfolioPageBackgroundData({
        loadMarketStatus: async requestSignal => {
          try {
            return await fetchAPI<MarketStatus[]>('/stocks/markets/status', { signal: requestSignal })
          } catch (e) {
            console.warn('获取市场状态失败:', e)
            return []
          }
        },
        buildQuoteItems: buildQuoteItemsFrom,
        loadSuggestions: requestSuggestions,
        loadPriceAlerts: requestPriceAlerts,
        loadKlines: requestKlineSummaries,
      }, coreData.stocks, coreData.portfolio, signal).then(data => {
        if (signal.aborted) return
        setMarketStatus(data.marketStatus)
        setKlineSummaries(data.klines)
        setPoolSuggestions(data.suggestions)
      }).catch(error => {
        if (!signal.aborted) console.warn('加载持仓页后台数据失败:', error)
      })
    })().catch(error => {
      if (!signal.aborted) console.error('加载持仓页面数据失败:', error)
    }).finally(() => {
      initialLoadPromiseRef.current = null
    })

    initialLoadPromiseRef.current = run
    return run
  }, [requestKlineSummaries, requestPriceAlerts, requestQuotes, requestSuggestions])

  // Load news for specific stock or all watchlist
  const loadNews = useCallback(async (stockName?: string) => {
    setNewsLoading(true)
    try {
      const params = new URLSearchParams({ hours: '168', limit: '50' })  // 7天
      if (stockName) {
        // 直接传递股票名称，比代码更稳定
        params.set('names', stockName)
      }
      const newsData = await fetchAPI<NewsItem[]>(`/news?${params}`)
      setNews(newsData)
    } catch (e) {
      console.error('加载新闻失败:', e)
    } finally {
      setNewsLoading(false)
    }
  }, [])

  const openKlineDialog = useCallback((symbol: string, market: string, name?: string, hasPosition?: boolean) => {
    setKlineDialogSymbol(symbol)
    setKlineDialogMarket(market || 'CN')
    setKlineDialogName(name)
    setKlineDialogHasPosition(!!hasPosition)
    const m = market || 'CN'
    setKlineDialogInitialSummary(klineSummaries[`${m}:${symbol}`] || null)
    setKlineDialogOpen(true)
  }, [klineSummaries])

  // Open news dialog - pass stock name for more stable search
  const openNewsDialog = useCallback((stockName?: string) => {
    setNewsDialogSymbol(stockName || '')  // 存储名称用于 UI 显示
    setNewsDialogOpen(true)
    loadNews(stockName)
  }, [loadNews])

  const openStockDetail = useCallback((stockSymbol: string, stockMarket: string, stockName?: string, hasPosition?: boolean) => {
    setInsightSymbol(stockSymbol)
    setInsightMarket(stockMarket || 'CN')
    setInsightName(stockName)
    setInsightHasPosition(!!hasPosition)
    setInsightOpen(true)
  }, [])

  const formatPreviewTime = (iso: string, tz?: string): string => {
    try {
      const d = new Date(iso)
      if (isNaN(d.getTime())) return iso
      return d.toLocaleString('zh-CN', {
        timeZone: tz || undefined,
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      })
    } catch {
      return iso
    }
  }

  const effectiveSchedule = (agent: AgentConfig, stockAgent?: StockAgentInfo | null): string => {
    const local = (stockAgent?.schedule || '').trim()
    if (local) return local
    return (agent.schedule || '').trim()
  }

  // Refresh quotes only (decoupled from portfolio and scans)
  const handleRefresh = useCallback(async () => {
    await Promise.all([
      refreshQuotes(),
      loadPoolSuggestions(),
      refreshKlines(),
    ])
  }, [loadPoolSuggestions, refreshKlines, refreshQuotes])

  useEffect(() => {
    const controller = new AbortController()
    void loadInitialData(controller.signal)
    return () => {
      controller.abort()
      initialLoadPromiseRef.current = null
    }
  }, [loadInitialData])

  useEffect(() => {
    if (agentDialogStock) void loadConfigAsync()
  }, [agentDialogStock, loadConfigAsync])

  // Agent 配置弹窗：预览未来触发时间（用于自检工作日/周末语义）
  useEffect(() => {
    if (!agentDialogStock) return
    if (!agents || agents.length === 0) return

    const stockAgentMap = new Map((agentDialogStock.agents || []).map(a => [a.agent_name, a]))
    const schedules = new Set<string>()
    for (const agent of agents) {
      if (agent.execution_mode === 'batch') continue
      const sa = stockAgentMap.get(agent.name)
      if (!sa) continue
      const eff = effectiveSchedule(agent, sa)
      if (eff) schedules.add(eff)
    }

    const toFetch = Array.from(schedules).filter(s => !schedulePreviewCache[s] && !schedulePreviewLoading[s])
    if (toFetch.length === 0) return

    let cancelled = false
    ;(async () => {
      // Mark loading
      setSchedulePreviewLoading(prev => {
        const next = { ...prev }
        for (const s of toFetch) next[s] = true
        return next
      })
      try {
        const pairs = await Promise.all(toFetch.map(async s => {
          try {
            const p = await fetchAPI<SchedulePreview>(`/agents/schedule/preview?schedule=${encodeURIComponent(s)}&count=5`)
            return [s, p] as const
          } catch (e) {
            const msg = e instanceof Error ? e.message : '预览失败'
            return [s, { error: msg }] as const
          }
        }))
        if (cancelled) return
        setSchedulePreviewCache(prev => ({ ...prev, ...Object.fromEntries(pairs) }))
      } finally {
        if (cancelled) return
        setSchedulePreviewLoading(prev => {
          const next = { ...prev }
          for (const s of toFetch) next[s] = false
          return next
        })
      }
    })()

    return () => { cancelled = true }
  }, [agentDialogStock, agents, schedulePreviewCache, schedulePreviewLoading])

  // 触发扫描：调用盘中监控扫描，并刷新建议池
  const scanAndReload = useCallback(async () => {
    setScanning(true)
    try {
      const url = '/agents/intraday/scan?analyze=true'
      await fetchAPI(url, { method: 'POST' })
      await loadPoolSuggestions()
      await refreshKlines()
      setLastRefreshTime(new Date())
    } catch (e) {
      console.error('扫描失败:', e)
      toast(e instanceof Error ? e.message : '扫描失败', 'error')
    } finally {
      setScanning(false)
    }
  }, [loadPoolSuggestions, refreshKlines, toast])

  // Auto-refresh timer
  useEffect(() => {
    if (autoRefresh) {
      refreshQuotes()
      refreshTimerRef.current = setInterval(() => {
        refreshQuotes()
      }, refreshInterval * 1000)
    } else {
      // Clear interval when disabled
      if (refreshTimerRef.current) {
        clearInterval(refreshTimerRef.current)
        refreshTimerRef.current = undefined
      }
    }

    return () => {
      if (refreshTimerRef.current) {
        clearInterval(refreshTimerRef.current)
      }
    }
  }, [autoRefresh, refreshInterval, refreshQuotes])

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setShowDropdown(false)
      }
      if (positionDropdownRef.current && !positionDropdownRef.current.contains(e.target as Node)) {
        setShowPositionDropdown(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  // ========== Stock handlers ==========
  const doSearch = async (q: string, market: string = searchMarket) => {
    if (q.length < 1) { setSearchResults([]); setShowDropdown(false); return }
    setSearching(true)
    try {
      const marketParam = market ? `&market=${market}` : ''
      const results = await fetchAPI<SearchResult[]>(`/stocks/search?q=${encodeURIComponent(q)}${marketParam}`)
      setSearchResults(results)
      setShowDropdown(results.length > 0)
    } catch { setSearchResults([]) }
    finally { setSearching(false) }
  }

  const handleSearchInput = (value: string) => {
    setSearchQuery(value)
    clearTimeout(searchTimer.current)
    searchTimer.current = setTimeout(() => doSearch(value), 500)
  }

  const handleSearchMarketChange = (market: string) => {
    setSearchMarket(market)
    if (searchQuery) {
      doSearch(searchQuery, market)
    }
  }

  const refreshStockListCache = async () => {
    setRefreshingStockList(true)
    try {
      const result = await fetchAPI<{ count: number }>('/stocks/refresh-list', { method: 'POST' })
      toast(`已刷新股票列表，共 ${result.count} 只`, 'success')
      if (searchQuery) {
        doSearch(searchQuery)
      }
    } catch (e) {
      toast('刷新失败', 'error')
    } finally {
      setRefreshingStockList(false)
    }
  }

  const selectStock = (item: SearchResult) => {
    setStockForm({ symbol: item.symbol, name: item.name, market: item.market })
    setSearchQuery(`${item.symbol} ${item.name}`)
    setShowDropdown(false)
  }

  const handleStockSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      await stocksApi.create(stockForm)
      setStockForm(emptyStockForm)
      setSearchQuery('')
      setShowStockForm(false)
      load()
      toast('股票已添加', 'success')
    } catch (e) {
      toast(e instanceof Error ? e.message : '添加股票失败', 'error')
    }
  }

  const hasAnyPositionForStockId = (id: number): boolean => {
    return (portfolio?.accounts || []).some(acc => (acc.positions || []).some(p => p.stock_id === id))
  }

  const removeFromWatchlist = async (stock: Stock) => {
    if (hasAnyPositionForStockId(stock.id)) {
      toast('该股票存在持仓，请先删除持仓后再删除股票', 'error')
      return
    }

    setRemovingWatchStock(true)
    try {
      await stocksApi.remove(stock.id)
      toast('股票已删除', 'success')
      setRemoveWatchStock(null)
      load()
      // 价格提醒/关联配置会随股票删除，刷新一次避免 UI 残留。
      loadPortfolio()
    } catch (e) {
      toast(e instanceof Error ? e.message : '删除失败', 'error')
    } finally {
      setRemovingWatchStock(false)
    }
  }

  // ========== Account handlers ==========
  const openAccountDialog = (account?: Account) => {
    if (account) {
      setAccountForm({ name: account.name, available_funds: account.available_funds.toString() })
      setEditAccountId(account.id)
    } else {
      setAccountForm(emptyAccountForm)
      setEditAccountId(null)
    }
    setAccountDialogOpen(true)
  }

  const handleAccountSubmit = async () => {
    try {
      const payload = {
        name: accountForm.name,
        available_funds: parseFloat(accountForm.available_funds) || 0,
      }
      if (editAccountId) {
        await fetchAPI(`/accounts/${editAccountId}`, { method: 'PUT', body: JSON.stringify(payload) })
      } else {
        await fetchAPI('/accounts', { method: 'POST', body: JSON.stringify(payload) })
      }
      setAccountDialogOpen(false)
      load()
      loadPortfolio()
      toast(editAccountId ? '账户已更新' : '账户已创建', 'success')
    } catch (e) {
      toast(e instanceof Error ? e.message : '保存账户失败', 'error')
    }
  }

  const handleDeleteAccount = async (id: number) => {
    if (!confirm('确定删除该账户？这将同时删除该账户的所有持仓记录')) return
    try {
      await fetchAPI(`/accounts/${id}`, { method: 'DELETE' })
      load()
      loadPortfolio()
      toast('账户已删除', 'success')
    } catch (e) {
      toast(e instanceof Error ? e.message : '删除账户失败', 'error')
    }
  }

  // ========== Position handlers ==========
  const openPositionDialog = (accountId: number, position?: Position) => {
    setPositionDialogAccountId(accountId)
    setPositionGate(null)
    setPositionGateAck(false)
    setPositionSearchQuery('')
    setPositionSearchResults([])
    setShowPositionDropdown(false)
    if (position) {
      setPositionForm({
        account_id: accountId,
        stock_id: position.stock_id,
        cost_price: position.cost_price.toString(),
        quantity: position.quantity.toString(),
        invested_amount: position.invested_amount?.toString() || '',
        trading_style: position.trading_style || '',
        stock_symbol: position.symbol,
        stock_name: position.name,
        stock_market: position.market,
        record_trade: false,
        deduct_cash: false,
      })
      setEditPositionId(position.id)
    } else {
      setPositionForm({
        account_id: accountId,
        stock_id: 0,
        cost_price: '',
        quantity: '',
        invested_amount: '',
        trading_style: '',
        stock_symbol: '',
        stock_name: '',
        stock_market: 'CN',
        record_trade: true,
        deduct_cash: false,
      })
      setEditPositionId(null)
    }
    setPositionDialogOpen(true)
  }

  const doPositionSearch = async (q: string, market: string = positionSearchMarket) => {
    if (q.length < 1) { setPositionSearchResults([]); setShowPositionDropdown(false); return }
    setPositionSearching(true)
    try {
      const marketParam = market ? `&market=${market}` : ''
      const results = await fetchAPI<SearchResult[]>(`/stocks/search?q=${encodeURIComponent(q)}${marketParam}`)
      setPositionSearchResults(results)
      setShowPositionDropdown(results.length > 0)
    } catch { setPositionSearchResults([]) }
    finally { setPositionSearching(false) }
  }

  const handlePositionSearchInput = (value: string) => {
    setPositionSearchQuery(value)
    clearTimeout(positionSearchTimer.current)
    positionSearchTimer.current = setTimeout(() => doPositionSearch(value), 500)
  }

  const handlePositionSearchMarketChange = (market: string) => {
    setPositionSearchMarket(market)
    if (positionSearchQuery) {
      doPositionSearch(positionSearchQuery, market)
    }
  }

  const selectPositionStock = (item: SearchResult) => {
    // 检查是否已有此股票
    const existing = stocks.find(s => s.symbol === item.symbol && s.market === item.market)
    setPositionForm({
      ...positionForm,
      stock_id: existing?.id || 0,
      stock_symbol: item.symbol,
      stock_name: item.name,
      stock_market: item.market,
    })
    setPositionSearchQuery(`${item.symbol} ${item.name}`)
    setShowPositionDropdown(false)
  }

  const handlePositionSubmit = async () => {
    if (!editPositionId && positionForm.record_trade && isGateBlocked(positionGate) && !positionGateAck) {
      toast('策略体检不建议现在建仓，勾选「已了解风险」后再添加', 'error')
      return
    }
    try {
      let stockId = positionForm.stock_id

      // 如果是新增且股票不在自选中，先添加到自选
      if (!editPositionId && !stockId && positionForm.stock_symbol) {
        try {
          const newStock = await fetchAPI<Stock>('/stocks', {
            method: 'POST',
            body: JSON.stringify({
              symbol: positionForm.stock_symbol,
              name: positionForm.stock_name,
              market: positionForm.stock_market,
            })
          })
          stockId = newStock.id
          load() // 刷新股票列表
        } catch {
          // 股票可能已存在，尝试获取（兼容并发创建/历史数据）。
          try {
            const existingStocks = await fetchAPI<Stock[]>('/stocks')
            const existing = existingStocks.find(s => s.symbol === positionForm.stock_symbol && s.market === positionForm.stock_market)
            if (existing) {
              stockId = existing.id
            } else {
              toast('添加股票失败', 'error')
              return
            }
          } catch (e) {
            toast(e instanceof Error ? e.message : '添加股票失败', 'error')
            return
          }
        }
      }

      const payload = {
        account_id: positionForm.account_id,
        stock_id: stockId,
        cost_price: parseFloat(positionForm.cost_price),
        quantity: parseInt(positionForm.quantity),
        invested_amount: positionForm.invested_amount ? parseFloat(positionForm.invested_amount) : null,
        trading_style: positionForm.trading_style,  // 空字符串表示清空
        record_trade: positionForm.record_trade,
        deduct_cash: positionForm.deduct_cash,
      }
      if (editPositionId) {
        await fetchAPI(`/positions/${editPositionId}`, { method: 'PUT', body: JSON.stringify(payload) })
      } else {
        await fetchAPI('/positions', { method: 'POST', body: JSON.stringify(payload) })
      }
      setPositionDialogOpen(false)
      loadPortfolio()
      setWealthRefreshKey((k) => k + 1)
      toast(editPositionId ? '持仓已更新' : '持仓已添加', 'success')
    } catch (e) {
      toast(e instanceof Error ? e.message : '保存持仓失败', 'error')
    }
  }

  const handleDeletePosition = async (id: number) => {
    if (!confirm('确定删除该持仓？')) return
    try {
      await fetchAPI(`/positions/${id}`, { method: 'DELETE' })
      loadPortfolio()
      toast('持仓已删除', 'success')
    } catch (e) {
      toast(e instanceof Error ? e.message : '删除持仓失败', 'error')
    }
  }

  // ========== Agent handlers ==========
  const toggleAgent = async (stock: Stock, agentName: string) => {
    try {
      const current = stock.agents || []
      const isAssigned = current.some(a => a.agent_name === agentName)
      const newAgents = isAssigned
        ? current.filter(a => a.agent_name !== agentName)
        : [...current, { agent_name: agentName, schedule: '', ai_model_id: null, notify_channel_ids: [] }]
      await fetchAPI(`/stocks/${stock.id}/agents`, { method: 'PUT', body: JSON.stringify({ agents: newAgents }) })
      load()
      setAgentDialogStock(prev => prev ? { ...prev, agents: newAgents } : null)
    } catch (e) {
      toast(e instanceof Error ? e.message : '更新 Agent 绑定失败', 'error')
    }
  }

  const triggerStockAgent = async (stockId: number, agentName: string) => {
    setTriggeringAgent(agentName)
    setRunningAgents(prev => ({ ...prev, [stockId]: agentName }))
    // 触发后立即关闭配置弹窗，避免多层弹窗干扰
    setAgentDialogStock(null)
    try {
      // 手动触发时跳过节流，方便测试
      const resp = await fetchAPI<{ result: AgentResult; success?: boolean; message?: string }>(
        `/stocks/${stockId}/agents/${agentName}/trigger?bypass_throttle=true`,
        { method: 'POST' }
      )
      const result = resp?.result
      if (result) {
        // 仅提示，不再弹出结果弹窗，避免干扰
        if (result.success === false) {
          toast(result.message || result.content || '执行未通过', 'info')
          return
        }
        const isSkipped = !!result.skipped || /已跳过执行|非交易时段/.test(result.content || '')
        if (isSkipped) {
          toast(result.content || '当前非交易时段，已跳过执行', 'info')
        } else {
          toast(result.should_alert ? 'AI 建议关注' : 'AI 判断无需关注', result.should_alert ? 'success' : 'info')
        }
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : '触发失败'
      if (/非交易时段|跳过执行/.test(msg)) {
        toast(msg, 'info')
      } else {
        toast(msg, 'error')
      }
    } finally {
      setTriggeringAgent(null)
      setRunningAgents(prev => ({ ...prev, [stockId]: null }))
    }
  }

  const updateStockAgentModel = async (stock: Stock, agentName: string, modelId: number | null) => {
    try {
      const newAgents = (stock.agents || []).map(a =>
        a.agent_name === agentName ? { ...a, ai_model_id: modelId } : a
      )
      await fetchAPI(`/stocks/${stock.id}/agents`, { method: 'PUT', body: JSON.stringify({ agents: newAgents }) })
      load()
      setAgentDialogStock(prev => prev ? { ...prev, agents: newAgents } : null)
    } catch (e) {
      toast(e instanceof Error ? e.message : '更新 Agent 模型失败', 'error')
    }
  }

  const toggleStockAgentChannel = async (stock: Stock, agentName: string, channelId: number) => {
    try {
      const newAgents = (stock.agents || []).map(a => {
        if (a.agent_name !== agentName) return a
        const current = a.notify_channel_ids || []
        const newIds = current.includes(channelId)
          ? current.filter(id => id !== channelId)
          : [...current, channelId]
        return { ...a, notify_channel_ids: newIds }
      })
      await fetchAPI(`/stocks/${stock.id}/agents`, { method: 'PUT', body: JSON.stringify({ agents: newAgents }) })
      load()
      setAgentDialogStock(prev => prev ? { ...prev, agents: newAgents } : null)
    } catch (e) {
      toast(e instanceof Error ? e.message : '更新 Agent 通知配置失败', 'error')
    }
  }

  const updateStockAgentSchedule = async (stock: Stock, agentName: string, schedule: string) => {
    try {
      const newAgents = (stock.agents || []).map(a =>
        a.agent_name === agentName ? { ...a, schedule } : a
      )
      await fetchAPI(`/stocks/${stock.id}/agents`, { method: 'PUT', body: JSON.stringify({ agents: newAgents }) })
      load()
      setAgentDialogStock(prev => prev ? { ...prev, agents: newAgents } : null)
    } catch (e) {
      toast(e instanceof Error ? e.message : '更新 Agent 调度失败', 'error')
    }
  }

  // ========== Helpers ==========
  const formatMoney = (value: number) => {
    if (Math.abs(value) >= 10000) {
      return `${(value / 10000).toFixed(2)}万`
    }
    return value.toFixed(2)
  }

  const marketLabel = (m: string) => m === 'CN' ? 'A股' : m === 'HK' ? '港股' : m === 'US' ? '美股' : m

  // 市场徽章样式和短标签
  const marketBadge = (m: string) => {
    if (m === 'HK') return { style: 'bg-orange-500/10 text-orange-600', label: '港' }
    if (m === 'US') return { style: 'bg-green-500/10 text-green-600', label: '美' }
    return { style: 'bg-blue-500/10 text-blue-600', label: 'A' }
  }

  // 保留原始精度显示价格（不强制截断小数位）
  const formatPrice = (value: number) => {
    // 最多显示4位小数，去除末尾的0
    const formatted = value.toFixed(4).replace(/\.?0+$/, '')
    return formatted
  }

  // 获取股票的行情信息
  const getStockQuote = (quoteKey: string) => {
    return quotes[quoteKey] || null
  }

  // 获取股票的建议信息（优先使用建议池，包含来源和时间信息）
  const getSuggestionForStock = (symbol: string, market: string, hasPosition?: boolean): { suggestion: SuggestionInfo | null; kline: KlineSummary | null } => {
    const key = `${market || 'CN'}:${symbol}`
    // 优先使用建议池的建议（包含来源和时间信息）
    const poolSug =
      poolSuggestions[key] ||
      (() => {
        const fallback = poolSuggestions[symbol]
        if (!fallback) return null
        const fm = String(fallback.stock_market || '').toUpperCase()
        return fm && fm !== String(market || 'CN').toUpperCase() ? null : fallback
      })()
    if (poolSug) {
      const preloadedKline = klineSummaries[key] || (suggestions[symbol]?.kline as any) || null
      return {
        suggestion: {
          id: poolSug.id,
          action: poolSug.action,
          action_label: poolSug.action_label,
          signal: poolSug.signal,
          reason: poolSug.reason,
          should_alert: poolSug.should_alert ?? (['alert', 'avoid', 'sell', 'reduce'].includes(poolSug.action)),
          agent_name: poolSug.agent_name,
          agent_label: poolSug.agent_label,
          created_at: poolSug.created_at,
          is_expired: poolSug.is_expired,
          prompt_context: poolSug.prompt_context,
          ai_response: poolSug.ai_response,
          meta: poolSug.meta,
        },
        // 优先使用本页并发预取的 kline 摘要，确保徽章与弹窗一致且免加载
        kline: preloadedKline,
      }
    }

    // 无池建议时，使用 K 线评分构建轻量建议（仅用于徽章展示）
    const ks = klineSummaries[key]
    if (ks) {
      const scored = buildKlineSuggestion(ks as any, hasPosition)
      return {
        suggestion: {
          action: scored.action,
          action_label: scored.action_label,
          signal: scored.signal,
          reason: '',
          should_alert: false,
          agent_label: '技术指标',
        },
        kline: ks,
      }
    }

    return { suggestion: null, kline: null }
  }

  const positionsCount = useMemo(() => {
    return (portfolio?.accounts || []).reduce((acc, a) => acc + (a.positions?.length || 0), 0)
  }, [portfolio])

  const scopedAccounts = (portfolio?.accounts || []).filter(a => accountFilter === null || a.id === accountFilter)
  const selectedAccount = portfolio?.accounts.find(a => a.id === accountFilter)
  const scopedDue = duePlans.filter(p => accountFilter === null || p.account_id === accountFilter)
  const riskChecks = scopedAccounts.flatMap(account => account.positions.flatMap(pos => {
    if (!pos.current_price || pos.current_price <= 0) return []
    if (pos.stop_loss_price && pos.stop_loss_price > 0 && pos.current_price <= pos.stop_loss_price * 1.03) {
      return [{ account, pos, reason: pos.current_price <= pos.stop_loss_price ? '已触及止损计划价' : `距止损计划价 ${((pos.current_price / pos.stop_loss_price - 1) * 100).toFixed(1)}%` }]
    }
    if (pos.take_profit_price && pos.take_profit_price > 0 && pos.current_price >= pos.take_profit_price * 0.97) {
      return [{ account, pos, reason: pos.current_price >= pos.take_profit_price ? '已达到止盈计划价' : `距止盈计划价 ${((1 - pos.current_price / pos.take_profit_price) * 100).toFixed(1)}%` }]
    }
    return []
  }))
  const missingQuoteCount = scopedAccounts.reduce((sum, a) => sum + a.positions.filter(p => !p.current_price || p.current_price <= 0).length, 0)
  const openWorkspacePanel = (panel: 'analysis' | 'records' | 'accounts', scope = accountFilter) => {
    setPanelAccountId(panel === 'analysis' ? null : scope)
    setWorkspacePanel(panel)
  }
  const handleWealthChanged = () => { loadPortfolio(); setWealthRefreshKey(v => v + 1) }
  const positionActions = (account: AccountSummary, pos: Position) => [
    { label: '止损 / 止盈计划', onSelect: () => openRiskDialog(account, pos) },
    { label: 'K 线与技术指标', onSelect: () => openKlineDialog(pos.symbol, pos.market, pos.name, true) },
    { label: '相关新闻', onSelect: () => openNewsDialog(pos.name) },
    { label: '深度分析', onSelect: () => openDeepAnalysis(pos.stock_id, pos.symbol, pos.name) },
    { label: 'Agent 配置与运行', onSelect: () => { const stock = stocks.find(s => s.id === pos.stock_id); if (stock) setAgentDialogStock(stock) }, disabled: !stocks.some(s => s.id === pos.stock_id) },
    { label: '编辑持仓', onSelect: () => openPositionDialog(account.id, pos) },
    { label: '查看账户流水', onSelect: () => openWorkspacePanel('records', account.id) },
    { label: '删除持仓', destructive: true, onSelect: () => handleDeletePosition(pos.id) },
  ]

  useEffect(() => {
    if (portfolio && accountFilter !== null && !portfolio.accounts.some(a => a.id === accountFilter)) setAccountFilter(null)
    if (portfolio && panelAccountId !== null && !portfolio.accounts.some(a => a.id === panelAccountId)) setPanelAccountId(null)
  }, [portfolio, accountFilter, panelAccountId])

  const watchGroups = useMemo(() => {
    const map = new Map<string, number>()
    stocks.forEach((s) => {
      const g = s.group_name || ''
      map.set(g, (map.get(g) || 0) + 1)
    })
    return Array.from(map, ([name, count]) => ({ name, count })).sort(
      (a, b) => (a.name === '' ? 1 : 0) - (b.name === '' ? 1 : 0) || a.name.localeCompare(b.name)
    )
  }, [stocks])

  const watchlistCount = useMemo(() => {
    return stocks.length
  }, [stocks])

  const filteredWatchStocks = stocks
    .filter(stock => !watchSearch || `${stock.name} ${stock.symbol}`.toLowerCase().includes(watchSearch.toLowerCase()))
    .filter(stock => !stockListFilter || stock.market === stockListFilter)
    .filter(stock => watchGroupFilter === '__all__' || (stock.group_name || '') === watchGroupFilter)
    .filter(stock => !watchlistOnlyAlerts || !!getSuggestionForStock(stock.symbol, stock.market, false).suggestion?.should_alert)
    .sort((a, b) => Number(a.sort_order || 0) - Number(b.sort_order || 0) || a.id - b.id)

  const toggleAccountExpanded = (id: number) => {
    setExpandedAccounts(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  // 骨架屏：初始加载时显示
  if (loading) {
    return (
      <div>
        {/* Header Skeleton */}
        <div className="flex items-center justify-between mb-6">
          <div>
            <Skeleton className="h-6 w-16 mb-2" />
            <Skeleton className="h-4 w-32" />
          </div>
          <div className="hidden md:flex items-center gap-3">
            <Skeleton className="h-9 w-24" />
            <Skeleton className="h-9 w-24" />
            <Skeleton className="h-9 w-24" />
          </div>
        </div>
        {/* Summary Cards Skeleton */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
          {[...Array(4)].map((_, i) => (
            <div key={i} className="card p-4">
              <Skeleton className="h-4 w-16 mb-2" />
              <Skeleton className="h-6 w-24" />
            </div>
          ))}
        </div>
        {/* Account List Skeleton */}
        <div className="space-y-4">
          {[...Array(2)].map((_, i) => (
            <div key={i} className="card">
              <div className="px-4 py-3 border-b border-border/50">
                <Skeleton className="h-5 w-32" />
              </div>
              <div className="divide-y divide-border/50">
                {[...Array(3)].map((_, j) => (
                  <div key={j} className="px-4 py-3 flex items-center gap-4">
                    <Skeleton className="h-4 w-16" />
                    <Skeleton className="h-4 w-24" />
                    <Skeleton className="h-4 w-16 ml-auto" />
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className="wealth-workspace" ref={workspaceRef}>
      <header className="flex flex-wrap items-center justify-between gap-3 mb-3">
        <div><h1 className="text-xl md:text-2xl font-semibold tracking-tight">理财工作台</h1><p className="text-xs text-muted-foreground mt-1">持有有计划，记录有归属。</p></div>
        <div className="flex items-center gap-2">
          {viewTab !== 'watchlist' && <Button ref={panelFallbackRef} variant="ghost" onClick={() => openWorkspacePanel('records')}>记录</Button>}
          <WealthActionMenu label="工作台管理" triggerText="管理" actions={[
            { label: '管理账户', onSelect: () => openWorkspacePanel('accounts', viewTab === 'watchlist' ? null : accountFilter) },
            { label: '新增账户', onSelect: () => openAccountDialog() },
          ]} />
        </div>
      </header>
      <div className="flex flex-wrap justify-between items-center gap-x-6 gap-y-2 mb-4 border-b border-border">
        <nav className="wealth-tabs !mb-0 !border-0" aria-label="理财工作台视图">
          {([['positions', `持仓 ${positionsCount}`], ['watchlist', `关注 ${watchlistCount}`], ['plans', '计划']] as const).map(([key, label]) => <button key={key} type="button" aria-current={viewTab === key ? 'page' : undefined} onClick={() => setViewTab(key)}>{label}</button>)}
        </nav>
        {viewTab !== 'watchlist' && <label className="flex items-center gap-2 text-xs text-muted-foreground pb-2">统计账户<select aria-label="统计账户" className="bg-card text-foreground border border-border rounded-lg py-2 px-3 max-w-[200px]" value={accountFilter ?? ''} onChange={e => { setAccountFilter(e.target.value ? Number(e.target.value) : null); setTasksExpanded(false) }}><option value="">全部账户</option>{portfolio?.accounts.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}</select></label>}
      </div>
      {viewTab === 'positions' && (portfolio ? <WealthSummary total={selectedAccount || portfolio.total} accounts={scopedAccounts.length} positions={scopedAccounts.reduce((n, a) => n + a.positions.length, 0)} scopeLabel={selectedAccount?.name || '全部账户'} privateMode={privateMode} onPrivacyChange={setPrivateMode} /> : portfolioLoading ? <div className="card p-5 mb-4" role="status">资产概况加载中…</div> : <p className="text-sm text-muted-foreground mb-4">资产数据暂不可用，请刷新重试。</p>)}
      {viewTab === 'positions' && (scopedDue.length > 0 || riskChecks.length > 0 || dueError || missingQuoteCount > 0) && <section className="mb-4 border-b border-border pb-3" aria-label="待处理事项">
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <span>{scopedDue.length ? `${scopedDue.length} 笔定投待确认` : ''}{scopedDue.length && riskChecks.length ? ' · ' : ''}{riskChecks.length ? `${riskChecks.length} 项持有计划需检查` : ''}</span>
          {missingQuoteCount > 0 && <span className="text-xs text-muted-foreground">{missingQuoteCount} 只持仓行情缺失，暂未检查计划价</span>}
          {dueError && <button className="text-xs text-primary" onClick={() => setDueRetry(v => v + 1)}>定投待办加载失败，重试</button>}
          {(scopedDue.length > 0 || riskChecks.length > 0) && <Button variant="ghost" size="sm" aria-expanded={tasksExpanded} onClick={() => setTasksExpanded(v => !v)}>{tasksExpanded ? '收起事项' : '查看事项'}</Button>}
        </div>
        {tasksExpanded && <div className="mt-3 divide-y divide-border">
          {scopedDue.map(plan => <div key={plan.id} className="flex justify-between items-center gap-3 py-3"><div><b className="text-sm">{plan.name} · 定投待确认</b><p className="text-xs text-muted-foreground">{plan.account_name} · {plan.next_run_date}{plan.overdue ? ' · 已逾期' : ''} · {privateMode ? '••••' : `¥${plan.amount.toFixed(2)}`}</p></div><Button size="sm" onClick={() => { setFocusPlanId(plan.id); setViewTab('plans') }}>记录成交</Button></div>)}
          {riskChecks.map(({ account, pos, reason }) => <div key={`${account.id}-${pos.id}`} className="flex justify-between items-center gap-3 py-3"><div><b className="text-sm">{pos.name} · {privateMode ? '检查持有计划' : reason}</b><p className="text-xs text-muted-foreground">{account.name} · 接近范围为计划价的 3%，不代表自动交易指令</p></div><Button size="sm" variant="secondary" onClick={() => openRiskDialog(account, pos)}>查看计划</Button></div>)}
        </div>}
      </section>}
      {viewTab !== 'plans' && <div className="wealth-toolbar mb-4">
        <div className="flex flex-wrap items-center gap-3 min-w-0">
          {viewTab === 'positions' && <Input className="w-44" aria-label="搜索持仓" placeholder="搜索名称 / 代码" value={positionSearch} onChange={e => setPositionSearch(e.target.value)} />}
          <label className="flex items-center gap-2 text-xs text-muted-foreground"><Switch checked={autoRefresh} onCheckedChange={setAutoRefresh} aria-label="自动刷新行情" />自动刷新</label>
          {autoRefresh && <select className="bg-card border border-border rounded px-2 py-1 text-xs" aria-label="行情刷新间隔" value={refreshInterval} onChange={e => setRefreshInterval(Number(e.target.value))}><option value={10}>10 秒</option><option value={30}>30 秒</option><option value={60}>1 分钟</option><option value={120}>2 分钟</option></select>}
          <Button variant="ghost" size="sm" onClick={handleRefresh} disabled={quotesLoading}><RefreshCw className={`w-3.5 h-3.5 ${quotesLoading ? 'animate-spin' : ''}`} />刷新</Button>
          <span className="text-xs text-muted-foreground">{lastRefreshTime ? `更新于 ${lastRefreshTime.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}` : '行情更新中'}</span>
          {poolSuggestionsLoading && <span className="text-xs text-muted-foreground" role="status">更新分析中…</span>}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {viewTab === 'positions' ? <><Button variant="secondary" onClick={() => openWorkspacePanel('analysis', null)}>组合分析</Button><WealthActionMenu label="持仓显示与扫描" actions={[{ label: detailedColumns ? '切换精简列' : '显示完整列', onSelect: () => setDetailedColumns(v => !v) }, { label: scanning ? '扫描中…' : '扫描并分析持仓', onSelect: scanAndReload, disabled: scanning }]} /><Button onClick={() => setRecordPickerOpen(true)}>＋ 记一笔</Button></> : <><Button variant="secondary" onClick={scanAndReload} disabled={scanning}>{scanning ? '扫描中…' : '扫描分析'}</Button><Button onClick={() => { setStockForm(emptyStockForm); setSearchQuery(''); setShowStockForm(true) }}>＋ 添加关注</Button></>}
        </div>
        <div className="flex gap-3 flex-wrap basis-full text-xs text-muted-foreground">{marketStatus.map(m => <span key={m.code} title={`${m.sessions.join(', ')} (${m.local_time})`}>{m.name} · {m.status_text}</span>)}</div>
      </div>}

      {/* Add Stock Dialog */}
      <Dialog open={showStockForm} onOpenChange={(open) => { setShowStockForm(open); if (!open) { setSearchQuery(''); setSearchMarket('') } }}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>添加股票到自选</DialogTitle>
            <DialogDescription>搜索并添加到自选股列表</DialogDescription>
          </DialogHeader>
          <form onSubmit={handleStockSubmit}>
            <div className="relative" ref={dropdownRef}>
              <div className="flex items-center gap-2 mb-2">
                <Label className="mb-0">搜索股票</Label>
                <div className="flex items-center gap-1">
                  {[
                    { value: '', label: '全部' },
                    { value: 'CN', label: 'A股' },
                    { value: 'HK', label: '港股' },
                    { value: 'US', label: '美股' },
                  ].map(opt => (
                    <button
                      key={opt.value}
                      type="button"
                      onClick={() => handleSearchMarketChange(opt.value)}
                      className={`text-[11px] px-2 py-0.5 rounded transition-colors ${
                        searchMarket === opt.value
                          ? 'bg-primary text-primary-foreground'
                          : 'bg-accent/50 text-muted-foreground hover:bg-accent'
                      }`}
                    >
                      {opt.label}
                    </button>
                  ))}
                </div>
                <button
                  type="button"
                  onClick={refreshStockListCache}
                  disabled={refreshingStockList}
                  className="text-[10px] text-muted-foreground hover:text-foreground transition-colors ml-2"
                  title="搜索不到？点击刷新股票列表"
                >
                  {refreshingStockList ? (
                    <span className="flex items-center gap-1">
                      <RefreshCw className="w-3 h-3 animate-spin" /> 刷新中...
                    </span>
                  ) : (
                    <span className="flex items-center gap-1">
                      <RefreshCw className="w-3 h-3" /> 刷新列表
                    </span>
                  )}
                </button>
              </div>
              <div className="relative">
                <Search className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground/50" />
                <Input
                  value={searchQuery}
                  onChange={e => handleSearchInput(e.target.value)}
                  onFocus={() => searchResults.length > 0 && setShowDropdown(true)}
                  placeholder={searchMarket === 'HK' ? '代码或名称，如 00700 或 腾讯' : searchMarket === 'US' ? '代码或名称，如 AAPL 或 苹果' : '代码或名称，如 600519 或 茅台'}
                  className="pl-10"
                  autoComplete="off"
                />
                {searching && <span className="absolute right-3.5 top-1/2 -translate-y-1/2 w-4 h-4 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />}
              </div>
              {showDropdown && (
                <div className="absolute z-50 w-full mt-2 max-h-64 overflow-auto scrollbar card shadow-lg">
                  {searchResults.map(item => (
                    <button
                      key={`${item.market}-${item.symbol}`}
                      type="button"
                      onClick={() => selectStock(item)}
                      className="w-full flex items-center gap-3 px-4 py-3 text-[13px] hover:bg-accent/50 text-left transition-colors"
                    >
                      <span className="font-mono text-muted-foreground text-[12px] w-14">{item.symbol}</span>
                      <span className="flex-1 font-medium text-foreground">{item.name}</span>
                      <Badge variant="secondary">{marketLabel(item.market)}</Badge>
                    </button>
                  ))}
                </div>
              )}
              {stockForm.symbol && (
                <div className="mt-2.5 flex items-center gap-2">
                  <Badge><span className="font-mono">{stockForm.symbol}</span> {stockForm.name}</Badge>
                  <Badge variant="secondary">{marketLabel(stockForm.market)}</Badge>
                </div>
              )}
            </div>
            <div className="mt-6 flex items-center gap-3 justify-end">
              <Button type="button" variant="ghost" onClick={() => { setShowStockForm(false); setSearchQuery('') }}>取消</Button>
              <Button type="submit" disabled={!stockForm.symbol}>确认添加</Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>

      <div className={privateMode ? 'wealth-holdings wealth-private' : 'wealth-holdings'} data-details={detailedColumns}>
      {viewTab === 'positions' && positionSearch && !scopedAccounts.some(a => a.positions.some(p => `${p.name} ${p.symbol}`.toLowerCase().includes(positionSearch.toLowerCase()))) && <p className="text-sm text-muted-foreground py-4">当前账户范围没有匹配的持仓，请调整搜索。</p>}
      {/* Accounts & Positions */}
      {viewTab === 'positions' && (
        portfolio && portfolio.accounts.length === 0 ? (
          <div className="card flex flex-col items-center justify-center py-20">
            <div className="w-14 h-14 rounded-xl bg-primary/10 flex items-center justify-center mb-4">
              <Building2 className="w-6 h-6 text-primary" />
            </div>
            <p className="text-[15px] font-semibold text-foreground">还没有账户</p>
            <p className="text-[13px] text-muted-foreground mt-1.5 mb-4">创建账户后记录你的第一笔持仓</p>
            <Button onClick={() => openAccountDialog()}>新增账户</Button>
          </div>
        ) : (
          <div className="space-y-4">
            {scopedAccounts.map(account => (
              <div key={account.id} className="card overflow-hidden">
              {/* Account Header */}
              <div
                className="flex flex-col md:flex-row md:items-center justify-between p-3 md:p-4 cursor-pointer hover:bg-accent/30 transition-colors gap-2"
                onClick={() => toggleAccountExpanded(account.id)}
              >
                <div className="flex items-center gap-2 md:gap-3">
                  {expandedAccounts.has(account.id) ? (
                    <ChevronDown className="w-4 h-4 text-muted-foreground" />
                  ) : (
                    <ChevronRight className="w-4 h-4 text-muted-foreground" />
                  )}
                  <Building2 className="w-4 h-4 text-primary" />
                  <span className="text-[14px] md:text-[15px] font-semibold text-foreground">{account.name}</span>
                  {account.strategy_type && (
                    <span className={`text-[10px] px-1.5 py-0.5 rounded ${
                      account.strategy_type === 'aggr'
                        ? 'bg-rose-500/10 text-rose-600'
                        : 'bg-blue-500/10 text-blue-600'
                    }`}>
                      {account.strategy_type === 'aggr' ? '激进' : '稳健'}
                    </span>
                  )}
                  <span className="text-[11px] md:text-[12px] text-muted-foreground">
                    {account.positions.length} 只
                  </span>
                </div>
                <div className="flex items-center justify-between md:justify-end gap-2 md:gap-6 pl-6 md:pl-0">
                  <div className="wealth-sensitive wealth-account-metrics flex items-center gap-2.5 md:gap-6 min-w-0">
                    <div className="text-left md:text-right">
                      <div className="text-[10px] md:text-[11px] text-muted-foreground">市值</div>
                      <div className="text-[12px] md:text-[13px] font-mono font-medium whitespace-nowrap">{formatMoney(account.total_market_value)}</div>
                    </div>
                    <div className="text-left md:text-right">
                      <div className="text-[10px] md:text-[11px] text-muted-foreground">盈亏</div>
                      <div className={`text-[12px] md:text-[13px] font-mono font-medium whitespace-nowrap ${account.total_pnl >= 0 ? 'text-rose-500' : 'text-emerald-500'}`}>
                        {account.total_pnl >= 0 ? '+' : ''}{formatMoney(account.total_pnl)}
                        <span className="text-[10px] md:text-[11px] ml-1 hidden md:inline">({account.total_pnl_pct >= 0 ? '+' : ''}{account.total_pnl_pct.toFixed(2)}%)</span>
                      </div>
                    </div>
                    <div className="text-left md:text-right">
                      <div className="text-[10px] md:text-[11px] text-muted-foreground">今日</div>
                      <div className={`text-[12px] md:text-[13px] font-mono font-medium whitespace-nowrap ${account.total_daily_pnl >= 0 ? 'text-rose-500' : 'text-emerald-500'}`}>
                        {account.total_daily_pnl >= 0 ? '+' : ''}{formatMoney(account.total_daily_pnl)}
                      </div>
                    </div>
                    <div className="text-left md:text-right hidden sm:block">
                      <div className="text-[10px] md:text-[11px] text-muted-foreground">可用</div>
                      <div className="text-[12px] md:text-[13px] font-mono whitespace-nowrap">{formatMoney(account.available_funds)}</div>
                    </div>
                  </div>
                  <div className="flex items-center gap-1 shrink-0" onClick={e => e.stopPropagation()}>
                    <Button variant="ghost" size="sm" onClick={() => openPositionDialog(account.id)}>新增持仓</Button>
                    <WealthActionMenu label={`${account.name}账户管理`} actions={[
                      { label: '策略、费用与分红设置', onSelect: () => openWorkspacePanel('accounts', account.id) },
                      { label: '编辑账户名称与余额', onSelect: () => openAccountDialog(accounts.find(a => a.id === account.id)) },
                      { label: '账户流水', onSelect: () => openWorkspacePanel('records', account.id) },
                      { label: '删除账户', destructive: true, onSelect: () => handleDeleteAccount(account.id) },
                    ]} />
                  </div>
                </div>
              </div>

              {/* Positions */}
              {expandedAccounts.has(account.id) && (
                <div className="border-t border-border/30">
                  {account.positions.length === 0 ? (
                    <p className="text-[13px] text-muted-foreground text-center py-8">暂无持仓，点击 + 添加</p>
                  ) : (
                    <>
                      {/* Desktop Table */}
                      <div className="hidden md:block overflow-x-auto wealth-position-table">
                        <table className="w-full">
                          <thead>
                            <tr className="border-b border-border/30 bg-accent/20">
                              <th className="text-left px-4 py-2 text-[11px] font-semibold text-muted-foreground">股票</th>
                              <th className="text-right px-4 py-2 text-[11px] font-semibold text-muted-foreground">现价</th>
                              <th className="text-right px-4 py-2 text-[11px] font-semibold text-muted-foreground">涨跌</th>
                              <th className="text-right px-4 py-2 text-[11px] font-semibold text-muted-foreground">成本</th>
                              <th className="text-right px-4 py-2 text-[11px] font-semibold text-muted-foreground">持仓</th>
                              <th className="text-right px-4 py-2 text-[11px] font-semibold text-muted-foreground">市值</th>
                              <th className="text-right px-4 py-2 text-[11px] font-semibold text-muted-foreground">盈亏</th>
                              <th className="text-right px-4 py-2 text-[11px] font-semibold text-muted-foreground">今日</th>
                              <th className="text-center px-4 py-2 text-[11px] font-semibold text-muted-foreground">持有计划</th>
                              <th className="text-left px-4 py-2 text-[11px] font-semibold text-muted-foreground">Agent</th>
                              <th className="text-center px-4 py-2 text-[11px] font-semibold text-muted-foreground">操作</th>
                            </tr>
                          </thead>
                          <tbody>
                            {account.positions.filter(pos => !positionSearch || `${pos.name} ${pos.symbol}`.toLowerCase().includes(positionSearch.toLowerCase())).map((pos, i) => {
                              const stock = stocks.find(s => s.id === pos.stock_id)
                              const badge = marketBadge(pos.market)
                              const isForeign = pos.market === 'HK' || pos.market === 'US'
                              const changeColor = pos.change_pct != null
                                ? (pos.change_pct > 0 ? 'text-rose-500' : pos.change_pct < 0 ? 'text-emerald-500' : 'text-muted-foreground')
                                : 'text-muted-foreground'
                              const pnlColor = pos.pnl != null
                                ? (pos.pnl > 0 ? 'text-rose-500' : pos.pnl < 0 ? 'text-emerald-500' : 'text-muted-foreground')
                                : 'text-muted-foreground'
                              return (
                                <tr
                                  key={pos.id}
                                  draggable
                                  onDragStart={(e) => {
                                    positionDragSnapshotRef.current = portfolioRaw ? JSON.parse(JSON.stringify(portfolioRaw)) : null
                                    setDraggingPositionId(pos.id)
                                    setDraggingPositionAccountId(account.id)
                                    e.dataTransfer.effectAllowed = 'move'
                                  }}
                                  onDragOver={(e) => {
                                    e.preventDefault()
                                    e.dataTransfer.dropEffect = 'move'
                                    if (draggingPositionId != null && draggingPositionAccountId === account.id) {
                                      previewPositionReorder(account.id, draggingPositionId, pos.id)
                                    }
                                  }}
                                  onDrop={(e) => {
                                    e.preventDefault()
                                    if (draggingPositionId != null && draggingPositionAccountId === account.id) {
                                      commitPositionReorder(account.id)
                                    }
                                    setDraggingPositionId(null)
                                    setDraggingPositionAccountId(null)
                                    positionDragSnapshotRef.current = null
                                  }}
                                  onDragEnd={() => {
                                    setDraggingPositionId(null)
                                    setDraggingPositionAccountId(null)
                                    positionDragSnapshotRef.current = null
                                  }}
                                  className={`group hover:bg-accent/30 transition-colors ${i > 0 ? 'border-t border-border/20' : ''} ${draggingPositionId === pos.id ? 'opacity-60' : ''}`}
                                >
                                  <td className="px-4 py-2.5">
                                    <span className={`text-[9px] px-1 py-0.5 rounded mr-1.5 ${badge.style}`}>{badge.label}</span>
                                    <span className="font-mono text-[12px] font-semibold text-foreground">
                                      {pos.symbol}
                                    </span>
                                    {strategyMap[pos.symbol] && (
                                      <span
                                        className={`ml-1 rounded px-1 py-0.5 text-[9px] ${
                                          strategyMap[pos.symbol] === '符合卖出' || strategyMap[pos.symbol] === '禁止买入'
                                            ? 'bg-stock-down/15 text-stock-down'
                                            : strategyMap[pos.symbol] === '符合买入'
                                              ? 'bg-stock-up/15 text-stock-up'
                                              : 'bg-amber-500/15 text-amber-500'
                                        }`}
                                      >
                                        {strategyMap[pos.symbol] === '符合卖出'
                                          ? '卖'
                                          : strategyMap[pos.symbol] === '禁止买入'
                                            ? '禁'
                                            : strategyMap[pos.symbol] === '符合买入'
                                              ? '买'
                                              : '近'}
                                      </span>
                                    )}
                                    <button
                                      className="ml-1.5 text-[12px] text-muted-foreground hover:text-primary"
                                      onClick={() => openStockDetail(pos.symbol, pos.market, pos.name, true)}
                                    >
                                      {pos.name}
                                    </button>
                                    {(() => {
                                      const { suggestion, kline } = getSuggestionForStock(pos.symbol, pos.market, true)
                                      return (suggestion || kline) ? (
                                        <span className="ml-2">
                                          <SuggestionBadge
                                            suggestion={suggestion}
                                            stockName={pos.name}
                                            stockSymbol={pos.symbol}
                                            kline={kline}
                                            market={pos.market}
                                            hasPosition={true}
                                          />
                                        </span>
                                      ) : null
                                    })()}
                                  </td>
                                  <td className={`px-4 py-2.5 text-right font-mono text-[12px] ${changeColor}`}>
                                    {pos.current_price != null ? <span>{pos.current_price.toFixed(2)}{isForeign ? (pos.market === 'HK' ? ' HKD' : ' USD') : ''}</span> : '—'}
                                  </td>
                                  <td className={`px-4 py-2.5 text-right font-mono text-[12px] ${changeColor}`}>
                                    {pos.change_pct != null ? `${pos.change_pct >= 0 ? '+' : ''}${pos.change_pct.toFixed(2)}%` : '—'}
                                  </td>
                                  <td className="wealth-sensitive px-4 py-2.5 text-right font-mono text-[12px] text-muted-foreground">
                                    <div>{formatPrice(pos.cost_price)}</div>
                                    {(pos.stop_loss_price != null || pos.take_profit_price != null) && (
                                      <div className="text-[9px] text-muted-foreground/70">
                                        {pos.stop_loss_price != null && `损 ${formatPrice(pos.stop_loss_price)}`}
                                        {pos.stop_loss_price != null && pos.take_profit_price != null && ' / '}
                                        {pos.take_profit_price != null && `盈 ${formatPrice(pos.take_profit_price)}`}
                                      </div>
                                    )}
                                  </td>
                                  <td className="wealth-sensitive px-4 py-2.5 text-right font-mono text-[12px] text-muted-foreground">{pos.quantity}</td>
                                  <td className="wealth-sensitive px-4 py-2.5 text-right font-mono text-[12px] text-muted-foreground">
                                    {pos.market_value != null ? (
                                      <div className="flex flex-col items-end">
                                        {isForeign ? (
                                          <>
                                            <span>{formatMoney(pos.market_value)} {pos.market === 'HK' ? 'HKD' : 'USD'}</span>
                                            {pos.market_value_cny && <span className="text-[10px] text-muted-foreground/60">≈{formatMoney(pos.market_value_cny)}</span>}
                                          </>
                                        ) : <span>{formatMoney(pos.market_value)}</span>}
                                      </div>
                                    ) : '—'}
                                  </td>
                                  <td className={`wealth-sensitive px-4 py-2.5 text-right font-mono text-[12px] ${pnlColor}`}>
                                    {pos.pnl != null ? (
                                      <div className="flex flex-col items-end">
                                        <span>{pos.pnl >= 0 ? '+' : ''}{formatMoney(pos.pnl)}</span>
                                        <span className="text-[10px] opacity-70">{pos.pnl_pct != null ? `${pos.pnl_pct >= 0 ? '+' : ''}${pos.pnl_pct.toFixed(2)}%` : ''}{isForeign && ' CNY'}</span>
                                      </div>
                                    ) : '—'}
                                  </td>
                                  <td className={`wealth-sensitive px-4 py-2.5 text-right font-mono text-[12px] ${pos.daily_pnl != null ? (pos.daily_pnl >= 0 ? 'text-rose-500' : 'text-emerald-500') : ''}`}>
                                    {pos.daily_pnl != null ? (
                                      <div className="flex flex-col items-end">
                                        <span>{pos.daily_pnl >= 0 ? '+' : ''}{formatMoney(pos.daily_pnl)}</span>
                                        <span className="text-[10px] opacity-70">{pos.daily_pnl_pct != null ? `${pos.daily_pnl_pct >= 0 ? '+' : ''}${pos.daily_pnl_pct.toFixed(2)}%` : ''}</span>
                                      </div>
                                    ) : '—'}
                                  </td>
                                  <td className="px-4 py-2.5 text-center">
                                    {pos.trading_style ? (
                                      <span className={`text-[10px] px-1.5 py-0.5 rounded ${pos.trading_style === 'short' ? 'bg-rose-500/10 text-rose-600' : pos.trading_style === 'long' ? 'bg-blue-500/10 text-blue-600' : 'bg-amber-500/10 text-amber-600'}`}>
                                        {pos.trading_style === 'short' ? '短线' : pos.trading_style === 'long' ? '长线' : '波段'}
                                      </span>
                                    ) : (
                                      <span className="text-[10px] text-muted-foreground/50">-</span>
                                    )}
                                    <button className="block text-xs text-muted-foreground mt-1 hover:text-primary whitespace-nowrap" onClick={() => openRiskDialog(account, pos)}>
                                      {pos.stop_loss_price || pos.take_profit_price ? <span className="wealth-sensitive">{pos.stop_loss_price ? `止损 ${formatPrice(pos.stop_loss_price)}` : ''}{pos.stop_loss_price && pos.take_profit_price ? ' / ' : ''}{pos.take_profit_price ? `止盈 ${formatPrice(pos.take_profit_price)}` : ''}</span> : '设置止损止盈'}
                                    </button>
                                  </td>
                                  <td className="px-4 py-2.5">
                                    {stock && (
                                      <button onClick={() => setAgentDialogStock(stock)} className="flex items-center gap-1.5 hover:opacity-70 transition-opacity">
                                        {stock.agents && stock.agents.length > 0 ? (
                                          <div className="flex items-center gap-1.5 flex-wrap">
                                            {stock.agents.map(sa => {
                                              const agent = agents.find(a => a.name === sa.agent_name)
                                              const isRunning = runningAgents[stock.id] === sa.agent_name
                                              return (
                                                <span key={sa.agent_name} className="inline-flex items-center gap-1">
                                                  <Badge variant="default" className="text-[10px]">{sa.display_name || agent?.display_name || sa.agent_name}</Badge>
                                                  {isRunning && (
                                                    <span className="inline-flex items-center gap-1 text-[10px] text-amber-600">
                                                      <span className="w-3 h-3 border-2 border-current/30 border-t-current rounded-full animate-spin" />
                                                      执行中
                                                    </span>
                                                  )}
                                                </span>
                                              )
                                            })}
                                          </div>
                                        ) : (
                                          <span className="text-[11px] text-muted-foreground/50 flex items-center gap-1"><Bot className="w-3 h-3" /> 未配置</span>
                                        )}
                                      </button>
                                    )}
                                  </td>
                                  <td className="px-4 py-2.5 text-center">
                                    <div className="flex items-center justify-end gap-1" onClick={e => e.stopPropagation()}>
                                      <Button variant="ghost" size="sm" onClick={() => openStockDetail(pos.symbol, pos.market, pos.name, true)}>详情</Button>
                                      <Button variant="ghost" size="sm" onClick={() => openTradeDialog(account, pos, 'buy')}>加仓</Button>
                                      <Button variant="ghost" size="sm" onClick={() => openTradeDialog(account, pos, 'sell')}>减仓</Button>
                                      <WealthActionMenu label={`${pos.name}更多操作`} actions={positionActions(account, pos)} />
                                    </div>
                                  </td>
                                </tr>
                              )
                            })}
                          </tbody>
                        </table>
                      </div>

                      {/* Mobile Cards */}
                      <div className="md:hidden divide-y divide-border/30">
                        {account.positions.filter(pos => !positionSearch || `${pos.name} ${pos.symbol}`.toLowerCase().includes(positionSearch.toLowerCase())).map(pos => {
                          const stock = stocks.find(s => s.id === pos.stock_id)
                          const badge = marketBadge(pos.market)
                          const changeColor = pos.change_pct != null
                            ? (pos.change_pct > 0 ? 'text-rose-500' : pos.change_pct < 0 ? 'text-emerald-500' : 'text-muted-foreground')
                            : 'text-muted-foreground'
                          const pnlColor = pos.pnl != null
                            ? (pos.pnl > 0 ? 'text-rose-500' : pos.pnl < 0 ? 'text-emerald-500' : 'text-muted-foreground')
                            : 'text-muted-foreground'
                          return (
                            <div
                              key={pos.id}
                              draggable
                              onDragStart={(e) => {
                                positionDragSnapshotRef.current = portfolioRaw ? JSON.parse(JSON.stringify(portfolioRaw)) : null
                                setDraggingPositionId(pos.id)
                                setDraggingPositionAccountId(account.id)
                                e.dataTransfer.effectAllowed = 'move'
                              }}
                              onDragOver={(e) => {
                                e.preventDefault()
                                e.dataTransfer.dropEffect = 'move'
                                if (draggingPositionId != null && draggingPositionAccountId === account.id) {
                                  previewPositionReorder(account.id, draggingPositionId, pos.id)
                                }
                              }}
                              onDrop={(e) => {
                                e.preventDefault()
                                if (draggingPositionId != null && draggingPositionAccountId === account.id) {
                                  commitPositionReorder(account.id)
                                }
                                setDraggingPositionId(null)
                                setDraggingPositionAccountId(null)
                                positionDragSnapshotRef.current = null
                              }}
                              onDragEnd={() => {
                                setDraggingPositionId(null)
                                setDraggingPositionAccountId(null)
                                positionDragSnapshotRef.current = null
                              }}
                              className={`p-3 hover:bg-accent/30 transition-colors ${draggingPositionId === pos.id ? 'opacity-60' : ''}`}
                            >
                              {/* Row 1: Stock info + Current price */}
                              <div className="flex items-center justify-between gap-2 mb-2">
                                <div className="flex items-center gap-1.5 min-w-0">
                                  <span className={`shrink-0 text-[9px] px-1 py-0.5 rounded ${badge.style}`}>{badge.label}</span>
                                  <span className="shrink-0 font-mono text-[12px] font-semibold text-foreground">
                                    {pos.symbol}
                                  </span>
                                  {strategyMap[pos.symbol] && (
                                    <span
                                      className={`shrink-0 rounded px-1 py-0.5 text-[9px] ${
                                        strategyMap[pos.symbol] === '符合卖出' || strategyMap[pos.symbol] === '禁止买入'
                                          ? 'bg-stock-down/15 text-stock-down'
                                          : strategyMap[pos.symbol] === '符合买入'
                                            ? 'bg-stock-up/15 text-stock-up'
                                            : 'bg-amber-500/15 text-amber-500'
                                      }`}
                                    >
                                      {strategyMap[pos.symbol] === '符合卖出'
                                        ? '卖'
                                        : strategyMap[pos.symbol] === '禁止买入'
                                          ? '禁'
                                          : strategyMap[pos.symbol] === '符合买入'
                                            ? '买'
                                            : '近'}
                                    </span>
                                  )}
                                  <button
                                    className="text-[12px] text-muted-foreground hover:text-primary truncate"
                                    onClick={() => openStockDetail(pos.symbol, pos.market, pos.name, true)}
                                  >
                                    {pos.name}
                                  </button>
                                  {pos.trading_style && (
                                    <span className={`shrink-0 text-[9px] px-1 py-0.5 rounded ${pos.trading_style === 'short' ? 'bg-rose-500/10 text-rose-600' : pos.trading_style === 'long' ? 'bg-blue-500/10 text-blue-600' : 'bg-amber-500/10 text-amber-600'}`}>
                                      {pos.trading_style === 'short' ? '短' : pos.trading_style === 'long' ? '长' : '波'}
                                    </span>
                                  )}
                                </div>
                                <div className={`font-mono text-[13px] font-medium whitespace-nowrap shrink-0 ${changeColor}`}>
                                  {pos.current_price?.toFixed(2) || '—'}
                                  {pos.change_pct != null && <span className="text-[11px] ml-1">{pos.change_pct >= 0 ? '+' : ''}{pos.change_pct.toFixed(2)}%</span>}
                                </div>
                              </div>
                              {/* Row 2 (Suggestion badge, dedicated row to avoid wrapping mess) */}
                              {(() => {
                                const { suggestion, kline } = getSuggestionForStock(pos.symbol, pos.market, true)
                                return (suggestion || kline) ? (
                                  <div className="mb-2">
                                    <SuggestionBadge
                                      suggestion={suggestion}
                                      stockName={pos.name}
                                      stockSymbol={pos.symbol}
                                      kline={kline}
                                      market={pos.market}
                                      hasPosition={true}
                                    />
                                  </div>
                                ) : null
                              })()}
                              {/* Row 3: Stats grid (4 cols, whitespace-nowrap to prevent "万" wrapping) */}
                              <div className="wealth-sensitive grid grid-cols-4 gap-2 text-[11px]">
                                <div className="min-w-0">
                                  <div className="text-[10px] text-muted-foreground">成本</div>
                                  <div className="font-mono text-foreground truncate" title={String(pos.cost_price)}>{formatPrice(pos.cost_price)}</div>
                                  {(pos.stop_loss_price != null || pos.take_profit_price != null) && (
                                    <div className="text-[9px] text-muted-foreground/70">
                                      {pos.stop_loss_price != null && `损 ${formatPrice(pos.stop_loss_price)}`}
                                      {pos.stop_loss_price != null && pos.take_profit_price != null && ' / '}
                                      {pos.take_profit_price != null && `盈 ${formatPrice(pos.take_profit_price)}`}
                                    </div>
                                  )}
                                </div>
                                <div className="min-w-0">
                                  <div className="text-[10px] text-muted-foreground">数量</div>
                                  <div className="font-mono text-foreground truncate" title={String(pos.quantity)}>{pos.quantity}</div>
                                </div>
                                <div className="min-w-0">
                                  <div className="text-[10px] text-muted-foreground">盈亏</div>
                                  <div className={`font-mono whitespace-nowrap ${pnlColor}`}>
                                    {pos.pnl != null ? `${pos.pnl >= 0 ? '+' : ''}${formatMoney(pos.pnl)}` : '—'}
                                  </div>
                                  {pos.pnl_pct != null && (
                                    <div className={`text-[10px] font-mono ${pnlColor} opacity-80`}>
                                      {pos.pnl_pct >= 0 ? '+' : ''}{pos.pnl_pct.toFixed(2)}%
                                    </div>
                                  )}
                                </div>
                                <div className="min-w-0">
                                  <div className="text-[10px] text-muted-foreground">今日</div>
                                  <div className={`font-mono whitespace-nowrap ${pos.daily_pnl != null ? (pos.daily_pnl >= 0 ? 'text-rose-500' : 'text-emerald-500') : 'text-muted-foreground'}`}>
                                    {pos.daily_pnl != null ? `${pos.daily_pnl >= 0 ? '+' : ''}${formatMoney(pos.daily_pnl)}` : '—'}
                                  </div>
                                </div>
                              </div>
                              {/* Row 4: Actions */}
                              <div className="flex items-center justify-between mt-2 pt-2 border-t border-border/20">
                                <div>
                                  {stock && stock.agents && stock.agents.length > 0 ? (
                                    <button onClick={() => setAgentDialogStock(stock)} className="flex items-center gap-1">
                                      {stock.agents.slice(0, 2).map(sa => {
                                        const agent = agents.find(a => a.name === sa.agent_name)
                                        const isRunning = runningAgents[stock.id] === sa.agent_name
                                        return (
                                          <span key={sa.agent_name} className="inline-flex items-center gap-1">
                                          <Badge variant="secondary" className="text-[9px]">{sa.display_name || agent?.display_name || sa.agent_name}</Badge>
                                            {isRunning && (
                                              <span className="inline-flex items-center gap-1 text-[10px] text-amber-600">
                                                <span className="w-3 h-3 border-2 border-current/30 border-t-current rounded-full animate-spin" />
                                                执行中
                                              </span>
                                            )}
                                          </span>
                                        )
                                      })}
                                    </button>
                                  ) : (
                                    <button onClick={() => stock && setAgentDialogStock(stock)} className="text-[10px] text-muted-foreground/50 flex items-center gap-1">
                                      <Bot className="w-3 h-3" /> Agent
                                    </button>
                                  )}
                                </div>
                                <div className="flex items-center justify-end gap-1" onClick={e => e.stopPropagation()}>
                                      <Button variant="ghost" size="sm" onClick={() => openStockDetail(pos.symbol, pos.market, pos.name, true)}>详情</Button>
                                      <Button variant="ghost" size="sm" onClick={() => openTradeDialog(account, pos, 'buy')}>加仓</Button>
                                      <Button variant="ghost" size="sm" onClick={() => openTradeDialog(account, pos, 'sell')}>减仓</Button>
                                      <WealthActionMenu label={`${pos.name}更多操作`} actions={positionActions(account, pos)} />
                                    </div>
                              </div>
                            </div>
                          )
                        })}
                      </div>
                    </>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
        )
      )}

      {/* Watchlist */}
      {viewTab === 'watchlist' && (
        <div className="card p-4">
          <Input className="w-full sm:w-60 mb-4" aria-label="搜索关注" placeholder="搜索名称 / 代码" value={watchSearch} onChange={e => setWatchSearch(e.target.value)} />
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-[13px] font-semibold text-foreground">关注列表</h3>
            <div className="flex items-center gap-1">
              {[
                { value: '', label: '全部', count: stocks.length },
                { value: 'CN', label: 'A股', count: stocks.filter(s => s.market === 'CN').length },
                { value: 'HK', label: '港股', count: stocks.filter(s => s.market === 'HK').length },
                { value: 'US', label: '美股', count: stocks.filter(s => s.market === 'US').length },
              ].map(opt => (
                <button
                  key={opt.value}
                  onClick={() => setStockListFilter(opt.value)}
                  className={`text-[11px] px-2 py-0.5 rounded transition-colors ${
                    stockListFilter === opt.value
                      ? 'bg-primary text-primary-foreground'
                      : 'bg-accent/50 text-muted-foreground hover:bg-accent'
                  }`}
                >
                  {opt.label} ({opt.count})
                </button>
              ))}
            </div>
          </div>

          {watchGroups.length > 1 && (
            <div className="flex flex-wrap items-center gap-1 mb-3">
              <span className="text-[10px] text-muted-foreground mr-1">分组</span>
              <button
                onClick={() => setWatchGroupFilter('__all__')}
                className={`text-[11px] px-2 py-0.5 rounded transition-colors ${
                  watchGroupFilter === '__all__'
                    ? 'bg-primary text-primary-foreground'
                    : 'bg-accent/50 text-muted-foreground hover:bg-accent'
                }`}
              >
                全部 ({stocks.length})
              </button>
              {watchGroups.map((g) => (
                <button
                  key={g.name || '__ungrouped__'}
                  onClick={() => setWatchGroupFilter(g.name)}
                  className={`text-[11px] px-2 py-0.5 rounded transition-colors ${
                    watchGroupFilter === g.name
                      ? 'bg-primary text-primary-foreground'
                      : 'bg-accent/50 text-muted-foreground hover:bg-accent'
                  }`}
                >
                  {g.name || '未分组'} ({g.count})
                </button>
              ))}
            </div>
          )}

          <div className="flex items-center justify-between mb-3">
            <div className="text-[11px] text-muted-foreground">筛选</div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setWatchlistOnlyAlerts(!watchlistOnlyAlerts)}
                className={`text-[11px] px-2.5 py-1 rounded-md border transition-colors ${
                  watchlistOnlyAlerts
                    ? 'bg-rose-500/10 border-rose-500/30 text-rose-600'
                    : 'bg-accent/30 border-border/50 text-muted-foreground hover:border-rose-500/30'
                }`}
                title="只显示需要关注/预警的股票"
              >
                仅预警
              </button>
            </div>
          </div>
          {stocks.length === 0 ? (
            <div className="py-12 text-center">
              <div className="text-[13px] text-muted-foreground">还没有添加关注股票</div>
              <div className="mt-2 text-[11px] text-muted-foreground/70">点击“添加关注”开始</div>
            </div>
          ) : filteredWatchStocks.length === 0 ? (
            <div className="py-10 text-center text-sm text-muted-foreground">
              <p>当前筛选下没有匹配的关注标的。</p>
              <Button variant="ghost" className="mt-2" onClick={() => { setWatchSearch(''); setStockListFilter(''); setWatchGroupFilter('__all__'); setWatchlistOnlyAlerts(false) }}>清除筛选</Button>
            </div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {filteredWatchStocks.map((stock) => {
                const quote = getStockQuote(`${stock.market}:${stock.symbol}`)
                const changeColor = quote?.change_pct != null
                  ? (quote.change_pct > 0 ? 'text-rose-500' : quote.change_pct < 0 ? 'text-emerald-500' : 'text-muted-foreground')
                  : 'text-muted-foreground'
                const { suggestion, kline } = getSuggestionForStock(stock.symbol, stock.market, false)
                return (
                  <div
                    key={stock.id}
                    draggable={!watchSearch && stockListFilter === '' && !watchlistOnlyAlerts && watchGroupFilter === '__all__'}
                    onDragStart={(e) => {
                      if (watchSearch || stockListFilter !== '' || watchlistOnlyAlerts || watchGroupFilter !== '__all__') return
                      watchDragSnapshotRef.current = stocks
                      setDraggingWatchStockId(stock.id)
                      e.dataTransfer.effectAllowed = 'move'
                    }}
                    onDragOver={(e) => {
                      if (watchSearch || stockListFilter !== '' || watchlistOnlyAlerts || watchGroupFilter !== '__all__') return
                      e.preventDefault()
                      e.dataTransfer.dropEffect = 'move'
                      if (draggingWatchStockId != null) {
                        previewWatchlistReorder(draggingWatchStockId, stock.id)
                      }
                    }}
                    onDrop={(e) => {
                      if (watchSearch || stockListFilter !== '' || watchlistOnlyAlerts || watchGroupFilter !== '__all__') return
                      e.preventDefault()
                      if (draggingWatchStockId != null) commitWatchlistReorder()
                      setDraggingWatchStockId(null)
                      watchDragSnapshotRef.current = null
                    }}
                    onDragEnd={() => {
                      setDraggingWatchStockId(null)
                      watchDragSnapshotRef.current = null
                    }}
                    className={`group rounded-xl border border-border/40 bg-background/30 hover:bg-accent/20 transition-colors p-3 cursor-pointer ${draggingWatchStockId === stock.id ? 'opacity-60' : ''}`}
                    onClick={() => {
                      if (isSuppressCardClick()) return
                      openStockDetail(stock.symbol, stock.market, stock.name, false)
                    }}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 min-w-0">
                          <span className={`text-[9px] px-1 py-0.5 rounded ${marketBadge(stock.market).style}`}>
                            {marketBadge(stock.market).label}
                          </span>
                          {stock.group_name && (
                            <span className="text-[9px] px-1 py-0.5 rounded bg-primary/10 text-primary">
                              {stock.group_name}
                            </span>
                          )}
                          <button
                            className="font-mono text-[12px] font-semibold text-foreground hover:text-primary"
                            onClick={(e) => { e.stopPropagation(); openStockDetail(stock.symbol, stock.market, stock.name, false) }}
                          >
                            {stock.symbol}
                          </button>
                          <button
                            className="text-[12px] text-muted-foreground truncate hover:text-primary"
                            onClick={(e) => { e.stopPropagation(); openStockDetail(stock.symbol, stock.market, stock.name, false) }}
                          >
                            {stock.name}
                          </button>
                        </div>
                      </div>
                      <div className="text-right">
                        <div className={`font-mono text-[14px] font-bold leading-tight ${changeColor}`}>
                          {quote?.current_price != null ? quote.current_price.toFixed(2) : '--'}
                        </div>
                        <div className={`font-mono text-[11px] leading-tight ${changeColor}`}>
                          {quote?.change_pct != null ? `${quote.change_pct >= 0 ? '+' : ''}${quote.change_pct.toFixed(2)}%` : '--'}
                        </div>
                      </div>
                    </div>

                    <div className="mt-2">
                      {(suggestion || kline) ? (
                        <SuggestionBadge
                          suggestion={suggestion}
                          stockName={stock.name}
                          stockSymbol={stock.symbol}
                          kline={kline}
                          market={stock.market}
                          hasPosition={false}
                        />
                      ) : (
                        <div className="text-[11px] text-muted-foreground/70 py-2">暂无技术面/AI 分析</div>
                      )}
                    </div>

                    <div className="mt-2 pt-2 border-t border-border/30 flex items-center justify-between gap-2">
                      <div className="flex items-center gap-1 flex-wrap">
                        {stock.agents && stock.agents.length > 0 ? (
                          <Badge variant="secondary" className="text-[10px]">{stock.agents.length} Agent</Badge>
                        ) : (
                          <span className="text-[10px] text-muted-foreground/60">未配置 Agent</span>
                        )}
                        {runningAgents[stock.id] && (
                          <span className="inline-flex items-center gap-1 text-[10px] text-amber-600">
                            <span className="w-3 h-3 border-2 border-current/30 border-t-current rounded-full animate-spin" />
                            {agents.find(a => a.name === runningAgents[stock.id])?.display_name || runningAgents[stock.id]}
                          </span>
                        )}
                      </div>
                      <div className="flex items-center gap-1" onClick={e => e.stopPropagation()}>
                        <Button variant="ghost" size="sm" onClick={() => openStockDetail(stock.symbol, stock.market, stock.name, false)}>详情</Button>
                        <WealthActionMenu label={`${stock.name}更多操作`} actions={[
                          { label: 'K 线与技术指标', onSelect: () => openKlineDialog(stock.symbol, stock.market, stock.name, false) },
                          { label: '相关新闻', onSelect: () => openNewsDialog(stock.name) },
                          { label: '深度分析', onSelect: () => openDeepAnalysis(stock.id, stock.symbol, stock.name) },
                          { label: 'Agent 配置与运行', onSelect: () => setAgentDialogStock(stock) },
                          { label: '移除关注', destructive: true, onSelect: () => setRemoveWatchStock(stock) },
                        ]} />
                      </div>
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}

      </div>
      {viewTab === 'plans' && <div>
        <WealthPanel mode="dca" accountId={accountFilter} privateMode={privateMode} focusPlanId={focusPlanId} onFocusPlanHandled={() => setFocusPlanId(null)} onOpenRecords={() => openWorkspacePanel('records')} onChanged={handleWealthChanged} refreshKey={wealthRefreshKey} />
      </div>}
      <Dialog open={workspacePanel !== null} onOpenChange={open => { if (!open) setWorkspacePanel(null) }}>
        <DialogContent className="wealth-workspace wealth-action-panel" onCloseAutoFocus={e => {
          e.preventDefault()
          const returnTarget = panelFallbackRef.current || workspaceRef.current?.querySelector<HTMLButtonElement>('.wealth-tabs [aria-current]')
          returnTarget?.focus()
        }}>
          <DialogHeader><DialogTitle>{workspacePanel === 'analysis' ? '全账户组合分析' : workspacePanel === 'records' ? '统一账本' : '账户管理'}</DialogTitle><DialogDescription>{workspacePanel === 'analysis' ? '分析范围为全部账户，按需生成建议并显示生成时间。' : '在当前账户范围内查看记录或管理设置。'}</DialogDescription></DialogHeader>
          {workspacePanel !== 'analysis' && <label className="flex items-center gap-3 text-sm mb-4">账户<select aria-label="侧栏账户" className="border border-border rounded-lg p-2 bg-card" value={panelAccountId ?? ''} onChange={e => setPanelAccountId(e.target.value ? Number(e.target.value) : null)}><option value="">全部账户</option>{portfolio?.accounts.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}</select></label>}
          {workspacePanel === 'accounts' && <div className="flex flex-wrap gap-2 mb-4"><Button onClick={() => { setWorkspacePanel(null); openAccountDialog() }}>新增账户</Button>{panelAccountId !== null && <Button variant="secondary" onClick={() => { setWorkspacePanel(null); openAccountDialog(accounts.find(a => a.id === panelAccountId)) }}>编辑名称与余额</Button>}</div>}
          {workspacePanel && <WealthPanel embedded mode={workspacePanel === 'analysis' ? 'overview' : workspacePanel} accountId={panelAccountId} privateMode={privateMode} onChanged={handleWealthChanged} refreshKey={wealthRefreshKey} onOpenRecords={() => setWorkspacePanel('records')} />}
        </DialogContent>
      </Dialog>
      <Dialog open={recordPickerOpen} onOpenChange={setRecordPickerOpen}>
        <DialogContent className="max-w-lg"><DialogHeader><DialogTitle>记一笔交易</DialogTitle><DialogDescription>选择账户与持仓，进入原有加仓或减仓表单。</DialogDescription></DialogHeader>
          <div className="max-h-[55vh] overflow-y-auto space-y-3">{scopedAccounts.map(a => <div key={a.id}><h3 className="text-sm font-semibold mb-2">{a.name}</h3>{a.positions.map(p => <div key={p.id} className="flex items-center justify-between gap-3 py-2 border-b border-border"><span>{p.name} <span className="text-xs text-muted-foreground">{p.symbol}</span></span><div className="flex gap-2"><Button size="sm" variant="secondary" onClick={() => { setRecordPickerOpen(false); openTradeDialog(a, p, 'buy') }}>加仓</Button><Button size="sm" variant="secondary" onClick={() => { setRecordPickerOpen(false); openTradeDialog(a, p, 'sell') }}>减仓</Button></div></div>)}<Button variant="ghost" onClick={() => { setRecordPickerOpen(false); openPositionDialog(a.id) }}>＋ 新增持仓</Button></div>)}{!portfolio?.accounts.length && <Button onClick={() => { setRecordPickerOpen(false); openAccountDialog() }}>先添加账户</Button>}</div>
        </DialogContent>
      </Dialog>
      {/* Kline Dialog */}
      <KlineSummaryDialog
        open={klineDialogOpen}
        onOpenChange={setKlineDialogOpen}
        symbol={klineDialogSymbol}
        market={klineDialogMarket}
        stockName={klineDialogName}
        hasPosition={klineDialogHasPosition}
        initialSummary={klineDialogInitialSummary as any}
      />

      {/* 加仓 / 减仓（记账，不自动下单） */}
      <Dialog open={!!tradeTarget} onOpenChange={(open) => !open && setTradeTarget(null)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>
              {tradeTarget?.action === 'buy' ? '加仓' : '减仓'} · {tradeTarget?.pos.name}
            </DialogTitle>
            <DialogDescription>
              当前持仓 {tradeTarget?.pos.quantity} 股，成本 {tradeTarget ? formatPrice(tradeTarget.pos.cost_price) : '--'}。
              确认后更新持仓与可用资金，并写入交易记录；不会自动下单。
            </DialogDescription>
          </DialogHeader>
          {tradeTarget && (
            <TradeGate
              symbol={tradeTarget.pos.symbol}
              side={tradeTarget.action}
              market={tradeTarget.pos.market || 'CN'}
              price={Number(tradeForm.price) || undefined}
              quantity={Number(tradeForm.quantity) || undefined}
              onGate={setTradeGate}
            />
          )}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label className="text-[11px]">数量（股/份）</Label>
              <Input
                type="number"
                value={tradeForm.quantity}
                onChange={(e) => setTradeForm({ ...tradeForm, quantity: e.target.value })}
              />
            </div>
            <div>
              <Label className="text-[11px]">成交价</Label>
              <Input
                type="number"
                step="0.001"
                value={tradeForm.price}
                onChange={(e) => setTradeForm({ ...tradeForm, price: e.target.value })}
              />
            </div>
            <div>
              <Label className="text-[11px]">日期</Label>
              <Input
                type="date"
                value={tradeForm.date}
                onChange={(e) => setTradeForm({ ...tradeForm, date: e.target.value })}
              />
            </div>
            <div>
              <Label className="text-[11px]">备注</Label>
              <Input
                value={tradeForm.note}
                placeholder="例如：突破加仓 / 止盈一部分"
                onChange={(e) => setTradeForm({ ...tradeForm, note: e.target.value })}
              />
            </div>
          </div>
          <div className="mt-3 grid grid-cols-2 gap-3">
            <div>
              <Label className="text-[11px]">手续费（手动，可空）</Label>
              <Input
                type="number"
                step="0.01"
                value={tradeForm.fee}
                placeholder={tradeFeePreview ? `自动 ${tradeFeePreview.total.toFixed(2)}` : '自动'}
                onChange={(e) => setTradeForm({ ...tradeForm, fee: e.target.value })}
              />
            </div>
            <div>
              <Label className="text-[11px]">成交后成本（手动，可空）</Label>
              <Input
                type="number"
                step="0.001"
                value={tradeForm.cost}
                placeholder="按券商实际成本"
                onChange={(e) => setTradeForm({ ...tradeForm, cost: e.target.value })}
              />
            </div>
          </div>
          {tradeFeePreview && (
            <div className="mt-3 rounded-md border border-border/50 bg-muted/30 p-2 text-[11px] text-muted-foreground">
              <div className="flex justify-between"><span>成交金额</span><span className="font-mono">{formatMoney(tradeFeePreview.gross)}</span></div>
              <div className="flex justify-between"><span>佣金（最低 5 元）</span><span className="font-mono">{formatMoney(tradeFeePreview.commission)}</span></div>
              <div className="flex justify-between"><span>印花税</span><span className="font-mono">{formatMoney(tradeFeePreview.stamp)}</span></div>
              <div className="flex justify-between"><span>过户费</span><span className="font-mono">{formatMoney(tradeFeePreview.transfer)}</span></div>
              <div className="mt-1 flex justify-between border-t border-border/40 pt-1 text-foreground">
                <span>{tradeFeePreview.manualFee != null ? '费用合计（手动）' : '费用合计'}</span>
                <span className="font-mono">{formatMoney(tradeFeePreview.total)}</span>
              </div>
              {tradeForm.cost !== '' && (
                <div className="mt-1 text-[10px] text-amber-600">成交后成本将按你填写的数值覆盖自动摊薄结果。</div>
              )}
            </div>
          )}
          {isGateBlocked(tradeGate) && (
            <label className="mt-3 flex items-start gap-2 text-[11px] text-amber-600">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={tradeGateAck}
                onChange={(e) => setTradeGateAck(e.target.checked)}
              />
              <span>我知道当前形态/策略不满足，仍要记录这笔交易</span>
            </label>
          )}
          <div className="mt-4 flex justify-end gap-2">
            <Button variant="secondary" size="sm" onClick={() => setTradeTarget(null)}>取消</Button>
            <Button
              size="sm"
              onClick={submitTrade}
              disabled={tradeBusy || (isGateBlocked(tradeGate) && !tradeGateAck)}
            >
              {tradeBusy ? '记录中…' : isGateBlocked(tradeGate) && !tradeGateAck ? '策略不建议' : '确认记录'}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* 止损 / 止盈（只保存，不自动建提醒） */}
      <Dialog open={!!riskTarget} onOpenChange={(open) => !open && setRiskTarget(null)}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>止损 / 止盈 · {riskTarget?.pos.name}</DialogTitle>
            <DialogDescription>
              保存后会进入 AI 建议上下文；系统不会自动创建价格提醒，需要时请前往「提醒」页面创建。
            </DialogDescription>
          </DialogHeader>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label className="text-[11px]">止损价</Label>
              <Input
                type="number"
                step="0.001"
                value={riskForm.stop}
                placeholder="留空表示不设"
                onChange={(e) => setRiskForm({ ...riskForm, stop: e.target.value })}
              />
            </div>
            <div>
              <Label className="text-[11px]">止盈价</Label>
              <Input
                type="number"
                step="0.001"
                value={riskForm.take}
                placeholder="留空表示不设"
                onChange={(e) => setRiskForm({ ...riskForm, take: e.target.value })}
              />
            </div>
          </div>
          {riskHint && (
            <div className="mt-3 rounded-md border border-amber-300/60 bg-amber-50 p-2 text-[11px] text-amber-800">
              {riskHint}
            </div>
          )}
          <div className="mt-4 flex justify-end gap-2">
            <Button variant="secondary" size="sm" onClick={() => setRiskTarget(null)}>关闭</Button>
            <Button size="sm" onClick={submitRisk} disabled={riskBusy}>
              {riskBusy ? '保存中…' : '保存'}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <StockInsightModal
        open={insightOpen}
        onOpenChange={setInsightOpen}
        symbol={insightSymbol}
        market={insightMarket}
        stockName={insightName}
        hasPosition={insightHasPosition}
      />

      {/* TradingAgents 深度分析弹窗 */}
      {deepAnalysisTarget && (
        <DeepAnalysisModal
          open={!!deepAnalysisTarget}
          onOpenChange={(open) => { if (!open) setDeepAnalysisTarget(null) }}
          stockId={deepAnalysisTarget.stockId}
          stockSymbol={deepAnalysisTarget.symbol}
          stockName={deepAnalysisTarget.name}
        />
      )}

      {/* Remove Watchlist Dialog */}
      <Dialog open={!!removeWatchStock} onOpenChange={(open) => { if (!open) setRemoveWatchStock(null) }}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>删除股票</DialogTitle>
            <DialogDescription>删除后将从系统中移除该股票及其关注配置</DialogDescription>
          </DialogHeader>
          {removeWatchStock && (
            <div className="space-y-4 mt-2">
              <div className="rounded-lg border border-border/40 bg-accent/20 p-3">
                <div className="text-[13px] font-semibold text-foreground">
                  {removeWatchStock.name}
                  <span className="ml-2 font-mono text-[12px] text-muted-foreground">{removeWatchStock.symbol}</span>
                </div>
                <div className="mt-1 text-[12px] text-muted-foreground">
                  {hasAnyPositionForStockId(removeWatchStock.id)
                    ? '该股票存在持仓，不能直接删除。请先在“持仓”Tab 删除持仓记录。'
                    : '删除后将不再出现在关注列表，同时会清理该股票关联的价格提醒。'}
                </div>
              </div>

              <div className="flex justify-end gap-2">
                <Button variant="ghost" onClick={() => setRemoveWatchStock(null)} disabled={removingWatchStock}>取消</Button>
                <Button
                  variant="destructive"
                  onClick={() => removeFromWatchlist(removeWatchStock)}
                  disabled={removingWatchStock || hasAnyPositionForStockId(removeWatchStock.id)}
                >
                  {hasAnyPositionForStockId(removeWatchStock.id) ? '请先删除持仓' : (removingWatchStock ? '处理中…' : '删除股票')}
                </Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      {/* Account Dialog */}
      <Dialog open={accountDialogOpen} onOpenChange={setAccountDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editAccountId ? '编辑账户' : '添加账户'}</DialogTitle>
            <DialogDescription>设置交易账户信息</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 mt-2">
            <div>
              <Label>账户名称</Label>
              <Input
                value={accountForm.name}
                onChange={e => setAccountForm({ ...accountForm, name: e.target.value })}
                placeholder="如：招商证券、华泰证券"
              />
            </div>
            <div>
              <Label>可用资金（元）</Label>
              <Input
                value={accountForm.available_funds}
                onChange={e => setAccountForm({ ...accountForm, available_funds: e.target.value })}
                placeholder="0"
                className="font-mono"
                inputMode="decimal"
              />
            </div>
            <div className="flex justify-end gap-2 pt-2">
              <Button variant="ghost" onClick={() => setAccountDialogOpen(false)}>取消</Button>
              <Button onClick={handleAccountSubmit} disabled={!accountForm.name}>
                {editAccountId ? '保存' : '创建'}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      {/* Position Dialog */}
      <Dialog
        open={positionDialogOpen}
        onOpenChange={(open) => {
          setPositionDialogOpen(open)
          if (!open) {
            setPositionSearchQuery('')
            setPositionSearchResults([])
            setShowPositionDropdown(false)
            setPositionSearchMarket('')
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editPositionId ? '编辑持仓' : '添加持仓'}</DialogTitle>
            <DialogDescription>
              {accounts.find(a => a.id === positionDialogAccountId)?.name} 账户持仓
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 mt-2">
            {editPositionId ? (
              <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-accent/30">
                <span className={`text-[9px] px-1.5 py-0.5 rounded ${marketBadge(positionForm.stock_market).style}`}>
                  {marketBadge(positionForm.stock_market).label}
                </span>
                <span className="font-mono text-[12px] text-muted-foreground">{positionForm.stock_symbol}</span>
                <span className="text-[13px] text-foreground">{positionForm.stock_name}</span>
              </div>
            ) : (
              <div>
                <div className="flex items-center gap-2 mb-2">
                  <Label className="mb-0">搜索股票</Label>
                  <div className="flex items-center gap-1">
                    {[
                      { value: '', label: '全部' },
                      { value: 'CN', label: 'A股' },
                      { value: 'HK', label: '港股' },
                      { value: 'US', label: '美股' },
                    ].map(opt => (
                      <button
                        key={opt.value}
                        type="button"
                        onClick={() => handlePositionSearchMarketChange(opt.value)}
                        className={`text-[11px] px-2 py-0.5 rounded transition-colors ${
                          positionSearchMarket === opt.value
                            ? 'bg-primary text-primary-foreground'
                            : 'bg-accent/50 text-muted-foreground hover:bg-accent'
                        }`}
                      >
                        {opt.label}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="relative" ref={positionDropdownRef}>
                  <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground/50" />
                  <Input
                    value={positionSearchQuery}
                    onChange={e => handlePositionSearchInput(e.target.value)}
                    onFocus={() => positionSearchResults.length > 0 && setShowPositionDropdown(true)}
                    placeholder={positionSearchMarket === 'HK' ? '代码或名称，如 00700 或 腾讯' : positionSearchMarket === 'US' ? '代码或名称，如 LI 或 理想汽车' : positionSearchMarket === 'CN' ? '代码或名称，如 600519 或 茅台' : '代码或名称，如 600519 / 00700 / AAPL'}
                    className="pl-9"
                    autoComplete="off"
                  />
                  {positionSearching && <span className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />}
                  {showPositionDropdown && positionSearchResults.length > 0 && (
                    <div className="absolute z-50 w-full mt-1 max-h-48 overflow-auto scrollbar card shadow-lg">
                      {positionSearchResults.map(item => (
                        <button
                          key={`${item.market}-${item.symbol}`}
                          type="button"
                          onClick={() => selectPositionStock(item)}
                          className="w-full flex items-center gap-2 px-3 py-2 text-[13px] hover:bg-accent/50 text-left transition-colors"
                        >
                          <span className={`text-[9px] px-1 py-0.5 rounded ${marketBadge(item.market).style}`}>
                            {marketBadge(item.market).label}
                          </span>
                          <span className="font-mono text-muted-foreground text-[12px]">{item.symbol}</span>
                          <span className="flex-1 text-foreground">{item.name}</span>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
                {positionForm.stock_symbol && (
                  <div className="mt-2 flex items-center gap-2">
                    <span className={`text-[9px] px-1.5 py-0.5 rounded ${marketBadge(positionForm.stock_market).style}`}>
                      {marketBadge(positionForm.stock_market).label}
                    </span>
                    <span className="font-mono text-[12px] text-muted-foreground">{positionForm.stock_symbol}</span>
                    <span className="text-[13px] text-foreground">{positionForm.stock_name}</span>
                    <button
                      type="button"
                      onClick={() => {
                        setPositionForm({ ...positionForm, stock_id: 0, stock_symbol: '', stock_name: '', stock_market: '' })
                        setPositionSearchQuery('')
                      }}
                      className="ml-1 text-muted-foreground hover:text-destructive"
                    >
                      <X className="w-3.5 h-3.5" />
                    </button>
                  </div>
                )}
              </div>
            )}
            <div className="grid grid-cols-2 gap-4">
              <div>
                <Label>成本价</Label>
                <Input
                  value={positionForm.cost_price}
                  onChange={e => setPositionForm({ ...positionForm, cost_price: e.target.value })}
                  placeholder="0.00"
                  className="font-mono"
                  inputMode="decimal"
                />
              </div>
              <div>
                <Label>持仓数量</Label>
                <Input
                  value={positionForm.quantity}
                  onChange={e => setPositionForm({ ...positionForm, quantity: e.target.value })}
                  placeholder="0"
                  className="font-mono"
                  inputMode="numeric"
                />
              </div>
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <Label>投入资金 <span className="text-muted-foreground/60 text-[11px]">(选填)</span></Label>
                <Input
                  value={positionForm.invested_amount}
                  onChange={e => setPositionForm({ ...positionForm, invested_amount: e.target.value })}
                  placeholder="选填"
                  className="font-mono"
                  inputMode="decimal"
                />
              </div>
              <div>
                <Label>交易风格 <span className="text-muted-foreground font-normal">(选填)</span></Label>
                <Select
                  value={positionForm.trading_style}
                  onValueChange={val => setPositionForm({ ...positionForm, trading_style: val === '__none__' ? '' : val })}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="不设置" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__none__">不设置</SelectItem>
                    <SelectItem value="short">短线 (1-5天)</SelectItem>
                    <SelectItem value="swing">波段 (1-4周)</SelectItem>
                    <SelectItem value="long">长线 (数月)</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
            {!editPositionId && (
              <div className="rounded-lg border border-border/50 bg-muted/20 p-2.5 text-[11px] space-y-1">
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={positionForm.record_trade}
                    onChange={(e) => setPositionForm({ ...positionForm, record_trade: e.target.checked, deduct_cash: e.target.checked ? positionForm.deduct_cash : false })}
                  />
                  同时记为一次「建仓买入」（写入交易流水）
                </label>
                {positionForm.record_trade && (
                  <label className="flex items-center gap-2 pl-5">
                    <input
                      type="checkbox"
                      checked={positionForm.deduct_cash}
                      onChange={(e) => setPositionForm({ ...positionForm, deduct_cash: e.target.checked })}
                    />
                    扣减可用资金（实际买入时勾选；录入已有仓位不要勾）
                  </label>
                )}
              </div>
            )}
            {!editPositionId && positionForm.record_trade && positionForm.stock_symbol && (
              <TradeGate
                symbol={positionForm.stock_symbol}
                side="buy"
                market={positionForm.stock_market || 'CN'}
                enabled={positionDialogOpen}
                onGate={setPositionGate}
              />
            )}
            {!editPositionId && isGateBlocked(positionGate) && (
              <label className="flex items-start gap-2 text-[11px] text-amber-600">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={positionGateAck}
                  onChange={(e) => setPositionGateAck(e.target.checked)}
                />
                <span>我知道当前形态/策略不满足，仍要建仓</span>
              </label>
            )}
            <div className="flex justify-end gap-2 pt-2">
              <Button variant="ghost" onClick={() => setPositionDialogOpen(false)}>取消</Button>
              <Button
                onClick={handlePositionSubmit}
                disabled={
                  !positionForm.cost_price ||
                  !positionForm.quantity ||
                  (!editPositionId && !positionForm.stock_id && !positionForm.stock_symbol) ||
                  (!editPositionId && positionForm.record_trade && isGateBlocked(positionGate) && !positionGateAck)
                }
              >
                {editPositionId ? '保存' : '添加'}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      {/* Agent Assignment Dialog */}
      <Dialog open={!!agentDialogStock} onOpenChange={open => !open && setAgentDialogStock(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>配置监控 Agent</DialogTitle>
            <DialogDescription>
              为 {agentDialogStock?.name}（{agentDialogStock?.symbol}）选择要监控的 Agent
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3 mt-2">
            {agents.length === 0 ? (
              <p className="text-[13px] text-muted-foreground py-4 text-center">暂无可用 Agent</p>
            ) : (
              agents.map(agent => {
                const stockAgent = agentDialogStock?.agents?.find(a => a.agent_name === agent.name)
                const isAssigned = !!stockAgent
                const isBatchMode = agent.execution_mode === 'batch'
                return (
                  <div key={agent.name} className="rounded-xl bg-accent/30 hover:bg-accent/50 transition-colors overflow-hidden">
                    <div className="flex items-center justify-between p-3.5">
                      <div className="flex items-center gap-3">
                        <div className={`w-2 h-2 rounded-full ${agent.enabled ? 'bg-emerald-500' : 'bg-border'}`} />
                        <div>
                          <div className="flex items-center gap-2">
                            <span className="text-[13px] font-medium text-foreground">{agent.display_name}</span>
                            <Badge variant="secondary" className="text-[9px]">
                              {isBatchMode ? '批量' : '逐只'}
                            </Badge>
                          </div>
                          <p className="text-[11px] text-muted-foreground mt-0.5">{agent.description}</p>
                        </div>
                      </div>
                      <Switch
                        checked={isAssigned}
                        onCheckedChange={() => agentDialogStock && toggleAgent(agentDialogStock, agent.name)}
                        disabled={!agent.enabled}
                      />
                    </div>
                    {isAssigned && isBatchMode && (
                      <div className="px-3.5 pb-3.5 pt-0">
                        <p className="text-[11px] text-muted-foreground">
                          调度、AI模型、通知渠道请在 <a href="/agents" className="text-primary hover:underline">Agent 配置</a> 页面统一设置
                        </p>
                      </div>
                    )}
                    {isAssigned && !isBatchMode && (
                      <div className="px-3.5 pb-3.5 pt-0 space-y-2.5">
                        {/* Schedule/Interval Select */}
                        <div className="flex items-center gap-2">
                          <Clock className="w-3.5 h-3.5 text-muted-foreground flex-shrink-0" />
                          <Select
                            value={stockAgent?.schedule || '__default__'}
                            onValueChange={val => agentDialogStock && updateStockAgentSchedule(agentDialogStock, agent.name, val === '__default__' ? '' : val)}
                          >
                            <SelectTrigger className="h-7 text-[11px] w-auto min-w-[140px] px-2.5 bg-accent/50 border-border/50">
                              <SelectValue placeholder="执行间隔" />
                            </SelectTrigger>
                            <SelectContent>
                              <SelectItem value="__default__">跟随全局</SelectItem>
                              <SelectItem value="*/1 9-15 * * 1-5">每 1 分钟</SelectItem>
                              <SelectItem value="*/3 9-15 * * 1-5">每 3 分钟</SelectItem>
                              <SelectItem value="*/5 9-15 * * 1-5">每 5 分钟</SelectItem>
                              <SelectItem value="*/10 9-15 * * 1-5">每 10 分钟</SelectItem>
                              <SelectItem value="*/15 9-15 * * 1-5">每 15 分钟</SelectItem>
                              <SelectItem value="*/30 9-15 * * 1-5">每 30 分钟</SelectItem>
                            </SelectContent>
                          </Select>
                          <span className="text-[10px] text-muted-foreground">交易时段</span>
                        </div>

                        {/* Schedule Preview */}
                        {(() => {
                          const eff = effectiveSchedule(agent, stockAgent)
                          const isFollowingGlobal = !(stockAgent?.schedule || '').trim() && !!(agent.schedule || '').trim()
                          const preview = eff ? schedulePreviewCache[eff] : null
                          const isLoading = eff ? !!schedulePreviewLoading[eff] : false
                          if (!eff) return null
                          return (
                            <div className="ml-[22px] rounded-lg border border-border/40 bg-background/30 px-2.5 py-2">
                              <div className="flex items-center justify-between">
                                <div className="text-[11px] text-muted-foreground">
                                  未来触发时间预览{isFollowingGlobal ? <span className="ml-1 opacity-70">(跟随全局)</span> : null}
                                </div>
                                {isLoading && (
                                  <span className="w-3 h-3 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
                                )}
                              </div>
                              {'error' in (preview || {}) ? (
                                <div className="mt-1 text-[11px] text-muted-foreground">{(preview as any).error}</div>
                              ) : (preview as SchedulePreview | undefined)?.next_runs?.length ? (
                                <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
                                  {(preview as SchedulePreview).next_runs.map((t, i) => (
                                    <span key={i} className="px-1.5 py-0.5 rounded border border-border/60 bg-accent/20 font-mono" title={t}>
                                      {formatPreviewTime(t, (preview as SchedulePreview).timezone)}
                                    </span>
                                  ))}
                                  {(preview as SchedulePreview).timezone ? (
                                    <span className="opacity-60">({(preview as SchedulePreview).timezone})</span>
                                  ) : null}
                                </div>
                              ) : (
                                <div className="mt-1 text-[11px] text-muted-foreground">—</div>
                              )}
                              <div className="mt-1 text-[10px] text-muted-foreground/70 font-mono">schedule: {eff}</div>
                            </div>
                          )
                        })()}

                        {/* AI Model Select */}
                        <div className="flex items-center gap-2">
                          <Cpu className="w-3.5 h-3.5 text-muted-foreground flex-shrink-0" />
                          <Select
                            value={stockAgent?.ai_model_id?.toString() ?? '__default__'}
                            onValueChange={val => agentDialogStock && updateStockAgentModel(agentDialogStock, agent.name, val === '__default__' ? null : parseInt(val))}
                          >
                            <SelectTrigger className="h-7 text-[11px] w-auto min-w-[140px] px-2.5 bg-accent/50 border-border/50">
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              <SelectItem value="__default__">系统默认</SelectItem>
                              {services.map(svc => (
                                <SelectGroup key={svc.id}>
                                  <SelectLabel>{svc.name}</SelectLabel>
                                  {svc.models.map(m => (
                                    <SelectItem key={m.id} value={m.id.toString()}>
                                      {m.name}{m.name !== m.model ? ` (${m.model})` : ''}
                                    </SelectItem>
                                  ))}
                                </SelectGroup>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>
                        {/* Notification Channels */}
                        {channels.length > 0 && (
                          <div className="flex items-center gap-2 flex-wrap">
                            <Bell className="w-3.5 h-3.5 text-muted-foreground flex-shrink-0" />
                            {channels.map(ch => {
                              const isSelected = (stockAgent?.notify_channel_ids || []).includes(ch.id)
                              return (
                                <button
                                  key={ch.id}
                                  onClick={() => agentDialogStock && toggleStockAgentChannel(agentDialogStock, agent.name, ch.id)}
                                  className={`text-[10px] px-2 py-0.5 rounded-md border transition-colors ${
                                    isSelected
                                      ? 'bg-primary/10 border-primary/30 text-primary font-medium'
                                      : 'bg-accent/30 border-border/50 text-muted-foreground hover:border-primary/30'
                                  }`}
                                >
                                  {ch.name}
                                </button>
                              )
                            })}
                            {(stockAgent?.notify_channel_ids || []).length === 0 && (
                              <span className="text-[10px] text-muted-foreground">系统默认</span>
                            )}
                          </div>
                        )}
                        {/* Trigger Button */}
                        <div className="flex items-center gap-2 pt-1">
                          <Button
                            variant="secondary" size="sm" className="h-7 text-[11px] px-2.5"
                            disabled={triggeringAgent === agent.name}
                            onClick={() => agentDialogStock && triggerStockAgent(agentDialogStock.id, agent.name)}
                          >
                            {triggeringAgent === agent.name ? (
                              <span className="w-3 h-3 border-2 border-current/30 border-t-current rounded-full animate-spin" />
                            ) : (
                              <Play className="w-3 h-3" />
                            )}
                            立即分析
                          </Button>
                        </div>
                      </div>
                    )}
                  </div>
                )
              })
            )}
          </div>
        </DialogContent>
      </Dialog>

      {/* Agent 分析结果弹窗 */}
      <Dialog open={!!agentResultDialog} onOpenChange={open => !open && setAgentResultDialog(null)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle className="text-base">{agentResultDialog?.title}</DialogTitle>
            <DialogDescription className="flex items-center gap-2 pt-1">
              {agentResultDialog?.should_alert ? (
                <Badge variant="default" className="text-[10px]">建议关注</Badge>
              ) : (
                <Badge variant="secondary" className="text-[10px]">无需关注</Badge>
              )}
              {agentResultDialog?.notified && (
                <Badge variant="outline" className="text-[10px]">已发送通知</Badge>
              )}
            </DialogDescription>
          </DialogHeader>
          <div className="mt-2 p-3 bg-accent/30 rounded-lg">
            <pre className="text-[13px] whitespace-pre-wrap font-sans leading-relaxed">
              {agentResultDialog?.content}
            </pre>
          </div>
          <div className="flex justify-end mt-2">
            <Button variant="outline" size="sm" onClick={() => setAgentResultDialog(null)}>
              关闭
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* 相关资讯弹窗 */}
      <Dialog open={newsDialogOpen} onOpenChange={setNewsDialogOpen}>
        <DialogContent className="max-w-2xl max-h-[85vh] flex flex-col">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Newspaper className="w-5 h-5 text-blue-500" />
              相关资讯
            </DialogTitle>
            <DialogDescription>
              {newsDialogSymbol
                ? `${newsDialogSymbol} 的相关新闻和公告`
                : '自选股相关新闻和公告（近 72 小时）'
              }
            </DialogDescription>
          </DialogHeader>

          {/* 股票筛选器 */}
          <div className="flex items-center gap-2 flex-wrap py-2 border-b">
            <span className="text-[12px] text-muted-foreground">筛选:</span>
            <button
              onClick={() => { setNewsDialogSymbol(''); loadNews() }}
              className={`text-[11px] px-2.5 py-1 rounded-md transition-colors ${
                !newsDialogSymbol
                  ? 'bg-primary text-primary-foreground'
                  : 'bg-accent/50 text-muted-foreground hover:bg-accent'
              }`}
            >
              全部
            </button>
            {stocks.slice(0, 10).map(stock => (
              <button
                key={stock.symbol}
                onClick={() => { setNewsDialogSymbol(stock.name); loadNews(stock.name) }}
                className={`text-[11px] px-2.5 py-1 rounded-md transition-colors ${
                  newsDialogSymbol === stock.name
                    ? 'bg-primary text-primary-foreground'
                    : 'bg-accent/50 text-muted-foreground hover:bg-accent'
                }`}
              >
                {stock.name}
              </button>
            ))}
            {stocks.length > 10 && (
              <span className="text-[10px] text-muted-foreground">+{stocks.length - 10}</span>
            )}
          </div>

          {/* 新闻列表 */}
          <div className="flex-1 overflow-y-auto min-h-0 py-2">
            {newsLoading ? (
              <div className="flex items-center justify-center py-12">
                <span className="w-5 h-5 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
                <span className="ml-2 text-[13px] text-muted-foreground">加载中...</span>
              </div>
            ) : news.length === 0 ? (
              <div className="text-center py-12 text-muted-foreground text-[13px]">
                暂无相关资讯
              </div>
            ) : (
              <div className="space-y-2">
                {news.map((item, idx) => (
                  <div
                    key={`${item.source}-${item.external_id}-${idx}`}
                    className="p-3 rounded-lg bg-accent/30 hover:bg-accent/50 transition-colors"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 mb-1.5">
                          <span className={`text-[10px] px-1.5 py-0.5 rounded ${
                            item.source === 'eastmoney' ? 'bg-amber-500/10 text-amber-600 dark:text-amber-400' :
                            item.source === 'eastmoney_news' ? 'bg-blue-500/10 text-blue-500' :
                            'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                          }`}>
                            {item.source_label}
                          </span>
                          {item.importance >= 2 && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-rose-500/10 text-rose-500">
                              重要
                            </span>
                          )}
                          <span className="text-[10px] text-muted-foreground">
                            {item.publish_time}
                          </span>
                        </div>
                        <a
                          href={item.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-[13px] font-medium text-foreground hover:text-primary transition-colors block"
                        >
                          {item.title}
                        </a>
                        {item.symbols.length > 0 && (
                          <div className="flex items-center gap-1.5 mt-2">
                            {item.symbols.slice(0, 5).map(sym => {
                              const stockInfo = stocks.find(s => s.symbol === sym)
                              const stockName = stockInfo?.name || sym
                              return (
                                <button
                                  key={sym}
                                  onClick={() => { setNewsDialogSymbol(stockName); loadNews(stockName) }}
                                  className="text-[10px] px-1.5 py-0.5 rounded bg-primary/10 text-primary font-mono hover:bg-primary/20 transition-colors"
                                >
                                  {stockName}
                                </button>
                              )
                            })}
                            {item.symbols.length > 5 && (
                              <span className="text-[10px] text-muted-foreground">+{item.symbols.length - 5}</span>
                            )}
                          </div>
                        )}
                      </div>
                      <a
                        href={item.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="flex-shrink-0 p-1.5 rounded-md hover:bg-accent transition-colors"
                        title="查看原文"
                      >
                        <ExternalLink className="w-4 h-4 text-muted-foreground" />
                      </a>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* 底部刷新按钮 */}
          <div className="flex items-center justify-between pt-2 border-t">
            <span className="text-[11px] text-muted-foreground">
              共 {news.length} 条资讯
            </span>
            <Button variant="secondary" size="sm" onClick={() => loadNews(newsDialogSymbol || undefined)} disabled={newsLoading}>
              <RefreshCw className={`w-3 h-3 ${newsLoading ? 'animate-spin' : ''}`} />
              刷新
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
