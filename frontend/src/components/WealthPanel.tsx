import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
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

function StrategyBadge({ type }: { type: string }) {
  const cls =
    type === 'aggr'
      ? 'bg-rose-500/10 text-rose-600'
      : type === 'steady'
        ? 'bg-blue-500/10 text-blue-600'
        : 'bg-muted text-muted-foreground'
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] ${cls}`}>
      {STRATEGY_LABEL[type] || '未设置'}
    </span>
  )
}

export default function WealthPanel({ onChanged }: { onChanged?: () => void }) {
  const [data, setData] = useState<WealthOverview | null>(null)
  const [dividends, setDividends] = useState<DividendRecord[]>([])
  const [stocks, setStocks] = useState<StockItem[]>([])
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState<'overview' | 'dca' | 'records'>('overview')
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState('')

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

  const showToast = useCallback((msg: string) => {
    setToast(msg)
    window.setTimeout(() => setToast(''), 2400)
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [ov, dv, st] = await Promise.allSettled([
        portfolioApi.wealthOverview(),
        portfolioApi.dividendRecords(80),
        stocksApi.list(),
      ])
      if (ov.status === 'fulfilled') {
        setData(ov.value)
        setGoals(ov.value.goals)
      }
      if (dv.status === 'fulfilled') setDividends(dv.value)
      if (st.status === 'fulfilled') setStocks(st.value)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const accounts = data?.accounts || []
  const due = data?.dca_due || []
  const trades = data?.trades || []
  const plans = useMemo(() => accounts.flatMap((a) => a.dca_plans || []), [accounts])

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
      showToast(enabled ? '已开启分红再投资' : '已关闭分红再投资')
    } catch {
      showToast('保存失败，请重试')
    } finally {
      setBusy(false)
    }
  }

  const saveGoals = async () => {
    setBusy(true)
    try {
      const next = await portfolioApi.updateGoals(goals)
      setGoals(next)
      setGoalsOpen(false)
      await load()
      showToast('理财目标已更新')
    } catch {
      showToast('保存失败，请重试')
    } finally {
      setBusy(false)
    }
  }

  const submitPlan = async () => {
    if (!planForm.account_id || !planForm.stock_id) {
      showToast('请选择账户和标的')
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
      showToast('定投计划已创建')
    } catch {
      showToast('创建失败，请检查金额/标的')
    } finally {
      setBusy(false)
    }
  }

  const submitExec = async () => {
    if (!execPlan) return
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

  const runDividends = async () => {
    setBusy(true)
    try {
      const res = await portfolioApi.processDividends(3)
      await load()
      onChanged?.()
      showToast(`分红处理完成，新增 ${res.recorded} 笔`)
    } catch {
      showToast('分红处理失败')
    } finally {
      setBusy(false)
    }
  }

  if (loading) {
    return <div className="card p-4 mb-4 text-[12px] text-muted-foreground">理财信息加载中…</div>
  }
  if (!data) return null

  return (
    <div className="card mb-4 overflow-hidden">
      <div className="flex flex-wrap items-center gap-2 border-b border-border/40 px-4 py-3">
        <h2 className="text-[14px] font-semibold">理财账户</h2>
        <span className="text-[11px] text-muted-foreground">
          策略、定投、分红与交易流水；全部为记账，不会自动下单
        </span>
        <div className="ml-auto inline-flex rounded-lg bg-muted p-0.5">
          {([
            ['overview', '概览'],
            ['dca', `定投${due.length ? ` · ${due.length}` : ''}`],
            ['records', '流水'],
          ] as const).map(([key, label]) => (
            <button
              key={key}
              onClick={() => setTab(key)}
              className={`rounded-md px-3 py-1 text-[11px] ${
                tab === key ? 'bg-background font-medium shadow-sm' : 'text-muted-foreground'
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="p-4 space-y-3">
        {tab === 'overview' && (
          <>
            <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
              <div className="rounded-lg border border-border/50 p-3">
                <div className="text-[10px] text-muted-foreground">年度被动收入目标</div>
                <div className="mt-1 font-mono text-[16px] font-semibold">
                  {money(goals.annual_passive_income)}
                </div>
              </div>
              <div className="rounded-lg border border-border/50 p-3">
                <div className="text-[10px] text-muted-foreground">应急金目标</div>
                <div className="mt-1 font-mono text-[16px] font-semibold">
                  {goals.emergency_months} 个月
                </div>
              </div>
              <div className="rounded-lg border border-border/50 p-3">
                <div className="text-[10px] text-muted-foreground">目标年化</div>
                <div className="mt-1 font-mono text-[16px] font-semibold">
                  {goals.annual_return_target}%
                </div>
              </div>
              <div className="flex items-end">
                <Button variant="secondary" size="sm" className="h-8" onClick={() => setGoalsOpen(true)}>
                  编辑目标
                </Button>
              </div>
            </div>

            <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
              {accounts.map((account) => (
                <div key={account.id} className="rounded-lg border border-border/50 p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[13px] font-semibold">{account.name}</span>
                    <StrategyBadge type={account.strategy_type} />
                    <span className="text-[11px] text-muted-foreground">
                      {account.positions.length} 只 · 可用 {money(account.available_funds)}
                    </span>
                  </div>
                  <div className="mt-2 flex flex-wrap items-center gap-1">
                    {(['aggr', 'steady', ''] as const).map((type) => (
                      <button
                        key={type || 'none'}
                        disabled={busy}
                        onClick={() => patchStrategy(account, type)}
                        className={`rounded-md border px-2 py-0.5 text-[10px] ${
                          account.strategy_type === type
                            ? 'border-primary bg-primary/10 text-primary'
                            : 'border-border text-muted-foreground hover:bg-accent/40'
                        }`}
                      >
                        {STRATEGY_LABEL[type]}
                      </button>
                    ))}
                    <label className="ml-auto inline-flex items-center gap-1 text-[10px] text-muted-foreground">
                      <input
                        type="checkbox"
                        checked={account.dividend_reinvest}
                        onChange={(e) => toggleReinvest(account, e.target.checked)}
                      />
                      分红再投
                    </label>
                  </div>
                  {account.strategy_type && (
                    <div className="mt-1 text-[10px] text-muted-foreground">
                      单仓上限{' '}
                      {account.risk_policy?.max_single_weight != null
                        ? `${(Number(account.risk_policy.max_single_weight) * 100).toFixed(0)}%`
                        : '--'}
                      {' · '}最少 {account.risk_policy?.min_positions ?? '--'} 只
                    </div>
                  )}
                  {account.dca_plans.length > 0 && (
                    <div className="mt-1 text-[10px] text-muted-foreground">
                      定投：{account.dca_plans
                        .map((p) => `${p.name} ${money(p.amount)}/${p.frequency === 'monthly' ? '月' : p.frequency === 'weekly' ? '周' : '双周'}`)
                        .join('；')}
                    </div>
                  )}
                </div>
              ))}
            </div>

            <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
              <div>
                <div className="mb-1 text-[11px] text-muted-foreground">最近交易</div>
                <TradeTable rows={trades.slice(0, 5)} />
              </div>
              <div>
                <div className="mb-1 flex items-center justify-between text-[11px] text-muted-foreground">
                  <span>最近分红</span>
                  <button className="text-[10px] text-primary" onClick={runDividends} disabled={busy}>
                    立即处理
                  </button>
                </div>
                <DividendTable rows={dividends.slice(0, 5)} />
              </div>
            </div>
          </>
        )}

        {tab === 'dca' && (
          <>
            {due.length > 0 && (
              <div className="rounded-lg border border-amber-300/60 bg-amber-50 px-3 py-2 text-[12px] text-amber-800">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">定投提醒</span>
                  <span>
                    {due
                      .map(
                        (d) =>
                          `${d.account_name} · ${d.name} ${money(d.amount)}（${d.next_run_date}${d.overdue ? '，已逾期' : ''}）`,
                      )
                      .join('；')}
                  </span>
                  <Button
                    size="sm"
                    className="ml-auto h-7"
                    onClick={() => {
                      const plan = plans.find((p) => p.id === due[0].id)
                      if (plan) {
                        setExecPlan(plan)
                        setExecForm({ price: 0, quantity: 0, trade_date: '' })
                      }
                    }}
                  >
                    记一笔
                  </Button>
                </div>
              </div>
            )}

            <div className="flex items-center justify-between">
              <div className="text-[11px] text-muted-foreground">
                共 {plans.length} 个计划。到期只提醒，不自动扣款。
              </div>
              <Button size="sm" variant="secondary" className="h-8" onClick={() => {
                setPlanForm((f) => ({ ...f, account_id: accounts[0]?.id || 0 }))
                setPlanOpen(true)
              }}>
                新建定投
              </Button>
            </div>

            <div className="overflow-x-auto">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-border/30 text-[10px] text-muted-foreground">
                    <th className="px-2 py-1.5 text-left">账户 / 标的</th>
                    <th className="px-2 py-1.5 text-right">金额</th>
                    <th className="px-2 py-1.5 text-right">频率</th>
                    <th className="px-2 py-1.5 text-right">下次</th>
                    <th className="px-2 py-1.5 text-right">已投</th>
                    <th className="px-2 py-1.5 text-center">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {plans.map((p) => (
                    <tr key={p.id} className="border-b border-border/20 text-[12px]">
                      <td className="px-2 py-1.5">
                        <div>{p.name} <span className="text-[10px] text-muted-foreground">{p.symbol}</span></div>
                        <div className="text-[10px] text-muted-foreground">{p.account_name}</div>
                      </td>
                      <td className="px-2 py-1.5 text-right font-mono">{money(p.amount)}</td>
                      <td className="px-2 py-1.5 text-right text-muted-foreground">
                        {p.frequency === 'monthly' ? `每月 ${p.day_of_month} 日` : p.frequency === 'weekly' ? `每周${'一二三四五六日'[p.day_of_week] || ''}` : '双周'}
                      </td>
                      <td className="px-2 py-1.5 text-right font-mono">{p.next_run_date || '--'}</td>
                      <td className="px-2 py-1.5 text-right font-mono">{p.executed_count} 期</td>
                      <td className="px-2 py-1.5 text-center">
                        <button
                          className="text-[11px] text-primary"
                          onClick={() => {
                            setExecPlan(p)
                            setExecForm({ price: 0, quantity: 0, trade_date: '' })
                          }}
                        >
                          记一笔
                        </button>
                        <button
                          className="ml-2 text-[11px] text-muted-foreground hover:text-destructive"
                          onClick={async () => {
                            if (!window.confirm(`删除「${p.name}」的定投计划？`)) return
                            await portfolioApi.dcaDelete(p.id)
                            await load()
                          }}
                        >
                          删除
                        </button>
                      </td>
                    </tr>
                  ))}
                  {plans.length === 0 && (
                    <tr>
                      <td colSpan={6} className="px-2 py-6 text-center text-[12px] text-muted-foreground">
                        还没有定投计划
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </>
        )}

        {tab === 'records' && (
          <>
            <div className="flex items-center justify-between">
              <div className="text-[11px] text-muted-foreground">最近 50 笔交易与分红入账</div>
              <Button size="sm" variant="secondary" className="h-8" onClick={runDividends} disabled={busy}>
                处理分红
              </Button>
            </div>
            <TradeTable rows={trades} />
            <div className="pt-2">
              <div className="mb-1 text-[11px] text-muted-foreground">分红入账</div>
              <DividendTable rows={dividends} />
            </div>
          </>
        )}
      </div>

      {/* 目标编辑 */}
      {goalsOpen && (
        <Modal title="理财目标" onClose={() => setGoalsOpen(false)}>
          <div className="grid grid-cols-2 gap-2">
            <Field label="年度被动收入（元）">
              <input
                type="number"
                className="input"
                value={goals.annual_passive_income}
                onChange={(e) => setGoals({ ...goals, annual_passive_income: Number(e.target.value) })}
              />
            </Field>
            <Field label="应急金（月）">
              <input
                type="number"
                className="input"
                value={goals.emergency_months}
                onChange={(e) => setGoals({ ...goals, emergency_months: Number(e.target.value) })}
              />
            </Field>
            <Field label="目标年化（%）">
              <input
                type="number"
                className="input"
                value={goals.annual_return_target}
                onChange={(e) => setGoals({ ...goals, annual_return_target: Number(e.target.value) })}
              />
            </Field>
          </div>
          <ModalActions onCancel={() => setGoalsOpen(false)} onSubmit={saveGoals} busy={busy} />
        </Modal>
      )}

      {/* 新建定投 */}
      {planOpen && (
        <Modal title="新建定投计划" onClose={() => setPlanOpen(false)}>
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
                  max={28}
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
        <Modal title={`记一笔 · ${execPlan.name}`} onClose={() => setExecPlan(null)}>
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

      {toast && (
        <div className="fixed bottom-6 left-1/2 z-[60] -translate-x-1/2 rounded-lg bg-foreground px-4 py-2 text-[12px] text-background shadow-lg">
          {toast}
        </div>
      )}
    </div>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-[11px] text-muted-foreground">
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
}: {
  title: string
  children: ReactNode
  onClose: () => void
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/35 p-4" onClick={onClose}>
      <div className="w-full max-w-md rounded-xl border border-border bg-background p-4 shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="mb-3 flex items-center justify-between">
          <div className="text-[14px] font-semibold">{title}</div>
          <button className="text-muted-foreground hover:text-foreground" onClick={onClose}>×</button>
        </div>
        {children}
      </div>
    </div>
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

function TradeTable({ rows }: { rows: TradeRecord[] }) {
  if (!rows.length) {
    return <div className="py-4 text-center text-[11px] text-muted-foreground">暂无交易记录</div>
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full">
        <thead>
          <tr className="border-b border-border/30 text-[10px] text-muted-foreground">
            <th className="px-2 py-1.5 text-left">日期</th>
            <th className="px-2 py-1.5 text-left">类型</th>
            <th className="px-2 py-1.5 text-left">标的</th>
            <th className="px-2 py-1.5 text-right">数量</th>
            <th className="px-2 py-1.5 text-right">金额</th>
            <th className="px-2 py-1.5 text-right">费用</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-b border-border/20 text-[11px]">
              <td className="px-2 py-1.5 font-mono text-muted-foreground">{r.trade_date}</td>
              <td className="px-2 py-1.5">
                <span className={`rounded px-1.5 py-0.5 text-[10px] ${ACTION_CLASS[r.action] || 'bg-muted'}`}>
                  {ACTION_LABEL[r.action] || r.action}
                </span>
              </td>
              <td className="px-2 py-1.5">{r.name || r.symbol}</td>
              <td className="px-2 py-1.5 text-right font-mono">{r.quantity || '--'}</td>
              <td className="px-2 py-1.5 text-right font-mono">{money(r.amount)}</td>
              <td className="px-2 py-1.5 text-right font-mono text-muted-foreground">{money(r.total_fee)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function DividendTable({ rows }: { rows: DividendRecord[] }) {
  if (!rows.length) {
    return <div className="py-4 text-center text-[11px] text-muted-foreground">暂无分红记录</div>
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full">
        <thead>
          <tr className="border-b border-border/30 text-[10px] text-muted-foreground">
            <th className="px-2 py-1.5 text-left">除权日</th>
            <th className="px-2 py-1.5 text-left">标的</th>
            <th className="px-2 py-1.5 text-right">金额(税前)</th>
            <th className="px-2 py-1.5 text-right">处理</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-b border-border/20 text-[11px]">
              <td className="px-2 py-1.5 font-mono text-muted-foreground">{r.ex_date}</td>
              <td className="px-2 py-1.5">
                {r.name} <span className="text-[10px] text-muted-foreground">{r.account_name}</span>
              </td>
              <td className="px-2 py-1.5 text-right font-mono">{money(r.amount)}</td>
              <td className="px-2 py-1.5 text-right text-muted-foreground">
                {r.action === 'reinvest' ? `再投 ${r.shares_added} 份` : '现金'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
