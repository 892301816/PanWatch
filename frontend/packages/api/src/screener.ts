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
  /** 距20日均线 % */
  ma20_dev?: number | null
  /** 距20日新高 %(0 = 正好新高) */
  high20_gap?: number | null
  /** 今日成交额 / 前5日平均成交额 */
  amount_ratio_5d?: number | null
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
  /** 命中总数(分页前) */
  count: number
  total?: number
  /** 本次实际返回条数 */
  returned?: number
  offset?: number
  universe: number
  items: ScreenerItem[]
  as_of: string
  /** 数据降级提示(如涨停池取数失败改用本地日K推算) */
  warnings?: string[]
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
  /** 主主题 + 最多 2 个副主题(主题优先,风格标签靠后) */
  concepts?: string[]
  industry: string
  stat: string
  seal_fund: number | null
  break_count: number
}

export interface LadderResponse {
  trade_date: string
  stats: { trade_date: string; limit_up: number; limit_down: number; max_days: number }
  groups: Array<{ level: string; days: number; items: LadderItem[] }>
  filter?: { concept: string | null; count: number; max_days: number }
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

export interface ScreenerRunSummary {
  id: number
  trade_date: string
  created_at: string
  source: string
  rule_name: string
  conditions: ScreenerCondition[]
  logic: string
  matched_count: number
  universe: number
  stored_items: number
  duration_ms: number
}

export interface ScreenerHistoryItem {
  rank: number
  symbol: string
  name: string
  price: number | null
  change_pct: number | null
  turnover_rate: number | null
  amount: number | null
  float_mv: number | null
  limit_up_days: number
  concept_rank: number | null
  concepts: string[]
}

export interface ScreenerHistoryPerformance {
  trade_date: string
  horizons: Record<string, { count: number; avg?: number; median?: number; win_rate?: number }>
  computed_at: string
}

export interface ScreenerHistoryDetail extends ScreenerRunSummary {
  exclude: string[]
  order_by: string
  warnings: string[]
  performance: ScreenerHistoryPerformance | null
  items: ScreenerHistoryItem[]
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
    offset?: number
    save?: boolean
    code?: string
    with_spark?: boolean
    refresh?: boolean
  }, options?: { timeoutMs?: number }) =>
    fetchAPI<ScreenerRunResponse>('/screener/run', {
      ...json(payload),
      timeoutMs: options?.timeoutMs ?? 60000,
    }),

  ladder: (date?: string, concept?: string | null) => {
    const qs = new URLSearchParams()
    if (date) qs.set('date', date)
    if (concept) qs.set('concept', concept)
    const query = qs.toString()
    return fetchAPI<LadderResponse>(query ? `/screener/ladder?${query}` : '/screener/ladder')
  },

  boards: (date?: string, limit = 30, includeStyle = false) => {
    const qs = new URLSearchParams({ limit: String(limit) })
    if (date) qs.set('date', date)
    if (includeStyle) qs.set('include_style', 'true')
    return fetchAPI<{ trade_date: string; items: BoardItem[]; error?: string }>(
      `/screener/boards?${qs.toString()}`
    )
  },

  /** 今日概念名候选(条件编辑器用) */
  concepts: () =>
    fetchAPI<{ trade_date: string; items: Array<{ name: string; rank: number }>; source: string }>(
      '/screener/concepts',
      { timeoutMs: 60000 }
    ),

  /** 选股历史 */
  history: (params?: { limit?: number; offset?: number; date?: string; q?: string }) => {
    const qs = new URLSearchParams()
    if (params?.limit) qs.set('limit', String(params.limit))
    if (params?.offset) qs.set('offset', String(params.offset))
    if (params?.date) qs.set('date', params.date)
    if (params?.q) qs.set('q', params.q)
    const query = qs.toString()
    return fetchAPI<{ total: number; items: ScreenerRunSummary[] }>(
      query ? `/screener/history?${query}` : '/screener/history'
    )
  },

  historyDetail: (id: number) =>
    fetchAPI<ScreenerHistoryDetail>(`/screener/history/${id}`, { timeoutMs: 60000 }),

  historyPerformance: (id: number) =>
    fetchAPI<ScreenerHistoryPerformance>(`/screener/history/${id}/performance`, {
      timeoutMs: 60000,
    }),

  deleteHistory: (id: number) =>
    fetchAPI<{ deleted: number }>(`/screener/history/${id}`, { method: 'DELETE' }),

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
