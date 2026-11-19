import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import SyncSection from '@/components/SyncSection'

const mocks = vi.hoisted(() => ({ preview: vi.fn(), commit: vi.fn(), fetch: vi.fn(), toast: vi.fn() }))
vi.mock('@panwatch/api', () => ({
  fetchAPI: mocks.fetch, patsApi: {}, syncApi: { imagePreview: mocks.preview, imageCommit: mocks.commit },
}))
vi.mock('@panwatch/base-ui/components/ui/toast', () => ({ useToast: () => ({ toast: mocks.toast }) }))

const result = () => ({
  ok: true, mode: 'positions', account: { id: 1, name: '证券账户' }, expected_state_hash: 'fingerprint',
  rows: [{ symbol: '001358', name: '兴欣新材', quantity: 200, cost_price: 22.5,
    status: 'new_stock', trade_action: 'buy', trade_price: 22.5, price_source: 'cost', delta: 200 }],
  summary: { total: 1, new_stock: 1 }, account_summary: { available: 10000 },
  missing_rows: [{ symbol: '002324', name: '普利特', quantity: 0, current_quantity: 100,
    cost_price: 5, status: 'close', missing: true, closing: true, trade_action: 'sell',
    trade_price: 5, price_source: 'cost', delta: -100 }],
  agent_plan: { attach: [{ symbol: '001358', name: '兴欣新材', agent_name: 'daily_report' }], detach: [], retained: [], warnings: [] },
  agent_plan_full: {
    attach: [{ symbol: '001358', name: '兴欣新材', agent_name: 'daily_report' }],
    detach: [{ symbol: '002324', name: '普利特', agent_name: 'daily_report' }], retained: [], warnings: [],
  },
})

async function preview() {
  const view = render(<SyncSection embedded />)
  await waitFor(() => expect((screen.getByLabelText('截图记账账户') as HTMLSelectElement).value).toBe('1'))
  const file = new File(['image'], 'holdings.png', { type: 'image/png' })
  fireEvent.change(view.container.querySelector('input[accept="image/*"]')!, { target: { files: [file] } })
  await screen.findByRole('button', { name: '确认同步' })
  return view
}

describe('持仓截图同步', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.fetch.mockResolvedValue([{ id: 1, name: '证券账户' }, { id: 2, name: '其他账户' }])
    mocks.preview.mockImplementation(async () => result())
    mocks.commit.mockResolvedValue({ ok: true, mode: 'positions', total: 2, created: 1,
      closed: 1, buy_trades: 1, sell_trades: 1, agents: { attached: 1, detached: 1, retained: 0, warnings: [] } })
    // jsdom 不解码图片；走真实 FileReader + 压缩失败回退，不调用视觉模型。
    vi.stubGlobal('Image', class {
      onerror?: () => void
      set src(_value: string) { queueMicrotask(() => this.onerror?.()) }
    })
  })
  afterEach(() => { cleanup(); vi.unstubAllGlobals() })

  it('previews missing holdings and submits both sides with checked date, prices and Agent migration', async () => {
    await preview()
    expect(screen.queryByLabelText('普利特记账价格')).toBeNull()
    expect((screen.getByLabelText('Agent 跟随持仓同步') as HTMLInputElement).checked).toBe(true)
    fireEvent.click(screen.getByLabelText(/清仓截图中未出现的持仓/))
    const region = screen.getByRole('region', { name: 'Agent 同步预览' })
    expect(within(region).getByText(/解除 普利特/)).toBeTruthy()
    fireEvent.change(screen.getByLabelText('普利特记账价格'), { target: { value: '5.6' } })
    fireEvent.change(screen.getByLabelText('兴欣新材记账价格'), { target: { value: '22.3' } })
    fireEvent.change(screen.getByLabelText('变动记账日期'), { target: { value: '2026-11-18' } })
    fireEvent.click(screen.getByRole('button', { name: '确认同步' }))
    await waitFor(() => expect(mocks.commit).toHaveBeenCalledTimes(1))
    expect(mocks.commit.mock.calls[0][0]).toMatchObject({
      account_id: 1, record_trades: true, sync_agents: true, remove_missing: true,
      expected_state_hash: 'fingerprint', trade_date: '2026-11-18', available_funds: 10000,
      rows: [{ symbol: '001358', trade_price: 22.3, price_source: 'manual' }],
      missing_rows: [{ symbol: '002324', trade_price: 5.6, price_source: 'manual' }],
    })
    expect(await screen.findByText(/买入流水 1 笔，卖出流水 1 笔/)).toBeTruthy()
    expect(screen.getByText(/Agent：新绑定 1，解除 1/)).toBeTruthy()
  })

  it('allows snapshot-only sync and preserves unseen holdings unless explicitly selected', async () => {
    await preview()
    fireEvent.click(screen.getByLabelText('Agent 跟随持仓同步'))
    fireEvent.click(screen.getByLabelText(/按变动生成流水/))
    expect(screen.queryByRole('region', { name: 'Agent 同步预览' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '确认同步' }))
    await waitFor(() => expect(mocks.commit).toHaveBeenCalledTimes(1))
    expect(mocks.commit.mock.calls[0][0]).toMatchObject({ record_trades: false, sync_agents: false, remove_missing: false })
    expect(mocks.commit.mock.calls[0][0].missing_rows).toBeUndefined()
  })

  it('clears old preview when the account or screenshot mode changes', async () => {
    await preview()
    fireEvent.change(screen.getByLabelText('截图记账账户'), { target: { value: '2' } })
    expect(screen.queryByRole('button', { name: '确认同步' })).toBeNull()
    expect(mocks.commit).not.toHaveBeenCalled()
  })

  it('keeps the preview and reports conflicts when server state changed', async () => {
    mocks.commit.mockRejectedValue(new Error('账户持仓或 Agent 配置已变化，请重新识别截图后再同步'))
    await preview()
    fireEvent.click(screen.getByRole('button', { name: '确认同步' }))
    await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith(expect.stringContaining('重新识别'), 'error'))
    expect(screen.getByRole('button', { name: '确认同步' })).toBeTruthy()
  })
})
