import { fetchAPI } from './client'

/** 成交同步：手机短信/通知、交割单导入（与后端 trade_ingest 同口径）。 */

export interface SyncFees {
  commission: number
  stamp_duty: number
  transfer_fee: number
  other_fee?: number
  total: number
}

export interface SyncParsed {
  ok: boolean
  reason: string
  action: 'buy' | 'sell' | null
  symbol: string
  name: string
  quantity: number
  price: number
  trade_date: string
  time: string
  fees: SyncFees
  external_id: string
  broker: string
  confidence: number
  warnings: string[]
}

export interface SyncTrade {
  id: number
  account_id: number
  symbol: string
  name: string
  action: string
  quantity: number
  price: number
  amount: number
  total_fee: number
  trade_date: string
  source: string
}

export interface SyncResult {
  ok: boolean
  duplicate?: boolean
  dry_run?: boolean
  message?: string
  error?: string
  stage?: string
  reason?: string
  hint?: string
  trade?: SyncTrade
  account?: { id: number; name: string }
  stock?: { symbol: string; name: string }
  position?: { quantity: number; cost_price: number } | null
  parsed?: SyncParsed | null
  payload?: Record<string, unknown>
}

export interface ImportRow {
  line: number
  raw: string
  ok: boolean
  status: 'ok' | 'duplicate' | 'invalid' | 'new_stock' | string
  message?: string
  reason?: string
  action: 'buy' | 'sell' | null
  symbol: string
  name: string
  quantity: number
  price: number
  trade_date: string
  time: string
  external_id: string
  fees: SyncFees
}

export interface ImportPreview {
  ok: boolean
  account: { id: number; name: string }
  summary: { total: number; ok: number; duplicate: number; invalid: number; new_stock: number }
  rows: ImportRow[]
}

export interface ImportCommit {
  ok: boolean
  account: { id: number; name: string }
  created: number
  duplicate: number
  failed: number
  total: number
  results: Array<{ line: number; status: string; message: string; trade_id?: number }>
}

export interface IngestPayload {
  text?: string
  account_id?: number | null
  account_name?: string
  source?: 'sms' | 'notification' | 'import' | 'assistant'
  market?: string
  dry_run?: boolean
  force?: boolean
  action?: 'buy' | 'sell'
  symbol?: string
  name?: string
  quantity?: number
  price?: number
  trade_date?: string
  time?: string
  fees?: Partial<SyncFees>
  external_id?: string
  note?: string
}

export interface ImportPayload {
  content?: string
  base64?: string
  filename?: string
  account_id?: number | null
  account_name?: string
  lines?: number[]
}

export const syncApi = {
  ingest: (payload: IngestPayload) =>
    fetchAPI<SyncResult>('/portfolio/trades/ingest', {
      method: 'POST',
      body: JSON.stringify({ source: 'sms', market: 'CN', ...payload }),
      timeoutMs: 30000,
    }),

  preview: (payload: ImportPayload) =>
    fetchAPI<ImportPreview>('/portfolio/trades/import/preview', {
      method: 'POST',
      body: JSON.stringify(payload),
      timeoutMs: 60000,
    }),

  commit: (payload: ImportPayload) =>
    fetchAPI<ImportCommit>('/portfolio/trades/import/commit', {
      method: 'POST',
      body: JSON.stringify(payload),
      timeoutMs: 60000,
    }),
}
