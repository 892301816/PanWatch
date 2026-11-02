import { fetchAPI } from './client'

export interface PortfolioDiagnostics {
  position_count: number
  total_market_value: number
  hhi: number
  max_weight: number
  by_market: Record<string, number>
  by_strategy: Record<string, number>
  total_unrealized_pnl: number
  alerts: string[]
}

export interface BenchmarkCurvePoint {
  date: string
  portfolio: number
  benchmark: number
}

export interface PortfolioBenchmark {
  empty?: boolean
  reason?: string
  portfolio_return?: number
  benchmark_return?: number
  excess_return?: number
  information_ratio?: number
  relative_drawdown?: number
  days?: number
  benchmark_code?: string
  benchmark_label?: string
  curve?: BenchmarkCurvePoint[]
}

export interface WealthGoals {
  annual_passive_income: number
  emergency_months: number
  annual_return_target: number
}

export interface WealthPosition {
  id: number
  stock_id: number
  symbol: string
  name: string
  market: string
  quantity: number
  cost_price: number
  stop_loss_price: number | null
  take_profit_price: number | null
  trading_style?: string | null
}

export interface DcaPlan {
  id: number
  account_id: number
  account_name: string
  stock_id: number
  symbol: string
  name: string
  amount: number
  frequency: 'monthly' | 'weekly' | 'biweekly'
  day_of_month: number
  day_of_week: number
  start_date: string
  next_run_date: string
  enabled: boolean
  executed_count: number
  total_invested: number
  note: string
}

export interface WealthAccount {
  id: number
  name: string
  available_funds: number
  strategy_type: '' | 'aggr' | 'steady'
  strategy_label: string
  risk_policy: Record<string, number>
  capital_plan: Record<string, unknown>
  fee_policy: Record<string, number>
  dividend_reinvest: boolean
  positions: WealthPosition[]
  dca_plans: DcaPlan[]
}

export interface TradeRecord {
  id: number
  account_id: number
  stock_id: number | null
  symbol: string
  name: string
  action: 'buy' | 'sell' | 'dca' | 'dividend' | 'reinvest'
  quantity: number
  price: number
  amount: number
  commission: number
  stamp_duty: number
  transfer_fee: number
  total_fee: number
  realized_pnl: number
  cash_change: number
  trade_date: string
  note: string
  source: string
  created_at: string
}

export interface DividendRecord {
  id: number
  account_id: number
  account_name: string
  symbol: string
  name: string
  ex_date: string
  quantity: number
  per_share: number
  amount: number
  action: 'cash' | 'reinvest'
  shares_added: number
  price: number
  plan_profile: string
}

export interface WealthOverview {
  goals: WealthGoals
  accounts: WealthAccount[]
  dca_due: Array<{
    id: number
    account_id: number
    account_name: string
    stock_id: number
    symbol: string
    name: string
    amount: number
    frequency: string
    next_run_date: string
    overdue: boolean
    executed_count: number
  }>
  trades: TradeRecord[]
}

export const portfolioApi = {
  /** 真实持仓组合诊断(集中度/分布/风险提示)。 */
  diagnostics: () => fetchAPI<PortfolioDiagnostics>('/portfolio/diagnostics'),

  // ── 理财化能力 ──────────────────────────────────────────────────────
  wealthOverview: () => fetchAPI<WealthOverview>('/portfolio/wealth/overview'),
  wealthAiAdvice: (modelId?: number) =>
    fetchAPI<{ content: string; generated_at: string; disclaimer: string; empty?: boolean; reason?: string }>(
      `/portfolio/wealth/ai-advice${modelId ? `?model_id=${modelId}` : ''}`,
      { method: 'POST', timeoutMs: 90000 },
    ),
  getGoals: () => fetchAPI<WealthGoals>('/portfolio/wealth/goals'),
  updateGoals: (data: Partial<WealthGoals>) =>
    fetchAPI<WealthGoals>('/portfolio/wealth/goals', {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  updateStrategy: (
    accountId: number,
    data: {
      strategy_type?: '' | 'aggr' | 'steady'
      risk_policy?: Record<string, number>
      capital_plan?: Record<string, unknown>
      fee_policy?: Record<string, number>
      dividend_reinvest?: boolean
    },
  ) =>
    fetchAPI<WealthAccount>(`/portfolio/accounts/${accountId}/strategy`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  updatePositionRisk: (
    positionId: number,
    data: { stop_loss_price: number | null; take_profit_price: number | null },
  ) =>
    fetchAPI<{ stop_loss_price: number | null; take_profit_price: number | null; hint: string }>(
      `/portfolio/positions/${positionId}/risk`,
      { method: 'PUT', body: JSON.stringify(data) },
    ),
  createTrade: (data: {
    account_id: number
    stock_id: number
    action: 'buy' | 'sell'
    quantity: number
    price: number
    trade_date?: string
    note?: string
    fee_override?: number
    cost_override?: number
  }) =>
    fetchAPI<{
      trade_id: number
      action: string
      quantity: number
      price: number
      amount: number
      fees: { commission: number; stamp_duty: number; transfer_fee: number; total: number }
      cash_change: number
      realized_pnl: number
      available_funds: number
      warning?: string
    }>('/portfolio/trades', { method: 'POST', body: JSON.stringify(data) }),
  trades: (params?: { account_id?: number; action?: string; limit?: number }) => {
    const q = new URLSearchParams()
    if (params?.account_id) q.set('account_id', String(params.account_id))
    if (params?.action) q.set('action', params.action)
    if (params?.limit) q.set('limit', String(params.limit))
    return fetchAPI<TradeRecord[]>(`/portfolio/trades${q.toString() ? `?${q}` : ''}`)
  },
  dcaList: (accountId?: number) =>
    fetchAPI<DcaPlan[]>(`/portfolio/dca${accountId ? `?account_id=${accountId}` : ''}`),
  dcaDue: (days = 1) => fetchAPI<WealthOverview['dca_due']>(`/portfolio/dca/due?days=${days}`),
  dcaCreate: (data: {
    account_id: number
    stock_id: number
    amount: number
    frequency: 'monthly' | 'weekly' | 'biweekly'
    day_of_month?: number
    day_of_week?: number
    start_date?: string
    enabled?: boolean
    note?: string
  }) =>
    fetchAPI<DcaPlan>('/portfolio/dca', { method: 'POST', body: JSON.stringify(data) }),
  dcaUpdate: (id: number, data: Record<string, unknown>) =>
    fetchAPI<DcaPlan>(`/portfolio/dca/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  dcaDelete: (id: number) => fetchAPI<{ success: boolean }>(`/portfolio/dca/${id}`, { method: 'DELETE' }),
  dcaExecute: (id: number, data: { price: number; quantity?: number; trade_date?: string; note?: string }) =>
    fetchAPI<Record<string, unknown>>(`/portfolio/dca/${id}/execute`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  dividendRecords: (limit = 50) =>
    fetchAPI<DividendRecord[]>(`/portfolio/dividends/records?limit=${limit}`),
  processDividends: (days = 3) =>
    fetchAPI<{ recorded: number }>(`/portfolio/dividends/process?days=${days}`, { method: 'POST' }),

  /** 组合 vs 基准(超额/信息比率/相对回撤 + 归一化曲线)。 */
  benchmark: (params?: { days?: number; benchmark?: string }) =>
    fetchAPI<PortfolioBenchmark>(
      `/portfolio/benchmark?days=${params?.days ?? 60}&benchmark=${encodeURIComponent(params?.benchmark ?? '000300')}`,
      { timeoutMs: 60000 },
    ),

  /** 个股对组合收益的贡献(谁拖累/贡献)。 */
  attribution: (days = 60) =>
    fetchAPI<{ items: AttributionItem[] }>(`/portfolio/attribution?days=${days}`, { timeoutMs: 60000 }),

  /** 组合 AI 体检(叙述结论 + 调仓建议)。 */
  aiReview: () => fetchAPI<PortfolioAiReview>('/portfolio/ai-review', { method: 'POST', timeoutMs: 60000 }),
}

export interface AttributionItem {
  symbol: string
  name: string
  market: string
  return_pct: number
  weight_pct: number
  contribution_pct: number
}

export interface PortfolioAiReview {
  empty?: boolean
  reason?: string
  content?: string
  top?: AttributionItem[]
  worst?: AttributionItem[]
}
