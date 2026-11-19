import { useEffect, useRef, useState } from 'react'
import { fetchAPI, type StrategyDraft } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'

type Job = { id: number; status: string; error?: string; progress?: { stage: string; done: number; total: number }; result?: { total_return: number; final_equity: number; data_hash: string; rules_hash: string; universe: string[]; limitations: string[]; curve: Array<{ date: string; equity: number }>; orders: Array<{ symbol: string; side: string; status: string; signal_date: string; execution_date?: string; quantity?: number; price?: number; reason?: string }> } }
type Props = { prepare: () => Promise<StrategyDraft>; disabled: boolean; storageKey: string; embedded?: boolean; policyKey?: string; onEvidenceChange?: (valid: boolean) => void }
const errorText = (e: unknown) => e instanceof Error ? e.message : '请求失败，请重试'
export default function StrategyPortfolioBacktest({ prepare, disabled, storageKey, embedded = false, policyKey = '', onEvidenceChange }: Props) {
  const [open, setOpen] = useState(embedded), [universe, setUniverse] = useState('watchlist'), [symbols, setSymbols] = useState('')
  const [start, setStart] = useState('2026-01-01'), [end, setEnd] = useState(new Date().toLocaleDateString('sv-SE'))
  const [capital, setCapital] = useState('100000'), [positions, setPositions] = useState('5'), [weight, setWeight] = useState('20')
  const [job, setJob] = useState<Job | null>(null), [jobKey, setJobKey] = useState(''), [expectedHash, setExpectedHash] = useState('')
  const [busy, setBusy] = useState(false), [error, setError] = useState('')
  const alive = useRef(true), evidence = useRef(onEvidenceChange)
  evidence.current = onEvidenceChange
  const currentKey = JSON.stringify({ policyKey, universe, symbols, start, end, capital, positions, weight })
  const result = job?.result
  const valid = job?.status === 'succeeded' && !!expectedHash && result?.rules_hash === expectedHash && jobKey === currentKey
  useEffect(() => { evidence.current?.(valid) }, [valid])
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  useEffect(() => {
    if (!open) return
    let active = true
    try {
      const stored = JSON.parse(localStorage.getItem(`${storageKey}:portfolio_run`) || 'null')
      const id = typeof stored === 'number' ? stored : stored?.id
      if (id) void fetchAPI<Job>(`/strategy/portfolio-runs/${Number(id)}`).then(r => { if (active) { setJob(r); setJobKey(stored?.key || ''); setExpectedHash(stored?.hash || '') } }).catch(e => { if (active) setError(`上次任务读取失败：${errorText(e)}`) })
    } catch { /* Invalid recovery metadata cannot produce a passing result. */ }
    return () => { active = false }
  }, [open, storageKey])
  useEffect(() => {
    if (job?.status !== 'running') return
    const timer = setInterval(() => { void fetchAPI<Job>(`/strategy/portfolio-runs/${job.id}`).then(r => { if (alive.current) { setJob(r); setError('') } }).catch(e => { if (alive.current) setError(errorText(e)) }) }, 2000)
    return () => clearInterval(timer)
  }, [job?.id, job?.status])
  const launch = async () => {
    setBusy(true); setError('')
    const snapshot = currentKey
    try {
      if (!start || !end || start > end || !Number.isFinite(Number(capital)) || Number(capital) <= 0 || !Number.isInteger(Number(positions)) || Number(positions) < 1 || !Number.isFinite(Number(weight)) || Number(weight) <= 0 || Number(weight) > 100) throw new Error('请核对日期、正数资金、持仓数量和仓位比例')
      const saved = await prepare(); if (!alive.current) return
      const next = await fetchAPI<Job>('/strategy/portfolio-runs', { method: 'POST', body: JSON.stringify({ draft_id: saved.id, start_date: start, end_date: end, universe, symbols: symbols.split(/[\s,，]+/).filter(Boolean), initial_capital: Number(capital), max_positions: Number(positions), max_weight: Number(weight) / 100 }) })
      if (alive.current) { setJob(next); setJobKey(snapshot); setExpectedHash(saved.rules_hash); localStorage.setItem(`${storageKey}:portfolio_run`, JSON.stringify({ id: next.id, key: snapshot, hash: saved.rules_hash })) }
    } catch (e) { if (alive.current) setError(errorText(e)) } finally { if (alive.current) setBusy(false) }
  }
  const cancel = async () => {
    if (!job) return
    try { await fetchAPI(`/strategy/portfolio-runs/${job.id}/cancel`, { method: 'POST' }); if (alive.current) setError('已请求取消，正在结束当前计算') } catch (e) { if (alive.current) setError(errorText(e)) }
  }
  const content = <>
    <h3 className="text-base font-medium">组合回测</h3><p className="my-3 text-sm text-muted-foreground">沪深普通 A 股共享一笔资金；使用启动时的规则与固定股票池，本次不逐股联网补数</p>
    <fieldset disabled={disabled || busy || job?.status === 'running'} className="grid gap-4 sm:grid-cols-3">
      <label>股票池<select aria-label="组合股票池" className="mt-2 block min-h-11 w-full rounded-lg border border-border bg-card px-3" value={universe} onChange={e => setUniverse(e.target.value)}><option value="watchlist">我的关注</option><option value="custom">自选股票池</option><option value="all">本地全市场（耗时较长）</option></select></label>
      {universe === 'custom' && <label>代码（逗号或空格分隔）<input aria-label="组合股票代码" className="mt-2 block min-h-11 w-full rounded-lg border border-border bg-background px-3" value={symbols} onChange={e => setSymbols(e.target.value)} /></label>}
      <label>开始日期<input aria-label="组合开始日期" type="date" className="mt-2 block min-h-11 w-full rounded-lg border border-border bg-background px-3" value={start} onChange={e => setStart(e.target.value)} /></label><label>结束日期<input aria-label="组合结束日期" type="date" className="mt-2 block min-h-11 w-full rounded-lg border border-border bg-background px-3" value={end} onChange={e => setEnd(e.target.value)} /></label>
      {([['初始资金（元）', '组合初始资金', capital, setCapital], ['最多持仓数量', '组合最大持仓数', positions, setPositions], ['单次建仓上限（%）', '组合单次建仓上限', weight, setWeight]] as const).map(([label, aria, value, update]) => <label key={aria}>{label}<input aria-label={aria} inputMode="decimal" className="mt-2 block min-h-11 w-full rounded-lg border border-border bg-background px-3" value={value} onChange={e => update(e.target.value)} /></label>)}
    </fieldset>
    <div className="mt-5 flex flex-wrap gap-3"><Button disabled={disabled || busy || job?.status === 'running'} onClick={() => void launch()}>{busy ? '正在保存快照…' : '运行组合回测'}</Button>{job?.status === 'running' && <Button variant="secondary" onClick={() => void cancel()}>取消任务</Button>}</div>
    {error && <p role="alert" className="mt-4 text-sm text-amber-700 dark:text-amber-300">{error}</p>}
    {job && <p className="mt-4 text-sm" role="status">报告 #{job.id} · {({ running: '运行中', succeeded: '完成', failed: '失败', cancelled: '已取消', interrupted: '已中断' } as Record<string, string>)[job.status] || job.status}{job.progress && job.status === 'running' ? ` · ${job.progress.stage} ${job.progress.done}/${job.progress.total}` : ''}{job.error ? ` · ${job.error}` : ''}</p>}
    {result && <section aria-label="组合回测报告" className="mt-5 space-y-4"><p className="text-sm text-muted-foreground">{valid ? '对应当前规则与组合参数' : '历史报告：规则或组合参数已变化，不能作为当前配置的验证结果'}</p><p className="text-lg tabular-nums">收益 {result.total_return.toFixed(2)}% · 期末资产 ¥{result.final_equity.toLocaleString('zh-CN', { maximumFractionDigits: 2 })} · {result.universe.length} 个标的</p><PortfolioCurve curve={result.curve} /><div className="max-h-72 overflow-auto"><table className="w-full text-left text-xs"><thead><tr><th className="p-2">标的</th><th>方向</th><th>信号 → 成交</th><th>结果</th></tr></thead><tbody>{result.orders.slice(-100).map((o, i) => <tr key={i} className="border-t border-border"><td className="p-2">{o.symbol}</td><td>{o.side === 'buy' ? '买入' : '卖出'}</td><td>{o.signal_date} → {o.execution_date || '无下一日'}</td><td>{o.status === 'filled' ? `${o.quantity}股 × ${o.price?.toFixed(3)}` : o.reason || '未成交'}</td></tr>)}</tbody></table></div><details><summary className="min-h-11 cursor-pointer">范围、局限与数据依据</summary><p className="my-3 break-all text-xs">数据 {result.data_hash} · 规则 {result.rules_hash}</p><ul className="list-disc space-y-2 pl-5 text-xs text-muted-foreground">{result.limitations.map(x => <li key={x}>{x}</li>)}</ul></details></section>}
  </>
  return embedded ? <section className="rounded-xl border border-border p-4 md:p-5">{content}</section> : <details className="rounded-xl border border-border p-4" onToggle={e => setOpen(e.currentTarget.open)}><summary className="min-h-11 cursor-pointer">组合回测</summary>{open && content}</details>
}

function PortfolioCurve({ curve }: { curve: Array<{ date: string; equity: number }> }) {
  const rows = curve.filter(p => Number.isFinite(p.equity)); if (!rows.length) return null
  const min = Math.min(...rows.map(p => p.equity)), max = Math.max(...rows.map(p => p.equity)), range = max - min || 1
  const points = rows.map((p, i) => `${20 + i / Math.max(1, rows.length - 1) * 760},${220 - (p.equity - min) / range * 190}`).join(' ')
  return <div><svg viewBox="0 0 800 250" className="h-64 w-full" role="img" aria-label={`组合资产曲线，${rows[0].date} 至 ${rows[rows.length - 1]?.date}`}><path d="M20 30H780 M20 125H780 M20 220H780" className="stroke-border" fill="none" /><polyline points={points} fill="none" stroke="#7395be" strokeWidth="2" /></svg><p className="flex justify-between text-xs text-muted-foreground"><span>{rows[0].date}</span><span>{rows[rows.length - 1]?.date}</span></p></div>
}
