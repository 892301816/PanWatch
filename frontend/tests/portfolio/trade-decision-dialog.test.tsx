import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import TradeDecisionDialog from '@/components/TradeDecisionDialog'

const mocks = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('@panwatch/api', () => ({ fetchAPI: mocks.request }))
const target = { symbol: '002943', name: '示例标的', market: 'CN', side: 'buy' as const, accountId: 1, price: 20, quantity: 100 }
const blocked = { decision_id: 31, status: 'BLOCK', revision_id: 5, playbook_name: '回踩规则', expires_at: '2099-01-01T00:00:00Z', buy_checks: [{ label: '回踩范围', actual: '-8%', expect: '-3% ~ 0%', ok: false }] }
const renderDialog = (record = vi.fn()) => render(<TradeDecisionDialog target={target} accounts={[{ id: 1, name: '稳健账户' }]} onClose={vi.fn()} onRecord={record} />)
beforeEach(() => { mocks.request.mockReset() })
afterEach(cleanup)

describe('independent trade discipline checks', () => {
  it('records an actual fill without claiming it passed a check', async () => {
    const record = vi.fn()
    renderDialog(record)
    fireEvent.click(screen.getByRole('button', { name: '已成交，去记账' }))
    expect(mocks.request).not.toHaveBeenCalled()
    expect(record).toHaveBeenCalledWith(expect.objectContaining({ accountId: 1, price: 20, quantity: 100, decisionId: undefined, note: '已成交补录：未关联有效的事前检查' }))
  })

  it('invalidates prior evidence immediately when quantity changes and ignores late responses', async () => {
    let resolve: (value: unknown) => void = () => {}
    mocks.request.mockImplementation(() => new Promise(done => { resolve = done }))
    renderDialog()
    fireEvent.click(screen.getByRole('button', { name: '检查这笔意图' }))
    expect(screen.getByRole('button', { name: '检查规则与账户约束…' })).toBeTruthy()
    fireEvent.change(screen.getByLabelText('计划数量'), { target: { value: '200' } })
    await act(async () => resolve({ ...blocked, status: 'PASS' }))
    expect(screen.queryByRole('region', { name: '纪律检查结果' })).toBeNull()
    expect(screen.queryByText('符合已启用策略')).toBeNull()
    expect(screen.getByRole('button', { name: '检查这笔意图' })).toBeTruthy()
  })

  it('keeps a block as a block after saving a reason, and discards it after an input change', async () => {
    mocks.request.mockImplementation(async (path: string) => path.includes('/exception') ? { ...blocked, exception_reason: '降低仓位进行试验' } : blocked)
    renderDialog()
    fireEvent.click(screen.getByRole('checkbox', { name: '补充 AI 解读' }))
    fireEvent.click(screen.getByRole('button', { name: '检查这笔意图' }))
    await screen.findByText('违反策略或账户约束')
    fireEvent.change(screen.getByLabelText('例外说明（可选）'), { target: { value: '降低仓位进行试验' } })
    fireEvent.click(screen.getByRole('button', { name: '记录例外说明' }))
    await screen.findByText('已记录例外：降低仓位进行试验')
    expect(screen.queryByText('符合已启用策略')).toBeNull()
    fireEvent.change(screen.getByLabelText('计划价格'), { target: { value: '21' } })
    expect(screen.queryByText('已记录例外：降低仓位进行试验')).toBeNull()
    expect(screen.queryByRole('region', { name: '纪律检查结果' })).toBeNull()
  })

  it('shows failure as incomplete and preserves rule evidence if only AI fails', async () => {
    mocks.request.mockResolvedValueOnce({ ...blocked, status: 'PASS' }).mockRejectedValueOnce(new Error('模型超时'))
    renderDialog()
    fireEvent.click(screen.getByRole('button', { name: '检查这笔意图' }))
    await screen.findByRole('alert')
    expect(screen.getByText('符合已启用策略')).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toContain('规则结果已保留，AI 解读未完成')
    expect(mocks.request).toHaveBeenCalledTimes(2)
    expect(mocks.request.mock.calls[1][0]).toBe('/strategy/decisions/31/explain')
    mocks.request.mockRejectedValueOnce(new Error('行情不可用'))
    fireEvent.click(screen.getByRole('button', { name: '重新检查这笔意图' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('行情不可用'))
    expect(screen.queryByText('符合已启用策略')).toBeNull()
  })

  it('does not attach an expired check to a factual record', async () => {
    mocks.request.mockResolvedValue({ ...blocked, expires_at: '2000-01-01T00:00:00Z' })
    const record = vi.fn()
    renderDialog(record)
    fireEvent.click(screen.getByRole('checkbox', { name: '补充 AI 解读' }))
    fireEvent.click(screen.getByRole('button', { name: '检查这笔意图' }))
    await screen.findByText('检查已过期，请重新检查')
    fireEvent.click(screen.getByRole('button', { name: '已成交，去记账' }))
    expect(record.mock.calls[0][0].decisionId).toBeUndefined()
  })

  it('marks evidence invalid when the account or policy changes during the AI explanation', async () => {
    mocks.request.mockResolvedValueOnce({ ...blocked, status: 'PASS' }).mockRejectedValueOnce(new Error('检查已失效，请重新检查：账户持仓已变化'))
    const record = vi.fn()
    renderDialog(record)
    fireEvent.click(screen.getByRole('button', { name: '检查这笔意图' }))
    await screen.findByText('检查已失效，请重新检查')
    expect(screen.queryByText('符合已启用策略')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '已成交，去记账' }))
    expect(record.mock.calls[0][0].decisionId).toBeUndefined()
  })

  it('renders account PASS/BLOCK/UNKNOWN evidence using its status and readable explanation', async () => {
    mocks.request.mockResolvedValue({ ...blocked, account_checks: [
      { field: 'cash', status: 'PASS', actual: 5000, required: 2005, reason: '按拟成交价估算资金及费用' },
      { field: 'max_single_weight', status: 'BLOCK', actual: 0.6, expected: 0.3 },
      { field: 't_plus_one', status: 'UNKNOWN', reason: '历史成交记录尚不完整' },
    ] })
    renderDialog()
    fireEvent.click(screen.getByRole('checkbox', { name: '补充 AI 解读' }))
    fireEvent.click(screen.getByRole('button', { name: '检查这笔意图' }))
    await screen.findByText('按拟成交价估算资金及费用')
    expect(screen.getByText('可用资金').closest('li')?.textContent).toContain('满足')
    expect(screen.getByText('买后单仓权重').closest('li')?.textContent).toContain('未满足')
    expect(screen.getByText(/实际 60.00%/).textContent).toContain('要求 30.00%')
    expect(screen.getByText('A 股 T+1 可卖数量').closest('li')?.textContent).toContain('未判断')
  })

  it('keeps historical checks read-only and separate from the current intent', async () => {
    mocks.request.mockResolvedValue({ items: [{ ...blocked, linked_trade_id: 88, input: { side: 'buy', price: 19, quantity: 200 }, created_at: '2026-11-12T10:00:00', exception_reason: '当时降低仓位试验' }] })
    const record = vi.fn()
    renderDialog(record)
    fireEvent.click(screen.getByRole('button', { name: '查看最近纪律检查' }))
    await screen.findByText(/只读历史快照/)
    expect(screen.getByText(/已关联成交 #88/)).toBeTruthy()
    expect(screen.queryByRole('region', { name: '纪律检查结果' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '已成交，去记账' }))
    expect(record.mock.calls[0][0].decisionId).toBeUndefined()
  })
})
