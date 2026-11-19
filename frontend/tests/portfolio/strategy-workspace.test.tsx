import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import type { StrategyCondition } from '@panwatch/api'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import StrategyBacktest from '@panwatch/biz-ui/components/StrategyBacktest'
import StrategySettings from '@panwatch/biz-ui/components/StrategySettings'
import { ConditionEditor } from '@panwatch/biz-ui/components/strategy-rules'

const api = vi.hoisted(() => ({ playbooks: vi.fn(), drafts: vi.fn(), saveDraft: vi.fn(), aiDraft: vi.fn(), activateDraft: vi.fn(), applyLibrary: vi.fn(), backtest: vi.fn(), save: vi.fn(), remove: vi.fn() }))
vi.mock('@panwatch/api', () => ({ strategyApi: api }))
const fields = [{ key: 'change_pct', label: '当日涨跌幅', unit: '%' }]
const preset = { id: 'pullback', name: '回踩草稿', description: '仅供试验', buy: [{ field: 'change_pct', op: 'gte', value: -3 }], sell: [{ field: 'change_pct', op: 'lte', value: -5 }], avoid: [], note: '回踩后观察' }
const active = { id: 1, scope: 'global', symbol: '', market: 'CN', name: '当前默认', buy: preset.buy, sell: preset.sell, avoid: [], note: '', enabled: true, is_override: false, version: 2 }
const report = {
  symbol: '600000', start: '2025-01-02', end: '2025-01-06', bars: 3,
  trade_count: 1, total_return: 2, buy_hold_return: 1, excess_return: 1, win_rate: 100, current_status: '观望中', rules_hash: 'hash-1', backtest_id: 7,
  rules: { buy: preset.buy, sell: preset.sell, avoid: [] }, metrics: { max_drawdown: -1 },
  curve: [{ date: '2025-01-02', price_pct: 0, equity_pct: 0 }, { date: '2025-01-03', price_pct: 2, equity_pct: 1 }, { date: '2025-01-06', price_pct: 1, equity_pct: 2 }],
  trades: [], orders: [{ side: 'buy', status: 'filled', signal_date: '2025-01-02', execution_date: '2025-01-03', fill_price: 10, quantity: 100, fees: { explicit_fees: 5, slippage_cost: 1 }, conditions: { buy: [{ field: 'change_pct', op: 'gte', value: -3, actual: 2, matched: true }], sell: [], avoid: [] } }],
}

beforeEach(() => {
  localStorage.clear()
  api.playbooks.mockResolvedValue({ items: [active], fields, presets: [preset], defaults: { buy: [], sell: [], avoid: [] } })
  api.drafts.mockResolvedValue({ items: [] })
  api.saveDraft.mockImplementation(async payload => ({ ...payload, id: 10, draft_id: 10, status: 'draft', rules_hash: 'hash-1', questions: payload.questions || [] }))
  api.aiDraft.mockResolvedValue({ ...preset, scope: 'stock', symbol: '600000', market: 'CN', id: 11, draft_id: 11, status: 'draft', rules_hash: 'ai-hash', questions: ['消息面无法用日K规则验证'] })
  api.activateDraft.mockResolvedValue({ ...active, scope: 'stock', symbol: '600000', version: 3 })
  api.backtest.mockResolvedValue(report)
  api.save.mockResolvedValue(active)
  api.remove.mockResolvedValue({ deleted: 1 })
})
afterEach(() => { cleanup(); localStorage.clear(); vi.clearAllMocks() })

async function openWorkspace() {
  const result = render(<StrategyBacktest symbol="600000" />)
  await screen.findByText('600000 · 策略与回测')
  return result
}

describe('strategy draft, validation and activation', () => {
  it('edits and saves the existing stock strategy identity without applying or duplicating it', async () => {
    const saved = { ...active, id: 42, draft_id: 42, scope: 'stock' as const, symbol: '600271', rules_hash: 'hash-1', status: 'draft' as const, questions: [] }
    api.saveDraft.mockImplementation(async payload => ({ ...payload, id: payload.draft_id, draft_id: payload.draft_id, rules_hash: 'edited', questions: [] }))
    render(<StrategyBacktest symbol="600271" libraryDraft={saved} guided />)
    await screen.findByLabelText('草稿名称')
    fireEvent.click(screen.getByText('编辑买入条件'))
    fireEvent.change(screen.getByLabelText('买入：全部满足才入场第1条数值'), { target: { value: '-2.5' } })
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
    await waitFor(() => expect(api.saveDraft).toHaveBeenCalledWith(expect.objectContaining({ draft_id: 42, scope: 'stock', symbol: '600271', buy: [{ field: 'change_pct', op: 'gte', value: -2.5 }] })))
    expect(api.applyLibrary).not.toHaveBeenCalled()
    expect(api.activateDraft).not.toHaveBeenCalled()
    expect(api.backtest).not.toHaveBeenCalled()
  })

  it('keeps the same library strategy when replacing all of its conditions', async () => {
    const saved = { ...active, id: 42, draft_id: 42, scope: 'stock' as const, symbol: '600271', rules_hash: 'hash-1', status: 'draft' as const, questions: [] }
    render(<StrategyBacktest symbol="600271" libraryDraft={saved} guided />)
    await screen.findByLabelText('草稿名称')
    fireEvent.click(screen.getByRole('button', { name: '从空白规则开始' }))
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
    await waitFor(() => expect(api.saveDraft).toHaveBeenCalledWith(expect.objectContaining({ draft_id: 42, scope: 'stock', symbol: '600271', name: '当前默认', buy: [], sell: [], avoid: [] })))
    expect(api.applyLibrary).not.toHaveBeenCalled()
  })
  it('keeps rule edits when choosing a different verification stock in the guided flow', async () => {
    const libraryDraft = { ...active, id: 42, draft_id: 42, scope: 'global' as const, symbol: '', market: 'CN', rules_hash: 'hash-1', status: 'draft' as const, questions: [] }
    render(<StrategyBacktest symbol="" libraryDraft={libraryDraft} guided />)
    const name = await screen.findByLabelText('草稿名称')
    fireEvent.change(name, { target: { value: '保留我正在编辑的规则' } })
    fireEvent.click(screen.getByRole('button', { name: /2. 历史验证/ }))
    fireEvent.change(screen.getByLabelText('验证标的'), { target: { value: '600001' } })
    fireEvent.click(screen.getByRole('button', { name: '确认标的' }))
    fireEvent.click(screen.getByRole('button', { name: /1. 确认规则/ }))
    expect((screen.getByLabelText('草稿名称') as HTMLInputElement).value).toBe('保留我正在编辑的规则')
    expect(api.activateDraft).not.toHaveBeenCalled()
    expect(api.applyLibrary).not.toHaveBeenCalled()
    expect(api.saveDraft).not.toHaveBeenCalled()
  })
  it('keeps a reusable library identity when saving, testing and applying to a stock', async () => {
    const libraryDraft = { ...active, id: 42, draft_id: 42, scope: 'global' as const, symbol: '', market: 'CN', rules_hash: 'hash-1', status: 'draft' as const, questions: [] }
    api.applyLibrary.mockResolvedValue({ ...active, revision_id: 9 })
    render(<StrategyBacktest symbol="600000" libraryDraft={libraryDraft} />)
    await screen.findByText('600000 · 策略与回测')
    fireEvent.click(screen.getByRole('button', { name: '运行草稿回测' }))
    await screen.findByText('对应当前草稿与参数')
    expect(api.saveDraft).toHaveBeenCalledWith(expect.objectContaining({ draft_id: 42, scope: 'global', symbol: '' }))
    fireEvent.click(screen.getByRole('button', { name: '选择应用范围' }))
    fireEvent.click(screen.getByRole('button', { name: '确认应用到本股' }))
    await waitFor(() => expect(api.applyLibrary).toHaveBeenCalledWith(10, { scope: 'stock', symbol: '600000', expected_hash: 'hash-1' }))
    expect(api.activateDraft).not.toHaveBeenCalled()
  })

  it('preserves every typed decimal, negative sign and comma while publishing numbers and keeping empty input empty', async () => {
    const user = userEvent.setup()
    for (const scenario of [{ op: 'gte', text: '0.05', expected: .05 }, { op: 'lte', text: '-3.25', expected: -3.25 }, { op: 'between', text: '-3.25,0.05', expected: [-3.25, .05] }]) {
      const observed = vi.fn()
      function Editor() {
        const [conditions, setConditions] = useState<StrategyCondition[]>([{ field: 'change_pct', op: scenario.op, value: '' }])
        return <ConditionEditor title="输入测试" fields={fields} value={conditions} onChange={next => { setConditions(next); observed(next) }} />
      }
      const { unmount } = render(<Editor />)
      const input = screen.getByLabelText('输入测试第1条数值') as HTMLInputElement
      let prefix = ''
      for (const character of scenario.text) {
        prefix += character
        await user.type(input, character)
        expect(input.value).toBe(prefix)
      }
      expect(observed.mock.lastCall?.[0][0].value).toEqual(scenario.expected)
      await user.tab()
      expect(input.value).toBe(scenario.text)
      await user.clear(input)
      expect(observed.mock.lastCall?.[0][0].value).toBe('')
      unmount()
    }
  })

  it('loads a template only into a draft, saves separately, and never changes active rules during experimentation', async () => {
    await openWorkspace()
    fireEvent.change(screen.getByLabelText('载入策略模板'), { target: { value: 'pullback' } })
    expect((screen.getByLabelText('草稿名称') as HTMLInputElement).value).toBe('回踩草稿')
    expect(within(screen.getByRole('region', { name: '当前生效规则' })).getByText(/当前默认/)).toBeTruthy()
    expect(api.saveDraft).not.toHaveBeenCalled()
    expect(api.save).not.toHaveBeenCalled()
    expect(api.activateDraft).not.toHaveBeenCalled()
    expect(api.backtest).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
    await waitFor(() => expect(api.saveDraft).toHaveBeenCalledTimes(1))
    expect(api.saveDraft.mock.calls[0][0]).toMatchObject({ symbol: '600000', scope: 'stock', name: '回踩草稿' })
    expect(api.activateDraft).not.toHaveBeenCalled()
  })

  it('binds a result to dates, cash and rules; changing parameters marks the old result stale', async () => {
    await openWorkspace()
    fireEvent.change(screen.getByLabelText('回测开始日期'), { target: { value: '2025-01-01' } })
    fireEvent.change(screen.getByLabelText('回测结束日期'), { target: { value: '2025-01-06' } })
    fireEvent.change(screen.getByLabelText('回测初始资金'), { target: { value: '10000' } })
    fireEvent.click(screen.getByRole('button', { name: '运行草稿回测' }))
    await screen.findByText('对应当前草稿与参数')
    expect(api.backtest.mock.calls[0][0]).toMatchObject({ draft_id: 10, start_date: '2025-01-01', end_date: '2025-01-06', initial_capital: 10000 })
    expect(screen.getByText('买入持有收益')).toBeTruthy()
    expect(screen.getByText('超额收益')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '买入 2025-01-03成交点' }))
    const selected = screen.getByRole('region', { name: '所选成交解读' })
    expect(selected.textContent).toContain('2025-01-02 收盘后发出信号')
    expect(selected.textContent).toContain('¥10')
    expect(selected.textContent).toContain('实际 2')
    fireEvent.change(screen.getByLabelText('回测初始资金'), { target: { value: '20000' } })
    expect(screen.getByText('规则或参数已变化，旧结果不代表当前草稿')).toBeTruthy()
    expect(api.activateDraft).not.toHaveBeenCalled()
  })

  it('defaults application to one stock and requires an explicit untested acknowledgment', async () => {
    await openWorkspace()
    fireEvent.click(screen.getByRole('button', { name: '选择应用范围' }))
    const dialog = await screen.findByRole('dialog', { name: '确认应用这份规则' })
    expect((within(dialog).getByRole('radio', { name: /仅应用到 600000/ }) as HTMLInputElement).checked).toBe(true)
    expect((within(dialog).getByRole('button', { name: '确认应用到本股' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(within(dialog).getByRole('checkbox', { name: /尚无匹配的成功回测/ }))
    fireEvent.click(within(dialog).getByRole('button', { name: '确认应用到本股' }))
    await waitFor(() => expect(api.activateDraft).toHaveBeenCalledWith(10))
    expect(api.saveDraft.mock.calls[0][0]).toMatchObject({ scope: 'stock', symbol: '600000' })
  })

  it('does not treat disabled stock rules as effective and does not auto-apply unresolved AI semantics', async () => {
    api.playbooks.mockResolvedValue({ items: [{ ...active, id: 2, scope: 'stock', symbol: '600000', name: '停用本股规则', enabled: false }, active], fields, presets: [preset] })
    await openWorkspace()
    expect(within(screen.getByRole('region', { name: '当前生效规则' })).getByText(/当前默认 · 继承默认/)).toBeTruthy()
    fireEvent.change(screen.getByLabelText('策略想法'), { target: { value: '消息出来再买' } })
    fireEvent.click(screen.getByRole('button', { name: '生成规则草稿' }))
    await screen.findByText('消息面无法用日K规则验证')
    expect((screen.getByRole('button', { name: '选择应用范围' }) as HTMLButtonElement).disabled).toBe(true)
    expect(api.activateDraft).not.toHaveBeenCalled()
    expect(api.aiDraft.mock.calls[0][0]).toMatchObject({ scope: 'stock', symbol: '600000', text: '消息出来再买' })
  })

  it('restores an explicitly chosen server draft without enabling it', async () => {
    api.drafts.mockResolvedValue({ items: [{ ...preset, id: 13, draft_id: 13, scope: 'stock', symbol: '600000', market: 'CN', name: '我保存的草稿', questions: [] }] })
    await openWorkspace()
    fireEvent.change(screen.getByLabelText('恢复服务器草稿'), { target: { value: '13' } })
    expect((screen.getByLabelText('草稿名称') as HTMLInputElement).value).toBe('我保存的草稿')
    expect(api.activateDraft).not.toHaveBeenCalled()
    expect(api.backtest).not.toHaveBeenCalled()
  })

  it('does not describe a mismatched server rule hash as a current validated result', async () => {
    api.backtest.mockResolvedValue({ ...report, rules_hash: 'different-snapshot' })
    await openWorkspace()
    fireEvent.click(screen.getByRole('button', { name: '运行草稿回测' }))
    await screen.findByText('规则或参数已变化，旧结果不代表当前草稿')
    fireEvent.click(screen.getByRole('button', { name: '选择应用范围' }))
    const dialog = await screen.findByRole('dialog', { name: '确认应用这份规则' })
    expect((within(dialog).getByRole('button', { name: '确认应用到本股' }) as HTMLButtonElement).disabled).toBe(true)
    expect(api.activateDraft).not.toHaveBeenCalled()
  })

  it('does not reuse the previous stock draft identity when a save finishes after switching symbols', async () => {
    let finish!: (value: unknown) => void
    api.saveDraft.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const { rerender } = await openWorkspace()
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
    rerender(<StrategyBacktest symbol="600001" />)
    await screen.findByText('600001 · 策略与回测')
    finish({ ...preset, id: 500, draft_id: 500, scope: 'stock', symbol: '600000', market: 'CN', rules_hash: 'old-stock', questions: [] })
    await waitFor(() => expect(screen.queryByText('草稿已保存到服务器，尚未应用。')).toBeNull())
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
    await waitFor(() => expect(api.saveDraft).toHaveBeenCalledTimes(2))
    expect(api.saveDraft.mock.calls[1][0]).toMatchObject({ symbol: '600001', draft_id: undefined })
  })

  it('uses the same editor in global settings, and template selection still needs scope confirmation', async () => {
    render(<StrategySettings open onOpenChange={vi.fn()} />)
    await screen.findByLabelText('草稿名称')
    fireEvent.change(screen.getByLabelText('载入策略模板'), { target: { value: 'pullback' } })
    expect(api.save).not.toHaveBeenCalled()
    expect(api.activateDraft).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '确认应用范围' }))
    const dialog = await screen.findByRole('dialog', { name: '应用为默认规则' })
    expect((within(dialog).getByRole('button', { name: '确认更改默认规则' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(within(dialog).getByRole('checkbox'))
    fireEvent.click(within(dialog).getByRole('button', { name: '确认更改默认规则' }))
    await waitFor(() => expect(api.activateDraft).toHaveBeenCalledWith(10))
    expect(api.saveDraft.mock.calls[0][0]).toMatchObject({ scope: 'global', symbol: '', name: '回踩草稿' })
  })

  it('ignores an AI draft from a global settings session that was closed and reopened', async () => {
    let finish!: (value: unknown) => void
    api.aiDraft.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const { rerender } = render(<StrategySettings open onOpenChange={vi.fn()} />)
    await screen.findByLabelText('草稿名称')
    fireEvent.change(screen.getByLabelText('策略想法'), { target: { value: '旧会话想法' } })
    fireEvent.click(screen.getByRole('button', { name: '生成规则草稿' }))
    rerender(<StrategySettings open={false} onOpenChange={vi.fn()} />)
    rerender(<StrategySettings open onOpenChange={vi.fn()} />)
    await screen.findByLabelText('草稿名称')
    finish({ ...preset, id: 999, draft_id: 999, scope: 'global', symbol: '', market: 'CN', name: '过期 AI 回复', questions: [] })
    await waitFor(() => expect((screen.getByLabelText('草稿名称') as HTMLInputElement).value).toBe('当前默认'))
    fireEvent.click(screen.getByRole('button', { name: '仅保存草稿' }))
    await waitFor(() => expect(api.saveDraft).toHaveBeenCalledTimes(1))
    expect(api.saveDraft.mock.calls[0][0]).toMatchObject({ name: '当前默认', draft_id: undefined })
  })

  it('offers to keep a manual default draft on close and restores it after reopening without applying it', async () => {
    const onOpenChange = vi.fn()
    const { rerender } = render(<StrategySettings open onOpenChange={onOpenChange} />)
    await screen.findByLabelText('草稿名称')
    fireEvent.change(screen.getByLabelText('草稿名称'), { target: { value: '未完成的手工草稿' } })
    fireEvent.keyDown(screen.getByRole('dialog', { name: '默认交易规则' }), { key: 'Escape', code: 'Escape' })
    const confirmation = await screen.findByRole('dialog', { name: '保留未保存的默认规则草稿？' })
    expect(onOpenChange).not.toHaveBeenCalled()
    fireEvent.click(within(confirmation).getByRole('button', { name: '保留本机草稿并关闭' }))
    expect(onOpenChange).toHaveBeenCalledWith(false)
    rerender(<StrategySettings open={false} onOpenChange={onOpenChange} />)
    rerender(<StrategySettings open onOpenChange={onOpenChange} />)
    fireEvent.click(await screen.findByRole('button', { name: '恢复本机草稿' }))
    expect((screen.getByLabelText('草稿名称') as HTMLInputElement).value).toBe('未完成的手工草稿')
    expect(api.saveDraft).not.toHaveBeenCalled()
    expect(api.activateDraft).not.toHaveBeenCalled()
  })

  it('does not prompt for untouched settings, and server-saving clears the dirty close protection', async () => {
    const onOpenChange = vi.fn()
    const { rerender } = render(<StrategySettings open onOpenChange={onOpenChange} />)
    await screen.findByLabelText('草稿名称')
    fireEvent.keyDown(screen.getByRole('dialog', { name: '默认交易规则' }), { key: 'Escape', code: 'Escape' })
    expect(onOpenChange).toHaveBeenCalledWith(false)
    expect(screen.queryByRole('dialog', { name: '保留未保存的默认规则草稿？' })).toBeNull()
    rerender(<StrategySettings open={false} onOpenChange={onOpenChange} />)
    rerender(<StrategySettings open onOpenChange={onOpenChange} />)
    await screen.findByLabelText('草稿名称')
    fireEvent.change(screen.getByLabelText('草稿名称'), { target: { value: '保存后的草稿' } })
    fireEvent.click(screen.getByRole('button', { name: '仅保存草稿' }))
    await screen.findByText('默认规则草稿已保存，当前生效规则没有变化。')
    expect(localStorage.getItem('panwatch_strategy_default_draft:CN')).toBeNull()
    onOpenChange.mockClear()
    fireEvent.keyDown(screen.getByRole('dialog', { name: '默认交易规则' }), { key: 'Escape', code: 'Escape' })
    expect(onOpenChange).toHaveBeenCalledWith(false)
    expect(screen.queryByRole('dialog', { name: '保留未保存的默认规则草稿？' })).toBeNull()
  })

  it('discards a manual default draft only after the explicit discard choice', async () => {
    const onOpenChange = vi.fn()
    const { rerender } = render(<StrategySettings open onOpenChange={onOpenChange} />)
    await screen.findByLabelText('草稿名称')
    fireEvent.change(screen.getByLabelText('草稿名称'), { target: { value: '要放弃的改动' } })
    fireEvent.keyDown(screen.getByRole('dialog', { name: '默认交易规则' }), { key: 'Escape', code: 'Escape' })
    const confirmation = await screen.findByRole('dialog', { name: '保留未保存的默认规则草稿？' })
    fireEvent.click(within(confirmation).getByRole('button', { name: '放弃更改并关闭' }))
    expect(localStorage.getItem('panwatch_strategy_default_draft:CN')).toBeNull()
    rerender(<StrategySettings open={false} onOpenChange={onOpenChange} />)
    rerender(<StrategySettings open onOpenChange={onOpenChange} />)
    await screen.findByLabelText('草稿名称')
    expect((screen.getByLabelText('草稿名称') as HTMLInputElement).value).toBe('当前默认')
    expect(screen.queryByRole('button', { name: '恢复本机草稿' })).toBeNull()
  })
})
