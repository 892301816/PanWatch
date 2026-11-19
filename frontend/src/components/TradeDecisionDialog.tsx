import { useEffect, useRef, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@panwatch/base-ui/components/ui/dialog'

export interface TradeIntentTarget {
  symbol: string
  name: string
  market: string
  side: 'buy' | 'sell'
  accountId?: number
  price?: number
  quantity?: number
  discoveryStrategyName?: string
}

export interface RecordIntent {
  accountId: number
  price: number
  quantity: number
  decisionId?: number
  note: string
}

interface CheckItem {
  label?: string
  field?: string
  actual?: unknown
  expect?: unknown
  expected?: unknown
  required?: unknown
  ok?: boolean
  status?: string
  text?: string
  reason?: string
  data_as_of?: string
  timing?: string
}

interface Decision {
  decision_id: number
  status: 'PASS' | 'BLOCK' | 'UNKNOWN' | 'NO_POLICY'
  summary?: string
  revision_id?: number | null
  rules_hash?: string
  playbook_name?: string
  data_as_of?: string
  as_of?: string
  expires_at?: string
  exception_reason?: string
  reasons?: string[]
  buy_checks?: CheckItem[]
  sell_checks?: CheckItem[]
  avoid_checks?: CheckItem[]
  account_checks?: CheckItem[]
  ai?: { ok: boolean; reason?: string; risk?: string; error?: string; content?: string }
  created_at?: string
  linked_trade_id?: number | null
  input?: { account_id?: number; side?: string; price?: number; quantity?: number }
  mandatory_exit?: boolean
}

const statusLabel = { PASS: '符合已启用策略', BLOCK: '违反策略或账户约束', UNKNOWN: '数据不足，尚未完成判断', NO_POLICY: '未配置对应策略' }
const decisionLabel = (decision: Decision) => decision.mandatory_exit && decision.status === 'PASS' ? '已触发持仓止损，应执行退出纪律' : statusLabel[decision.status]
const fieldLabel: Record<string, string> = { account: '操作账户', trade_input: '拟成交价量', lot_size: '买入交易单位', held_quantity: '账面可卖持仓', t_plus_one: 'A 股 T+1 可卖数量', cash: '可用资金', max_single_weight: '买后单仓权重', position_stop_loss: '持仓止损触发' }
const checkStatus = (item: CheckItem, avoid = false) => {
  if (item.status === 'UNKNOWN') return '未判断'
  if (item.ok != null) return avoid ? item.ok ? '命中禁止项' : '未命中' : item.ok ? '满足' : '未满足'
  return item.status === 'PASS' ? '满足' : item.status === 'BLOCK' ? '未满足' : '未判断'
}
const checkValue = (value: unknown, field?: string) => field === 'max_single_weight' && typeof value === 'number' ? `${(value * 100).toFixed(2)}%` : String(value)
const displayTime = (value?: string) => {
  if (!value) return '未知'
  const date = new Date(/[zZ]$|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

function CheckEvidence({ item, avoid = false }: { item: CheckItem; avoid?: boolean }) {
  const expected = item.expect ?? item.expected ?? item.required
  return <li className="rounded-lg border border-border p-2">
    <div className="flex flex-wrap justify-between gap-2"><span>{item.label || item.text || fieldLabel[item.field || ''] || item.field}</span><span>{checkStatus(item, avoid)}</span></div>
    {(item.actual != null || expected != null) && <p className="mt-1 text-xs text-muted-foreground">{item.actual != null ? `实际 ${checkValue(item.actual, item.field)}` : ''}{expected != null ? ` · ${item.required != null ? '本次需要' : '要求'} ${checkValue(expected, item.field)}` : ''}</p>}
    {item.reason && <p className="mt-1 text-xs text-muted-foreground">{item.reason}</p>}
    {item.data_as_of && <p className="mt-1 text-xs text-muted-foreground">{item.timing === 'live_price' ? '行情采集于 ' : '数据截至 '}{displayTime(item.data_as_of)}</p>}
  </li>
}

/** A proposed operation is checked separately from bookkeeping of an actual fill. */
export default function TradeDecisionDialog({ target, accounts, onClose, onRecord }: {
  target: TradeIntentTarget | null
  accounts: Array<{ id: number; name: string }>
  onClose: () => void
  onRecord: (intent: RecordIntent) => void
}) {
  const [account, setAccount] = useState('')
  const [price, setPrice] = useState('')
  const [quantity, setQuantity] = useState('100')
  const [useAi, setUseAi] = useState(true)
  const [decision, setDecision] = useState<Decision | null>(null)
  const [busy, setBusy] = useState(false)
  const [aiPending, setAiPending] = useState(false)
  const [invalidated, setInvalidated] = useState(false)
  const [error, setError] = useState('')
  const [reason, setReason] = useState('')
  const [exceptionBusy, setExceptionBusy] = useState(false)
  const [history, setHistory] = useState<Decision[] | null>(null)
  const [historyBusy, setHistoryBusy] = useState(false)
  const [historyError, setHistoryError] = useState('')
  const [now, setNow] = useState(Date.now())
  const sequence = useRef(0)
  const historySequence = useRef(0)

  useEffect(() => {
    sequence.current += 1
    setAccount(target?.accountId ? String(target.accountId) : '')
    setPrice(target?.price ? String(target.price) : '')
    setQuantity(String(target?.quantity || 100))
    setDecision(null)
    setReason('')
    setError('')
    setBusy(false)
    setAiPending(false)
    setInvalidated(false)
    setExceptionBusy(false)
    historySequence.current += 1
    setHistory(null)
    setHistoryBusy(false)
    setHistoryError('')
    return () => { sequence.current += 1; historySequence.current += 1 }
  }, [target])

  useEffect(() => {
    if (!decision?.expires_at) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [decision?.expires_at])

  const invalidate = () => {
    sequence.current += 1
    setDecision(null)
    setReason('')
    setError('')
    setBusy(false)
    setAiPending(false)
    setInvalidated(false)
    setExceptionBusy(false)
  }
  const validInput = !!target && !!account && Number(price) > 0 && Number(quantity) > 0 && Number.isFinite(Number(price)) && Number.isInteger(Number(quantity))
  const expired = invalidated || (!!decision?.expires_at && Date.parse(decision.expires_at) <= now)

  const run = async () => {
    if (!target || !validInput) { setError('请选择账户，并填写有效的价格和数量。'); return }
    const request = ++sequence.current
    setDecision(null)
    setError('')
    setReason('')
    setInvalidated(false)
    setBusy(true)
    const payload = { account_id: Number(account), symbol: target.symbol, market: target.market, side: target.side, price: Number(price), quantity: Number(quantity) }
    try {
      const ruleResult = await fetchAPI<Decision>('/strategy/decisions', { method: 'POST', body: JSON.stringify({ ...payload, use_ai: false }), timeoutMs: 30000 })
      if (request !== sequence.current) return
      setDecision(ruleResult)
      setNow(Date.now())
      if (useAi) {
        setAiPending(true)
        try {
          const fullResult = await fetchAPI<Decision>(`/strategy/decisions/${ruleResult.decision_id}/explain`, { method: 'POST', body: JSON.stringify({}), timeoutMs: 45000 })
          if (request !== sequence.current) return
          setDecision(fullResult)
          setNow(Date.now())
        } catch (e) {
          if (request !== sequence.current) return
          const message = e instanceof Error ? e.message : '请重试'
          if (message.startsWith('检查已失效，请重新检查')) { setInvalidated(true); setError(message) }
          else setError(`规则结果已保留，AI 解读未完成：${message}`)
        }
      }
    } catch (e) {
      if (request === sequence.current) setError(e instanceof Error ? e.message : '检查失败，请重试；尚未获得有效结论。')
    } finally {
      if (request === sequence.current) { setBusy(false); setAiPending(false) }
    }
  }

  const saveException = async () => {
    if (!decision || expired || busy || reason.trim().length < 5) return
    const request = sequence.current
    setExceptionBusy(true)
    setError('')
    try {
      const result = await fetchAPI<Decision>(`/strategy/decisions/${decision.decision_id}/exception`, { method: 'POST', body: JSON.stringify({ reason: reason.trim() }) })
      if (request === sequence.current) setDecision(result)
    } catch (e) {
      if (request === sequence.current) {
        const message = e instanceof Error ? e.message : '例外说明保存失败'
        if (message.startsWith('检查已失效，请重新检查')) setInvalidated(true)
        setError(message)
      }
    } finally {
      if (request === sequence.current) setExceptionBusy(false)
    }
  }

  const loadHistory = async () => {
    if (!target) return
    const request = ++historySequence.current
    setHistoryBusy(true)
    setHistoryError('')
    const query = new URLSearchParams({ symbol: target.symbol, market: target.market, limit: '8' })
    if (account) query.set('account_id', account)
    try {
      const result = await fetchAPI<{ items: Decision[] }>(`/strategy/decisions?${query}`)
      if (request === historySequence.current) setHistory(result.items)
    } catch (e) {
      if (request === historySequence.current) setHistoryError(e instanceof Error ? e.message : '历史检查读取失败')
    } finally {
      if (request === historySequence.current) setHistoryBusy(false)
    }
  }

  const recordActual = () => {
    if (!validInput) { setError('请先选择实际成交账户，填写成交价格和数量。'); return }
    const valid = decision && !busy && !expired ? decision : null
    onRecord({
      accountId: Number(account), price: Number(price), quantity: Number(quantity), decisionId: valid?.decision_id,
      note: valid ? `纪律检查 #${valid.decision_id}：${decisionLabel(valid)}${valid.exception_reason ? `；例外：${valid.exception_reason}` : ''}` : '已成交补录：未关联有效的事前检查',
    })
  }

  if (!target) return null
  const checks: Array<{ title: string; items: CheckItem[]; avoid?: boolean }> = [
    { title: target.side === 'buy' ? '买入必要条件' : '卖出条件', items: (target.side === 'buy' ? decision?.buy_checks : decision?.sell_checks) || [] },
    ...(target.side === 'buy' ? [{ title: '禁止买入条件', items: decision?.avoid_checks || [], avoid: true }] : []),
    { title: '账户与仓位约束', items: decision?.account_checks || [] },
  ]
  return <Dialog open onOpenChange={open => { if (!open) onClose() }}>
    <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
      <DialogHeader>
        <DialogTitle>{target.side === 'buy' ? '买入 / 加仓' : '减仓 / 卖出'} · {target.name}</DialogTitle>
        <DialogDescription>准备操作时先检查纪律。已在券商成交的交易可直接补录，真实账本不因策略结论而失真。</DialogDescription>
      </DialogHeader>
      {target.discoveryStrategyName && <p className="text-sm text-muted-foreground">发现策略：{target.discoveryStrategyName}。本次操作按实际启用版本检查；若不同，请先核对应用范围。</p>}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <label className="text-sm">操作账户<select aria-label="操作账户" className="mt-1 w-full rounded-lg border border-border bg-card p-2" value={account} onChange={e => { invalidate(); historySequence.current += 1; setHistory(null); setHistoryBusy(false); setHistoryError(''); setAccount(e.target.value) }}><option value="">请选择账户</option>{accounts.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}</select></label>
        <label className="text-sm">计划价格<Input className="mt-1" type="number" min="0" step="any" value={price} onChange={e => { invalidate(); setPrice(e.target.value) }} /></label>
        <label className="text-sm">计划数量<Input className="mt-1" type="number" min="1" step="1" aria-describedby="trade-intent-quantity-hint" value={quantity} onChange={e => { invalidate(); setQuantity(e.target.value) }} /></label>
      </div>
      <p id="trade-intent-quantity-hint" className="mt-2 text-xs text-muted-foreground">数量填写正整数；交易单位、可用资金和可卖持仓在检查时进一步核对。</p>
      <div className="mt-4 flex flex-wrap items-center gap-3">
        <Button onClick={() => void run()} disabled={!validInput || busy}>{busy ? aiPending ? '规则已检查，AI 解读中…' : '检查规则与账户约束…' : decision ? '重新检查这笔意图' : '检查这笔意图'}</Button>
        <label className="flex items-center gap-2 text-sm text-muted-foreground"><input type="checkbox" checked={useAi} onChange={e => { invalidate(); setUseAi(e.target.checked) }} />补充 AI 解读</label>
      </div>
      {error && <p className="mt-3 text-sm text-destructive" role="alert">{error}</p>}
      {decision && <section className="mt-5 space-y-4 border-t border-border pt-4" aria-label="纪律检查结果">
        <div role="status"><h3 className="font-semibold">{expired ? invalidated ? '检查已失效，请重新检查' : '检查已过期，请重新检查' : decisionLabel(decision)}</h3><p className="text-sm text-muted-foreground mt-1">{decision.summary}</p>{aiPending && <p className="mt-2 text-sm text-muted-foreground">以上是规则结果，AI 正在解读同一份快照；最终检查尚未完成。</p>}</div>
        <p className="text-xs text-muted-foreground">{decision.playbook_name || '未绑定策略'} · {decision.revision_id ? `版本 #${decision.revision_id}` : '无启用版本'} · 数据截至 {decision.data_as_of || decision.as_of || '未知'}{decision.expires_at ? ` · 有效至 ${displayTime(decision.expires_at)}` : ''}</p>
        {decision.status === 'NO_POLICY' && <p className="text-sm text-muted-foreground">请在个股详情的“策略回测”中核对并启用规则，再回来检查。未配置策略不会被当作通过。</p>}
        {checks.filter(group => group.items.length).map(group => <div key={group.title}><h4 className="text-sm font-medium mb-2">{group.title}</h4><ul className="space-y-2 text-sm">{group.items.map((item, i) => <CheckEvidence key={`${item.field || item.label}-${i}`} item={item} avoid={group.avoid} />)}</ul></div>)}
        {decision.ai && <div className="text-sm leading-6"><h4 className="font-medium">AI 解读</h4><p>{decision.ai.ok ? decision.ai.reason || decision.ai.content : decision.ai.error || 'AI 未返回，不改变规则事实。'}</p>{decision.ai.risk && <p className="text-muted-foreground">{decision.ai.risk}</p>}</div>}
        <p className="text-xs text-muted-foreground">此结论只适用于本账户、标的、方向、价量与当时数据；参数、规则或仓位变化后必须重新检查。符合策略不等于盈利承诺。</p>
        {decision.status !== 'PASS' && !expired && !busy && <div className="border-t border-border pt-4 space-y-2">
          <label className="block text-sm">例外说明（可选）<textarea className="mt-1 min-h-20 w-full rounded-lg border border-border bg-card p-2" value={reason} onChange={e => setReason(e.target.value)} placeholder="如果仍决定操作，请说明为何偏离这些条件。至少5个字。" /></label>
          <Button variant="secondary" disabled={exceptionBusy || reason.trim().length < 5} onClick={() => void saveException()}>{exceptionBusy ? '保存说明中…' : '记录例外说明'}</Button>
          <p className="text-xs text-muted-foreground">例外会保留“未通过/未完成”身份，不会改成检查通过。修改价量后说明失效。</p>
          {decision.exception_reason && <p className="text-sm">已记录例外：{decision.exception_reason}</p>}
        </div>}
      </section>}
      <section className="mt-4 border-t border-border pt-3" aria-label="历史纪律检查">
        <Button variant="ghost" size="sm" disabled={historyBusy} onClick={() => void loadHistory()}>{historyBusy ? '读取历史检查中…' : history ? '刷新最近纪律检查' : '查看最近纪律检查'}</Button>
        {historyError && <p className="mt-2 text-sm text-destructive" role="alert">{historyError}</p>}
        {history && <div className="mt-2 space-y-2">
          <p className="text-xs text-muted-foreground">只读历史快照，不能作为本次操作的有效检查。{account ? '当前账户' : '全部账户'} · 最近 8 条</p>
          {!history.length && <p className="text-sm text-muted-foreground">暂无检查记录。</p>}
          {history.map(item => <details key={item.decision_id} className="rounded-lg border border-border p-3 text-sm">
            <summary className="cursor-pointer">#{item.decision_id} · {accounts.find(account => account.id === item.input?.account_id)?.name || `账户 ${item.input?.account_id ?? '未指定'}`} · {item.input?.side === 'sell' ? '卖出' : '买入'} · {decisionLabel(item)}<span className="ml-2 text-xs text-muted-foreground">{displayTime(item.created_at)}</span></summary>
            <div className="mt-2 space-y-2"><p>{item.summary}</p><p className="text-xs text-muted-foreground">计划 {item.input?.price ?? '未知'} × {item.input?.quantity ?? '未知'} · 规则版本 {item.revision_id ? `#${item.revision_id}` : '无'} · 数据截至 {item.data_as_of || '未知'} · {item.linked_trade_id ? `已关联成交 #${item.linked_trade_id}` : '未关联成交'}</p>
              {item.exception_reason && <p>例外说明：{item.exception_reason}</p>}
              <ul className="space-y-2">{[...(item.input?.side === 'sell' ? item.sell_checks || [] : item.buy_checks || []), ...(item.account_checks || [])].map((check, i) => <CheckEvidence key={i} item={check} />)}</ul>
              {!!item.avoid_checks?.length && <div><p className="mb-2">禁止买入条件</p><ul className="space-y-2">{item.avoid_checks.map((check, i) => <CheckEvidence key={i} item={check} avoid />)}</ul></div>}
              {item.ai && <p>AI 解读：{item.ai.ok ? item.ai.reason || item.ai.content : item.ai.error || '未完成'}</p>}
            </div>
          </details>)}
        </div>}
      </section>
      <div className="mt-5 flex flex-wrap justify-between items-center gap-3 border-t border-border pt-4">
        <Button variant="ghost" onClick={onClose}>{!decision ? '取消本次意图' : expired ? '保留记录，返回工作台' : decision.status === 'PASS' ? '完成检查，返回工作台' : decision.exception_reason ? '保留例外，返回工作台' : '暂不操作，返回工作台'}</Button>
        <Button variant="secondary" disabled={!validInput || exceptionBusy} onClick={recordActual}>已成交，去记账</Button>
      </div>
      <p className="text-xs text-muted-foreground mt-2">检查记录自动保存；下单仍需在券商完成。补录入口不会下单，也不会把缺失检查伪装成通过。</p>
    </DialogContent>
  </Dialog>
}
