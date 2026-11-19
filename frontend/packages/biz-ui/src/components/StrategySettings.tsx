import { useCallback, useEffect, useRef, useState } from 'react'
import { strategyApi, type StrategyDraft, type StrategyDraftInput, type StrategyField, type StrategyPlaybook, type StrategyPreset } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import StrategyRuleEditor, { validateRuleDraft } from './StrategyRuleEditor'

const blank = (): StrategyDraftInput => ({ scope: 'global', symbol: '', market: 'CN', name: '', buy: [], sell: [], avoid: [], note: '', timing: 'completed_daily', questions: [] })
const localDraftKey = 'panwatch_strategy_default_draft:CN'
type LocalDraft = { draft: StrategyDraftInput; enabled: boolean }

/** The same draft editor as the stock workspace; selecting a template never activates it. */
export default function StrategySettings({ open, onOpenChange, onSaved }: { open: boolean; onOpenChange: (open: boolean) => void; onSaved?: () => void }) {
  const [draft, setDraft] = useState<StrategyDraftInput>(blank)
  const [draftId, setDraftId] = useState<number>()
  const [savedDrafts, setSavedDrafts] = useState<StrategyDraft[]>([])
  const [fields, setFields] = useState<StrategyField[]>([])
  const [presets, setPresets] = useState<StrategyPreset[]>([])
  const [global, setGlobal] = useState<StrategyPlaybook | null>(null)
  const [overrides, setOverrides] = useState<StrategyPlaybook[]>([])
  const [enabled, setEnabled] = useState(true)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [confirm, setConfirm] = useState(false)
  const [acknowledged, setAcknowledged] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [closeConfirm, setCloseConfirm] = useState(false)
  const [recovery, setRecovery] = useState<LocalDraft | null>(null)
  const session = useRef(0)

  const clearLocalDraft = () => {
    setDirty(false); setRecovery(null)
    try { localStorage.removeItem(localDraftKey) } catch { /* A saved server draft is still durable. */ }
  }
  const finishClose = (keep: boolean) => {
    try {
      if (keep) localStorage.setItem(localDraftKey, JSON.stringify({ draft, enabled }))
      else localStorage.removeItem(localDraftKey)
    } catch {
      if (keep) { setError('本机草稿保留失败。请继续编辑并保存到服务器，或明确放弃本次更改。'); return }
    }
    setDirty(false); setCloseConfirm(false); session.current += 1; onOpenChange(false)
  }
  const requestOpenChange = (next: boolean) => {
    if (!next && dirty) { setCloseConfirm(true); return }
    onOpenChange(next)
  }

  const load = useCallback(async () => {
    const current = session.current
    setLoading(true); setDirty(false); setRecovery(null); setError(''); setMessage(''); setDraftId(undefined)
    try {
      const list = await strategyApi.playbooks()
      if (session.current !== current) return
      setFields(list.fields); setPresets(list.presets || []); setOverrides(list.items.filter(row => row.scope === 'stock'))
      const active = list.items.find(row => row.scope === 'global') || null
      setGlobal(active); setEnabled(active?.enabled ?? true)
      setDraft({ ...blank(), ...(active ? { name: active.name, buy: active.buy, sell: active.sell, avoid: active.avoid || [], note: active.note, timing: active.timing || 'completed_daily' } : {}) })
      try {
        const local = JSON.parse(localStorage.getItem(localDraftKey) || 'null')
        if (local?.draft?.scope === 'global' && Array.isArray(local.draft.buy) && Array.isArray(local.draft.sell) && Array.isArray(local.draft.avoid)) setRecovery(local)
      } catch { /* Ignore malformed browser recovery without changing active settings. */ }
      try { const saved = await strategyApi.drafts('', 'CN'); if (session.current === current) setSavedDrafts(saved.items.filter(row => row.scope === 'global')) } catch { if (session.current === current) setMessage('服务器草稿暂不可用，仍可编辑并重试保存。') }
    } catch (e) { if (session.current === current) setError(e instanceof Error ? e.message : '策略加载失败') } finally { if (session.current === current) setLoading(false) }
  }, [])

  useEffect(() => {
    session.current += 1
    if (open) { setBusy(false); setCloseConfirm(false); setConfirm(false); setAcknowledged(false); void load() }
    return () => { session.current += 1 }
  }, [open, load])
  useEffect(() => {
    if (!open || !dirty || loading) return
    try { localStorage.setItem(localDraftKey, JSON.stringify({ draft, enabled })) } catch { /* Closing offers server saving or deliberate discard if local storage fails. */ }
  }, [open, dirty, loading, draft, enabled])
  const remember = (saved: StrategyDraft) => { setDraftId(saved.id); setSavedDrafts(rows => [saved, ...rows.filter(row => row.id !== saved.id)]) }
  const saveDraft = async () => {
    const current = session.current
    setBusy(true); setError('')
    try { const saved = await strategyApi.saveDraft({ ...draft, enabled, draft_id: draftId, name: draft.name.trim() || '默认策略草稿' }); if (session.current !== current) return; remember(saved); clearLocalDraft(); setMessage('默认规则草稿已保存，当前生效规则没有变化。') } catch (e) { if (session.current === current) setError(e instanceof Error ? e.message : '草稿保存失败') } finally { if (session.current === current) setBusy(false) }
  }
  const aiDraft = async (text: string) => {
    const current = session.current
    setBusy(true); setError('')
    try { const saved = await strategyApi.aiDraft({ text, scope: 'global', symbol: '', market: 'CN' }); if (session.current !== current) return; setDraft(saved); remember(saved); clearLocalDraft(); setMessage('AI 只生成了草稿，请确认条件含义后再应用。') } catch (e) { if (session.current === current) setError(e instanceof Error ? e.message : 'AI 草稿生成失败') } finally { if (session.current === current) setBusy(false) }
  }
  const apply = async () => {
    const errors = validateRuleDraft(draft, fields)
    if (errors.length || !acknowledged) { setError(errors[0] || '请确认全局影响范围'); return }
    const current = session.current
    setBusy(true); setError('')
    try {
      const saved = await strategyApi.saveDraft({ ...draft, enabled, draft_id: draftId, name: draft.name.trim() || '默认策略' })
      if (session.current !== current) return
      if (enabled) await strategyApi.activateDraft(saved.id)
      else await strategyApi.save({ scope: 'global', symbol: '', name: saved.name, buy: saved.buy, sell: saved.sell, avoid: saved.avoid, note: saved.note, enabled: false })
      if (session.current !== current) return
      clearLocalDraft(); onSaved?.(); setConfirm(false); onOpenChange(false)
    } catch (e) { if (session.current === current) setError(e instanceof Error ? e.message : '应用失败') } finally { if (session.current === current) setBusy(false) }
  }
  const remove = async (row: StrategyPlaybook) => {
    if (!window.confirm(row.scope === 'global' ? '删除默认生效规则？没有个股规则的股票将不再有默认策略。' : `移除 ${row.symbol} 的覆盖规则并改用默认规则？`)) return
    const current = session.current
    setBusy(true)
    try { await strategyApi.remove(row.id); if (session.current !== current) return; onSaved?.(); await load() } catch (e) { if (session.current === current) setError(e instanceof Error ? e.message : '删除失败') } finally { if (session.current === current) setBusy(false) }
  }

  return <Dialog open={open} onOpenChange={requestOpenChange}><DialogContent className="max-h-[90vh] max-w-3xl overflow-y-auto"><DialogHeader><DialogTitle>默认交易规则</DialogTitle><DialogDescription>对没有单独规则的股票生效。先编辑草稿，再明确应用；模板和 AI 都不会直接改变正在使用的规则。</DialogDescription></DialogHeader>
    {error && <p className="mb-4 rounded-lg border border-destructive/30 p-3 text-sm text-destructive" role="alert">{error}</p>}
    {message && <p className="mb-4 rounded-lg bg-muted p-3 text-sm" role="status">{message}</p>}
    {loading ? <p className="py-6 text-sm text-muted-foreground">正在读取默认规则…</p> : <div className="space-y-5">
      {recovery && <div className="rounded-xl border border-amber-500/40 p-4 text-sm"><p>发现本机保留的默认规则草稿，尚未应用。</p><div className="mt-3 flex flex-wrap gap-2"><Button variant="secondary" size="sm" onClick={() => { setDraft(recovery.draft); setEnabled(recovery.enabled); setDraftId(undefined); setDirty(true); setRecovery(null); setMessage('已恢复未保存草稿，当前生效规则没有变化。') }}>恢复本机草稿</Button><Button variant="ghost" size="sm" onClick={clearLocalDraft}>放弃本机副本</Button></div></div>}
      <section className="rounded-xl border border-border p-4"><p className="text-xs text-muted-foreground">当前默认规则</p><p className="mt-1 font-medium">{global ? `${global.name || '默认策略'}${global.version ? ` · v${global.version}` : ''} · ${global.enabled ? '已启用' : '已停用'}` : '尚未设置'}</p><p className="mt-2 text-xs leading-6 text-muted-foreground">先在任一个股的“策略与回测”验证，再决定是否推广到其他股票。本弹窗不会把未回测草稿标为已验证。</p></section>
      <label className="block text-sm">恢复服务器草稿<select aria-label="恢复默认策略草稿" className="ml-3 max-w-64 rounded-lg border border-border bg-background p-2" value="" disabled={busy} onChange={e => { const saved = savedDrafts.find(row => row.id === Number(e.target.value)); if (saved) { setDraft(saved); setDraftId(saved.id); setMessage('已恢复默认草稿，尚未应用。') } }}><option value="">选择草稿</option>{savedDrafts.map(row => <option value={row.id} key={row.id}>{row.name || `草稿 ${row.id}`}</option>)}</select></label>
      <StrategyRuleEditor value={draft} onChange={value => { setDraft(value); setDirty(true); setMessage('草稿已修改，尚未应用。') }} fields={fields} presets={presets} busy={busy} onAiDraft={aiDraft} />
      <label className="flex items-center gap-3 text-sm"><input type="checkbox" checked={enabled} disabled={busy} onChange={e => { setEnabled(e.target.checked); setDirty(true) }} />应用后启用默认规则<span className="text-xs text-muted-foreground">取消勾选将明确停用默认规则。</span></label>
      {!!overrides.length && <details className="rounded-xl border border-border p-4"><summary className="cursor-pointer text-sm font-medium">个股单独规则 · {overrides.length} 只</summary><p className="mt-2 text-xs text-muted-foreground">已启用的个股规则完全覆盖默认规则。移除后改用默认规则。</p><div className="mt-3 divide-y divide-border">{overrides.map(row => <div key={row.id} className="flex items-center justify-between gap-3 py-2 text-sm"><span>{row.symbol} · {row.name}{!row.enabled ? '（已停用）' : ''}</span><Button size="sm" variant="ghost" disabled={busy} onClick={() => void remove(row)}>移除覆盖</Button></div>)}</div></details>}
      <div className="flex flex-wrap justify-between gap-3 border-t border-border pt-4">{global ? <Button variant="destructive" disabled={busy} onClick={() => void remove(global)}>删除默认规则</Button> : <span />}<div className="flex gap-2"><Button variant="secondary" disabled={busy} onClick={() => void saveDraft()}>仅保存草稿</Button><Button disabled={busy || !!validateRuleDraft(draft, fields).length} onClick={() => { setAcknowledged(false); setConfirm(true) }}>确认应用范围</Button></div></div>
    </div>}
    <Dialog open={confirm} onOpenChange={setConfirm}><DialogContent className="max-w-md"><DialogHeader><DialogTitle>{enabled ? '应用为默认规则' : '停用默认规则'}</DialogTitle><DialogDescription>此操作影响所有未单独设置规则的股票，不只影响你最近查看的标的。</DialogDescription></DialogHeader><label className="flex gap-3 text-sm leading-6"><input type="checkbox" className="mt-1" checked={acknowledged} onChange={e => setAcknowledged(e.target.checked)} /><span>我已核对规则和全局影响；这里尚未对所有适用股票完成回测，仍要{enabled ? '应用' : '停用'}。</span></label>{error && <p className="text-sm text-destructive" role="alert">{error}</p>}<div className="mt-4 flex justify-end gap-2"><Button variant="ghost" onClick={() => setConfirm(false)}>取消</Button><Button disabled={busy || !acknowledged} onClick={() => void apply()}>{busy ? '应用中…' : '确认更改默认规则'}</Button></div></DialogContent></Dialog>
    <Dialog open={closeConfirm} onOpenChange={setCloseConfirm}>
      <DialogContent className="max-w-lg">
        <DialogHeader><DialogTitle>保留未保存的默认规则草稿？</DialogTitle><DialogDescription>当前编辑尚未保存到服务器，也没有应用。可以保留在这台设备下次恢复，或明确放弃。</DialogDescription></DialogHeader>
        {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
        <div className="flex flex-wrap justify-end gap-2">
          <Button variant="ghost" onClick={() => setCloseConfirm(false)}>继续编辑</Button>
          <Button variant="destructive" onClick={() => finishClose(false)}>放弃更改并关闭</Button>
          <Button onClick={() => finishClose(true)}>保留本机草稿并关闭</Button>
        </div>
      </DialogContent>
    </Dialog>
  </DialogContent></Dialog>
}
