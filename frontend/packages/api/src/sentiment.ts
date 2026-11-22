import { fetchAPI } from './client'

export interface MarketSentimentParts {
  tier?: number
  fry?: number
  promote?: number
  face?: number
}

export interface MarketSentimentSnapshot {
  date: string
  l1: number
  mood: 'panic' | 'neutral' | 'euphoric' | string
  systemic_risk: boolean
  drop_trigger: boolean
  limit_up: number
  limit_down: number
  high_face: number
  sky_floor: number
  main_sector: string | null
  main_sector_hl: number
  sector_sticky: boolean
  l2: number | null
  l2_parts: MarketSentimentParts
  l3: 'none' | 'warning' | 'confirm' | string
  leader: string | null
  leader_break_days: number
  fry_rate: number | null
  promote_rate: number | null
  global_fry_rate: number | null
  global_promote_rate: number | null
  fry_baseline: number | null
  promote_baseline: number | null
  ebb_confirmed: boolean
  tier: string | null
  single_position: number | null
  total_position: number | null
  coverage: number
  source: string
  warnings: string[]
  partial: boolean
}

export interface MarketSentimentResponse {
  today: MarketSentimentSnapshot | null
  history: MarketSentimentSnapshot[]
  stale: boolean
  latest_bar_date: string | null
  generated_at: string
}

export const sentimentApi = {
  get: (days = 60) => fetchAPI<MarketSentimentResponse>(`/market/sentiment?days=${days}`),
  refresh: () =>
    fetchAPI<{ ok: boolean; date?: string; existing?: boolean; error?: string }>(
      '/market/sentiment/refresh',
      { method: 'POST', body: JSON.stringify({}) },
    ),
}
