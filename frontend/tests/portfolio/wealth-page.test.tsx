import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import StocksPage from '@/pages/Stocks'
import { saveTradeTransfer } from '@/lib/trade-intent-transfer'

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), dcaDue: vi.fn(), summaryBatch: vi.fn(), createTrade: vi.fn(), toast: vi.fn() }))
vi.mock('@panwatch/api', () => ({
  fetchAPI: mocks.fetch,
  stocksApi: {},
  portfolioApi: { dcaDue: mocks.dcaDue, createTrade: mocks.createTrade },
  strategyApi: { board: vi.fn(async () => ({ items: [] })) },
}))
vi.mock('@panwatch/base-ui/components/ui/toast', () => ({ useToast: () => ({ toast: mocks.toast }) }))
vi.mock('@panwatch/api/klines', () => ({ klinesApi: { summaryBatch: mocks.summaryBatch } }))
vi.mock('@panwatch/biz-ui/components/suggestion-badge', () => ({ SuggestionBadge: () => null }))
vi.mock('@panwatch/biz-ui/components/kline-summary-dialog', () => ({ KlineSummaryDialog: () => null }))
vi.mock('@panwatch/biz-ui/components/stock-insight-modal', () => ({ default: () => null }))
vi.mock('@panwatch/biz-ui/components/deep-analysis-modal', () => ({ DeepAnalysisModal: () => null }))
vi.mock('@/components/WealthPanel', () => ({
  default: ({ mode, accountId, focusPlanId }: { mode: string; accountId?: number | null; focusPlanId?: number | null }) => <div data-testid="wealth-content" data-focus-plan={focusPlanId ?? ''}>{mode}:{accountId ?? 'all'}</div>,
}))
// Real Radix menu behavior has dedicated tests; this makes page tests focus on action wiring.
vi.mock('@/components/WealthActionMenu', () => ({
  default: ({ label = '更多', triggerText = '更多', actions }: { label?: string; triggerText?: string; actions: Array<{ label: string; onSelect: () => void; disabled?: boolean }> }) => (
    <details onClick={event => event.stopPropagation()}><summary aria-label={label}>{triggerText}</summary><div>{actions.map(action => <button key={action.label} disabled={action.disabled} onClick={action.onSelect}>{action.label}</button>)}</div></details>
  ),
}))

const stocks = [
  { id: 1, name: '稳健标的', symbol: '510300', market: 'CN', agents: [], group_name: 'ETF' },
  { id: 2, name: '成长标的', symbol: '300750', market: 'CN', agents: [], group_name: '个股' },
  { id: 3, name: '研究标的', symbol: '600519', market: 'CN', agents: [], group_name: '个股' },
]
const position = (id: number) => ({
  id, stock_id: id, symbol: stocks[id - 1].symbol, name: stocks[id - 1].name, market: 'CN',
  cost_price: 90, quantity: 10, invested_amount: null, trading_style: 'long', current_price: 100,
  current_price_cny: 100, change_pct: 0, market_value: 1000, market_value_cny: 1000, pnl: 100,
  pnl_pct: 11.11, daily_pnl: 0, daily_pnl_pct: 0, exchange_rate: null,
})
const account = (id: number, name: string, cash: number) => ({
  id, name, available_funds: cash, total_market_value: 1000, total_cost: 900,
  total_pnl: 100, total_pnl_pct: 11.11, total_daily_pnl: 0, total_assets: 1000 + cash,
  positions: [position(id)], strategy_type: id === 1 ? 'steady' : 'aggr',
})
const portfolio = {
  accounts: [account(1, '长期配置账户', 1000), account(2, '成长投资账户', 2000)],
  total: { total_market_value: 2000, total_cost: 1800, total_pnl: 200, total_pnl_pct: 11.11, total_daily_pnl: 0, available_funds: 3000, total_assets: 5000 },
}

beforeEach(() => {
  localStorage.clear()
  mocks.dcaDue.mockResolvedValue([])
  mocks.summaryBatch.mockResolvedValue([])
  mocks.fetch.mockImplementation(async (path: string) => {
    if (path === '/stocks') return stocks
    if (path.startsWith('/portfolio/summary')) return structuredClone(portfolio)
    if (path === '/quotes/batch') return stocks.map(stock => ({ symbol: stock.symbol, market: stock.market, current_price: 100, change_pct: 0 }))
    if (path.startsWith('/suggestions')) return {}
    if (path === '/stocks/markets/status' || path === '/price-alerts' || path.startsWith('/news')) return []
    throw new Error(`Unexpected page API: ${path}`)
  })
})
afterEach(() => { cleanup(); localStorage.clear(); vi.clearAllMocks() })

function renderPage(entry = '/portfolio') {
  return render(<MemoryRouter initialEntries={[entry]}><StocksPage /></MemoryRouter>)
}

describe('three-view wealth workspace', () => {
  it('reveals holdings and cash before slow quotes and klines, without showing false valuation losses', async () => {
    const base = mocks.fetch.getMockImplementation()!
    const unpriced = structuredClone(portfolio)
    unpriced.accounts.forEach(account => account.positions.forEach(position => Object.assign(position, { current_price: null, change_pct: null })))
    let finishQuotes!: (rows: unknown) => void
    mocks.fetch.mockImplementation((path: string, options?: unknown) => {
      if (path.startsWith('/portfolio/summary')) return Promise.resolve(unpriced)
      if (path === '/quotes/batch') return new Promise(resolve => { finishQuotes = resolve })
      return base(path, options)
    })
    mocks.summaryBatch.mockImplementation(() => new Promise(() => {}))
    const { container } = renderPage()
    const summary = await screen.findByRole('region', { name: '资产概况' })
    expect(container.querySelector('.wealth-holdings')!.textContent).toContain('稳健标的')
    expect(within(summary).getByText('¥3,000.00')).toBeTruthy()
    expect(within(summary).getByText('持仓市值 --')).toBeTruthy()
    expect(summary.textContent).not.toContain('100.00%')
    expect(screen.getByText('行情更新中，账户与持仓可先操作')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /记一笔/ }))
    expect(screen.getByRole('dialog', { name: '记一笔交易' })).toBeTruthy()
    await act(async () => finishQuotes(stocks.map(stock => ({ symbol: stock.symbol, market: stock.market, current_price: 100, change_pct: 0 }))))
    expect(within(summary).getByText('¥5,000.00')).toBeTruthy()
    expect(screen.queryByText('行情更新中，账户与持仓可先操作')).toBeNull()
  })

  it('ends loading on a core failure and can retry the page', async () => {
    const base = mocks.fetch.getMockImplementation()!
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    mocks.fetch.mockImplementation((path: string, options?: unknown) => path === '/stocks' ? Promise.reject(new Error('连接失败')) : base(path, options))
    renderPage()
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('连接失败')
    mocks.fetch.mockImplementation(base)
    fireEvent.click(within(alert).getByRole('button', { name: '重新加载' }))
    await screen.findByRole('region', { name: '资产概况' })
    log.mockRestore()
  })

  it('migrates the old overview view to holdings and exposes exactly three main destinations', async () => {
    localStorage.setItem('panwatch_stocks_viewTab', JSON.stringify('overview'))
    renderPage()
    const navigation = await screen.findByRole('navigation', { name: '理财工作台视图' })
    const destinations = within(navigation).getAllByRole('button')
    expect(destinations.map(button => button.textContent)).toEqual(['持仓 2', '关注 3', '计划'])
    expect(destinations[0].getAttribute('aria-current')).toBe('page')
    expect(screen.getByRole('region', { name: '资产概况' })).toBeTruthy()
    await waitFor(() => expect(JSON.parse(localStorage.getItem('panwatch_stocks_viewTab')!)).toBe('positions'))
  })

  it('gives watchlist its own research controls without asset or account-bookkeeping controls', async () => {
    renderPage()
    const navigation = await screen.findByRole('navigation', { name: '理财工作台视图' })
    fireEvent.click(within(navigation).getByRole('button', { name: '关注 3' }))
    expect(screen.queryByRole('region', { name: '资产概况' })).toBeNull()
    expect(screen.queryByLabelText('统计账户')).toBeNull()
    expect(screen.queryByRole('button', { name: /记一笔/ })).toBeNull()
    expect(screen.getByRole('button', { name: /添加关注/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: '扫描分析' })).toBeTruthy()
    expect(screen.getByText('研究标的')).toBeTruthy()
  })

  it('keeps the account summary, holdings and on-demand ledger in the same selected scope', async () => {
    const { container } = renderPage()
    await screen.findByRole('region', { name: '资产概况' })
    fireEvent.change(screen.getByLabelText('统计账户'), { target: { value: '1' } })
    const summary = screen.getByRole('region', { name: '资产概况' })
    expect(within(summary).getByText('长期配置账户')).toBeTruthy()
    expect(within(summary).getByText('¥2,000.00')).toBeTruthy()
    expect(container.querySelector('.wealth-holdings')!.textContent).toContain('稳健标的')
    expect(container.querySelector('.wealth-holdings')!.textContent).not.toContain('成长标的')
    expect(screen.queryByRole('dialog', { name: '统一账本' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '记录' }))
    const ledger = await screen.findByRole('dialog', { name: '统一账本' })
    expect(within(ledger).getByTestId('wealth-content').textContent).toBe('records:1')
    expect((within(ledger).getByLabelText('侧栏账户') as HTMLSelectElement).value).toBe('1')
  })

  it('opens only the selected account due plan directly in the plans view', async () => {
    mocks.dcaDue.mockResolvedValue([
      { id: 10, account_id: 1, account_name: '长期配置账户', name: '本期宽基计划', next_run_date: '2026-11-05', amount: 2000 },
      { id: 20, account_id: 2, account_name: '成长投资账户', name: '另一账户计划', next_run_date: '2026-11-05', amount: 1000 },
    ])
    renderPage()
    await screen.findByRole('region', { name: '资产概况' })
    fireEvent.change(screen.getByLabelText('统计账户'), { target: { value: '1' } })
    fireEvent.click(await screen.findByRole('button', { name: '查看事项' }))
    expect(screen.getByText('本期宽基计划 · 定投待确认')).toBeTruthy()
    expect(screen.queryByText('另一账户计划 · 定投待确认')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '记录成交' }))
    const planView = screen.getByTestId('wealth-content')
    expect(planView.textContent).toBe('dca:1')
    expect(planView.getAttribute('data-focus-plan')).toBe('10')
    expect(screen.queryByRole('region', { name: '资产概况' })).toBeNull()
  })

  it('opens the original ledger from a strategy center transfer without writing a trade', async () => {
    const id = saveTradeTransfer({ accountId: 1, price: 98, quantity: 5, decisionId: 77, note: '纪律检查 #77', target: { symbol: '510300', name: '稳健标的', market: 'CN', side: 'sell' } })
    renderPage(`/portfolio?record_intent=${id}`)
    const dialog = await screen.findByRole('dialog', { name: '减仓 · 稳健标的' })
    expect(within(dialog).getByDisplayValue('98')).toBeTruthy()
    expect(within(dialog).getByDisplayValue('5')).toBeTruthy()
    expect(within(dialog).getByDisplayValue('纪律检查 #77')).toBeTruthy()
    expect(mocks.createTrade).not.toHaveBeenCalled()
    expect(sessionStorage.getItem(`panwatch_trade_transfer:${id}`)).toBeNull()
  })

  it('shows buy and sell together and opens the original sell bookkeeping form directly', async () => {
    const { container } = renderPage()
    await screen.findByRole('region', { name: '资产概况' })
    const table = container.querySelector('.wealth-position-table')!
    expect(within(table as HTMLElement).getByRole('button', { name: '加仓' })).toBeTruthy()
    fireEvent.click(within(table as HTMLElement).getByRole('button', { name: '减仓' }))
    const intent = await screen.findByRole('dialog', { name: '减仓 / 卖出 · 稳健标的' })
    expect(within(intent).getByRole('button', { name: '检查这笔意图' })).toBeTruthy()
    fireEvent.click(within(intent).getByRole('button', { name: '已成交，去记账' }))
    const trade = await screen.findByRole('dialog', { name: '减仓 · 稳健标的' })
    expect(within(trade).getByText('数量（股/份）')).toBeTruthy()
    expect(within(trade).getByText('成交价')).toBeTruthy()
    expect(within(trade).getByText('费用合计')).toBeTruthy()
  })

  it('opens factual bookkeeping from 记一笔 without running today’s pre-trade checks', async () => {
    renderPage()
    await screen.findByRole('region', { name: '资产概况' })
    fireEvent.click(screen.getByRole('button', { name: /记一笔/ }))
    const picker = await screen.findByRole('dialog', { name: '记一笔交易' })
    fireEvent.click(within(picker).getAllByRole('button', { name: '减仓' })[0])
    await screen.findByRole('dialog', { name: '减仓 · 稳健标的' })
    expect(screen.queryByRole('button', { name: '检查这笔意图' })).toBeNull()
    expect(mocks.fetch.mock.calls.some(([path]) => path.startsWith('/strategy/decisions'))).toBe(false)
  })

  it('keeps a saved trade successful when attaching its discipline record fails', async () => {
    const base = mocks.fetch.getMockImplementation()!
    mocks.fetch.mockImplementation(async (path: string, options?: unknown) => {
      if (path === '/strategy/decisions') return { decision_id: 31, status: 'BLOCK', summary: '卖出条件未满足', expires_at: '2099-01-01T00:00:00Z' }
      if (path === '/strategy/decisions/31/link') throw new Error('参数已变化')
      return base(path, options)
    })
    mocks.createTrade.mockResolvedValue({ trade_id: 88, fees: { total: 5.01 } })
    const { container } = renderPage()
    await screen.findByRole('region', { name: '资产概况' })
    const table = container.querySelector('.wealth-position-table')!
    fireEvent.click(within(table as HTMLElement).getByRole('button', { name: '减仓' }))
    const intent = await screen.findByRole('dialog', { name: '减仓 / 卖出 · 稳健标的' })
    fireEvent.click(within(intent).getByRole('checkbox', { name: '补充 AI 解读' }))
    fireEvent.click(within(intent).getByRole('button', { name: '检查这笔意图' }))
    await within(intent).findByText('违反策略或账户约束')
    fireEvent.click(within(intent).getByRole('button', { name: '已成交，去记账' }))
    const trade = await screen.findByRole('dialog', { name: '减仓 · 稳健标的' })
    const currentFetch = mocks.fetch.getMockImplementation()!
    mocks.fetch.mockImplementation((path: string, options?: unknown) => path === '/quotes/batch' ? new Promise(() => {}) : currentFetch(path, options))
    mocks.summaryBatch.mockImplementation(() => new Promise(() => {}))
    fireEvent.click(within(trade).getByRole('button', { name: '确认记录实际成交' }))
    await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith(expect.stringContaining('成交已保存，但检查关联未成功'), 'success'))
    expect(mocks.createTrade).toHaveBeenCalledTimes(1)
    expect(mocks.createTrade).toHaveBeenCalledWith(expect.objectContaining({ action: 'sell', quantity: 10, note: expect.stringContaining('纪律检查 #31') }))
    expect(screen.queryByRole('dialog', { name: '减仓 · 稳健标的' })).toBeNull()
  })
})
