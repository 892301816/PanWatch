import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import StrategyCenter from '@/pages/StrategyCenter'

const mocks = vi.hoisted(() => ({ library: vi.fn(), saveDraft: vi.fn(), scanLibrary: vi.fn(), backtests: vi.fn(), deleteLibrary: vi.fn(), restoreLibrary: vi.fn(), fetch: vi.fn(), list: vi.fn() }))
vi.mock('@panwatch/api', () => ({ fetchAPI: mocks.fetch, stocksApi: { list: mocks.list }, strategyApi: mocks }))
vi.mock('@panwatch/biz-ui/components/StrategyBacktest', () => ({ default: ({ libraryDraft, symbol, initialStage }: { libraryDraft: { name: string; id: number }; symbol: string; initialStage?: string }) => <div data-testid="workspace" data-stage={initialStage}>{libraryDraft.name} #{libraryDraft.id} · {symbol || '尚未选择回测标的'}</div> }))
vi.mock('@/pages/OpportunityDiscovery', () => ({ default: ({ onChoose }: { onChoose: (target: unknown) => void }) => <button onClick={() => onChoose({ symbol: '600000', name: '系统示例', market: 'CN' })}>系统候选检查</button> }))
vi.mock('@/components/TradeDecisionDialog', () => ({ default: ({ target, onRecord }: { target: { symbol: string } | null; onRecord: (value: unknown) => void }) => target && <button onClick={() => onRecord({ accountId: 1, price: 10, quantity: 100, decisionId: 88, note: '纪律检查 #88' })}>记账 {target.symbol}</button> }))
const first = { id: 1, draft_id: 1, name: '缩量回踩', scope: 'global', symbol: '', market: 'CN', buy: [{ field: 'change_pct', op: 'gte', value: -3 }], sell: [], avoid: [], note: '等待趋势中的回踩', rules_hash: 'a', questions: [], status: 'draft', bindings: [], tested: false }
const second = { ...first, id: 2, draft_id: 2, name: '不追高', rules_hash: 'b' }
beforeEach(() => {
  mocks.library.mockResolvedValue({ items: [first, second], fields: [{ key: 'change_pct', label: '当日涨跌幅', unit: '%' }], bindings: [], presets: [{ ...first, id: 'pullback', description: '模板说明' }] })
  mocks.list.mockResolvedValue([{ symbol: '600000', market: 'CN', name: '示例' }])
  mocks.fetch.mockResolvedValue([])
  mocks.saveDraft.mockImplementation(async payload => ({ ...payload, id: 3, draft_id: 3, questions: [], rules_hash: 'c', bindings: [] }))
  mocks.backtests.mockResolvedValue({ items: [] })
  mocks.scanLibrary.mockResolvedValue({ items: [{ symbol: '600000', name: '示例', market: 'CN', status: 'BLOCK', summary: '命中禁止买入条件' }], draft_id: 1, rules_hash: 'a', as_of: '2026-11-14T02:00:00Z' })
})
afterEach(() => { cleanup(); vi.clearAllMocks(); sessionStorage.clear() })
function Location() { return <p data-testid="location">{useLocation().pathname}{useLocation().search}</p> }
function open(path = '/opportunities?view=my') { return render(<MemoryRouter initialEntries={[path]}><Routes><Route path="/opportunities" element={<StrategyCenter />}/><Route path="*" element={null}/></Routes><Location /></MemoryRouter>) }

describe('strategy center navigation and evidence', () => {
  it('opens the chart workspace on the primary route', async () => {
    open('/opportunities')
    await screen.findByRole('heading', { name:'策略工作台' })
    expect(screen.getByRole('button', {name:'K线工作台'}).getAttribute('aria-current')).toBe('page')
    expect(mocks.scanLibrary).not.toHaveBeenCalled()
  })
  it.each(['draft', 'tested', 'active'])('always offers an edit entry for an existing %s strategy', async status => {
    const item = { ...first, scope: 'stock', symbol: '600271', tested: status === 'tested', bindings: status === 'active' ? [{ id: 7, enabled: true, scope: 'stock', symbol: '600271', version: 2, rules_hash: 'a' }] : [] }
    mocks.library.mockResolvedValue({ items: [item], fields: [], bindings: item.bindings, presets: [] })
    open()
    fireEvent.click(await screen.findByRole('button', { name: '编辑规则 缩量回踩' }))
    expect(screen.getByTestId('workspace').getAttribute('data-stage')).toBe('rules')
    expect(screen.getByTestId('workspace').textContent).toContain('缩量回踩 #1 · 600271')
    expect(screen.getByTestId('location').textContent).toContain('step=rules')
    expect(mocks.saveDraft).not.toHaveBeenCalled()
    expect(mocks.scanLibrary).not.toHaveBeenCalled()
  })
  it('does not mistake a failed library request for an empty strategy library', async () => {
    mocks.library.mockRejectedValueOnce(new Error('接口返回了网页，服务可能尚未更新'))
    open()
    expect((await screen.findByRole('alert')).textContent).toContain('服务可能尚未更新')
    expect(screen.queryByText(/0 份策略/)).toBeNull()
    expect(screen.queryByRole('heading', { name: '从一套你理解的规则开始' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '重新读取策略' }))
    await screen.findByRole('article', { name: '策略 缩量回踩' })
  })
  it('opens my strategies without loading old discovery and explains each rule', async () => {
    open()
    await screen.findByRole('article', { name: '策略 缩量回踩' })
    // B 方案后「我的策略」不再是导航项，而是工作台内的弹窗\n    expect(screen.queryByRole('button', { name: '我的策略' })).toBeNull()\n    expect(screen.getByRole('button', { name: 'K线工作台' }).getAttribute('aria-current')).toBe('page')
    expect(screen.queryByText('系统候选检查')).toBeNull()
    expect(mocks.scanLibrary).not.toHaveBeenCalled()
    expect(screen.getAllByText(/应用范围：尚未应用/).length).toBe(2)
  })
  it('opens a general strategy editor without requiring a stock first', async () => {
    open()
    const card = await screen.findByRole('article', { name: '策略 缩量回踩' })
    fireEvent.click(within(card).getByRole('button', { name: '开始回测' }))
    expect(screen.getByTestId('workspace').textContent).toContain('缩量回踩 #1 · 尚未选择回测标的')
    expect(screen.queryByRole('article', { name: '策略 不追高' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '返回我的策略' }))
    await screen.findByRole('article', { name: '策略 不追高' })
  })
  it('copies templates to a new reusable draft without applying a rule', async () => {
    open()
    await screen.findByRole('article', { name: '策略 缩量回踩' })
    fireEvent.click(screen.getByRole('button', { name: '浏览模板' }))
    fireEvent.click(screen.getByRole('button', { name: '复制为我的策略' }))
    await waitFor(() => expect(mocks.saveDraft).toHaveBeenCalledWith(expect.objectContaining({ scope: 'global', symbol: '', draft_id: undefined })))
  })
  it('discards a slow scan when the selected strategy changes', async () => {
    let finish: (value: unknown) => void = () => {}
    mocks.scanLibrary.mockImplementation(() => new Promise(resolve => { finish = resolve }))
    open('/opportunities?view=opportunities&strategy=1')
    fireEvent.click(await screen.findByRole('button', { name: '检查候选' }))
    await waitFor(() => expect(mocks.scanLibrary).toHaveBeenCalled())
    fireEvent.change(screen.getByLabelText('选择我的策略'), { target: { value: '2' } })
    await act(async () => finish({ draft_id: 1, rules_hash: 'a', items: [{ symbol: '600000', name: '旧结果', market: 'CN', status: 'MATCH' }], as_of: '2026-11-14' }))
    expect(screen.queryByText('旧结果')).toBeNull()
  })
  it('keeps discovery verdicts distinct from actual trade checks and transfers facts without exposing them in the URL', async () => {
    open('/opportunities?view=opportunities&strategy=1')
    fireEvent.click(await screen.findByRole('button', { name: '检查候选' }))
    await screen.findByText('禁止买入')
    fireEvent.click(screen.getByRole('button', { name: '检查这笔操作（按实际启用规则）' }))
    fireEvent.click(screen.getByRole('button', { name: '记账 600000' }))
    await waitFor(() => expect(screen.getByTestId('location').textContent).toContain('/portfolio?record_intent='))
    expect(screen.getByTestId('location').textContent).not.toContain('纪律检查')
    expect(sessionStorage.length).toBe(1)
  })
  it('filters discipline history by strategy provenance', async () => {
    mocks.fetch.mockImplementation(async path => path === '/accounts' ? [] : { items: [{ decision_id: 8, status: 'BLOCK', revision_id: 4, input: { symbol: '600000', side: 'buy' }, exception_reason: '观察仓试验' }] })
    open('/opportunities?view=records&strategy=1')
    await screen.findByText(/#8 · 600000/)
    expect(mocks.fetch).toHaveBeenCalledWith('/strategy/decisions?market=ALL&limit=50&draft_id=1')
    expect(screen.getByText(/例外：观察仓试验/)).toBeTruthy()
  })

  it('loads original discovery only after opening it and checks its candidate with the selected policy', async () => {
    open('/opportunities?view=opportunities&strategy=1')
    await screen.findByLabelText('选择我的策略')
    expect(screen.queryByText('系统候选检查')).toBeNull()
    fireEvent.click(screen.getByText('原机会发现 · 系统候选与发现标签'))
    fireEvent.click(await screen.findByText('系统候选检查'))
    await waitFor(() => expect(mocks.scanLibrary).toHaveBeenCalledWith(1, expect.objectContaining({ expected_hash: 'a', symbols: [{ symbol: '600000', name: '系统示例', market: 'CN' }] })))
  })

  it('offers three creation paths without creating a draft before a path is chosen', async () => {
    open()
    fireEvent.click(await screen.findByRole('button', { name: '创建策略' }))
    expect(screen.getByRole('button', { name: /用中文描述想法/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /从模板开始/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /自己编写条件/ })).toBeTruthy()
    expect(mocks.saveDraft).not.toHaveBeenCalled()
  })

  it('requires explicit acknowledgement before disabling and deleting an active strategy', async () => {
    const binding = { id: 7, scope: 'stock', symbol: '600000', name: first.name, enabled: true, version: 2, rules_hash: 'a' }
    mocks.library.mockResolvedValue({ items: [{ ...first, bindings: [binding] }], fields: [], bindings: [binding], presets: [] })
    mocks.deleteLibrary.mockResolvedValue({ deleted: 1 })
    open()
    fireEvent.click(await screen.findByRole('button', { name: '删除 缩量回踩' }))
    const dialog = screen.getByRole('dialog', { name: '删除策略“缩量回踩”？' })
    expect((within(dialog).getByRole('button', { name: '停用并删除' }) as HTMLButtonElement).disabled).toBe(true)
    expect(mocks.deleteLibrary).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('checkbox', { name: '确认停用以上范围并删除策略' }))
    fireEvent.click(within(dialog).getByRole('button', { name: '停用并删除' }))
    await waitFor(() => expect(mocks.deleteLibrary).toHaveBeenCalledWith(1, 'a', true, [7]))
    expect(mocks.saveDraft).not.toHaveBeenCalled()
  })

  it('restores a deleted strategy without enabling any old binding', async () => {
    mocks.library.mockResolvedValue({ items: [], deleted_items: [first], fields: [], presets: [], bindings: [] })
    mocks.restoreLibrary.mockResolvedValue(first)
    open()
    fireEvent.click(await screen.findByText('回收站 · 1'))
    fireEvent.click(screen.getByRole('button', { name: '恢复' }))
    await waitFor(() => expect(mocks.restoreLibrary).toHaveBeenCalledWith(1))
    expect(mocks.saveDraft).not.toHaveBeenCalled()
  })

  it('starts a daily operation check without creating or selecting a research draft', async () => {
    open()
    fireEvent.click(await screen.findByRole('button', { name: '检查一笔买卖' }))
    fireEvent.change(screen.getByLabelText('操作检查证券代码'), { target: { value: '600000' } })
    fireEvent.click(screen.getByRole('button', { name: '填写价量并检查' }))
    expect(screen.getByRole('button', { name: '记账 600000' })).toBeTruthy()
    expect(mocks.saveDraft).not.toHaveBeenCalled()
  })

  it('preserves the chosen market for daily checks instead of applying A-share code rules to every market', async () => {
    open()
    fireEvent.click(await screen.findByRole('button', { name: '检查一笔买卖' }))
    fireEvent.change(screen.getByLabelText('操作检查市场'), { target: { value: 'HK' } })
    fireEvent.change(screen.getByLabelText('操作检查证券代码'), { target: { value: '00700' } })
    fireEvent.click(screen.getByRole('button', { name: '填写价量并检查' }))
    expect(screen.getByRole('button', { name: '记账 00700' })).toBeTruthy()
    expect(mocks.saveDraft).not.toHaveBeenCalled()
  })
})
