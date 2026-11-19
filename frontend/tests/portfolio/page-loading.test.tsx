import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import MessagesPage from '@/pages/Messages'
import ScreenerPage from '@/pages/Screener'

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), meta: vi.fn(), run: vi.fn() }))
vi.mock('@panwatch/api', () => ({
  fetchAPI: mocks.fetch,
  stocksApi: { list: async () => [], groups: async () => [] },
  screenerApi: { meta: mocks.meta, run: mocks.run },
}))
vi.mock('@panwatch/biz-ui/components/stock-insight-modal', () => ({ default: () => null }))

const feed = (title: string) => ({
  total: 1, returned: 1, hours: 24, watchlist_size: 1, watchlist_hits: 0, position_size: 0, position_hits: 0,
  category_counts: { 公司事件: 1 }, source_counts: {}, refreshing: true, brief: '',
  items: [{ id: title, title, content: '', source: 'local', time: '11-19 10:00', symbols: [], category: '公司事件', importance: 1 }],
})

beforeEach(() => { vi.clearAllMocks(); localStorage.clear() })
afterEach(cleanup)

describe('本地数据优先展示', () => {
  it('shows cached messages during background refresh and requests refresh without hiding them', async () => {
    mocks.fetch.mockResolvedValue(feed('缓存公告'))
    render(<MessagesPage />)
    expect(await screen.findByText('缓存公告')).toBeTruthy()
    expect(screen.getByText(/正在后台更新消息/)).toBeTruthy()
    fireEvent.click(screen.getByTitle('刷新'))
    await waitFor(() => expect(mocks.fetch.mock.calls.some(([url]) => url.includes('refresh=true'))).toBe(true))
    expect(screen.getByText('缓存公告')).toBeTruthy()
  })

  it('ignores a slow response from a previous message filter', async () => {
    let resolveOld!: (result: unknown) => void
    mocks.fetch.mockImplementation((url: string) => url.includes('hours=6&')
      ? new Promise(resolve => { resolveOld = resolve }) : Promise.resolve(feed('当前公告')))
    render(<MessagesPage />)
    await screen.findByText('当前公告')
    fireEvent.click(screen.getByRole('button', { name: '6小时' }))
    fireEvent.click(screen.getByRole('button', { name: '3天' }))
    await screen.findByText('当前公告')
    resolveOld(feed('过时的筛选结果'))
    await waitFor(() => expect(screen.queryByText('过时的筛选结果')).toBeNull())
  })

  it('loads screener candles from local data without requesting per-stock remote klines', async () => {
    mocks.meta.mockResolvedValue({ fields: [], ops: {}, presets: [], rules: [], status: {}, disclaimer: '' })
    mocks.run.mockResolvedValue({ trade_date: '2026-11-19', total: 1, count: 1, universe: 1, refreshing: true,
      items: [{ symbol: '600900', name: '长江电力', market: 'CN', price: 11, change_pct: 1, concepts: [], tags: [], spark: [] }] })
    mocks.fetch.mockResolvedValue({ data: { '600900': [{ o: 10, h: 11, l: 9, c: 10, v: 100 }, { o: 10, h: 12, l: 10, c: 11, v: 120 }] } })
    render(<ScreenerPage />)
    expect(await screen.findByText('长江电力')).toBeTruthy()
    await waitFor(() => expect(mocks.fetch.mock.calls.some(([url]) => url.includes('fallback=false&include_live=true'))).toBe(true))
    expect(mocks.fetch.mock.calls.some(([url]) => url.startsWith('/klines/'))).toBe(false)
    expect(screen.getByText(/行情后台刷新中/)).toBeTruthy()
  })
})
