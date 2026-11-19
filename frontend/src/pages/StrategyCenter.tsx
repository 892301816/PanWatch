import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { fetchAPI, stocksApi, strategyApi, type StrategyDraft, type StrategyDraftInput, type StrategyLibraryItem, type StrategyPlaybook, type StrategyField, type StrategyPreset, type StrategyScanItem, type StrategyReportSummary, type BacktestRecord, type StrategyDecisionCheck } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import StrategyBacktest from '@panwatch/biz-ui/components/StrategyBacktest'
import { strategySummary, templateCategory, templateCaveat, templateSignalDescription } from '@panwatch/biz-ui/components/strategy-description'
import { ArrowLeft, Copy, Trash2, RotateCcw } from 'lucide-react'
import './strategy-center.css'
import TradeDecisionDialog, { type TradeIntentTarget, type RecordIntent } from '@/components/TradeDecisionDialog'
import OpportunityDiscovery from './OpportunityDiscovery'
import { saveTradeTransfer } from '@/lib/trade-intent-transfer'

type Stock = { symbol: string; market: string; name: string; price?: number }
type View = 'my' | 'opportunities' | 'backtest' | 'records'
const views: Array<[View, string]> = [['my', '我的策略'], ['opportunities', '机会发现'], ['backtest', '回测报告'], ['records', '复盘记录']]
const marketName = (market: string) => ({ CN: 'A股', HK: '港股', US: '美股' }[market] || market)
const message = (error: unknown) => error instanceof Error ? error.message : '请求失败，请重试'
const pct = (value?: number | null) => value == null ? '--' : `${value.toFixed(2)}%`
const scanLabels = { MATCH: '符合入场条件', BLOCK: '禁止买入', WAIT: '等待条件', UNKNOWN: '数据不足 / 范围不符' }
const decisionLabels = { PASS: '通过当时检查', BLOCK: '违反条件或约束', UNKNOWN: '未完成判断', NO_POLICY: '未配置策略' }
const selectClass = 'min-h-10 max-w-full rounded-lg border border-border bg-card px-3 text-sm'

export default function StrategyCenter() {
  const [query, setQuery] = useSearchParams()
  const navigate = useNavigate()
  const requested = query.get('view')
  const view: View = views.some(([key]) => key === requested) ? requested as View : 'my'
  const selectedId = Number(query.get('strategy')) || 0
  const [items, setItems] = useState<StrategyLibraryItem[]>([])
  const [bindings, setBindings] = useState<StrategyPlaybook[]>([])
  const [fields, setFields] = useState<StrategyField[]>([])
  const [presets, setPresets] = useState<StrategyPreset[]>([])
  const [stocks, setStocks] = useState<Stock[]>([])
  const [accounts, setAccounts] = useState<Array<{ id: number; name: string }>>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [templatesOpen, setTemplatesOpen] = useState(false)
  const [templateQuery,setTemplateQuery]=useState('')
  const [systemOpen, setSystemOpen] = useState(query.get('system') === '1')
  const [compare, setCompare] = useState<number[]>([])
  const [editing, setEditing] = useState(!!query.get('step') || (requested === 'backtest' && !!selectedId && query.get('list') !== '1'))
  const [deleted, setDeleted] = useState<StrategyLibraryItem[]>([])
  const [deleteTarget, setDeleteTarget] = useState<StrategyLibraryItem | null>(null)
  const [disableBindings, setDisableBindings] = useState(false)
  const [creationOpen, setCreationOpen] = useState(false)
  const [sourceMode, setSourceMode] = useState('')
  const [search, setSearch] = useState('')
  const [category, setCategory] = useState('全部')
  const [compareMode, setCompareMode] = useState(false)
  const [notice, setNotice] = useState('')
  const [intentOpen, setIntentOpen] = useState(false)
  const [intentCode, setIntentCode] = useState('')
  const [intentMarket, setIntentMarket] = useState('CN')
  const [intentSide, setIntentSide] = useState<'buy' | 'sell'>('buy')
  const [symbol, setSymbol] = useState(query.get('symbol') || '')
  const [inputSymbol, setInputSymbol] = useState(symbol)
  const [universe, setUniverse] = useState('watchlist')
  const [batch, setBatch] = useState(0)
  const [allowNetwork, setAllowNetwork] = useState(false)
  const [results, setResults] = useState<{ items: StrategyScanItem[]; rules_hash: string; draft_id: number; as_of: string } | null>(null)
  const [scanBusy, setScanBusy] = useState(false)
  const [reports, setReports] = useState<StrategyReportSummary[]>([])
  const [decisions, setDecisions] = useState<StrategyDecisionCheck[]>([])
  const [recordLoading, setRecordLoading] = useState(false)
  const [report, setReport] = useState<BacktestRecord | null>(null)
  const [intent, setIntent] = useState<TradeIntentTarget | null>(null)
  const seq = useRef(0)
  const recordSeq = useRef(0)
  const loadSeq = useRef(0)
  const reportSeq = useRef(0)
  const mounted = useRef(true)
  const workspaceRef = useRef<HTMLElement>(null)
  const routeContext = useRef(query.toString())
  routeContext.current = query.toString()
  const selected = items.find(item => item.id === selectedId)
  const pool = stocks.filter(stock => stock.market === selected?.market && (selected.scope !== 'stock' || stock.symbol === selected.symbol))
  const stale = !!results && (!selected || results.draft_id !== selected.id || results.rules_hash !== selected.rules_hash)

  const go = (next: View, id = selectedId, ticker = symbol) => {
    const updated = new URLSearchParams(query)
    updated.set('view', next); updated.delete('step'); if(next==='backtest')updated.set('list','1');else updated.delete('list')
    if (id) updated.set('strategy', String(id)); else updated.delete('strategy')
    if (ticker) updated.set('symbol', ticker); else updated.delete('symbol')
    setQuery(updated)
    setEditing(false)
  }
  const load = useCallback(async () => {
    if (!mounted.current) return
    const request = ++loadSeq.current
    try {
      const data = await strategyApi.library()
      if (request !== loadSeq.current) return
      setItems(data.items); setDeleted(data.deleted_items || []); setBindings(data.bindings); setFields(data.fields); setPresets(data.presets)
    } catch (e) { if (request === loadSeq.current) setError(message(e)) }
    finally { if (request === loadSeq.current) setLoading(false) }
  }, [])
  useEffect(() => {
    mounted.current = true
    void load()
    let alive = true
    void stocksApi.list().then(rows => { if (alive) setStocks(rows) }).catch(() => { if (alive) setError('关注列表读取失败，请重试或使用单票检查。') })
    void fetchAPI<Array<{ id: number; name: string }>>('/accounts').then(rows => { if (alive) setAccounts(rows) }).catch(() => { if (alive) setError('账户读取失败，操作前检查暂不能选择账户，请刷新。') })
    return () => { alive = false; mounted.current = false; seq.current++; loadSeq.current++; recordSeq.current++; reportSeq.current++ }
  }, [load])
  useEffect(() => { setEditing(!!query.get('step') || (requested === 'backtest' && !!selectedId && query.get('list') !== '1')) }, [query.get('step'), requested, selectedId, query.get('list')])
  useEffect(() => { seq.current++; setScanBusy(false) }, [selectedId, selected?.rules_hash, universe, allowNetwork, batch])
  useEffect(() => {
    if (view !== 'records' && view !== 'backtest') return
    const request = ++recordSeq.current
    setRecordLoading(true)
    setReports([]); setDecisions([])
    const task = view === 'backtest' ? strategyApi.backtests(selectedId || undefined).then(data => { if (recordSeq.current === request) setReports(data.items) }) : fetchAPI<{ items: StrategyDecisionCheck[] }>(`/strategy/decisions?market=ALL&limit=50${selectedId ? `&draft_id=${selectedId}` : ''}`).then(data => { if (recordSeq.current === request) setDecisions(data.items) })
    void task.catch(e => { if (recordSeq.current === request) setError(message(e)) }).finally(() => { if (recordSeq.current === request) setRecordLoading(false) })
    return () => { recordSeq.current++ }
  }, [view, selectedId, editing])

  const create = async (source?: StrategyDraftInput, mode = '') => {
    const origin = routeContext.current
    setBusy(true); setError('')
    try {
      const saved = await strategyApi.saveDraft(source ? { ...source, draft_id: undefined, scope: 'global', symbol: '', name: `${source.name} · 我的副本` } : { scope: 'global', symbol: '', market: 'CN', name: '我的新策略', buy: [], sell: [], avoid: [], note: '', timing: 'completed_daily' })
      await load()
      if (!mounted.current || routeContext.current !== origin) return
      setItems(rows => [{ ...saved, bindings: [], tested: false }, ...rows.filter(row => row.id !== saved.id)]); setQuery(new URLSearchParams({view:'my',strategy:String(saved.id),step:'rules'})); setSymbol(''); setInputSymbol(''); setSourceMode(mode); setEditing(true); setTemplatesOpen(false); setCreationOpen(false)
    } catch (e) { setError(message(e)) } finally { setBusy(false) }
  }
  useEffect(() => {
    if (editing) { workspaceRef.current?.scrollIntoView?.({ block: 'start', behavior: 'smooth' }); workspaceRef.current?.focus({ preventScroll: true }) }
  }, [editing, selectedId])
  const draftSaved = (saved: StrategyDraft) => {
    setItems(rows => rows.map(row => row.id === saved.id ? { ...row, ...saved } : row))
    void load()
    if (saved.id !== selectedId) { const updated = new URLSearchParams(query); updated.set('strategy', String(saved.id)); setQuery(updated, { replace: true }) }
  }
  const openWork = (item: StrategyLibraryItem, target: View) => {
    const ticker = item.scope === 'stock' ? item.symbol : symbol
    setSymbol(ticker); setInputSymbol(ticker); setSourceMode(''); const updated = new URLSearchParams({view:'my',strategy:String(item.id),step:target==='backtest'?'backtest':'rules'}); if(ticker)updated.set('symbol',ticker); setQuery(updated); setEditing(true)
  }
  const scan = async (targets: Stock[]) => {
    if (!selected) { setError('请先选择我的策略。'); return }
    if (!targets.length) { setError('此范围没有候选；请添加关注或选择单票检查。'); return }
    const request = ++seq.current
    setScanBusy(true); setError(''); setResults(null)
    try {
      const data = await strategyApi.scanLibrary(selected.id, { symbols: targets.slice(0, 30), expected_hash: selected.rules_hash, allow_network: allowNetwork })
      if (request === seq.current) setResults(data)
    } catch (e) { if (request === seq.current) setError(message(e)) }
    finally { if (request === seq.current) setScanBusy(false) }
  }
  const inspectSystem = (stock: Stock) => { void scan([stock]) }
  const readReport = async (id: number) => {
    const request = ++reportSeq.current
    setError('')
    try { const data = await strategyApi.backtestRecord(id); if (request === reportSeq.current) setReport(data) } catch (e) { if (request === reportSeq.current) setError(message(e)) }
  }
  const record = (value: RecordIntent) => {
    if (!intent) return
    try { navigate(`/portfolio?record_intent=${saveTradeTransfer({ ...value, target: intent })}`) }
    catch { setError('待记账参数保存失败，请重试；成交尚未写入。') }
  }
  const ruleSummary = (item: StrategyDraftInput) => {
    const text = strategySummary(item, fields, presets)
    return <div className="space-y-2 text-sm leading-6"><p><span className="text-muted-foreground">买入 </span>{text.buy}</p><p><span className="text-muted-foreground">退出 </span>{text.sell}</p><p><span className="text-muted-foreground">禁买 </span>{text.avoid}</p></div>
  }
  const remove = async () => {
    if (!deleteTarget) return
    setBusy(true); setError('')
    try {
      await strategyApi.deleteLibrary(deleteTarget.id, deleteTarget.rules_hash, disableBindings, deleteEnabled.map(row=>row.id))
      localStorage.removeItem(`panwatch_strategy_library:${deleteTarget.id}`)
      setCompare(ids => ids.filter(id => id !== deleteTarget.id)); setDeleteTarget(null); setNotice('策略已移入回收站，历史报告和检查记录保留'); await load()
      if (deleteTarget.id === selectedId) go('my', 0, '')
    } catch (e) { setError(message(e)) } finally { setBusy(false) }
  }
  const restore = async (id: number) => {
    setBusy(true); setError('')
    try { await strategyApi.restoreLibrary(id); await load(); setNotice('策略已恢复，原来停用的范围不会自动启用') } catch (e) { setError(message(e)) } finally { setBusy(false) }
  }
  const deleteEnabled = deleteTarget?.bindings.filter(row => row.enabled) || []
  const work = selected && editing && <section ref={workspaceRef} tabIndex={-1} aria-label="策略编辑与验证">
    <Button className="mb-4 -ml-3" variant="ghost" onClick={() => go('my',0,'')}><ArrowLeft size={16} />返回我的策略</Button>
    <StrategyBacktest key={selected.id} symbol={symbol} market={selected.market} expanded libraryDraft={selected} libraryBindings={selected.bindings} guided initialStage={query.get('step') === 'apply' ? 'apply' : query.get('step') === 'backtest' || view==='backtest' ? 'backtest' : 'rules'} initialSource={sourceMode} onStageChange={step => { const updated = new URLSearchParams(query); updated.set('step', step); setQuery(updated, { replace: true }) }} onSymbolChange={ticker => { setSymbol(ticker); setInputSymbol(ticker); const updated = new URLSearchParams(query); ticker ? updated.set('symbol', ticker) : updated.delete('symbol'); setQuery(updated, { replace: true }) }} onDraftSaved={draftSaved} />
  </section>

  return <div className="page-container strategy-center pb-12">
    <header className="flex flex-wrap items-start justify-between gap-4"><div><h1>策略中心</h1><p className="mt-2 text-sm text-muted-foreground">把买卖想法写成规则，验证后用于每次操作检查</p></div><div className="flex gap-2"><Button variant="secondary" onClick={() => setIntentOpen(true)}>检查一笔买卖</Button>{!editing && <Button disabled={busy} onClick={() => setCreationOpen(true)}>创建策略</Button>}</div></header>
    <nav aria-label="策略中心视图" className="sc-nav">{views.filter(([key]) => key !== 'backtest').map(([key, label]) => <button key={key} aria-current={(view === key || key === 'records' && view === 'backtest') ? 'page' : undefined} onClick={() => go(key, 0, '')}>{label}</button>)}</nav>
    {notice && <p role="status" className="mb-4 rounded-lg bg-muted p-4 text-sm">{notice}</p>}
    {!editing && (view === 'records' || view === 'backtest') && <div className="mb-5 flex gap-2"><Button variant={view === 'records' ? 'secondary' : 'ghost'} onClick={() => go('records', selectedId)}>操作纪律</Button><Button variant={view === 'backtest' ? 'secondary' : 'ghost'} onClick={() => go('backtest', selectedId)}>回测报告</Button></div>}

    {error && <div role="alert" className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-destructive/40 p-3 text-sm"><p>{error}</p><Button variant="ghost" onClick={() => { setError(''); void load() }}>重新读取策略</Button></div>}
    {loading ? <p role="status">正在读取策略库…</p> : <>
      {view === 'my' && editing && work}
      {view === 'my' && !editing && <>
        {(!error || items.length > 0) && <p className="mb-4 text-sm text-muted-foreground">{items.length} 份策略 · {bindings.filter(row => row.enabled).length} 个已启用范围。保存草稿、回测与应用相互独立。</p>}
        {!error && !items.length && <div className="rounded-xl border border-dashed border-border p-8 text-center"><h2 className="text-lg font-medium">从一套你理解的规则开始</h2><p className="my-3 text-sm text-muted-foreground">可以复制模板，或创建空白策略，再手写或让 AI 整理条件。</p><Button onClick={() => setTemplatesOpen(true)}>了解模板的区别</Button></div>}
        <div className="sc-toolbar"><input aria-label="搜索我的策略" placeholder="搜索策略" className="sc-control" value={search} onChange={e => setSearch(e.target.value)} /><Button variant="ghost" onClick={() => setCompareMode(!compareMode)}>{compareMode ? '结束比较' : '比较策略'}</Button><Button variant="ghost" disabled={busy} onClick={() => setTemplatesOpen(true)}>浏览模板</Button></div>
        <div className="sc-list">{items.filter(item => `${item.name} ${item.symbol} ${item.note}`.toLowerCase().includes(search.toLowerCase())).map(item => {
          const active = item.bindings.filter(row => row.enabled), changed = active.some(row => row.rules_hash !== item.rules_hash)
          const pending = item.questions.length > 0 || !item.buy.length
          return <article key={item.id} className="sc-row" aria-label={`策略 ${item.name}`}><div className="min-w-0"><div className="flex flex-wrap items-center gap-x-4 gap-y-2"><h2>{item.name}</h2><span className="sc-status" data-state={active.length ? changed ? 'pending' : 'active' : 'draft'}>{active.length ? changed ? '有修改未启用' : '用于操作检查' : pending ? '待完善规则' : item.tested ? '已回测，尚未启用' : '尚未回测'}</span></div><p className="mt-2 text-sm text-muted-foreground">{item.config?.template_id ? `${templateCategory(item.config.template_id)} · ${templateSignalDescription(item.config, 'buy')}` : item.note || '补充买入、退出和禁买条件'}</p><p className="sc-meta">{marketName(item.market)} · {item.scope === 'stock' ? `仅 ${item.symbol}` : '通用策略'} · 应用范围：{active.length ? active.map(row => `${row.scope === 'global' ? '默认' : row.symbol} v${row.version || '?'}`).join('；') : '尚未应用'}</p>{changed && <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">操作检查仍使用已启用版本，草稿修改尚未生效</p>}<details className="mt-2"><summary className="text-sm text-muted-foreground">查看买入、退出与禁买规则</summary>{ruleSummary(item)}</details></div><div className="sc-actions">{compareMode && <label className="flex items-center gap-2"><input aria-label={`比较 ${item.name}`} type="checkbox" checked={compare.includes(item.id)} disabled={!compare.includes(item.id) && compare.length >= 3} onChange={e => setCompare(ids => e.target.checked ? [...ids, item.id] : ids.filter(id => id !== item.id))} />比较</label>}<Button variant="ghost" aria-label={`编辑规则 ${item.name}`} disabled={busy} onClick={() => openWork(item, 'my')}>编辑规则</Button><Button variant="secondary" onClick={() => { if (!active.length && !pending) openWork(item, 'backtest'); else openWork(item, 'my') }}>{active.length ? '管理策略' : pending ? '完善规则' : '开始回测'}</Button><Button variant="ghost" aria-label={`复制 ${item.name}`} disabled={busy} onClick={() => void create(item)}><Copy size={16} /></Button><Button variant="ghost" aria-label={`删除 ${item.name}`} disabled={busy} onClick={() => { setDeleteTarget(item); setDisableBindings(false) }}><Trash2 size={16} />删除</Button></div></article>
        })}</div>
        {!!search && !items.some(item => `${item.name} ${item.symbol} ${item.note}`.toLowerCase().includes(search.toLowerCase())) && <p className="py-8 text-muted-foreground">没有匹配策略，试试清空关键词</p>}
        {compare.length >= 2 && <section className="mt-5 rounded-xl border border-border bg-card p-4" aria-label="策略比较"><h2 className="mb-3 text-lg font-semibold">策略区别（最多3份）</h2><div className="grid gap-4 md:grid-cols-3">{items.filter(item => compare.includes(item.id)).map(item => <div key={item.id}><h3 className="mb-3 font-medium">{item.name}</h3>{ruleSummary(item)}<p className="mt-3 text-sm text-muted-foreground">{item.tested ? '有匹配规则的历史回测，适用标的和区间见策略卡' : '尚未验证当前规则'}</p></div>)}</div></section>}
        {!!bindings.length && <details className="mt-5 rounded-xl border border-border p-4"><summary className="cursor-pointer font-medium">实际应用管理 · {bindings.length} 个范围</summary><p className="my-3 text-sm text-muted-foreground">个股规则覆盖默认规则。旧规则也在这里，可以复制进策略库；停用后重新检查操作。</p>{bindings.map(row => <div key={row.id} className="flex flex-wrap items-center justify-between gap-3 border-t border-border py-3"><span className="text-sm">{row.name} · {row.scope === 'global' ? '默认' : row.symbol} · {row.enabled ? '启用' : '停用'} · v{row.version || '?'}</span><div className="flex gap-2"><Button size="sm" variant="ghost" disabled={busy} onClick={() => void create({ ...row, market: row.market || 'CN' })}>复制进策略库</Button><Button size="sm" variant="secondary" disabled={busy} onClick={async () => { if (!window.confirm(`${row.enabled ? '停用' : '启用'} ${row.name} 的${row.scope === 'global' ? '默认' : row.symbol}规则？`)) return; setBusy(true); try { await strategyApi.setBindingEnabled(row.id, !row.enabled); await load() } catch (e) { setError(message(e)) } finally { setBusy(false) } }}>{row.enabled ? '停用' : '启用'}</Button></div></div>)}</details>}
        <details className="mt-6 border-t border-border pt-4"><summary>回收站 · {deleted.length}</summary><p className="my-3 text-sm text-muted-foreground">恢复仅找回策略，不会自动重新启用</p>{deleted.map(item => <div key={item.id} className="flex items-center justify-between gap-3 border-t border-border py-4"><span>{item.name}</span><Button variant="secondary" disabled={busy} onClick={() => void restore(item.id)}><RotateCcw size={16} />恢复</Button></div>)}{!deleted.length && <p className="py-3 text-muted-foreground">回收站为空</p>}</details>
      </>}
      {view !== 'my' && !editing && <div className="mb-4 flex flex-wrap items-center gap-3"><label className="text-sm">我的策略<select aria-label="选择我的策略" className={`${selectClass} ml-2`} value={selectedId || ''} onChange={event => { go(view, Number(event.target.value)); setBatch(0); setResults(null) }}><option value="">{view === 'opportunities' ? '先选择一份策略' : '全部策略记录'}</option>{items.map(item => <option key={item.id} value={item.id}>{item.name} · {marketName(item.market)}</option>)}</select></label>{selected && <Button variant="ghost" onClick={() => openWork(selected, view === 'backtest' ? 'backtest' : 'my')}>查看规则与应用范围</Button>}</div>}
      {view === 'opportunities' && <>
        <section className="rounded-xl border border-border bg-card p-4 md:p-5" aria-label="按我的策略找机会"><h2 className="text-lg font-semibold">按我的策略检查候选</h2><p className="my-3 text-sm leading-6 text-muted-foreground">候选检查只判断入场条件；操作前检查使用实际启用版本，并核对账户与价量。每批最多30只，按关注列表顺序检查，不代表全市场扫描。</p>
          <div className="flex flex-wrap items-center gap-3"><select aria-label="候选范围" className={selectClass} value={universe} onChange={event => { setUniverse(event.target.value); setResults(null) }}><option value="watchlist">我的关注</option><option value="single">单票检查</option></select>{universe === 'watchlist' && pool.length > 30 && <select aria-label="检查批次" className={selectClass} value={batch} onChange={event => { setBatch(Number(event.target.value)); setResults(null) }}>{Array.from({ length: Math.ceil(pool.length / 30) }, (_, i) => <option key={i} value={i}>{i * 30 + 1}—{Math.min(pool.length, (i + 1) * 30)} / {pool.length}只</option>)}</select>}{universe === 'single' && <input aria-label="候选证券代码" placeholder={intentMarket==='CN'?'6位A股代码':'证券代码'} className={selectClass} value={inputSymbol} onChange={event => { seq.current++; setScanBusy(false); setResults(null); setInputSymbol(event.target.value) }} />}<label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={allowNetwork} onChange={event => setAllowNetwork(event.target.checked)} />缺行情时尝试补数（较慢）</label><Button disabled={!selected || scanBusy} onClick={() => { if (universe === 'single' && !/^\d{6}$/.test(inputSymbol.trim())) { setError('单票检查请填写6位A股代码。'); return } void scan(universe === 'single' ? [{ symbol: inputSymbol.trim(), market: selected?.market || 'CN', name: inputSymbol.trim() }] : pool.slice(batch * 30, batch * 30 + 30)) }}>{scanBusy ? '正在检查候选…' : '检查候选'}</Button></div>
          {!selected && <p className="mt-3 text-sm text-muted-foreground">先从上方选择策略，或回“我的策略”创建一份。</p>}
          {results && <div className="mt-4 space-y-3"><p className="text-xs text-muted-foreground">检查 {results.items.length} 只 · {new Date(results.as_of).toLocaleString()} · 草稿 #{results.draft_id} · {stale ? '规则已变化，结果过时，请重新检查' : '对应所选规则快照'}</p>{results.items.map(row => <article key={`${row.market}:${row.symbol}`} className="rounded-lg border border-border p-4"><div className="flex flex-wrap justify-between gap-2"><h3 className="font-medium">{row.name} · {row.symbol}</h3><strong className="text-sm">{stale ? '结果已过时' : scanLabels[row.status]}</strong></div><p className="mt-2 text-sm text-muted-foreground">{row.summary} · 日线截至 {row.as_of || '未知'}</p><details className="mt-3 text-sm"><summary className="cursor-pointer">逐条依据</summary>{[...(row.buy_checks || []), ...(row.avoid_checks || [])].map((check, i) => <p className="mt-2" key={i}>{check.text || check.label} · 实际 {check.actual} · {check.ok == null ? '未知' : (row.avoid_checks || []).includes(check) ? check.ok ? '命中禁止项' : '未命中禁止项' : check.ok ? '满足' : '未满足'}</p>)}</details><Button variant="secondary" className="mt-3" disabled={stale} onClick={() => setIntent({ symbol: row.symbol, market: row.market, name: row.name, side: 'buy', price: row.price, quantity: 100, discoveryStrategyName: selected?.name })}>检查这笔操作（按实际启用规则）</Button></article>)}</div>}
        </section>
        <details open={systemOpen} onToggle={event => setSystemOpen(event.currentTarget.open)} className="mt-5 rounded-xl border border-border p-4"><summary className="cursor-pointer font-medium">原机会发现 · 系统候选与发现标签</summary><p className="my-3 text-sm text-muted-foreground">系统评分用于发现和排序，未验证你的交易纪律。选好“我的策略”后，可逐只检查；不自动改动实际启用规则。</p>{systemOpen && <OpportunityDiscovery onChoose={inspectSystem} />}</details>
      </>}
      {view === 'backtest' && <>
        {selected && !editing && <Button onClick={() => openWork(selected, 'backtest')}>选择标的，运行此策略回测</Button>}
        {work}
        {!editing && <section className="mt-5 rounded-xl border border-border bg-card p-4"><h2 className="mb-3 text-lg font-semibold">历史回测报告</h2><p className="mb-3 text-sm text-muted-foreground">报告保存当时规则与参数；信号后验表现单独保留在系统候选中。</p>{recordLoading ? <p role="status">读取回测记录中…</p> : !reports.length ? <p>暂无报告，选择策略和标的后可运行回测。</p> : reports.map(row => <div key={row.id} className="flex flex-wrap items-center justify-between gap-3 border-t border-border py-3 text-sm"><div>#{row.id} · {row.symbol} · {row.start}—{row.end}<p className="mt-1 text-muted-foreground">{row.status === 'succeeded' ? `策略收益 ${pct(row.total_return)} · 最大回撤 ${pct(row.max_drawdown)}` : row.status === 'running' ? '运行中' : `失败：${row.error}`}{selected && row.rules_hash !== selected.rules_hash ? ' · 规则已变化' : ''}</p></div><Button variant="ghost" onClick={() => void readReport(row.id)}>查看报告快照</Button></div>)}</section>}

      </>}
      {view === 'records' && <section className="rounded-xl border border-border bg-card p-4"><h2 className="text-lg font-semibold">操作纪律记录</h2><p className="my-3 text-sm text-muted-foreground">只读历史检查，保留当时版本与例外说明。历史通过不能用作当前操作依据；成交需在券商完成。</p>{recordLoading ? <p role="status">读取检查记录中…</p> : !decisions.length ? <p>暂无对应检查。可从持仓的加仓、减仓，或机会中的“检查这笔操作”开始。</p> : decisions.map(row => <details key={row.decision_id} className="border-t border-border py-3 text-sm"><summary className="cursor-pointer">#{row.decision_id} · {row.input?.symbol} · {row.input?.side === 'sell' ? '卖出' : '买入'} · {decisionLabels[row.status]} · {row.linked_trade_id ? '已关联成交' : '未关联成交'}</summary><div className="mt-3 space-y-2 leading-6"><p>{row.summary}</p><p>账户 {accounts.find(account => account.id === row.input?.account_id)?.name || row.input?.account_id || '未指定'} · 价量 {row.input?.price} × {row.input?.quantity} · 版本 #{row.revision_id || '无'}</p><p>检查时间 {row.created_at ? new Date(row.created_at).toLocaleString() : '未知'} · 日线截至 {row.data_as_of || '未知'}</p><p>例外：{row.exception_reason || '无'} · 成交编号：{row.linked_trade_id || '无'}</p>{row.input?.policy && ruleSummary(row.input.policy as unknown as StrategyDraftInput)}</div></details>)}</section>}
    </>}
    <Dialog open={templatesOpen} onOpenChange={open => { if (!busy) setTemplatesOpen(open) }}><DialogContent className="strategy-dialog max-w-3xl max-h-[85vh] overflow-y-auto"><DialogHeader><DialogTitle>从模板理解策略区别</DialogTitle><DialogDescription>复制成我的策略后可修改；模板本身不会自动应用，历史收益也不保证未来表现。</DialogDescription></DialogHeader><input aria-label="搜索策略模板" placeholder="搜索策略名称或适用场景" className="mb-4 min-h-11 w-full rounded-lg border border-border bg-background px-3" value={templateQuery} onChange={e=>setTemplateQuery(e.target.value)}/><div className="mb-4 flex flex-wrap gap-2">{['全部','趋势跟随','趋势突破','回调反弹','短线动量','多因子选股','交易纪律'].map(n=><Button key={n} variant={category===n?'secondary':'ghost'} onClick={()=>setCategory(n)}>{n}</Button>)}</div><p className="mb-3 text-xs text-muted-foreground">{presets.filter(p=>p.source==='tick-stock-panel').length} 个 Tick 策略定义，加上原有模板。是否可计算取决于标的、日期与输入数据；复制不会启用。</p><div className="space-y-4">{presets.filter(p=>(category==='全部'||templateCategory(p.id)===category)&&`${p.name} ${p.description}`.toLowerCase().includes(templateQuery.toLowerCase())).map(preset => <article key={preset.id} className="rounded-lg border border-border p-4"><h3 className="font-semibold">{preset.name}</h3><p className="my-2 text-sm text-muted-foreground">{preset.config?.template_id ? templateSignalDescription(preset.config, 'buy') : preset.description}</p><p className="my-2 text-sm text-muted-foreground">{templateCaveat(preset.id)}</p><p className="text-xs text-muted-foreground">{preset.config?.template_id ? `包含原始信号、参数、评分与风险执行。止损 ${preset.config.execution?.stop_loss_pct}% · 最长 ${preset.config.execution?.max_hold_days} 个交易日。` : '原有规则模板'}</p>{ruleSummary({ ...preset, market: 'CN', scope: 'global', symbol: '' })}<Button variant="secondary" className="mt-3" disabled={busy} onClick={() => void create({ ...preset, market: 'CN', scope: 'global', symbol: '', timing: 'completed_daily' })}>复制为我的策略</Button></article>)}</div></DialogContent></Dialog>
    <Dialog open={!!report} onOpenChange={open => { if (!open) { reportSeq.current++; setReport(null) } }}><DialogContent className="strategy-dialog max-w-3xl max-h-[85vh] overflow-y-auto"><DialogHeader><DialogTitle>回测报告 #{report?.id}</DialogTitle><DialogDescription>历史报告快照，保存的是当时规则与参数。</DialogDescription></DialogHeader>{report && <div className="space-y-3 text-sm"><p>状态 {report.status} · {report.result?.start}—{report.result?.end}</p><p>策略收益 {pct(report.result?.total_return)} · 买入持有 {pct(report.result?.buy_hold_return)} · 最大回撤 {pct(report.result?.metrics?.max_drawdown)}</p><p>{report.result?.explain || report.error}</p>{report.result?.rules && ruleSummary({ ...report.result.rules, scope: 'global', symbol: '', market: 'CN', name: '', note: '' })}<details><summary className="cursor-pointer">查看资金、费用与数据依据</summary><pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-all rounded-lg bg-muted p-3 text-xs">{JSON.stringify({ config: report.config, snapshot: report.input_snapshot }, null, 2)}</pre></details><h3 className="font-medium">信号与成交</h3>{report.result?.orders?.map((order, i) => <p key={i}>{order.side === 'buy' ? '买入' : '卖出'} · 信号 {order.signal_date} → {order.execution_date || '无次日行情'} · {order.status === 'filled' ? `${order.fill_price} × ${order.quantity}` : order.reason || '未成交'}</p>)}</div>}</DialogContent></Dialog>
    <Dialog open={creationOpen} onOpenChange={open => { if (!busy) setCreationOpen(open) }}><DialogContent className="strategy-dialog max-w-3xl"><DialogHeader><DialogTitle>你想怎么创建策略？</DialogTitle><DialogDescription>先形成可编辑规则，明确启用后才会用于操作检查</DialogDescription></DialogHeader><div className="grid gap-4 md:grid-cols-3"><button className="sc-entry" disabled={busy} onClick={() => void create(undefined, 'idea')}><strong>用中文描述想法</strong><span>让 AI 整理条件，再逐条确认</span></button><button className="sc-entry" disabled={busy} onClick={() => { setCreationOpen(false); setTemplatesOpen(true) }}><strong>从模板开始</strong><span>先看买卖思路和局限，再复制修改</span></button><button className="sc-entry" disabled={busy} onClick={() => void create(undefined, 'manual')}><strong>自己编写条件</strong><span>使用因子、历史时点与条件组</span></button></div></DialogContent></Dialog>
    <Dialog open={!!deleteTarget} onOpenChange={open => { if (!busy && !open) setDeleteTarget(null) }}><DialogContent className="strategy-dialog max-w-lg"><DialogHeader><DialogTitle>删除策略“{deleteTarget?.name}”？</DialogTitle><DialogDescription>移入回收站后可以恢复，历史回测、规则版本与操作检查记录保留</DialogDescription></DialogHeader>{deleteEnabled.length > 0 ? <div className="space-y-3 text-sm"><p>以下范围仍在使用这份策略：</p><ul className="list-disc pl-5">{deleteEnabled.map(row => <li key={row.id}>{row.scope === 'global' ? '默认规则' : row.symbol} · v{row.version || '?'}</li>)}</ul><p>停用个股规则后继承仍启用的默认规则；没有默认规则时会变为未配置。停用默认会影响没有个股覆盖的标的</p><label className="flex items-start gap-3"><input className="mt-1" type="checkbox" checked={disableBindings} onChange={e => setDisableBindings(e.target.checked)} />确认停用以上范围并删除策略</label></div> : <p className="text-sm">没有正在使用的应用范围，删除不会改变其他策略</p>}{error && <p className="text-sm text-destructive">{error}</p>}<div className="mt-4 flex justify-end gap-2"><Button variant="ghost" disabled={busy} onClick={() => setDeleteTarget(null)}>取消</Button><Button variant="destructive" disabled={busy || (!!deleteEnabled.length && !disableBindings)} onClick={() => void remove()}>{busy ? '正在处理…' : deleteEnabled.length ? '停用并删除' : '移入回收站'}</Button></div></DialogContent></Dialog>
    <Dialog open={intentOpen} onOpenChange={setIntentOpen}><DialogContent className="strategy-dialog max-w-md"><DialogHeader><DialogTitle>检查一笔买卖</DialogTitle><DialogDescription>自动使用标的实际启用的策略，不使用研究草稿</DialogDescription></DialogHeader><label>证券代码<input aria-label="操作检查证券代码" className="sc-control mt-2 w-full" value={intentCode} onChange={e => setIntentCode(e.target.value)} placeholder={intentMarket==='CN'?'6位A股代码':'证券代码'} /></label><label>或从关注选择<select aria-label="选择检查标的" className="sc-control mt-2 w-full" value={stocks.some(s=>s.market===intentMarket&&s.symbol===intentCode)?`${intentMarket}:${intentCode}`:''} onChange={e => {const [market,code]=e.target.value.split(':');setIntentMarket(market||'CN');setIntentCode(code||'')}}><option value="">选择标的</option>{stocks.map(s => <option key={`${s.market}:${s.symbol}`} value={`${s.market}:${s.symbol}`}>{s.name} · {s.symbol} · {marketName(s.market)}</option>)}</select></label><label>市场<select aria-label="操作检查市场" className="sc-control mt-2 w-full" value={intentMarket} onChange={e=>setIntentMarket(e.target.value)}><option value="CN">A股</option><option value="HK">港股</option><option value="US">美股</option></select></label><label>方向<select className="sc-control mt-2 w-full" value={intentSide} onChange={e => setIntentSide(e.target.value as 'buy' | 'sell')}><option value="buy">买入 / 加仓</option><option value="sell">卖出 / 减仓</option></select></label><Button disabled={!intentCode.trim()||(intentMarket==='CN'&&!/^\d{6}$/.test(intentCode.trim()))} onClick={() => { const stock = stocks.find(s => s.symbol === intentCode.trim() && s.market === intentMarket); setIntent({ symbol: intentCode.trim(), market: intentMarket, name: stock?.name || intentCode, side: intentSide, price: stock?.price, quantity: 100 }); setIntentOpen(false) }}>填写价量并检查</Button></DialogContent></Dialog>
    <TradeDecisionDialog target={intent} accounts={accounts} onClose={() => setIntent(null)} onRecord={record} />
  </div>
}
