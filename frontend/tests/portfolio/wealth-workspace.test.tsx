import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import WealthPanel from '@/components/WealthPanel'

const mocks = vi.hoisted(() => ({
  overview: vi.fn(), dividends: vi.fn(), stocks: vi.fn(), updateStrategy: vi.fn(),
  plans: vi.fn(), updatePlan: vi.fn(), trades: vi.fn(), processDividends: vi.fn(), advice: vi.fn(),
}))
vi.mock('@panwatch/api', () => ({
  portfolioApi: {
    wealthOverview: mocks.overview, dividendRecords: mocks.dividends, updateStrategy: mocks.updateStrategy,
    dcaList: mocks.plans, dcaUpdate: mocks.updatePlan, trades: mocks.trades,
    processDividends: mocks.processDividends, wealthAiAdvice: mocks.advice,
  },
  stocksApi: { list: mocks.stocks },
}))

const basePlan = {
  id: 10, account_id: 1, account_name: '长期配置', stock_id: 1, name: '宽基定投', symbol: '510300',
  amount: 2000, frequency: 'monthly', day_of_month: 15, day_of_week: 0,
  start_date: '2026-01-15', next_run_date: '2026-11-15', enabled: true,
  executed_count: 8, total_invested: 16000, note: '',
}

beforeEach(() => {
  mocks.overview.mockResolvedValue({
    goals: { annual_passive_income: 12000, emergency_months: 6, annual_return_target: 8 },
    accounts: [
      { id: 1, name: '长期配置', available_funds: 10000, strategy_type: 'steady', risk_policy: {}, fee_policy: {}, dividend_reinvest: true, positions: [], dca_plans: [] },
      { id: 2, name: '成长投资', available_funds: 5000, strategy_type: 'aggr', risk_policy: {}, fee_policy: {}, dividend_reinvest: false, positions: [], dca_plans: [] },
    ],
    dca_due: [{ id: 10, account_id: 1 }], trades: [],
  })
  mocks.dividends.mockResolvedValue([])
  mocks.stocks.mockResolvedValue([])
  mocks.plans.mockResolvedValue([])
  mocks.trades.mockResolvedValue([])
  mocks.advice.mockResolvedValue({ content: '当前账户可用资金 12345 元', generated_at: '2026-11-05T12:00:00', disclaimer: '研究参考' })
})
afterEach(() => { cleanup(); vi.resetAllMocks() })

describe('wealth workspace compatibility', () => {
  it('keeps account controls and plan forms reachable without writing when changing modes', async () => {
    const { rerender } = render(<WealthPanel mode="overview" />)
    await screen.findByRole('heading', { name: '全账户组合分析' })
    expect(screen.queryByText('今日待办')).toBeNull()
    expect(screen.queryByText('编辑目标')).toBeNull()

    rerender(<WealthPanel mode="accounts" accountId={1} />)
    await screen.findByText('长期配置')
    expect(screen.queryByText('成长投资')).toBeNull()
    expect(screen.getByText('费用设置')).toBeTruthy()
    expect(screen.getByText('分红再投')).toBeTruthy()

    rerender(<WealthPanel mode="dca" accountId={1} />)
    fireEvent.click(await screen.findByRole('button', { name: '新建定投' }))
    expect(mocks.plans).toHaveBeenCalledWith(1)
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('option', { name: '长期配置' })).toBeTruthy()
    expect(within(dialog).queryByRole('option', { name: '成长投资' })).toBeNull()
    fireEvent.keyDown(dialog, { key: 'Escape', code: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    rerender(<WealthPanel mode="records" accountId={1} />)
    expect(await screen.findByRole('heading', { name: '统一账本' })).toBeTruthy()
    expect(mocks.trades).toHaveBeenLastCalledWith({ account_id: 1, limit: 50 })
    rerender(<WealthPanel mode="goals" />)
    expect(await screen.findByText('编辑目标')).toBeTruthy()
    expect(mocks.updateStrategy).not.toHaveBeenCalled()
    expect(mocks.updatePlan).not.toHaveBeenCalled()
    expect(mocks.processDividends).not.toHaveBeenCalled()
    expect(mocks.advice).not.toHaveBeenCalled()
  })

  it('keeps a paused plan visible and restores it through the existing update endpoint', async () => {
    let enabled = true
    mocks.plans.mockImplementation(async () => [{ ...basePlan, enabled }])
    mocks.updatePlan.mockImplementation(async (_id, changes) => { enabled = changes.enabled; return { ...basePlan, enabled } })
    const changed = vi.fn()
    render(<WealthPanel mode="dca" accountId={1} onChanged={changed} />)
    fireEvent.click(await screen.findByRole('button', { name: '暂停' }))
    expect(await screen.findByRole('button', { name: '恢复' })).toBeTruthy()
    expect(mocks.updatePlan).toHaveBeenNthCalledWith(1, 10, { enabled: false })
    expect(screen.getByText('宽基定投')).toBeTruthy()
    expect((screen.getByRole('button', { name: '记录成交' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: '恢复' }))
    await screen.findByRole('button', { name: '暂停' })
    expect(mocks.updatePlan).toHaveBeenNthCalledWith(2, 10, { enabled: true })
    expect((screen.getByRole('button', { name: '记录成交' }) as HTMLButtonElement).disabled).toBe(false)
    expect(changed).toHaveBeenCalledTimes(2)
  })

  it('filters both ledger sections by account and hides sensitive numbers in privacy mode', async () => {
    mocks.trades.mockResolvedValue([
      { id: 1, account_id: 1, name: '账户一交易', action: 'buy', quantity: 100, amount: 1234, total_fee: 5, trade_date: '2026-11-01' },
      { id: 2, account_id: 2, name: '账户二交易', action: 'sell', quantity: 200, amount: 5678, total_fee: 6, trade_date: '2026-11-02' },
    ])
    mocks.dividends.mockResolvedValue([
      { id: 1, account_id: 1, account_name: '长期配置', name: '账户一分红', amount: 300, action: 'cash', ex_date: '2026-10-01' },
      { id: 2, account_id: 2, account_name: '成长投资', name: '账户二分红', amount: 400, action: 'cash', ex_date: '2026-10-02' },
    ])
    const { rerender } = render(<WealthPanel mode="records" accountId={1} privateMode />)
    await screen.findByText('账户一交易')
    expect(screen.queryByText('账户二交易')).toBeNull()
    expect(screen.getByText('账户一分红')).toBeTruthy()
    expect(screen.queryByText('账户二分红')).toBeNull()
    expect(screen.queryByText('¥1234.00')).toBeNull()
    expect(screen.getAllByText('••••').length).toBeGreaterThan(0)
    rerender(<WealthPanel mode="records" accountId={2} />)
    await screen.findByText('账户二交易')
    expect(screen.queryByText('账户一交易')).toBeNull()
    expect(mocks.trades).toHaveBeenLastCalledWith({ account_id: 2, limit: 50 })
    expect(mocks.processDividends).not.toHaveBeenCalled()
  })

  it('opens an existing due plan from focusPlanId without recording a trade', async () => {
    mocks.plans.mockResolvedValue([basePlan])
    const handled = vi.fn()
    const { rerender } = render(<WealthPanel mode="dca" accountId={1} focusPlanId={10} onFocusPlanHandled={handled} />)
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('heading', { name: '记一笔 · 宽基定投' })).toBeTruthy()
    expect(handled).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(dialog, { key: 'Escape', code: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    rerender(<WealthPanel mode="dca" accountId={1} focusPlanId={null} onFocusPlanHandled={handled} />)
    rerender(<WealthPanel mode="dca" accountId={1} focusPlanId={10} onFocusPlanHandled={handled} />)
    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect(handled).toHaveBeenCalledTimes(2)
    expect(mocks.updatePlan).not.toHaveBeenCalled()
  })

  it('retries a failed plan query and keeps global AI scope explicit', async () => {
    mocks.plans.mockRejectedValueOnce(new Error('offline')).mockResolvedValue([])
    const { rerender } = render(<WealthPanel mode="dca" />)
    await screen.findByRole('alert')
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    await screen.findByRole('button', { name: '新建定投' })
    rerender(<WealthPanel mode="overview" accountId={1} />)
    fireEvent.click(await screen.findByRole('button', { name: '分析全部账户' }))
    await screen.findByText('当前账户可用资金 12345 元')
    expect(screen.getByText('分析覆盖全部账户，不受账户筛选影响。')).toBeTruthy()
    rerender(<WealthPanel mode="overview" accountId={1} privateMode />)
    expect(screen.queryByText('当前账户可用资金 12345 元')).toBeNull()
    expect(screen.queryByText('今日待办')).toBeNull()
  })
})
