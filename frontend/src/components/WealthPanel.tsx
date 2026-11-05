import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@panwatch/base-ui/components/ui/dialog'
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  portfolioApi,
  stocksApi,
  type DividendRecord,
  type DcaPlan,
  type StockItem,
  type TradeRecord,
  type WealthAccount,
  type WealthGoals,
  type WealthOverview,
} from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'

const money = (v: number | null | undefined) =>
  v == null || Number.isNaN(v) ? '--' : `¥${v.toFixed(2)}`

const ACTION_LABEL: Record<string, string> = {
  buy: '加仓',
  sell: '减仓',
  dca: '定投',
  dividend: '现金分红',
  reinvest: '分红再投',
}

const ACTION_CLASS: Record<string, string> = {
  buy: 'bg-rose-500/10 text-rose-600',
  sell: 'bg-emerald-500/10 text-emerald-600',
  dca: 'bg-blue-500/10 text-blue-600',
  dividend: 'bg-amber-500/10 text-amber-600',
  reinvest: 'bg-emerald-500/10 text-emerald-600',
}

const STRATEGY_LABEL: Record<string, string> = {
  aggr: '激进',
  steady: '稳健',
  '': '未设置',
}

export default function WealthPanel({
  onChanged,
  refreshKey = 0,
  mode = 'overview',
  accountId = null,
  privateMode = false,
  embedded = false,
  onOpenRecords,
  focusPlanId = null,
  onFocusPlanHandled,
}: {
  onChanged?: () => void
  refreshKey?: number
  mode?: 'overview' | 'dca' | 'records' | 'goals' | 'accounts'
  accountId?: number | null
  privateMode?: boolean
  embedded?: boolean
  onOpenRecords?: () => void
  focusPlanId?: number | null
  onFocusPlanHandled?: () => void
}) {
  const [data, setData] = useState<WealthOverview | null>(null)
  const [dividends, setDividends] = useState<DividendRecord[]>([])
  const [stocks, setStocks] = useState<StockItem[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [allPlans, setAllPlans] = useState<DcaPlan[]>([])
  const [ledgerTrades, setLedgerTrades] = useState<TradeRecord[]>([])
  const [planFilter, setPlanFilter] = useState<'all' | 'active' | 'paused'>('all')
  const sequence = useRef(0)
  const handledFocus = useRef<number | null>(null)
  const displayMoney = (value: number | null | undefined) => privateMode ? '••••' : money(value)
  const tab = mode
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState('')
  const [advice, setAdvice] = useState('')
  const [adviceMeta, setAdviceMeta] = useState<{ generated_at: string; disclaimer: string } | null>(null)
  const [adviceLoading, setAdviceLoading] = useState(false)
  const [adviceExpanded, setAdviceExpanded] = useState(false)

  const [goalsOpen, setGoalsOpen] = useState(false)
  const [goals, setGoals] = useState<WealthGoals>({
    annual_passive_income: 12000,
    emergency_months: 6,
    annual_return_target: 8,
  })

  const [planOpen, setPlanOpen] = useState(false)
  const [planForm, setPlanForm] = useState({
    account_id: 0,
    stock_id: 0,
    amount: 2000,
    frequency: 'monthly' as 'monthly' | 'weekly' | 'biweekly',
    day_of_month: 15,
    day_of_week: 0,
  })

  const [execPlan, setExecPlan] = useState<DcaPlan | null>(null)
  const [execForm, setExecForm] = useState({ price: 0, quantity: 0, trade_date: '' })

  const [feeAccount, setFeeAccount] = useState<WealthAccount | null>(null)
  const [feeForm, setFeeForm] = useState({
    commission: '0.025',
    min_commission: '5',
    stamp: '0.05',
    transfer: '0.001',
  })

  const showToast = useCallback((message: string) => { setToast(message) }, [])

  const load = useCallback(async () => {
    const request = ++sequence.current
    setLoading(true)
    setLoadError('')
    try {
      const [overview, nextPlans, nextStocks, nextTrades, nextDividends] = await Promise.all([
        portfolioApi.wealthOverview(),
        mode === 'dca' ? portfolioApi.dcaList(accountId ?? undefined) : Promise.resolve([]),
        mode === 'dca' ? stocksApi.list() : Promise.resolve([]),
        mode === 'records' ? portfolioApi.trades({ account_id: accountId ?? undefined, limit: 50 }) : Promise.resolve([]),
        mode === 'records' ? portfolioApi.dividendRecords(80) : Promise.resolve([]),
      ])
      if (request !== sequence.current) return
      setData(overview)
      setGoals(overview.goals)
      setAllPlans(nextPlans)
      setStocks(nextStocks)
      setLedgerTrades(nextTrades)
      setDividends(nextDividends)
    } catch {
      if (request === sequence.current) setLoadError('信息加载失败，请重试。未确认的数据不会作为当前结果展示。')
    } finally {
      if (request === sequence.current) setLoading(false)
    }
  }, [mode, accountId])

  useEffect(() => {
    void load()
    return () => { sequence.current += 1 }
  }, [load, refreshKey])

  const accounts = useMemo(() => (data?.accounts || []).filter(a => accountId == null || a.id === accountId), [data, accountId])
  const due = (data?.dca_due || []).filter(item => accountId == null || item.account_id === accountId)
  const trades = ledgerTrades.filter(item => accountId == null || item.account_id === accountId)
  const scopedDividends = dividends.filter(item => accountId == null || item.account_id === accountId)
  const plans = useMemo(() => allPlans.filter(p => accountId == null || p.account_id === accountId), [allPlans, accountId])
  const visiblePlans = plans
    .filter(p => planFilter === 'all' || (planFilter === 'active' ? p.enabled : !p.enabled))
    .sort((a, b) => Number(b.enabled) - Number(a.enabled) || a.next_run_date.localeCompare(b.next_run_date) || a.id - b.id)
  const scopeName = accountId == null ? '全部账户' : accounts[0]?.name || '所选账户'
  const accountNames = new Map((data?.accounts || []).map(a => [a.id, a.name]))

  useEffect(() => {
    if (focusPlanId == null) { handledFocus.current = null; return }
    if (mode !== 'dca' || loading || loadError || handledFocus.current === focusPlanId) return
    const plan = plans.find(p => p.id === focusPlanId && p.enabled)
    handledFocus.current = focusPlanId
    if (plan) {
      setExecPlan(plan)
      setExecForm({ price: 0, quantity: 0, trade_date: '' })
    } else setToast('此计划已暂停、已移除，或不在当前账户范围内。')
    onFocusPlanHandled?.()
  }, [focusPlanId, mode, loading, loadError, plans, onFocusPlanHandled])

  const togglePlan = async (plan: DcaPlan) => {
    setBusy(true)
    try {
      await portfolioApi.dcaUpdate(plan.id, { enabled: !plan.enabled })
      await load()
      onChanged?.()
      showToast(plan.enabled ? '计划已暂停，可从列表恢复' : '计划已恢复，请核实下次执行日期')
    } catch {
      showToast('计划状态更新失败，请重试')
    } finally { setBusy(false) }
  }

  const deletePlan = async (plan: DcaPlan) => {
    if (!window.confirm('删除「' + plan.name + '」的定投计划？已有交易记录会保留。')) return
    setBusy(true)
    try {
      await portfolioApi.dcaDelete(plan.id)
      await load()
      onChanged?.()
      showToast('计划已删除，交易记录保留')
    } catch {
      showToast('计划删除失败，请重试')
    } finally { setBusy(false) }
  }

  const patchStrategy = async (account: WealthAccount, strategy: '' | 'aggr' | 'steady') => {
    setBusy(true)
    try {
      await portfolioApi.updateStrategy(account.id, { strategy_type: strategy })
      await load()
      onChanged?.()
      showToast(`已将「${account.name}」切换为${STRATEGY_LABEL[strategy]}`)
    } catch (e) {
      showToast('保存失败，请重试')
    } finally {
      setBusy(false)
    }
  }

  const toggleReinvest = async (account: WealthAccount, enabled: boolean) => {
    setBusy(true)
    try {
      await portfolioApi.updateStrategy(account.id, { dividend_reinvest: enabled })
      await load()
      onChanged?.()
      showToast(enabled ? '已开启分红再投资' : '已关闭分红再投资')
    } catch {
      showToast('保存失败，请重试')
    } finally {
      setBusy(false)
    }
  }

  const openFee = (account: WealthAccount) => {
    const p = account.fee_policy || {}
    setFeeForm({
      commission: String(((p.commission_rate ?? 0.00025) * 100).toFixed(4)),
      min_commission: String(p.min_commission ?? 5),
      stamp: String(((p.stamp_duty_rate ?? 0.0005) * 100).toFixed(4)),
      transfer: String(((p.transfer_fee_rate ?? 0.00001) * 100).toFixed(4)),
    })
    setFeeAccount(account)
  }

  const saveFee = async () => {
    if (!feeAccount) return
    if (Object.values(feeForm).some(value => !Number.isFinite(Number(value)) || Number(value) < 0)) { showToast('费用必须为有效的非负数'); return }
    setBusy(true)
    try {
      await portfolioApi.updateStrategy(feeAccount.id, {
        fee_policy: {
          commission_rate: Number(feeForm.commission) / 100,
          min_commission: Number(feeForm.min_commission),
          stamp_duty_rate: Number(feeForm.stamp) / 100,
          transfer_fee_rate: Number(feeForm.transfer) / 100,
        },
      })
      setFeeAccount(null)
      await load()
      onChanged?.()
      showToast('费用设置已保存')
    } catch {
      showToast('保存失败，请检查数值')
    } finally {
      setBusy(false)
    }
  }

  const saveGoals = async () => {
    if (Object.values(goals).some(value => !Number.isFinite(value) || value < 0)) { showToast('目标必须为有效的非负数'); return }
    setBusy(true)
    try {
      const next = await portfolioApi.updateGoals(goals)
      setGoals(next)
      setGoalsOpen(false)
      await load()
      onChanged?.()
      showToast('理财目标已更新')
    } catch {
      showToast('保存失败，请重试')
    } finally {
      setBusy(false)
    }
  }

  const submitPlan = async () => {
    if (!planForm.account_id || !planForm.stock_id || !Number.isFinite(planForm.amount) || planForm.amount <= 0) {
      showToast('请选择账户和标的，并填写大于零的金额')
      return
    }
    setBusy(true)
    try {
      await portfolioApi.dcaCreate({
        account_id: planForm.account_id,
        stock_id: planForm.stock_id,
        amount: planForm.amount,
        frequency: planForm.frequency,
        day_of_month: planForm.day_of_month,
        day_of_week: planForm.day_of_week,
      })
      setPlanOpen(false)
      await load()
      onChanged?.()
      showToast('定投计划已创建')
    } catch {
      showToast('创建失败，请检查金额/标的')
    } finally {
      setBusy(false)
    }
  }

  const submitExec = async () => {
    if (!execPlan) return
    if (!Number.isFinite(execForm.price) || execForm.price <= 0 || execForm.quantity < 0) { showToast('请填写有效成交价和数量'); return }
    setBusy(true)
    try {
      await portfolioApi.dcaExecute(execPlan.id, {
        price: execForm.price,
        quantity: execForm.quantity || undefined,
        trade_date: execForm.trade_date || undefined,
      })
      setExecPlan(null)
      await load()
      onChanged?.()
      showToast('已记录本期定投')
    } catch (e: any) {
      showToast(e?.message || '记账失败，请检查价格')
    } finally {
      setBusy(false)
    }
  }

  const generateAdvice = async () => {
    setAdviceLoading(true)
    try {
      const res = await portfolioApi.wealthAiAdvice()
      if (res.empty) {
        showToast('还没有账户，先添加账户再生成建议')
        return
      }
      setAdvice(res.content || '')
      setAdviceMeta({ generated_at: res.generated_at, disclaimer: res.disclaimer })
    } catch (e: any) {
      showToast(e?.message || 'AI 建议生成失败')
    } finally {
      setAdviceLoading(false)
    }
  }

  const runDividends = async () => {
    setBusy(true)
    try {
      const res = await portfolioApi.processDividends(3)
      await load()
      onChanged?.()
      showToast(`全部账户分红处理完成，新增 ${res.recorded} 笔`)
    } catch {
      showToast('分红处理失败')
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <div className="py-8 text-sm text-muted-foreground" role="status">信息加载中…</div>
  if (loadError || !data) return (
    <div className="space-y-3 py-5" role="alert">
      <p className="text-sm text-muted-foreground">{loadError || '理财信息加载失败'}</p>
      <Button variant="secondary" onClick={() => void load()}>重试</Button>
    </div>
  )

  return (
    <div className="space-y-5">
      {!embedded && <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold">{({ overview: '全账户组合分析', dca: '计划', records: '统一账本', goals: '理财目标', accounts: '账户设置' })[tab]}</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            {tab === 'overview' ? '分析覆盖全部账户，不受账户筛选影响。' : tab === 'goals' ? '全局规划参数，不代表收益承诺。' : scopeName + ' · ' + (tab === 'records' ? '已记录的交易与分红' : '计划与记账分别管理，不自动下单')}
          </p>
        </div>
        {tab === 'dca' && <Button disabled={!accounts.length || busy} onClick={() => {
          setPlanForm(f => ({ ...f, account_id: accountId ?? accounts[0]?.id ?? 0, stock_id: 0 }))
          setPlanOpen(true)
        }}>新建定投</Button>}
      </header>}

      {tab === 'overview' && (
        <section className="space-y-4">
          <Button variant="secondary" onClick={generateAdvice} disabled={adviceLoading || privateMode}>
            {adviceLoading ? '分析中…' : advice ? '重新分析全部账户' : '分析全部账户'}
          </Button>
          {privateMode ? <p className="text-sm text-muted-foreground">隐私模式下隐藏组合分析，请关闭隐私模式后查看。</p> : advice ? (
            <>
              <div className={'whitespace-pre-wrap text-sm leading-7 ' + (adviceExpanded ? '' : 'line-clamp-8')}>{advice}</div>
              <button type="button" className="min-h-10 text-sm text-primary" onClick={() => setAdviceExpanded(v => !v)}>{adviceExpanded ? '收起建议' : '展开完整建议'}</button>
              <div className="text-xs leading-6 text-muted-foreground">
                {adviceMeta?.generated_at && <p>生成于 {adviceMeta.generated_at.replace('T', ' ')}</p>}
                <p>{adviceMeta?.disclaimer || 'AI 建议仅供研究参考，不构成投资建议。'}</p>
                <p>分析不会随行情自动更新，涉及外币或缺失行情时请核对数据口径。</p>
              </div>
            </>
          ) : <p className="text-sm leading-6 text-muted-foreground">需要时再生成组合分析。单个标的的行情、风控与操作计划请从持仓详情查看。</p>}
        </section>
      )}

      {(tab === 'dca' || tab === 'goals') && (
        <section className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-4" aria-label="理财目标摘要">
          <p className="text-xs leading-6 text-muted-foreground">全局目标：年被动收入 {displayMoney(data.goals.annual_passive_income)} · 应急金 {privateMode ? '••••' : data.goals.emergency_months} 个月 · 目标年化 {privateMode ? '••••' : data.goals.annual_return_target}%</p>
          <Button variant="ghost" size="sm" onClick={() => { setGoals({ ...data.goals }); setGoalsOpen(true) }}>编辑目标</Button>
        </section>
      )}

      {tab === 'accounts' && (
        <div className="space-y-4">
          {!accounts.length && <EmptyState>当前范围没有账户，请先添加账户。</EmptyState>}
          {accounts.map(account => (
            <section key={account.id} className="rounded-xl border border-border p-4 space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h3 className="text-sm font-medium">{account.name}</h3>
                  <p className="mt-1 text-xs text-muted-foreground">{account.positions.length} 只持仓 · 可用 {displayMoney(account.available_funds)}</p>
                </div>
                <Button variant="secondary" size="sm" onClick={() => openFee(account)} disabled={busy}>费用设置</Button>
              </div>
              <fieldset disabled={busy}>
                <legend className="text-xs text-muted-foreground mb-2">账户策略</legend>
                <div className="flex flex-wrap gap-2">
                  {(['steady', 'aggr', ''] as const).map(type => (
                    <Button key={type || 'none'} size="sm" variant={account.strategy_type === type ? 'secondary' : 'ghost'} aria-pressed={account.strategy_type === type} onClick={() => void patchStrategy(account, type)}>{STRATEGY_LABEL[type]}</Button>
                  ))}
                </div>
                <p className="mt-2 text-xs text-muted-foreground">切换策略会恢复该策略的默认风险参数。</p>
              </fieldset>
              {account.strategy_type && (
                <p className="text-xs text-muted-foreground">
                  单仓上限 {privateMode ? '••••' : account.risk_policy?.max_single_weight != null ? (Number(account.risk_policy.max_single_weight) * 100).toFixed(0) + '%' : '--'}
                  {' · '}最少 {privateMode ? '••••' : account.risk_policy?.min_positions ?? '--'} 只持仓
                </p>
              )}
              <label className="flex items-start gap-3 text-sm">
                <input type="checkbox" className="mt-1" disabled={busy} checked={account.dividend_reinvest} onChange={event => void toggleReinvest(account, event.target.checked)} />
                <span>分红再投<span className="block mt-1 text-xs text-muted-foreground">仅影响此账户的分红记账规则。实际券商成交仍需核对。</span></span>
              </label>
            </section>
          ))}
        </div>
      )}

      {tab === 'dca' && (
        <>
          <section aria-label="定投计划" className="space-y-4">
            <div className="flex flex-wrap gap-2" aria-label="计划状态筛选">
              {([['all', '全部 ' + plans.length], ['active', '进行中 ' + plans.filter(p => p.enabled).length], ['paused', '已暂停 ' + plans.filter(p => !p.enabled).length]] as const).map(([filter, label]) => <Button key={filter} size="sm" variant={planFilter === filter ? 'secondary' : 'ghost'} aria-pressed={planFilter === filter} onClick={() => setPlanFilter(filter)}>{label}</Button>)}
            </div>
            {!visiblePlans.length && <EmptyState>{!accounts.length ? '当前范围没有账户，请先添加账户。' : !plans.length ? '还没有定投计划，可为已有标的创建按月或按周计划。' : '当前筛选下没有计划，请切换状态查看。'}</EmptyState>}
            <div className="space-y-3">
              {visiblePlans.map(plan => (
                <article key={plan.id} className="rounded-xl border border-border p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div><h3 className="font-medium">{plan.name} <span className="ml-1 text-xs text-muted-foreground">{plan.symbol}</span></h3><p className="mt-1 text-xs text-muted-foreground">{plan.account_name} · {plan.frequency === 'monthly' ? '每月' : plan.frequency === 'weekly' ? '每周' : '每两周'} {displayMoney(plan.amount)}</p></div>
                    <span className={'rounded-md px-2 py-1 text-xs ' + (!plan.enabled ? 'bg-muted text-muted-foreground' : due.some(d => d.id === plan.id) ? 'bg-amber-500/10 text-amber-800 dark:text-amber-300' : 'bg-primary/10 text-primary')}>{!plan.enabled ? '已暂停' : due.some(d => d.id === plan.id) ? '待确认成交' : '进行中'}</span>
                  </div>
                  <p className="mt-3 text-xs text-muted-foreground">下次计划 {plan.next_run_date || '未设置'} · 已记录 {plan.executed_count} 期 · 累计投入 {displayMoney(plan.total_invested)}</p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button size="sm" variant="secondary" disabled={busy || !plan.enabled} onClick={() => { setExecPlan(plan); setExecForm({ price: 0, quantity: 0, trade_date: '' }) }}>记录成交</Button>
                    <Button size="sm" variant="ghost" disabled={busy} onClick={() => void togglePlan(plan)}>{plan.enabled ? '暂停' : '恢复'}</Button>
                    <Button size="sm" variant="ghost" disabled={busy} onClick={() => void deletePlan(plan)}>删除计划</Button>
                  </div>
                </article>
              ))}
            </div>
          </section>
          <section className="border-t border-border pt-5 space-y-3" aria-label="分红记账">
            <h3 className="text-sm font-medium">分红记账</h3>
            <p className="text-xs leading-6 text-muted-foreground">再投资规则在各账户设置中管理。分红处理覆盖全部账户，按各账户规则记录近期 A 股分红，不受当前账户筛选影响。</p>
            <div className="flex flex-wrap gap-2">
              <Button variant="secondary" size="sm" disabled={busy} onClick={runDividends}>处理全部账户分红</Button>
              {onOpenRecords && <Button variant="ghost" size="sm" onClick={onOpenRecords}>查看{accountId == null ? '统一' : '此账户'}账本</Button>}
            </div>
          </section>
        </>
      )}

      {tab === 'records' && (
        <div className="space-y-6">
          <section aria-label="交易记录">
            <h3 className="text-sm font-medium mb-3">交易记录</h3>
            <TradeTable rows={trades} accountNames={accountNames} privateMode={privateMode} />
            <p className="mt-2 text-xs text-muted-foreground">最近 50 笔记录；金额沿用成交记账口径，不合并为多币种收益。</p>
          </section>
          <section aria-label="分红入账记录">
            <h3 className="text-sm font-medium mb-3">分红入账记录</h3>
            <DividendTable rows={scopedDividends} privateMode={privateMode} />
            <p className="mt-2 text-xs text-muted-foreground">仅展示已有记录，打开账本不会触发分红处理。账户筛选作用于最近 80 笔分红记录。</p>
          </section>
        </div>
      )}
      {tab === 'goals' && <p className="text-sm leading-6 text-muted-foreground">目标只用于规划与 AI 分析参考，尚未记录月支出与完整收入口径，因此不展示应急金或被动收入达成率。</p>}

      {/* 目标编辑 */}
      {goalsOpen && (
        <Modal feedback={toast} title="理财目标" onClose={() => setGoalsOpen(false)}>
          <div className="grid grid-cols-2 gap-2">
            <Field label="年度被动收入（元）">
              <input
                type="number"
                className="input"
                min={0}
                value={goals.annual_passive_income}
                onChange={(e) => setGoals({ ...goals, annual_passive_income: Number(e.target.value) })}
              />
            </Field>
            <Field label="应急金（月）">
              <input
                type="number"
                className="input"
                min={0}
                value={goals.emergency_months}
                onChange={(e) => setGoals({ ...goals, emergency_months: Number(e.target.value) })}
              />
            </Field>
            <Field label="目标年化（%）">
              <input
                type="number"
                className="input"
                min={0}
                value={goals.annual_return_target}
                onChange={(e) => setGoals({ ...goals, annual_return_target: Number(e.target.value) })}
              />
            </Field>
          </div>
          <ModalActions onCancel={() => setGoalsOpen(false)} onSubmit={saveGoals} busy={busy} />
        </Modal>
      )}

      {/* 费用设置 */}
      {feeAccount && (
        <Modal feedback={toast} title={`费用设置 · ${feeAccount.name}`} onClose={() => setFeeAccount(null)}>
          <div className="text-[12px] text-muted-foreground">
            按你券商的真实费率填写；交易时仍可手动覆盖单笔费用。
          </div>
          <div className="mt-2 grid grid-cols-2 gap-2">
            <Field label="佣金费率（%）">
              <input
                type="number"
                step="0.001"
                className="input"
                value={feeForm.commission}
                onChange={(e) => setFeeForm({ ...feeForm, commission: e.target.value })}
              />
            </Field>
            <Field label="最低佣金（元）">
              <input
                type="number"
                step="0.01"
                className="input"
                value={feeForm.min_commission}
                onChange={(e) => setFeeForm({ ...feeForm, min_commission: e.target.value })}
              />
            </Field>
            <Field label="印花税率（%，仅卖出）">
              <input
                type="number"
                step="0.001"
                className="input"
                value={feeForm.stamp}
                onChange={(e) => setFeeForm({ ...feeForm, stamp: e.target.value })}
              />
            </Field>
            <Field label="过户费率（%）">
              <input
                type="number"
                step="0.001"
                className="input"
                value={feeForm.transfer}
                onChange={(e) => setFeeForm({ ...feeForm, transfer: e.target.value })}
              />
            </Field>
          </div>
          <ModalActions onCancel={() => setFeeAccount(null)} onSubmit={saveFee} busy={busy} />
        </Modal>
      )}

      {/* 新建定投 */}
      {planOpen && (
        <Modal feedback={toast} title="新建定投计划" onClose={() => setPlanOpen(false)}>
          <div className="grid grid-cols-2 gap-2">
            <Field label="账户">
              <select
                className="input"
                value={planForm.account_id}
                onChange={(e) => setPlanForm({ ...planForm, account_id: Number(e.target.value) })}
              >
                {accounts.map((a) => (
                  <option key={a.id} value={a.id}>{a.name}</option>
                ))}
              </select>
            </Field>
            <Field label="标的">
              <select
                className="input"
                value={planForm.stock_id}
                onChange={(e) => setPlanForm({ ...planForm, stock_id: Number(e.target.value) })}
              >
                <option value={0}>请选择</option>
                {stocks.map((s) => (
                  <option key={s.id} value={s.id}>{s.name} {s.symbol}</option>
                ))}
              </select>
            </Field>
            <Field label="每期金额">
              <input
                type="number"
                className="input"
                value={planForm.amount}
                onChange={(e) => setPlanForm({ ...planForm, amount: Number(e.target.value) })}
              />
            </Field>
            <Field label="频率">
              <select
                className="input"
                value={planForm.frequency}
                onChange={(e) => setPlanForm({ ...planForm, frequency: e.target.value as typeof planForm.frequency })}
              >
                <option value="monthly">每月</option>
                <option value="weekly">每周</option>
                <option value="biweekly">每两周</option>
              </select>
            </Field>
            {planForm.frequency === 'monthly' ? (
              <Field label="每月几号">
                <input
                  type="number"
                  min={1}
                  max={31}
                  className="input"
                  value={planForm.day_of_month}
                  onChange={(e) => setPlanForm({ ...planForm, day_of_month: Number(e.target.value) })}
                />
              </Field>
            ) : (
              <Field label="星期几">
                <select
                  className="input"
                  value={planForm.day_of_week}
                  onChange={(e) => setPlanForm({ ...planForm, day_of_week: Number(e.target.value) })}
                >
                  {['周一', '周二', '周三', '周四', '周五'].map((d, i) => (
                    <option key={d} value={i}>{d}</option>
                  ))}
                </select>
              </Field>
            )}
          </div>
          <ModalActions onCancel={() => setPlanOpen(false)} onSubmit={submitPlan} busy={busy} />
        </Modal>
      )}

      {/* 记一笔定投 */}
      {execPlan && (
        <Modal feedback={toast} title={`记一笔 · ${execPlan.name}`} onClose={() => setExecPlan(null)}>
          <div className="text-[12px] text-muted-foreground">
            计划金额 {money(execPlan.amount)}；未填数量时按金额/成交价自动计算份额。
          </div>
          <div className="mt-2 grid grid-cols-2 gap-2">
            <Field label="成交价">
              <input
                type="number"
                step="0.001"
                className="input"
                value={execForm.price || ''}
                onChange={(e) => setExecForm({ ...execForm, price: Number(e.target.value) })}
              />
            </Field>
            <Field label="数量（可空）">
              <input
                type="number"
                className="input"
                value={execForm.quantity || ''}
                onChange={(e) => setExecForm({ ...execForm, quantity: Number(e.target.value) })}
              />
            </Field>
            <Field label="成交日期（可空）">
              <input
                type="date"
                className="input"
                value={execForm.trade_date}
                onChange={(e) => setExecForm({ ...execForm, trade_date: e.target.value })}
              />
            </Field>
          </div>
          <ModalActions onCancel={() => setExecPlan(null)} onSubmit={submitExec} busy={busy} />
        </Modal>
      )}

      {toast && <div className="rounded-lg bg-muted p-3 text-sm" role="status">{toast}</div>}

    </div>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-[12px] text-muted-foreground">
      {label}
      <span className="[&_.input]:w-full [&_.input]:rounded-md [&_.input]:border [&_.input]:border-border [&_.input]:bg-background [&_.input]:px-2 [&_.input]:py-1.5 [&_.input]:text-[12px] [&_.input]:text-foreground">
        {children}
      </span>
    </label>
  )
}

function Modal({
  title,
  children,
  onClose,
  feedback,
}: {
  title: string
  children: ReactNode
  onClose: () => void
  feedback?: string
}) {
  return (
    <Dialog open onOpenChange={open => { if (!open) onClose() }}>
      <DialogContent className="max-w-md max-h-[85vh] overflow-y-auto">
        <DialogHeader><DialogTitle>{title}</DialogTitle><DialogDescription>确认信息后保存到理财记账，不会自动下单。</DialogDescription></DialogHeader>
        {children}
        {feedback && <p role="status" className="rounded-lg bg-muted p-3 text-sm">{feedback}</p>}
      </DialogContent>
    </Dialog>
  )
}

function ModalActions({
  onCancel,
  onSubmit,
  busy,
}: {
  onCancel: () => void
  onSubmit: () => void
  busy: boolean
}) {
  return (
    <div className="mt-4 flex justify-end gap-2">
      <Button variant="secondary" size="sm" onClick={onCancel}>取消</Button>
      <Button size="sm" onClick={onSubmit} disabled={busy}>{busy ? '保存中…' : '确认'}</Button>
    </div>
  )
}

function TradeTable({ rows, accountNames, privateMode }: { rows: TradeRecord[]; accountNames: Map<number, string>; privateMode: boolean }) {
  if (!rows.length) {
    return <div className="py-4 text-center text-[12px] text-muted-foreground">暂无交易记录</div>
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full">
        <thead>
          <tr className="border-b border-border/30 text-[12px] text-muted-foreground">
            <th className="px-2 py-1.5 text-left">日期</th>
            <th className="px-2 py-1.5 text-left">类型</th>
            <th className="px-2 py-1.5 text-left">标的</th>
            <th className="px-2 py-1.5 text-right">数量</th>
            <th className="px-2 py-1.5 text-right">记账金额</th>
            <th className="px-2 py-1.5 text-right">费用</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-b border-border/20 text-[12px]">
              <td className="px-2 py-1.5 font-mono text-muted-foreground">{r.trade_date}<span className="block mt-1">{accountNames.get(r.account_id) || "账户 " + r.account_id}</span></td>
              <td className="px-2 py-1.5">
                <span className={`rounded px-1.5 py-0.5 text-[12px] ${ACTION_CLASS[r.action] || 'bg-muted'}`}>
                  {ACTION_LABEL[r.action] || r.action}
                </span>
              </td>
              <td className="px-2 py-1.5">{r.name || r.symbol}</td>
              <td className="px-2 py-1.5 text-right font-mono">{privateMode ? '••••' : r.quantity || '--'}</td>
              <td className="px-2 py-1.5 text-right font-mono">{privateMode ? '••••' : money(r.amount).replace('¥', '')}</td>
              <td className="px-2 py-1.5 text-right font-mono text-muted-foreground">{privateMode ? '••••' : money(r.total_fee).replace('¥', '')}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function DividendTable({ rows, privateMode }: { rows: DividendRecord[]; privateMode: boolean }) {
  if (!rows.length) {
    return <div className="py-4 text-center text-[12px] text-muted-foreground">暂无分红记录</div>
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full">
        <thead>
          <tr className="border-b border-border/30 text-[12px] text-muted-foreground">
            <th className="px-2 py-1.5 text-left">除权日</th>
            <th className="px-2 py-1.5 text-left">标的</th>
            <th className="px-2 py-1.5 text-right">金额(税前)</th>
            <th className="px-2 py-1.5 text-right">处理</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-b border-border/20 text-[12px]">
              <td className="px-2 py-1.5 font-mono text-muted-foreground">{r.ex_date}</td>
              <td className="px-2 py-1.5">
                {r.name} <span className="text-[12px] text-muted-foreground">{r.account_name}</span>
              </td>
              <td className="px-2 py-1.5 text-right font-mono">{privateMode ? '••••' : money(r.amount)}</td>
              <td className="px-2 py-1.5 text-right text-muted-foreground">
                {r.action === 'reinvest' ? privateMode ? '再投 •••• 份' : '再投 ' + r.shares_added + ' 份' : '现金'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function EmptyState({ children }: { children: ReactNode }) {
  return <p className="rounded-xl border border-dashed border-border px-5 py-8 text-center text-sm leading-6 text-muted-foreground">{children}</p>
}
