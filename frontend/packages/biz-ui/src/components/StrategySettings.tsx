import { useCallback, useEffect, useState } from 'react'
import {
  strategyApi,
  type StrategyCondition,
  type StrategyField,
  type StrategyPlaybook,
  type StrategyPreset,
} from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@panwatch/base-ui/components/ui/dialog'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { ConditionEditor, rulesText } from '@panwatch/biz-ui/components/strategy-rules'

/**
 * 全局交易策略设置（首页入口）。
 *
 * 与个股详情里的「策略回测」共用同一张策略卡：这里配置 `scope=global`，
 * 对没有个股覆盖的股票全部生效；个股覆盖仍然在个股详情里配。
 */
export default function StrategySettings({
  open,
  onOpenChange,
  onSaved,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onSaved?: () => void
}) {
  const { toast } = useToast()
  const [fields, setFields] = useState<StrategyField[]>([])
  const [presets, setPresets] = useState<StrategyPreset[]>([])
  const [overrides, setOverrides] = useState<StrategyPlaybook[]>([])
  const [globalRow, setGlobalRow] = useState<StrategyPlaybook | null>(null)
  const [name, setName] = useState('')
  const [buy, setBuy] = useState<StrategyCondition[]>([])
  const [sell, setSell] = useState<StrategyCondition[]>([])
  const [avoid, setAvoid] = useState<StrategyCondition[]>([])
  const [enabled, setEnabled] = useState(true)
  const [note, setNote] = useState('')
  const [presetId, setPresetId] = useState('')
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setErr('')
    try {
      const data = await strategyApi.playbooks()
      setFields(data.fields)
      setPresets(data.presets || [])
      const g = (data.items || []).find((p) => p.scope === 'global') || null
      setGlobalRow(g)
      setOverrides((data.items || []).filter((p) => p.scope === 'stock'))
      if (g) {
        setName(g.name || '')
        setBuy(g.buy || [])
        setSell(g.sell || [])
        setAvoid(g.avoid || [])
        setEnabled(g.enabled)
        setNote(g.note || '')
        const matched = (data.presets || []).find((p) => p.name === g.name)
        setPresetId(matched?.id || '')
      } else {
        setName('')
        setBuy(data.defaults.buy || [])
        setSell(data.defaults.sell || [])
        setAvoid(data.defaults.avoid || [])
        setEnabled(true)
        setNote('')
        setPresetId('')
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : '策略加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (open) void load()
  }, [open, load])

  const applyPreset = (id: string) => {
    const preset = presets.find((p) => p.id === id)
    setPresetId(id)
    if (!preset) {
      setNote('')
      return
    }
    setBuy(preset.buy)
    setSell(preset.sell)
    setAvoid(preset.avoid)
    setName(preset.name)
    setNote(preset.note)
  }

  const save = async () => {
    setBusy(true)
    setErr('')
    try {
      await strategyApi.save({
        scope: 'global',
        symbol: '',
        name: name.trim() || '全局策略',
        buy,
        sell,
        avoid,
        note,
        enabled,
      })
      toast('全局策略已保存，之后的买卖前体检与新检查都会用它', 'success')
      onSaved?.()
      onOpenChange(false)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }

  const removeOverride = async (row: StrategyPlaybook) => {
    try {
      await strategyApi.remove(row.id)
      toast(`已删除 ${row.symbol} 的个股策略`, 'success')
      await load()
    } catch (e) {
      toast(e instanceof Error ? e.message : '删除失败', 'error')
    }
  }

  const preset = presets.find((p) => p.id === presetId)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>交易策略设置</DialogTitle>
          <DialogDescription>
            全局策略对所有没有单独覆盖的股票生效。买卖前体检、首页策略信号、持仓徽标都用这里的规则；
            个股可以在个股详情 →「策略回测」里单独覆盖。
          </DialogDescription>
        </DialogHeader>

        {loading ? (
          <div className="py-6 text-center text-[12px] text-muted-foreground">加载中…</div>
        ) : (
          <div className="space-y-3">
            {err && <div className="text-[11px] text-destructive">{err}</div>}

            <div className="rounded-lg border border-border/60 bg-muted/20 p-2.5">
              <div className="flex flex-wrap items-center gap-2 text-[11px]">
                <span className="text-muted-foreground">从模板开始</span>
                <select
                  value={presetId}
                  onChange={(e) => applyPreset(e.target.value)}
                  className="rounded border bg-background px-2 py-1"
                >
                  <option value="">不使用模板</option>
                  {presets.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
                {preset && (
                  <span className="min-w-0 flex-1 truncate text-muted-foreground" title={preset.note}>
                    {preset.description}
                  </span>
                )}
              </div>
              {preset?.note && (
                <div className="mt-1 text-[10px] leading-relaxed text-muted-foreground">{preset.note}</div>
              )}
            </div>

            <div className="flex flex-wrap items-center gap-2 text-[11px]">
              <span className="text-muted-foreground">策略名称</span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="例如：小跌抄底 + 不追高开低走"
                className="min-w-0 flex-1 rounded border bg-background px-2 py-1"
              />
              <label className="flex shrink-0 items-center gap-1.5 text-muted-foreground">
                <input
                  type="checkbox"
                  checked={enabled}
                  onChange={(e) => setEnabled(e.target.checked)}
                />
                启用
              </label>
            </div>

            <ConditionEditor
              title="买入条件（全部满足）"
              hint="少而精：2~3 条就够"
              value={buy}
              onChange={setBuy}
              fields={fields}
            />
            <ConditionEditor
              title="卖出条件（任一满足）"
              hint="持仓风控，任一命中即卖"
              value={sell}
              onChange={setSell}
              fields={fields}
            />
            <ConditionEditor
              title="禁买条件（任一命中即不买）"
              hint="如「高开低走未修复天数 ≥ 1」"
              value={avoid}
              onChange={setAvoid}
              fields={fields}
            />

            <div className="rounded-lg border border-border/60 bg-muted/20 p-2.5 text-[10px] leading-relaxed text-muted-foreground">
              <div className="mb-0.5 text-[10px] font-medium text-foreground">当前规则</div>
              <div>
                <span className="text-stock-up">买入</span>：{rulesText(buy, fields, '未设置', ' 且 ')}
              </div>
              <div>
                <span className="text-stock-down">卖出</span>：{rulesText(sell, fields, '未设置', ' 或 ')}
              </div>
              <div>
                <span className="text-amber-500">禁买</span>：{rulesText(avoid, fields, '未设置', ' 或 ')}
              </div>
            </div>

            {overrides.length > 0 && (
              <div className="rounded-lg border border-border/60 p-2.5">
                <div className="mb-1 text-[10px] font-medium text-foreground">
                  个股覆盖（优先于全局，共 {overrides.length} 只）
                </div>
                <div className="space-y-1">
                  {overrides.map((row) => (
                    <div key={row.id} className="flex items-center gap-2 text-[11px]">
                      <span className="font-mono text-muted-foreground">{row.symbol}</span>
                      <span className="min-w-0 flex-1 truncate">{row.name || '个股策略'}</span>
                      <button
                        type="button"
                        className="shrink-0 text-[10px] text-muted-foreground hover:text-destructive"
                        onClick={() => void removeOverride(row)}
                      >
                        删除
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {globalRow && !enabled && (
              <div className="text-[10px] text-amber-600">当前全局策略处于停用状态，停用后不会参与检查。</div>
            )}
          </div>
        )}

        <div className="mt-3 flex justify-end gap-2">
          <Button variant="secondary" size="sm" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button size="sm" onClick={() => void save()} disabled={busy || loading}>
            {busy ? '保存中…' : '保存全局策略'}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
