import { fetchAPI } from './client'

export interface ScreenerField {
  key: string
  label: string
  unit: string
  group: string
  type: 'number' | 'string' | 'list' | 'boolean'
  available?: boolean
}

export interface ScreenerCondition {
  field: string
  op: string
  value: unknown
}

export interface ScreenerStrategy {
  code: string
  id?: number
  name: string
  description?: string
  conditions: ScreenerCondition[]
  logic?: 'all' | 'any'
  exclude?: string[]
  order_by?: string
  limit?: number
  is_preset?: boolean
}

export interface ScreenerItem {
  symbol: string
  name: string
  market: string
  price: number | null
  change_pct: number | null
  amplitude: number | null
  turnover_rate: number | null
  volume_ratio: number | null
  amount: number | null
  float_mv: number | null
  total_mv: number | null
  main_net_inflow: number | null
  pe_ttm: number | null
  pb: number | null
  industry: string
  board?: string
  is_st: boolean
  limit_up_days: number
  limit_up_stat_days: number
  limit_up_stat_count: number
  seal_fund: number | null
  break_count: number
  first_seal_time: string
  concepts: Array<{ code: string; name: string; change_pct: number | null }>
  tags: string[]
  spark: number[]
}

export interface ScreenerRunResponse {
  trade_date: string
  count: number
  universe: number
  items: ScreenerItem[]
  as_of: string
  applied?: {
    logic: 'all' | 'any'
    conditions: ScreenerCondition[]
    exclude: string[]
    order_by: string
    limit: number
  }
}

export interface ScreenerMetaResponse {
  fields: ScreenerField[]
  ops: Record<string, string>
  presets: ScreenerStrategy[]
  rules: ScreenerStrategy[]
  status: {
    trade_date: string
    count: number
    trading_session: boolean
    cached: boolean
    source: string
    concepts: { rows: number; concepts: number }
  }
  disclaimer: string
}

export interface LadderItem {
  symbol: string
  name: string
  time: string
  concept: string
  industry: string
  stat: string
  seal_fund: number | null
  break_count: number
}

export interface LadderResponse {
  trade_date: string
  stats: { trade_date: string; limit_up: number; limit_down: number; max_days: number }
  groups: Array<{ level: string; days: number; items: LadderItem[] }>
  concept_index_ready: boolean
}

export interface BoardItem {
  code: string
  name: string
  change_pct: number | null
  main_net_inflow: number | null
  limit_up_count: number
  max_days: number
  members: number
  leader_name: string
  leader_code: string
  leader_change_pct: number | null
  score: number
}

export interface ScreenerParseResponse {
  conditions: ScreenerCondition[]
  logic: 'all' | 'any'
  exclude: string[]
  order_by: string
  limit: number
  explanation: string
  warnings: string[]
  ok: boolean
  model?: string
  raw?: string
}

const json = (body: unknown): RequestInit => ({
  method: 'POST',
  body: JSON.stringify(body),
})

export const screenerApi = {
  meta: () => fetchAPI<ScreenerMetaResponse>('/screener/meta'),

  run: (payload: {
    conditions: ScreenerCondition[]
    logic?: 'all' | 'any'
    exclude?: string[]
    order_by?: string
    limit?: number
    with_spark?: boolean
    refresh?: boolean
  }, options?: { timeoutMs?: number }) =>
    fetchAPI<ScreenerRunResponse>('/screener/run', {
      ...json(payload),
      timeoutMs: options?.timeoutMs ?? 60000,
    }),

  ladder: (date?: string) =>
    fetchAPI<LadderResponse>(
      date ? `/screener/ladder?date=${encodeURIComponent(date)}` : '/screener/ladder'
    ),

  boards: (date?: string, limit = 30) => {
    const qs = new URLSearchParams({ limit: String(limit) })
    if (date) qs.set('date', date)
    return fetchAPI<{ trade_date: string; items: BoardItem[]; error?: string }>(
      `/screener/boards?${qs.toString()}`
    )
  },

  parse: (text: string) =>
    fetchAPI<ScreenerParseResponse>('/screener/parse', {
      ...json({ text }),
      timeoutMs: 60000,
    }),

  refresh: () =>
    fetchAPI<{ trade_date: string; count: number; concept_rows: number }>(
      '/screener/refresh',
      { method: 'POST', timeoutMs: 180000 }
    ),

  createRule: (payload: {
    name: string
    conditions: ScreenerCondition[]
    logic?: 'all' | 'any'
    exclude?: string[]
    order_by?: string
    limit?: number
  }) => fetchAPI<ScreenerStrategy>('/screener/rules', json(payload)),

  deleteRule: (id: number) =>
    fetchAPI<{ deleted: number }>(`/screener/rules/${id}`, { method: 'DELETE' }),
}
