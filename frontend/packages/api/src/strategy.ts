import { fetchAPI } from './client'

export interface StrategyCondition {
  field: string
  op: string
  value: unknown
}

export interface StrategyField {
  key: string
  label: string
  unit: string
  group?: string
}

export interface StrategyPlaybook {
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
}

export interface StrategyPreset {
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
  ok: boolean
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

export interface BacktestResult {
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
}

export const strategyApi = {
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
  }) =>
    fetchAPI<BacktestResult>('/strategy/backtest', {
      method: 'POST',
      body: JSON.stringify(payload),
      timeoutMs: 60000,
    }),
}
