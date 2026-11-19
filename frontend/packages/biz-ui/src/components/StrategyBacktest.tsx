import { useEffect, useMemo, useRef, useState } from 'react'
import { strategyApi, type BacktestOrder, type BacktestResult, type StrategyCondition, type StrategyDraft, type StrategyDraftInput, type StrategyField, type StrategyPlaybook, type StrategyPreset } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import StrategyRuleEditor, { validateRuleDraft } from './StrategyRuleEditor'
import { conditionText, rulesText } from './strategy-rules'
import StrategyDataCoverage from './StrategyDataCoverage'
import StrategyPortfolioBacktest from './StrategyPortfolioBacktest'
import { strategySummary } from './strategy-description'
export type StrategyStage = 'rules' | 'backtest' | 'apply'

type Params = { start_date: string; end_date: string; initial_capital: number; commission_rate: number; min_commission: number; stamp_duty_rate: number; transfer_fee_rate: number; slippage_bps: number }
const localDate = (date: Date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
const defaultParams = (): Params => { const end = new Date(); const start = new Date(end); start.setFullYear(start.getFullYear() - 2); return { start_date: localDate(start), end_date: localDate(end), initial_capital: 100000, commission_rate: .00025, min_commission: 5, stamp_duty_rate: .0005, transfer_fee_rate: .00001, slippage_bps: 5 } }
const emptyDraft = (symbol: string, market: string): StrategyDraftInput => ({ scope: 'stock', symbol, market, name: '', buy: [], sell: [], avoid: [], note: '', timing: 'completed_daily', questions: [] })
const fingerprint = (draft: StrategyDraftInput, params: Params) => JSON.stringify({ buy: draft.buy, sell: draft.sell, avoid: draft.avoid, timing: draft.timing || 'completed_daily', config: draft.config, params })
const percentage = (value?: number) => typeof value === 'number' && Number.isFinite(value) ? `${value > 0 ? '+' : ''}${value.toFixed(2)}%` : '--'
const currency = (value?: number) => typeof value === 'number' && Number.isFinite(value) ? `¥${value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}` : '--'
const errorText = (error: unknown) => error instanceof Error ? error.message : '请求失败，请重试'
function conditionDescription(conditions: BacktestOrder['conditions'], fields: StrategyField[]): string {
  if (!conditions) return '此旧报告未提供逐条触发原因'
  const groups: Array<[string, unknown[]]> = Array.isArray(conditions) ? [['', conditions]] : Object.entries(conditions)
  const labels: Record<string, string> = { buy: '买入', sell: '卖出', avoid: '禁买' }
  const descriptions = groups.flatMap(([group, items]) => items.map(item => {
    if (typeof item === 'string') return item
    if (!item || typeof item !== 'object' || !('field' in item)) return ''
    const row = item as StrategyCondition & { actual?: unknown; matched?: boolean }
    return `${labels[group] || group} ${conditionText(row, fields)}${row.actual != null ? `（实际 ${String(row.actual)}，${row.matched ? '命中' : '未命中'}）` : ''}`
  })).filter(Boolean)
  return descriptions.join('；') || '没有可解释的条件'
}
function feeDescription(fees: BacktestOrder['fees']): string {
  if (typeof fees === 'number') return currency(fees)
  if (!fees) return '未提供'
  if (fees.explicit_fees != null) return `${currency(fees.explicit_fees)}，滑点影响 ${currency(fees.slippage_cost)}（已计入成交价）`
  const labels: Record<string, string> = { commission: '佣金', stamp_duty: '印花税', transfer_fee: '过户费', slippage_cost: '滑点影响' }
  return Object.entries(fees).map(([key, value]) => `${labels[key] || key} ${currency(value)}`).join('；')
}

/** One editable draft, one immutable test snapshot, and an explicit activation step. */
export default function StrategyBacktest({ symbol: initialSymbol, market = 'CN', expanded = false, onExpandedChange, libraryDraft, onDraftSaved, guided = false, initialStage = 'rules', initialSource = '', libraryBindings = [], onStageChange, onSymbolChange }: { symbol: string; market?: string; expanded?: boolean; onExpandedChange?: (expanded: boolean) => void; libraryDraft?: StrategyDraft; onDraftSaved?: (draft: StrategyDraft) => void; libraryBindings?: StrategyPlaybook[]; guided?: boolean; initialStage?: StrategyStage; initialSource?: string; onStageChange?: (stage: StrategyStage) => void; onSymbolChange?: (symbol: string) => void }) {
  const [targetSymbol, setTargetSymbol] = useState(initialSymbol)
  const [targetInput, setTargetInput] = useState(initialSymbol)
  const symbol = libraryDraft ? targetSymbol : initialSymbol
  const [stage, setStage] = useState<StrategyStage>(initialStage)
  const [mode, setMode] = useState<'single' | 'portfolio'>('single')
  const [portfolioEvidence, setPortfolioEvidence] = useState('')
  const changeStage = (next: StrategyStage) => { setStage(next); onStageChange?.(next) }
  useEffect(() => { setStage(initialStage) }, [initialStage])

  const [fields, setFields] = useState<StrategyField[]>([])
  const [presets, setPresets] = useState<StrategyPreset[]>([])
  const [playbooks, setPlaybooks] = useState<StrategyPlaybook[]>([])
  const [savedDrafts, setSavedDrafts] = useState<StrategyDraft[]>([])
  const [draft, setDraft] = useState<StrategyDraftInput>(() => emptyDraft(symbol, market))
  const [draftId, setDraftId] = useState<number>()
  const [dirty, setDirty] = useState(false)
  const [params, setParams] = useState<Params>(defaultParams)
  const [recovery, setRecovery] = useState<{ draft: StrategyDraftInput; params: Params } | null>(null)
  const [result, setResult] = useState<BacktestResult | null>(null)
  const [resultKey, setResultKey] = useState('')
  const [resultParams, setResultParams] = useState<Params | null>(null)
  const [testRulesHash, setTestRulesHash] = useState('')
  const [selectedOrder, setSelectedOrder] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [applyOpen, setApplyOpen] = useState(false)
  const [applyScope, setApplyScope] = useState<'stock' | 'global'>('stock')
  const [confirmGlobal, setConfirmGlobal] = useState(false)
  const [confirmUntested, setConfirmUntested] = useState(false)
  const request = useRef(0)
  const storageKey = libraryDraft ? `panwatch_strategy_library:${libraryDraft.id}` : `panwatch_strategy_draft:${market}:${symbol}`
  const context = useRef(storageKey)
  const generation = useRef(0)
  if (context.current !== storageKey) { context.current = storageKey; generation.current += 1 }

  const own = playbooks.find(row => row.scope === 'stock' && row.symbol === symbol && (!row.market || row.market === market))
  const global = playbooks.find(row => row.scope === 'global' && (!row.market || row.market === market))
  const effective = own?.enabled ? own : global?.enabled ? global : undefined
  const currentKey = JSON.stringify({ policy: fingerprint(draft, params), symbol, market })
  const policyKey = JSON.stringify({ buy: draft.buy, sell: draft.sell, avoid: draft.avoid, timing: draft.timing, config: draft.config })
  const rankOnly = draft.config?.template_id === 'factor_rank_research'
  const portfolioFresh = !!portfolioEvidence && portfolioEvidence === policyKey
  const fresh = !!result && !result.error && (!result.symbol || result.symbol===symbol) && resultKey === currentKey && (!result.rules_hash || result.rules_hash === testRulesHash)
  const validated = mode === 'portfolio' ? portfolioFresh : fresh
  const validation = validateRuleDraft(draft, fields)

  useEffect(() => {
    let alive = true
    request.current += 1
    setLoading(true); setBusy(''); setDirty(false); setApplyOpen(false); setResult(null); setResultKey(''); setDraftId(undefined); setSavedDrafts([]); setRecovery(null); setError(''); setMessage(''); setSelectedOrder(null)
    void strategyApi.playbooks().then(async list => {
      if (!alive) return
      setFields(list.fields); setPresets(list.presets || []); setPlaybooks(list.items)
      const ownRow = list.items.find(row => row.scope === 'stock' && row.symbol === symbol && row.enabled && (!row.market || row.market === market))
      const globalRow = list.items.find(row => row.scope === 'global' && row.enabled && (!row.market || row.market === market))
      const active = ownRow || globalRow
      setDraft({ ...emptyDraft(symbol, market), ...(active ? { config: active.config, name: active.name, buy: active.buy, sell: active.sell, avoid: active.avoid || [], note: active.note, timing: active.timing || 'completed_daily' } : {}) })
      if (libraryDraft) { setDraft(libraryDraft); setDraftId(libraryDraft.id) }
      setParams(defaultParams())
      try {
        const local = JSON.parse(localStorage.getItem(storageKey) || 'null')
        if (local?.draft?.market === market && (libraryDraft || local.draft.symbol === symbol) && Array.isArray(local.draft.buy) && Array.isArray(local.draft.sell) && Array.isArray(local.draft.avoid) && local.params) setRecovery(local)
      } catch { /* A malformed local recovery must not block the live rule. */ }
      try { const saved = await strategyApi.drafts(symbol, market); if (alive) setSavedDrafts(saved.items) } catch { if (alive) setMessage('服务器草稿暂不可用，可继续编辑并稍后重试保存。') }
    }).catch(e => { if (alive) setError(errorText(e)) }).finally(() => { if (alive) setLoading(false) })
    return () => { alive = false; request.current += 1; generation.current += 1 }
  }, [market, storageKey])

  useEffect(() => { if (rankOnly) setMode('portfolio') }, [rankOnly])

  useEffect(() => {
    if (loading || recovery || !dirty) return
    try { localStorage.setItem(storageKey, JSON.stringify({ draft, params })) } catch { /* Server saving remains available if browser storage is full. */ }
  }, [draft, params, loading, recovery, dirty, storageKey])

  const updateDraft = (next: StrategyDraftInput) => { setDraft(next); setDirty(true); setMessage('草稿已修改，正在使用的策略没有变化。') }
  const updateParams = (next: Params) => { setParams(next); setDirty(true) }
  const rememberDraft = (saved: StrategyDraft) => { setDraftId(saved.draft_id || saved.id); setSavedDrafts(rows => [saved, ...rows.filter(row => row.id !== saved.id)]) }
  const persist = async (scope: 'stock' | 'global' = 'stock') => {
    const origin = generation.current
    const saved = await strategyApi.saveDraft({ ...draft, enabled: true, draft_id: libraryDraft ? draftId : scope === 'stock' ? draftId : undefined, name: draft.name.trim() || `${symbol} 策略草稿`, scope: libraryDraft ? draft.scope : scope, symbol: libraryDraft ? draft.symbol : scope === 'stock' ? symbol : '', market })
    if ((libraryDraft || scope === 'stock') && origin === generation.current) { rememberDraft(saved); onDraftSaved?.(saved) }
    return saved
  }
  const saveDraft = async () => {
    const origin = generation.current
    setBusy('save'); setError('')
    try { await persist(); if (generation.current !== origin) return; setDirty(false); localStorage.removeItem(storageKey); setMessage('草稿已保存到服务器，尚未应用。') } catch (e) { if (generation.current === origin) setError(errorText(e)) } finally { if (generation.current === origin) setBusy('') }
  }
  const makeAiDraft = async (text: string) => {
    const origin = generation.current
    setBusy('ai'); setError('')
    try { const saved = await strategyApi.aiDraft({ text, draft_id: draftId, expected_hash: libraryDraft?.rules_hash || savedDrafts.find(row=>row.id===draftId)?.rules_hash, scope: libraryDraft ? draft.scope : 'stock', symbol: libraryDraft ? draft.symbol : symbol, market }); if (generation.current !== origin) return; setDraft(saved); rememberDraft(saved); onDraftSaved?.(saved); setMessage('AI 草稿已生成。请逐条核对；仍未改变生效策略。') } catch (e) { if (generation.current === origin) setError(errorText(e)) } finally { if (generation.current === origin) setBusy('') }
  }
  const runBacktest = async () => {
    if (rankOnly) { setMode('portfolio'); setError('因子排名需要同日股票池，请使用组合回测'); return }
    if (!symbol) { setError('请先选择验证标的，再运行回测。'); return }
    const errors = validateRuleDraft(draft, fields)
    if (errors.length) { setError(errors.join('；')); return }
    if (!params.start_date || !params.end_date || params.start_date > params.end_date || params.initial_capital <= 0 || Object.values(params).some(value => typeof value === 'number' && (!Number.isFinite(value) || value < 0))) { setError('请填写有效的起止日期、正数资金和非负费用参数。'); return }
    const seq = ++request.current
    const snapshot = currentKey
    setBusy('backtest'); setError('')
    try {
      const saved = await persist()
      if (request.current !== seq) return
      const tested = await strategyApi.backtest({ symbol, market, draft_id: saved.id, buy: draft.buy, sell: draft.sell, avoid: draft.avoid, days: 5000, ...params })
      if (request.current !== seq) return
      setResult(tested); setResultKey(snapshot); setResultParams({ ...params }); setTestRulesHash(saved.rules_hash); setSelectedOrder(null)
      if (libraryDraft) onDraftSaved?.(saved)
      if (tested.error) setError(tested.error)
      else setMessage('回测完成。结果对应这份规则快照，尚未应用。')
    } catch (e) { if (request.current === seq) setError(errorText(e)) } finally { if (request.current === seq) setBusy('') }
  }
  const activate = async () => {
    if (applyScope === 'stock' && !symbol) { setError('请先选择要应用的标的。'); return }
    if (validation.length || (!validated && !confirmUntested) || (applyScope === 'global' && !confirmGlobal)) return
    const origin = generation.current
    setBusy('apply'); setError('')
    try {
      const saved = await persist(applyScope)
      if (generation.current !== origin) return
      if (libraryDraft) await strategyApi.applyLibrary(saved.id, { scope: applyScope, symbol: applyScope === 'stock' ? symbol : '', expected_hash: saved.rules_hash })
      else await strategyApi.activateDraft(saved.id)
      if (generation.current !== origin) return
      const list = await strategyApi.playbooks()
      if (generation.current !== origin) return
      setPlaybooks(list.items); setApplyOpen(false); onDraftSaved?.(saved); setMessage(applyScope === 'stock' ? '已应用到本股，之后的操作检查使用此版本。' : '已更新默认策略，未单独绑定策略的股票将使用此版本。')
    } catch (e) { if (generation.current === origin) setError(errorText(e)) } finally { if (generation.current === origin) setBusy('') }
  }
  const manageOwn = async (action: 'toggle' | 'remove') => {
    if (!own || !window.confirm(action === 'remove' ? '删除本股生效规则？删除后将继承已启用的默认规则，草稿与回测记录不受影响。' : `${own.enabled ? '停用' : '启用'}本股规则？`)) return
    const origin = generation.current
    setBusy('manage'); setError('')
    try {
      if (action === 'remove') await strategyApi.remove(own.id); else if (libraryDraft) await strategyApi.setBindingEnabled(own.id, !own.enabled); else await strategyApi.save({ ...own, enabled: !own.enabled })
      if (generation.current !== origin) return
      const list = await strategyApi.playbooks()
      if (generation.current !== origin) return
      setPlaybooks(list.items); setMessage('生效规则已更新，回测草稿没有变化。')
    } catch (e) { if (generation.current === origin) setError(errorText(e)) } finally { if (generation.current === origin) setBusy('') }
  }

  const orders = useMemo<BacktestOrder[]>(() => result?.orders || (result?.trades || []).flatMap(trade => [{ signal_date: trade.entry_signal_date || trade.entry_date, execution_date: trade.entry_date, side: 'buy', status: 'filled', price: trade.entry_price, quantity: trade.quantity, fees: trade.buy_fees, conditions: trade.trigger_conditions?.entry || trade.entry_conditions }, { signal_date: trade.exit_signal_date || trade.exit_date, execution_date: trade.exit_date, side: 'sell', status: 'filled', price: trade.exit_price, quantity: trade.quantity, fees: trade.sell_fees, conditions: trade.trigger_conditions?.exit || trade.exit_conditions }]), [result])
  const selected = selectedOrder == null ? undefined : orders[selectedOrder]

  if (loading) return <div className="py-8 text-sm text-muted-foreground" role="status">正在读取生效规则与草稿…</div>
  return <div className="space-y-5 text-sm">

    <header className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-xl font-medium">{guided ? draft.name || '编辑策略' : `${symbol || '通用策略'} · 策略与回测`}</h2><p className="mt-1 text-sm text-muted-foreground">先写规则，再验证，确认后才应用。历史收益不代表未来表现。</p></div>{onExpandedChange && <Button variant="outline" onClick={() => onExpandedChange(!expanded)}>{expanded ? '收起全屏' : '全屏展开'}</Button>}</header>
    {guided && <nav className="sc-stepbar" aria-label="策略设置步骤">{([['rules','1. 确认规则','买入、退出与禁买'],['backtest','2. 历史验证','单股信号或组合资金'],['apply','3. 用于操作检查','确认实际使用范围']] as const).map(([key,label,desc])=><button key={key} disabled={!!busy} aria-current={stage===key?'step':undefined} onClick={()=>changeStage(key)}>{label}<small>{desc}</small></button>)}</nav>}
    {guided && libraryDraft && stage !== 'rules' && <form className="mb-5 flex flex-wrap items-end gap-3" onSubmit={e=>{e.preventDefault();const ticker=targetInput.trim().toUpperCase();if(!ticker||(market==='CN'&&!/^\d{6}$/.test(ticker))){setError('请填写有效证券代码，A股为6位数字');return}setTargetSymbol(ticker);onSymbolChange?.(ticker);setError('')}}><label className="text-sm">{stage==='apply'?'应用到个股时选择标的':'验证标的（组合回测无需单独选择）'}<input aria-label="验证标的" className="mt-2 block rounded-lg border border-border bg-background px-3 py-2" value={targetInput} disabled={!!busy||libraryDraft.scope==='stock'} onChange={e=>setTargetInput(e.target.value)} placeholder="例如 600000" /></label><Button variant="secondary" disabled={!!busy||libraryDraft.scope==='stock'} type="submit">确认标的</Button>{symbol&&<span className="pb-2 text-sm text-muted-foreground">当前 {symbol}</span>}</form>}
    <section className="rounded-xl border border-border bg-muted/20 p-4" aria-label="当前生效规则"><div className="flex flex-wrap items-center justify-between gap-3"><div><span className="text-xs text-muted-foreground">当前生效</span><p className="mt-1 font-medium">{libraryDraft && !symbol ? libraryBindings.some(row=>row.enabled) ? libraryBindings.filter(row=>row.enabled).map(row=>`${row.scope==='global'?'默认':row.symbol} · v${row.version||'?'}`).join('；') : '这份策略尚未用于操作检查' : effective ? `${effective.name || '未命名策略'} · ${effective.scope === 'stock' ? '本股规则' : '继承默认'}${effective.version ? ` · v${effective.version}` : ''}` : '未启用规则'}</p></div>{own && <div className="flex gap-2"><Button size="sm" variant="ghost" disabled={!!busy} onClick={() => void manageOwn('toggle')}>{own.enabled ? '停用本股规则' : '启用本股规则'}</Button><Button size="sm" variant="destructive" disabled={!!busy} onClick={() => void manageOwn('remove')}>删除本股规则</Button></div>}</div><p className="mt-2 text-xs leading-6 text-muted-foreground">草稿、模板和回测都不会自动替换此规则。个股规则完全覆盖默认规则。</p>{effective && <details className="mt-3 text-sm"><summary className="cursor-pointer text-primary">查看生效规则</summary><div className="mt-2 space-y-1 leading-6 text-muted-foreground"><p>买入：{rulesText(effective.buy, fields, '不产生买入信号', ' 且 ')}</p><p>卖出：{rulesText(effective.sell, fields, '不产生主动卖出信号', ' 或 ')}</p><p>禁买：{rulesText(effective.avoid, fields, '无额外禁买限制', ' 或 ')}</p></div></details>}</section>
    {error && <p className="rounded-lg border border-destructive/30 p-3 text-destructive" role="alert">{error}</p>}
    {message && <p className="rounded-lg bg-muted p-3 text-sm" role="status">{message}</p>}
    {recovery && <div className="flex flex-wrap items-center gap-3 rounded-xl border border-amber-500/30 p-4"><span>发现上次留在本机的草稿。</span><Button size="sm" variant="secondary" onClick={() => { setDraft(recovery.draft); setDirty(true); setParams({ ...defaultParams(), ...recovery.params }); setRecovery(null); setMessage('已恢复本机草稿，尚未应用。') }}>恢复未保存草稿</Button><Button size="sm" variant="ghost" onClick={() => { localStorage.removeItem(storageKey); setRecovery(null) }}>忽略并使用生效规则副本</Button></div>}
    <div className={expanded && !guided ? 'grid items-start gap-6 xl:grid-cols-[minmax(340px,0.85fr)_minmax(0,1.35fr)]' : 'space-y-6'}>
      <section className="min-w-0 rounded-xl border border-border p-4 md:p-5" aria-label="策略草稿" hidden={guided && stage!=='rules'}><div className="mb-4 flex flex-wrap items-center justify-between gap-3"><h3 className="text-base font-semibold">1. 编辑草稿</h3>{!libraryDraft && <label className="text-xs text-muted-foreground">服务器草稿<select aria-label="恢复服务器草稿" value="" disabled={!!busy} className="ml-2 max-w-44 rounded-lg border border-border bg-background p-2" onChange={e => { const saved = savedDrafts.find(row => row.id === Number(e.target.value)); if (saved) { setDraft({ ...saved, scope: 'stock', symbol, market }); setDraftId(saved.scope === 'stock' && saved.symbol === symbol ? saved.id : undefined); setDirty(false); setMessage(saved.scope === 'global' ? '已复制默认草稿到本股，原草稿和生效规则均未改变。' : '服务器草稿已恢复，尚未应用。') } }}><option value="">选择已保存草稿</option>{savedDrafts.map(row => <option key={row.id} value={row.id}>{row.name || `草稿 ${row.id}`}</option>)}</select></label>}</div>
        <StrategyRuleEditor value={draft} onChange={updateDraft} fields={fields} presets={presets} busy={!!busy} guided={guided} initialMode={initialSource} onAiDraft={makeAiDraft} />
        <div className="mt-5 flex flex-wrap gap-2"><Button variant="secondary" disabled={!!busy} onClick={() => void saveDraft()}>{busy === 'save' ? '保存中…' : '保存草稿'}</Button>{guided && <Button disabled={!!busy || !!validation.length} onClick={()=>changeStage('backtest')}>确认规则，去回测</Button>}<Button variant="ghost" disabled={!!busy} onClick={() => { if (libraryDraft) { updateDraft({ ...draft, buy: [], sell: [], avoid: [], config: {}, questions: [], note: '' }) } else { updateDraft(emptyDraft(symbol, market)); setDraftId(undefined) } setMessage('已清空草稿规则，生效策略没有变化。') }}>从空白规则开始</Button></div>
      </section>
      <section className="min-w-0 space-y-5" aria-label="回测与应用" hidden={guided && stage==='rules'}>
        <div hidden={guided && stage==='apply'}>
          <div className="mb-5 grid gap-3 sm:grid-cols-2"><button className="rounded-lg border p-4 text-left" aria-pressed={mode==='single'} disabled={!!busy||rankOnly} onClick={()=>setMode('single')}><span className="block font-medium">看一只股票的买卖点</span><span className="mt-1 block text-xs text-muted-foreground">逐笔核对信号与成交原因</span></button><button className="rounded-lg border p-4 text-left" aria-pressed={mode==='portfolio'} disabled={!!busy} onClick={()=>setMode('portfolio')}><span className="block font-medium">看一组股票的资金表现</span><span className="mt-1 block text-xs text-muted-foreground">共享资金、持仓数量与因子排序</span></button></div>
          {rankOnly && <p className="mb-4 text-sm text-muted-foreground">这是同日因子排名策略，只能使用组合回测</p>}
          <div hidden={mode!=='single'||rankOnly} className="rounded-xl border border-border p-4 md:p-5"><h3 className="text-base font-medium">单股回测</h3><p className="mt-1 text-xs leading-6 text-muted-foreground">日线收盘产生信号，下一交易日开盘尝试成交；同时对照直接买入并持有。</p><fieldset disabled={!!busy} className="mt-4 grid gap-3 sm:grid-cols-3"><label>开始日期<input aria-label="回测开始日期" type="date" className="mt-1 block w-full rounded-lg border border-border bg-background p-2" value={params.start_date} onChange={e => updateParams({ ...params, start_date: e.target.value })} /></label><label>结束日期<input aria-label="回测结束日期" type="date" className="mt-1 block w-full rounded-lg border border-border bg-background p-2" value={params.end_date} onChange={e => updateParams({ ...params, end_date: e.target.value })} /></label><label>初始资金（元）<input aria-label="回测初始资金" type="number" min={1} className="mt-1 block w-full rounded-lg border border-border bg-background p-2" value={params.initial_capital} onChange={e => updateParams({ ...params, initial_capital: Number(e.target.value) })} /></label></fieldset>
          <details className="mt-3"><summary className="cursor-pointer text-xs text-muted-foreground">费用与成交假设</summary><fieldset disabled={!!busy} className="mt-3 grid gap-3 sm:grid-cols-2">{([['commission_rate', '佣金费率'], ['min_commission', '最低佣金（元）'], ['stamp_duty_rate', '卖出印花税率'], ['transfer_fee_rate', '过户费率'], ['slippage_bps', '滑点（基点）']] as const).map(([key, label]) => <label key={key} className="text-xs">{label}<input aria-label={label} type="number" min={0} step="any" className="mt-1 block w-full rounded-lg border border-border bg-background p-2" value={params[key]} onChange={e => updateParams({ ...params, [key]: Number(e.target.value) })} /></label>)}</fieldset><p className="mt-2 text-xs text-muted-foreground">费率使用小数，例如 0.00025 = 0.025%；与券商的实际成交可能不同。</p></details>
          <Button className="mt-4" disabled={!!busy} onClick={() => void runBacktest()}>{busy === 'backtest' ? '回测中…' : '运行草稿回测'}</Button>
        </div>
        <div hidden={mode!=='portfolio'&&!rankOnly}><StrategyPortfolioBacktest key={storageKey} storageKey={storageKey} embedded policyKey={policyKey} onEvidenceChange={valid=>setPortfolioEvidence(valid?policyKey:'')} disabled={!!busy||!!validation.length} prepare={async()=>{const errors=validateRuleDraft(draft,fields);if(errors.length)throw new Error(errors.join('；'));return persist()}}/></div>
        {mode==='single' && !rankOnly && result && <section className="rounded-xl border border-border p-4 md:p-5" aria-label="回测结果"><div className="flex flex-wrap items-center justify-between gap-2"><h3 className="text-base font-semibold">回测结果</h3><span className={fresh ? 'text-xs text-muted-foreground' : 'text-xs text-amber-600 dark:text-amber-400'}>{fresh ? '对应当前草稿与参数' : '规则或参数已变化，旧结果不代表当前草稿'}</span></div>{!result.error && <>
          <p className="mt-2 text-xs text-muted-foreground">实际区间 {result.start} — {result.end} · {result.bars} 根日K{result.backtest_id ? ` · 报告 #${result.backtest_id}` : ''}</p>
          <div className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-4"><Metric label="策略收益" value={percentage(result.total_return)} /><Metric label="买入持有收益" value={percentage(result.buy_hold_return)} description="区间首末收盘价涨跌，不含交易成本" /><Metric label="超额收益" value={percentage(result.excess_return)} description="策略净收益与上述价格基准之差" /><Metric label="最大回撤" value={percentage(result.metrics?.max_drawdown)} /></div>
          <p className="my-4 text-xs leading-6 text-muted-foreground">已结束交易 {result.trade_count} 笔 · 胜率 {percentage(result.win_rate)} · 期末 {result.current_status} · 初始资金 {currency(result.initial_capital ?? resultParams?.initial_capital)}{result.open_position ? ' · 含未平仓市值，不将其算为已结束交易' : ''}</p>
          <ResultChart result={result} orders={orders} selectedOrder={selectedOrder} onSelectOrder={setSelectedOrder} />
          <div className="mt-3 space-y-1 rounded-lg bg-muted/30 p-3 text-xs leading-6"><p>买入：{strategySummary({buy:result.rules?.buy||[],sell:result.rules?.sell||[],avoid:result.rules?.avoid||[],config:result.rules_snapshot?.config as StrategyDraftInput['config']},fields,presets).buy}</p><p>卖出：{rulesText(result.rules?.sell, fields, '不产生主动卖出信号', ' 或 ')}</p><p>禁买：{rulesText(result.rules?.avoid, fields, '无额外禁买限制', ' 或 ')}</p></div>
          {!!orders.length && <div className="mt-4"><h4 className="font-medium">信号与成交</h4><div className="mt-2 max-h-56 overflow-auto"><table className="w-full text-left text-xs"><thead className="sticky top-0 bg-card"><tr><th className="p-2">方向 / 信号日</th><th className="p-2">成交日</th><th className="p-2 text-right">成交价</th><th className="p-2">状态</th></tr></thead><tbody>{orders.map((order, index) => <tr key={index} className={selectedOrder === index ? 'bg-primary/10' : ''}><td className="p-2"><button className="min-h-8 text-primary underline-offset-4 hover:underline" onClick={() => setSelectedOrder(index)}>{order.side === 'buy' ? '买入' : '卖出'} · {order.signal_date}</button></td><td className="p-2">{order.execution_date || '未成交'}</td><td className="p-2 text-right">{currency(order.fill_price ?? order.price)}</td><td className="p-2">{order.status === 'filled' ? '已成交' : '未成交'}</td></tr>)}</tbody></table></div></div>}
          {selected && <div className="mt-3 rounded-lg border border-border p-3 text-sm leading-7" role="region" aria-label="所选成交解读"><p className="font-medium">{selected.side === 'buy' ? '买入' : '卖出'}：{selected.signal_date} 收盘后发出信号</p><p>{selected.execution_date || '未成交'} · {currency(selected.fill_price ?? selected.price)} · {selected.quantity ?? '--'} 股</p><p>费用：{feeDescription(selected.fees)}</p><p>原因：{selected.reason || conditionDescription(selected.conditions, fields)}</p></div>}
          {result.explain && <p className="mt-4 text-sm leading-7 text-muted-foreground">{result.explain}</p>}
          <p className="mt-4 text-xs leading-6 text-muted-foreground">数据：{result.data_info?.source || '由后端行情源提供'} · {result.data_info?.adjustment || '复权口径见数据源'}{result.data_info?.warmup_bars != null ? ` · 预热 ${result.data_info.warmup_bars} 根` : ''}</p>
          <details className="mt-2 text-xs leading-6 text-muted-foreground"><summary className="cursor-pointer text-primary">数据来源与回测假设</summary><p className="mt-2">实际区间 {result.data_info?.actual_start || result.start} — {result.data_info?.actual_end || result.end}。费用及滑点使用本次回测参数，收益比较口径见主指标说明。</p><ul className="mt-2 list-disc space-y-1 pl-4">{(result.data_info?.limitations?.length ? result.data_info.limitations : ['单票全仓进出，历史撮合与真实市场存在差异。']).map((limitation, index) => <li key={index}>{limitation}</li>)}</ul></details>
        </>}{result.error && <p className="mt-3 text-destructive">{result.error}</p>}</section>}
        <div className="mt-5"><StrategyDataCoverage key={symbol} symbol={symbol}/></div>
        {guided&&<div className="sc-foot"><Button variant="ghost" onClick={()=>changeStage('rules')}>返回修改规则</Button><Button disabled={!!busy} onClick={()=>changeStage('apply')}>{validated?'核对结果，选择使用范围':'查看使用范围（尚未验证）'}</Button></div>}
        </div>
        <section hidden={guided && stage!=='apply'} className="rounded-xl border border-border p-4 md:p-5"><h3 className="text-base font-medium">用于操作检查</h3><p className="mt-2 text-sm leading-6 text-muted-foreground">应用后，操作前检查才会使用这份规则。回测不会替你下单，也不会自动改变持仓。</p><Button className="mt-4" variant="secondary" disabled={!!busy || !!validation.length} onClick={() => { setApplyScope('stock'); setConfirmGlobal(false); setConfirmUntested(false); setApplyOpen(true) }}>选择应用范围</Button>{!!validation.length && <p className="mt-2 text-xs text-muted-foreground">{validation[0]}</p>}</section>
      </section>
    </div>
    <Dialog open={applyOpen} onOpenChange={setApplyOpen}><DialogContent className={guided ? "strategy-dialog max-w-lg" : "max-w-lg"}><DialogHeader><DialogTitle>确认应用这份规则</DialogTitle><DialogDescription>此操作会替换正在使用的规则；草稿保存和回测本身不会生效。</DialogDescription></DialogHeader><fieldset disabled={!!busy} className="space-y-3 text-sm"><label className="flex items-start gap-3"><input type="radio" name="apply-scope" checked={applyScope === 'stock'} onChange={() => setApplyScope('stock')} className="mt-1" /><span>仅应用到 {symbol || '个股（先选择标的）'}<span className="block text-xs text-muted-foreground">替换本股规则，其他股票不受影响。</span></span></label><label className="flex items-start gap-3"><input type="radio" name="apply-scope" checked={applyScope === 'global'} onChange={() => setApplyScope('global')} className="mt-1" /><span>设为默认规则<span className="block text-xs text-muted-foreground">影响所有没有单独规则的股票。</span></span></label>{applyScope === 'global' && <label className="flex gap-3 rounded-lg bg-amber-500/10 p-3"><input type="checkbox" checked={confirmGlobal} onChange={e => setConfirmGlobal(e.target.checked)} /><span>我确认要更改默认规则，并理解本股回测不能证明其他股票适用。</span></label>}{!validated && <label className="flex gap-3 rounded-lg bg-amber-500/10 p-3"><input type="checkbox" checked={confirmUntested} onChange={e => setConfirmUntested(e.target.checked)} /><span>当前草稿与参数尚无匹配的成功回测。我已了解，仍要应用。</span></label>}</fieldset>{error && <p className="text-sm text-destructive" role="alert">{error}</p>}<div className="mt-4 flex justify-end gap-2"><Button variant="ghost" onClick={() => setApplyOpen(false)}>取消</Button><Button disabled={!!busy || (applyScope === 'stock' && !symbol) || (!validated && !confirmUntested) || (applyScope === 'global' && !confirmGlobal)} onClick={() => void activate()}>{busy === 'apply' ? '应用中…' : applyScope === 'stock' ? '确认应用到本股' : '确认更新默认规则'}</Button></div></DialogContent></Dialog>
  </div>
}

function Metric({ label, value, description }: { label: string; value: string; description?: string }) { return <div className="rounded-lg bg-muted/30 p-3"><p className="text-xs text-muted-foreground">{label}</p><strong className="mt-1 block text-lg font-semibold tabular-nums">{value}</strong>{description && <p className="mt-1 text-xs leading-5 text-muted-foreground">{description}</p>}</div> }

function ResultChart({ result, orders, selectedOrder, onSelectOrder }: { result: BacktestResult; orders: BacktestOrder[]; selectedOrder: number | null; onSelectOrder: (index: number) => void }) {
  const curve = result.curve || []
  if (!curve.length) return <p className="text-sm text-muted-foreground">此报告未提供收益曲线。</p>
  const w = 760, h = 280, left = 48, top = 16, bottom = 32
  const values = curve.flatMap(point => [point.price_pct, point.equity_pct]).filter(Number.isFinite)
  const low = Math.min(0, ...values), high = Math.max(0, ...values), span = high - low || 1
  const x = (index: number) => left + index / Math.max(1, curve.length - 1) * (w - left - 16)
  const y = (value: number) => top + (high - value) / span * (h - top - bottom)
  const path = (key: 'price_pct' | 'equity_pct') => curve.map((point, index) => `${index ? 'L' : 'M'}${x(index)},${y(point[key])}`).join(' ')
  return <div><div className="overflow-x-auto"><svg viewBox={`0 0 ${w} ${h}`} className="w-full min-w-[640px]" role="group" aria-label="同一区间策略收益与买入持有收益，买卖点可点击查看成交原因">{[0, .25, .5, .75, 1].map(tick => { const value = low + tick * span; return <g key={tick}><line x1={left} x2={w - 16} y1={y(value)} y2={y(value)} stroke="currentColor" opacity=".12" /><text x={left - 5} y={y(value) + 4} fontSize="14" textAnchor="end" fill="currentColor">{value.toFixed(0)}%</text></g> })}<path d={path('price_pct')} fill="none" stroke="#64748b" strokeWidth="1.8" /><path d={path('equity_pct')} fill="none" stroke="#2563eb" strokeWidth="2.4" />{orders.map((order, index) => { if (order.status !== 'filled') return null; const i = curve.findIndex(point => point.date === order.execution_date); if (i < 0) return null; return <g key={index} role="button" tabIndex={0} aria-label={`${order.side === 'buy' ? '买入' : '卖出'} ${order.execution_date}成交点`} onClick={() => onSelectOrder(index)} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelectOrder(index) } }} className="cursor-pointer"><title>{order.signal_date} 信号 → {order.execution_date} {order.reason || ''}</title><circle cx={x(i)} cy={y(curve[i].price_pct)} r={selectedOrder === index ? 8 : 5} fill={order.side === 'buy' ? '#be123c' : '#047857'} stroke="white" strokeWidth="1.5" /><circle cx={x(i)} cy={y(curve[i].price_pct)} r="12" fill="transparent" /></g> })}{[0, Math.floor((curve.length - 1) / 2), curve.length - 1].map((index, tick) => <text key={tick} x={x(index)} y={h - 8} fontSize="14" textAnchor={tick === 0 ? 'start' : tick === 2 ? 'end' : 'middle'} fill="currentColor">{curve[index].date}</text>)}</svg></div><div className="flex flex-wrap gap-4 text-xs text-muted-foreground"><span>蓝线：策略收益</span><span>灰线：标的价格涨跌（持有基准）</span><span>红点：买入</span><span>绿点：卖出</span><span>点选成交点与下方记录联动</span></div></div>
}
