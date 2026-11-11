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
}

export interface StrategyPlaybook {
  id: number
  scope: 'global' | 'stock'
  symbol: string
  name: string
  buy: StrategyCondition[]
  sell: StrategyCondition[]
  note: string
  enabled: boolean
  is_override: boolean
}

export interface StrategyCheckItem {
  field: string
  label: string
  op: string
  expect: string
  actual: string
  ok: boolean
}

export interface StrategyCheck {
  symbol: string
  name?: string
  scope?: string
  verdict: string
  buy_hits: number
  buy_total: number
  sell_hits: number
  sell_total: number
  buy_checks: StrategyCheckItem[]
  sell_checks: StrategyCheckItem[]
  price?: number
  as_of?: string
  estimated?: boolean
  has_playbook: boolean
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
  rules?: { buy: StrategyCondition[]; sell: StrategyCondition[] }
  error?: string
}

export const strategyApi = {
  playbooks: () =>
    fetchAPI<{ items: StrategyPlaybook[]; fields: StrategyField[]; defaults: { buy: StrategyCondition[]; sell: StrategyCondition[] } }>(
      '/strategy/playbooks'
    ),

  save: (payload: {
    scope: 'global' | 'stock'
    symbol?: string
    name?: string
    buy: StrategyCondition[]
    sell: StrategyCondition[]
    note?: string
    enabled?: boolean
  }) => fetchAPI<StrategyPlaybook>('/strategy/playbooks', { method: 'POST', body: JSON.stringify(payload) }),

  remove: (id: number) =>
    fetchAPI<{ deleted: number }>(`/strategy/playbooks/${id}`, { method: 'DELETE' }),

  check: (symbol: string) =>
    fetchAPI<StrategyCheck>('/strategy/check', { method: 'POST', body: JSON.stringify({ symbol }), timeoutMs: 30000 }),

  board: (symbols: string[]) =>
    fetchAPI<{ items: StrategyCheck[] }>('/strategy/board', {
      method: 'POST',
      body: JSON.stringify({ symbols }),
      timeoutMs: 60000,
    }),

  backtest: (payload: { symbol: string; buy?: StrategyCondition[]; sell?: StrategyCondition[]; days?: number }) =>
    fetchAPI<BacktestResult>('/strategy/backtest', {
      method: 'POST',
      body: JSON.stringify(payload),
      timeoutMs: 60000,
    }),
}
