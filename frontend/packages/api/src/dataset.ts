import { fetchAPI } from './client'

export interface DatasetProviderItem {
  id: number
  provider: string
  priority: number
  enabled: boolean
  configured: boolean
  config: Record<string, unknown>
  last_ok_at: string
  last_error: string
  fail_count: number
}

export interface DatasetProgress {
  running: boolean
  stage: string
  message: string
  provider: string
  rows: number
  total: number
  done: number
  started_at: number
  finished_at: number
  error: string
}

export interface DatasetStatus {
  providers: DatasetProviderItem[]
  sync: {
    last_date: string
    last_mode: string
    rows: number
    status: string
    error: string
    started_at: string
    finished_at: string
  }
  stats: { rows: number; days: number; symbols: number }
  progress: DatasetProgress
  schedule: { enabled: boolean; hour: number; minute: number; next_run_at: string }
  stale: boolean
}

const json = (body: unknown): RequestInit => ({
  method: 'PUT',
  body: JSON.stringify(body),
})

export const datasetApi = {
  status: () => fetchAPI<DatasetStatus>('/dataset/status'),

  startSync: (payload: {
    mode: 'incremental' | 'full'
    days?: number
    provider?: string | null
    allow_fallback?: boolean
  }) =>
    fetchAPI<{ started: boolean; mode: string }>('/dataset/sync', {
      method: 'POST',
      body: JSON.stringify(payload),
      timeoutMs: 30000,
    }),

  progress: () => fetchAPI<DatasetProgress>('/dataset/progress'),

  updateProvider: (
    id: number,
    payload: { priority?: number; enabled?: boolean; config?: Record<string, unknown> }
  ) => fetchAPI<DatasetProviderItem>(`/dataset/providers/${id}`, json(payload)),

  testProvider: (id: number) =>
    fetchAPI<{ provider: string; ok: boolean; detail: string }>(
      `/dataset/providers/${id}/test`,
      { method: 'POST', timeoutMs: 60000 }
    ),

  updateSchedule: (payload: { enabled?: boolean; hour?: number; minute?: number }) =>
    fetchAPI<{ enabled: boolean; hour: number; minute: number; next_run_at: string }>(
      '/dataset/schedule',
      json(payload)
    ),

  bars: (symbol: string, days = 60) =>
    fetchAPI<{
      symbol: string
      count: number
      bars: Array<{
        date: string
        open: number
        high: number
        low: number
        close: number
        volume: number
        amount: number
      }>
    }>(`/dataset/bars/${encodeURIComponent(symbol)}?days=${days}`),
}
