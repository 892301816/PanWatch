import { useEffect, useRef, useState } from 'react'
import { fetchAPI, type StrategyLibraryItem } from '@panwatch/api'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@panwatch/base-ui/components/ui/dialog'
import { Button } from '@panwatch/base-ui/components/ui/button'

export type StudioDraft = { id?: number; name: string; source: string }
type WriterResult = {
  name: string
  source: string
  explanation: string
  references: string[]
}
export function ScriptWriter({
  draft,
  close,
  accept,
}: {
  draft: StudioDraft
  close: () => void
  accept: (next: StudioDraft) => void
}) {
  const [instruction, setInstruction] = useState(''),
    [result, setResult] = useState<WriterResult>(),
    [busy, setBusy] = useState(false),
    [error, setError] = useState('')
  const request = useRef<AbortController>()
  useEffect(() => () => request.current?.abort(), [])
  async function generate() {
    const controller = new AbortController()
    request.current = controller
    setBusy(true)
    setError('')
    setResult(undefined)
    try {
      const data = await fetchAPI<WriterResult>('/strategy/chart-script/ai', {
        method: 'POST',
        signal: controller.signal,
        timeoutMs: 70000,
        body: JSON.stringify({ instruction, current: draft.source }),
      })
      if (!controller.signal.aborted) setResult(data)
    } catch (e) {
      if (!controller.signal.aborted)
        setError(e instanceof Error ? e.message : '生成失败')
    } finally {
      if (!controller.signal.aborted) setBusy(false)
    }
  }
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) close()
      }}
    >
      <DialogContent className="strategy-dialog studio-modal">
        <DialogHeader>
          <DialogTitle>AI 编写脚本</DialogTitle>
          <DialogDescription>
            根据你的描述和当前草稿生成候选脚本。函数参考来自观潮知识库，结果按
            PanWatch 语法校验。
          </DialogDescription>
        </DialogHeader>
        <label className="studio-field">
          策略想法
          <textarea
            autoFocus
            maxLength={3000}
            rows={3}
            value={instruction}
            disabled={busy}
            placeholder="例如：将快慢线改为 8 和 21 周期 EMA，上穿买入，下穿卖出。"
            onChange={(e) => {
              setInstruction(e.target.value)
              setResult(undefined)
              setError('')
            }}
          />
        </label>
        <Button
          disabled={busy || !instruction.trim()}
          onClick={() => void generate()}
        >
          {busy ? '正在编写…' : result ? '重新生成' : '生成脚本'}
        </Button>
        {error && <p role="alert">{error}</p>}
        {result && (
          <section aria-live="polite">
            <h3>{result.source ? result.name : '需要调整条件'}</h3>
            <p>{result.explanation}</p>
            {result.source && <pre>{result.source}</pre>}
            <details>
              <summary>
                本次提供给 AI 的函数参考 · {result.references.length}
              </summary>
              <pre>{result.references.join('\n')}</pre>
            </details>
            {result.source && (
              <Button
                onClick={() =>
                  accept({ ...draft, name: result.name, source: result.source })
                }
              >
                采用到编辑器
              </Button>
            )}
          </section>
        )}
      </DialogContent>
    </Dialog>
  )
}
type GalleryProps = {
  templates: StudioDraft[]
  items: StrategyLibraryItem[]
  shelf: StudioDraft[]
  initial: 'templates' | 'mine' | 'recent'
  market: string
  close: () => void
  choose: (d: StudioDraft) => void
  openItem: (i: StrategyLibraryItem) => void
  copy: (d: StudioDraft) => Promise<void>
  /** 打开完整管理页（编辑规则、绑定范围、删除） */
  editRules?: (i: StrategyLibraryItem) => void
}
export function ScriptGallery({
  templates,
  items,
  shelf,
  initial,
  market,
  close,
  choose,
  openItem,
  copy,
  editRules,
}: GalleryProps) {
  const [tab, setTab] = useState<'templates' | 'mine' | 'recent'>(initial),
    [query, setQuery] = useState(''),
    [saving, setSaving] = useState(''),
    [status, setStatus] = useState('')
  const visible = (name: string) =>
    name.toLowerCase().includes(query.trim().toLowerCase())
  const pool = tab === 'mine' ? items : tab === 'recent' ? shelf : templates
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) close()
      }}
    >
      <DialogContent className="strategy-dialog studio-modal">
        <DialogHeader>
          <DialogTitle>我的策略</DialogTitle>
          <DialogDescription>
            我的策略都在这里；需要从零开始时可以用内置模板。
          </DialogDescription>
        </DialogHeader>
        <div className="studio-gallery-tabs">
          {(
            [
              ['mine', '我的策略'],
              ['templates', '内置模板'],
              ['recent', '最近草稿'],
            ] as const
          ).map(([key, label]) => (
            <Button
              variant={tab === key ? 'secondary' : 'ghost'}
              key={key}
              aria-pressed={tab === key}
              onClick={() => {
                setTab(key)
                setQuery('')
                setStatus('')
              }}
            >
              {label}
            </Button>
          ))}
        </div>
        <input
          className="studio-search"
          aria-label="搜索脚本"
          placeholder="搜索脚本名称"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <div className="studio-gallery-rows">
          {pool
            .filter((d) => visible(d.name))
            .map((d, i) => {
              const item = 'rules_hash' in d ? d : undefined
              const code =
                item?.config?.chart_script || ('source' in d ? d.source : '')
              const saved =
                tab === 'templates' &&
                items.find(
                  (s) =>
                    s.name === d.name &&
                    s.market === market &&
                    s.config?.chart_script === code,
                )
              return (
                <article key={item?.id ?? i}>
                  <div>
                    <h3>{d.name}</h3>
                    <small>
                      {item
                        ? item.market +
                          ' · ' +
                          (item.scope === 'stock' ? item.symbol : '通用策略')
                        : tab === 'recent'
                          ? '本次会话草稿'
                          : '均线交叉策略'}
                    </small>
                    {code && (
                      <details>
                        <summary>查看脚本</summary>
                        <pre>{code}</pre>
                      </details>
                    )}
                  </div>
                  <div className="studio-gallery-actions">
                    <Button
                      variant="secondary"
                      onClick={() =>
                        item
                          ? openItem(item)
                          : saved
                            ? openItem(saved)
                            : choose(d as StudioDraft)
                      }
                    >
                      载入编辑器
                    </Button>
                    {tab === 'mine' && item && editRules && (
                      <Button
                        variant="ghost"
                        title="打开完整管理页：编辑规则、绑定范围、删除"
                        onClick={() => editRules(item)}
                      >
                        管理
                      </Button>
                    )}
                    {tab === 'templates' && (
                      <Button
                        variant="ghost"
                        disabled={!!saving || !!saved}
                        onClick={async () => {
                          setSaving(d.name)
                          try {
                            await copy(d as StudioDraft)
                            setTab('mine')
                            setQuery('')
                            setStatus('已加入我的策略')
                          } catch (e) {
                            setStatus(
                              e instanceof Error ? e.message : '保存失败',
                            )
                          } finally {
                            setSaving('')
                          }
                        }}
                      >
                        {saved
                          ? '已加入'
                          : saving === d.name
                            ? '保存中…'
                            : '加入我的策略'}
                      </Button>
                    )}
                  </div>
                </article>
              )
            })}
        </div>
        {!pool.some((d) => visible(d.name)) && (
          <p>
            {query
              ? '没有匹配的脚本'
              : tab === 'mine'
                ? '还没有已保存的策略'
                : '暂无最近草稿'}
          </p>
        )}
        {status && <p role="status">{status}</p>}
      </DialogContent>
    </Dialog>
  )
}
