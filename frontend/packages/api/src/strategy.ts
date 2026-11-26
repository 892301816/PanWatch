import { fetchAPI } from './client'

export interface StrategyCondition {
  field: string
  op: string
  value: unknown
  children?: StrategyCondition[]
  logic?: 'all' | 'any'
  offset?: number
  consecutive?: number
}

export interface StrategyConfig {
  chart_script?: string
  template_id?: string
  params?: Record<string, unknown>
  execution?: { stop_loss_pct?: number; take_profit_pct?: number; max_hold_days?: number }
  scoring?: Record<string, number>
  directions?: Record<string, 'high' | 'low'>
  factor_version?: string
}

export interface StrategyField {
  source_id?: string
  key: string
  label: string
  unit: string
  group?: string
  description?: string
  formula?: string
  dependency?: string
  warmup?: number
  version?: string
  kind?: string
}

export interface StrategyPlaybook {
  config?: StrategyConfig
  id: number
  scope: 'global' | 'stock'
  symbol: string
  name: string
  buy: StrategyCondition[]
  sell: StrategyCondition[]
  avoid: StrategyCondition[]
  note: string
  enabled: boolean
  is_override: boolean
  market?: string
  timing?: StrategyTiming
  revision_id?: number
  version?: number
  rules_hash?: string
}

export type StrategyTiming = 'completed_daily' | 'intraday_preview'

export interface StrategyDraftInput {
  config?: StrategyConfig
  draft_id?: number
  scope: 'global' | 'stock'
  symbol: string
  market: string
  name: string
  buy: StrategyCondition[]
  sell: StrategyCondition[]
  avoid: StrategyCondition[]
  note: string
  timing?: StrategyTiming
  questions?: string[]
  enabled?: boolean
  source_text?: string
}

export interface StrategyDraft extends StrategyDraftInput {
  archived_at?: string | null
  id: number
  draft_id: number
  status: 'draft'
  rules_hash: string
  questions: string[]
  source_text?: string
}

export interface StrategyLibraryItem extends StrategyDraft {
  bindings: StrategyPlaybook[]
  tested: boolean
  last_backtest?: StrategyReportSummary | null
}

export interface StrategyReportSummary {
  id: number
  draft_id?: number
  status: string
  symbol: string
  market: string
  start: string
  end: string
  rules_hash?: string
  total_return?: number | null
  max_drawdown?: number | null
  error?: string
}

export interface StrategyScanItem extends Partial<StrategyCheck> {
  symbol: string
  name: string
  market: string
  status: 'MATCH' | 'BLOCK' | 'WAIT' | 'UNKNOWN'
  summary: string
}

export interface StrategyDecisionInput {
  account_id?: number
  symbol: string
  market: string
  side: 'buy' | 'sell'
  price: number
  quantity: number
  use_ai?: boolean
  model_id?: number
}

export interface StrategyDecisionCheck {
  decision_id: number
  status: 'PASS' | 'BLOCK' | 'UNKNOWN' | 'NO_POLICY'
  verdict?: string
  summary?: string
  reasons?: string[]
  expires_at?: string
  rules_hash?: string
  revision_id?: number | null
  input_hash?: string
  created_at?: string
  data_as_of?: string
  expired?: boolean
  exception_reason?: string
  linked_trade_id?: number | null
  input?: Partial<StrategyDecisionInput> & { account?: Record<string, unknown>; policy?: Record<string, unknown> | null }
}

export interface StrategyPreset {
  config?: StrategyConfig
  params?: Array<{id:string;label:string;type:string;default:number|boolean;min?:number;max?:number}>
  /** 后端归一后的统一条件行：开关 + 字段 + 比较符 + 数值 + 单位 */
  paramRows?: Array<{
    key: string
    gate?: string | null
    label: string
    control: 'toggle' | 'number'
    op?: string
    unit?: string
    default?: number | boolean
    min?: number
    max?: number
    step?: number
    hint?: string
  }>
  source?: string
  id: string
  name: string
  description: string
  note: string
  buy: StrategyCondition[]
  sell: StrategyCondition[]
  avoid: StrategyCondition[]
}

export interface StrategyPattern {
  tags: string[]
  summary: string
  risk_hint: string
  event?: Record<string, unknown> | null
}

export interface StrategyCheckItem {
  field: string
  label: string
  group?: string
  op: string
  expect: string
  actual: string
  ok: boolean | null
  known?: boolean
  text?: string
}

export interface StrategyCheck {
  symbol: string
  name?: string
  scope?: string
  note?: string
  verdict: string
  buy_hits: number
  buy_total: number
  sell_hits: number
  sell_total: number
  avoid_hits?: number
  avoid_total?: number
  buy_checks: StrategyCheckItem[]
  sell_checks: StrategyCheckItem[]
  avoid_checks: StrategyCheckItem[]
  pattern?: StrategyPattern
  price?: number
  as_of?: string
  estimated?: boolean
  has_playbook: boolean
}

export interface GateAI {
  enabled: boolean
  ok: boolean
  tone: string
  verdict: string
  reason: string
  risk: string
  content: string
  elapsed_ms: number
  error: string
}

export interface StrategyGate {
  symbol: string
  name: string
  market: string
  side: 'buy' | 'sell'
  /** 可操作 / 接近 / 不建议 / 无策略 */
  verdict: string
  rule_verdict: string | null
  summary: string
  reasons: string[]
  has_playbook: boolean
  playbook_name: string
  playbook_note: string
  buy_checks: StrategyCheckItem[]
  sell_checks: StrategyCheckItem[]
  avoid_checks: StrategyCheckItem[]
  pattern: StrategyPattern
  price?: number
  as_of?: string
  estimated?: boolean
  ai: GateAI | null
  cached: boolean
  elapsed_ms: number
}

export interface BacktestTrade {
  entry_date: string
  entry_price: number
  exit_date: string
  exit_price: number
  pnl: number
  pnl_pct: number
  hold_days: number
  entry_signal_date?: string
  exit_signal_date?: string
  quantity?: number
  buy_fees?: number | Record<string, number>
  sell_fees?: number | Record<string, number>
  entry_conditions?: string[]
  exit_conditions?: string[]
  trigger_conditions?: { entry?: Record<string, unknown[]>; exit?: Record<string, unknown[]> }
}

export interface BacktestOrder {
  signal_date: string
  execution_date?: string | null
  side: 'buy' | 'sell'
  status: 'filled' | 'unfilled'
  price?: number
  fill_price?: number
  reference_price?: number
  quantity?: number
  fees?: number | Record<string, number> | null
  conditions?: unknown[] | Record<string, unknown[]>
  reason?: string
}

export interface BacktestCurvePoint {
  date: string
  price_pct: number
  equity_pct: number
  buy?: boolean
  sell?: boolean
}

export interface BacktestMetrics {
  win_rate: number
  avg_hold_days: number
  profit_factor: number | null
  best_trade_pct: number
  worst_trade_pct: number
  avg_win_pct: number
  avg_loss_pct: number
  max_drawdown: number
  annual_return: number
}

export interface StudioBar {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  fast?: number | null
  slow?: number | null
  template_entry?: boolean | number | null
  template_exit?: boolean | number | null
  /** Pine 脚本里每个 plot 的名字都会成为 bar 上的动态字段（值为数值或 null）。 */
  [key: string]: unknown
}

/** Pine `plot()` 的绘图声明：名字就是 bar 上的动态字段名。 */
export interface StudioPlotSpec {
  name: string
  overlay: boolean
  style: string
  color: string | null
  linewidth: number
}

/** Pine `input.*()` 的入参声明，前端按 kind 渲染控件。 */
export type StudioInputKind =
  | 'int'
  | 'float'
  | 'bool'
  | 'string'
  | 'color'
  | 'source'
  | 'timeframe'
  | 'session'
  | 'symbol'
  | 'time'
  | 'price'
  | 'text'
  | 'raw'

export interface StudioInputMeta {
  id: string
  name: string
  kind: StudioInputKind
  default: unknown
  minval?: number | null
  maxval?: number | null
  step?: number | null
  group?: string | null
  tooltip?: string | null
  options?: unknown[] | null
}

/** Pine 策略统计，口径由后端 statistics.py 统一计算（字段可能缺省）。 */
export interface StudioStats {
  [key: string]: number | undefined
  total_return_pct?: number
  annualized_return_pct?: number
  max_drawdown?: number
  max_drawdown_pct?: number
  sharpe?: number
  sortino?: number
  calmar?: number
  profit_factor?: number
  win_rate_pct?: number
  trades?: number
  wins?: number
  losses?: number
  flats?: number
  avg_win?: number
  avg_loss?: number
  expectancy?: number
  total_fees?: number
  initial_capital?: number
  final_equity?: number
  avg_holding_bars?: number
  min_holding_bars?: number
  max_holding_bars?: number
  max_consecutive_wins?: number
  max_consecutive_losses?: number
  trades_per_month?: number
  years?: number
}

/** Pine 策略的一笔成交（逐笔由后端模拟撮合得出）。 */
export interface StudioTrade {
  entry_id: string
  direction: 1 | -1
  size: number
  entry_price: number
  exit_price: number
  entry_date: string
  exit_date: string
  profit: number
  commission: number
  bars: number
  max_runup: number
  max_drawdown: number
}

/** `/strategy/chart-preview` 的完整响应（Pine 脚本会带出绘图、参数与统计）。 */
export interface ChartPreviewResult {
  source: string
  adjustment: string
  engine: 'v1' | 'pine'
  plots: StudioPlotSpec[]
  inputs: StudioInputMeta[]
  warnings: string[]
  script_title: string | null
  script_kind: 'indicator' | 'strategy' | null
  stats: StudioStats
  trades: StudioTrade[]
  equity_curve: number[]
  bars: StudioBar[]
}

export interface StudioDiagnostic {
  line: number
  column: number
  message: string
}

/** `/strategy/chart-compile` 的响应：只做语法检查，不取行情。 */
export interface ChartCompileResult {
  ok: boolean
  engine: 'v1' | 'pine'
  diagnostics: StudioDiagnostic[]
  title: string | null
  kind: string | null
  warnings: string[]
  inputs: StudioInputMeta[]
  plots: StudioPlotSpec[]
}

export interface BacktestResult {
  chart_bars?: StudioBar[]
  symbol: string
  start: string
  end: string
  bars: number
  trades: BacktestTrade[]
  trade_count: number
  win_rate: number
  total_return: number
  buy_hold_return: number
  excess_return: number
  current_status: string
  curve: BacktestCurvePoint[]
  rules?: { buy: StrategyCondition[]; sell: StrategyCondition[]; avoid: StrategyCondition[] }
  rule_text?: string
  explain?: string
  metrics?: BacktestMetrics
  pattern?: StrategyPattern
  current_features?: Record<string, number | null>
  error?: string
  backtest_id?: number
  draft_id?: number
  revision_id?: number
  rules_hash?: string
  rules_snapshot?: Record<string, unknown>
  orders?: BacktestOrder[]
  open_position?: Record<string, unknown> | null
  initial_capital?: number
  final_cash?: number
  final_equity?: number
  data_info?: {
    source?: string
    adjustment?: string
    actual_start?: string
    actual_end?: string
    warmup_bars?: number
    data_hash?: string
    cost_config?: Record<string, number>
    limitations?: string[]
  }
}

export interface BacktestRecord {
  id: number
  backtest_id: number
  status: 'running' | 'succeeded' | 'failed'
  config: Record<string, unknown>
  input_snapshot: Record<string, unknown>
  result: BacktestResult | null
  error: string
}

export const strategyApi = {
  library: () => fetchAPI<{ items: StrategyLibraryItem[]; deleted_items?: StrategyLibraryItem[]; bindings: StrategyPlaybook[]; fields: StrategyField[]; presets: StrategyPreset[] }>('/strategy/library?include_deleted=true'),
  deleteLibrary: (id: number, expected_hash: string, disable_bindings = false, binding_ids: number[] = []) => fetchAPI<{ deleted: number; disabled_bindings: number[] }>(`/strategy/library/${id}`, { method: 'DELETE', body: JSON.stringify({ expected_hash, disable_bindings, binding_ids }) }),
  restoreLibrary: (id: number) => fetchAPI<StrategyDraft>(`/strategy/library/${id}/restore`, { method: 'POST', body: '{}' }),
  setBindingEnabled: (id: number, enabled: boolean) => fetchAPI<StrategyPlaybook>(`/strategy/bindings/${id}/enabled`, { method: 'POST', body: JSON.stringify({ enabled }) }),
  applyLibrary: (id: number, payload: { scope: 'stock' | 'global'; symbol: string; expected_hash: string }) => fetchAPI<StrategyPlaybook>(`/strategy/library/${id}/apply`, { method: 'POST', body: JSON.stringify(payload) }),
  scanLibrary: (id: number, payload: { symbols: Array<{ symbol: string; market: string; name: string }>; expected_hash: string; allow_network: boolean }) => fetchAPI<{ items: StrategyScanItem[]; draft_id: number; rules_hash: string; as_of: string }>(`/strategy/library/${id}/scan`, { method: 'POST', body: JSON.stringify(payload), timeoutMs: 180000 }),
  backtests: (draft_id?: number) => fetchAPI<{ items: StrategyReportSummary[] }>(`/strategy/backtests${draft_id ? `?draft_id=${draft_id}` : ''}`),
  drafts: (symbol: string, market = 'CN') => fetchAPI<{ items: StrategyDraft[] }>(`/strategy/drafts?${new URLSearchParams({ symbol, market })}`),
  saveDraft: (payload: StrategyDraftInput) => fetchAPI<StrategyDraft>('/strategy/drafts', { method: 'POST', body: JSON.stringify(payload) }),
  aiDraft: (payload: { draft_id?: number; expected_hash?: string; text: string; scope: 'global' | 'stock'; symbol: string; market: string; model_id?: number }) => fetchAPI<StrategyDraft>('/strategy/drafts/ai', { method: 'POST', body: JSON.stringify(payload), timeoutMs: 90000 }),
  activateDraft: (id: number) => fetchAPI<StrategyPlaybook>(`/strategy/drafts/${id}/activate`, { method: 'POST', body: '{}' }),
  backtestRecord: (id: number) => fetchAPI<BacktestRecord>(`/strategy/backtests/${id}`),
  decision: (payload: StrategyDecisionInput) => fetchAPI<StrategyDecisionCheck>('/strategy/decisions', { method: 'POST', body: JSON.stringify(payload), timeoutMs: 60000 }),
  explainDecision: (id: number, model_id?: number) => fetchAPI<StrategyDecisionCheck>(`/strategy/decisions/${id}/explain`, { method: 'POST', body: JSON.stringify({ model_id }), timeoutMs: 45000 }),
  decisions: (symbol: string, market = 'CN', account_id?: number) => fetchAPI<{ items: StrategyDecisionCheck[] }>(`/strategy/decisions?${new URLSearchParams({ symbol, market, ...(account_id != null ? { account_id: String(account_id) } : {}) })}`),
  playbooks: () =>
    fetchAPI<{
      items: StrategyPlaybook[]
      fields: StrategyField[]
      presets: StrategyPreset[]
      defaults: { buy: StrategyCondition[]; sell: StrategyCondition[]; avoid: StrategyCondition[] }
    }>('/strategy/playbooks'),

  presets: () =>
    fetchAPI<{ presets: StrategyPreset[]; fields: StrategyField[] }>('/strategy/presets'),

  save: (payload: {
    scope: 'global' | 'stock'
    symbol?: string
    name?: string
    buy: StrategyCondition[]
    sell: StrategyCondition[]
    avoid?: StrategyCondition[]
    note?: string
    enabled?: boolean
    market?: string
    timing?: StrategyTiming
  }) => fetchAPI<StrategyPlaybook>('/strategy/playbooks', { method: 'POST', body: JSON.stringify(payload) }),

  remove: (id: number) =>
    fetchAPI<{ deleted: number }>(`/strategy/playbooks/${id}`, { method: 'DELETE' }),

  check: (symbol: string, market = 'CN') =>
    fetchAPI<StrategyCheck>('/strategy/check', {
      method: 'POST',
      body: JSON.stringify({ symbol, market }),
      timeoutMs: 30000,
    }),

  board: (symbols: string[]) =>
    fetchAPI<{ items: StrategyCheck[] }>('/strategy/board', {
      method: 'POST',
      body: JSON.stringify({ symbols }),
      timeoutMs: 60000,
    }),

  /** 买卖前体检:规则 + 形态 + AI(默认启用) */
  gate: (payload: {
    symbol: string
    side?: 'buy' | 'sell'
    market?: string
    price?: number
    quantity?: number
    use_ai?: boolean
  }) =>
    fetchAPI<StrategyGate>('/strategy/gate', {
      method: 'POST',
      body: JSON.stringify({ side: 'buy', market: 'CN', use_ai: true, ...payload }),
      timeoutMs: 45000,
    }),

  backtest: (payload: {
    symbol: string
    market?: string
    buy?: StrategyCondition[]
    sell?: StrategyCondition[]
    avoid?: StrategyCondition[]
    days?: number
    draft_id?: number
    revision_id?: number
    start_date?: string
    end_date?: string
    initial_capital?: number
    commission_rate?: number
    min_commission?: number
    stamp_duty_rate?: number
    transfer_fee_rate?: number
    slippage_bps?: number
  }) =>
    fetchAPI<BacktestResult>('/strategy/backtest', {
      method: 'POST',
      body: JSON.stringify(payload),
      timeoutMs: 60000,
    }),

  /** 图表预览：Pine 脚本返回绘图声明、入参、统计与逐笔成交。 */
  chartPreview: (
    payload: {
      symbol: string
      market?: string
      instrument?: 'stock' | 'index'
      interval?: '1d' | '1w' | '1m'
      script: string
      price_only?: boolean
      draft_id?: number
      inputs?: Record<string, unknown> | null
    },
    options?: { signal?: AbortSignal },
  ) =>
    fetchAPI<ChartPreviewResult>('/strategy/chart-preview', {
      method: 'POST',
      timeoutMs: 90000,
      signal: options?.signal,
      body: JSON.stringify(payload),
    }),

  /** 只检查脚本语法：返回诊断、脚本类型、参数与绘图概要，不取行情。 */
  chartCompile: (payload: { script: string }) =>
    fetchAPI<ChartCompileResult>('/strategy/chart-compile', {
      method: 'POST',
      timeoutMs: 30000,
      body: JSON.stringify(payload),
    }),
}
