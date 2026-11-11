import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  strategyApi,
  type BacktestResult,
  type StrategyCheck,
  type StrategyCondition,
  type StrategyField,
  type StrategyPlaybook,
  type StrategyPreset,
} from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'

const OPS: Array<[string, string]> = [
  ['gte', '≥'],
  ['lte', '≤'],
  ['gt', '>'],
  ['lt', '<'],
  ['eq', '='],
  ['between', '介于'],
]

const OP_TEXT: Record<string, string> = {
  gte: '≥',
  lte: '≤',
  gt: '>',
  lt: '<',
  eq: '=',
  ne: '≠',
  between: '',
  in: '属于',
  not_in: '不属于',
}

function conditionText(cond: StrategyCondition, fields: StrategyField[]): string {
  const field = fields.find((f) => f.key === cond.field)
  const label = field?.label || cond.field
  const unit = field?.unit || ''
  const value = cond.value
  if (cond.field === 'gap_drop_flag' && cond.op === 'eq') {
    return `${label} = ${Number(value) === 1 ? '是' : '否'}`
  }
  if (cond.op === 'between' && Array.isArray(value) && value.length === 2) {
    return `${label} 介于 ${value[0]} ~ ${value[1]}${unit}`
  }
  return `${label} ${OP_TEXT[cond.op] || cond.op} ${Array.isArray(value) ? value.join(',') : value}${unit}`
}

function rulesText(conds: StrategyCondition[] | undefined, fields: StrategyField[], empty = '未设置'): string {
  if (!conds || conds.length === 0) return empty
  return conds.map((c) => conditionText(c, fields)).join(' 且 ')
}

/** 单票策略检查 + 回测(同花顺「策略回测」的简化版)。 */
export default function StrategyBacktest({ symbol, market = 'CN' }: { symbol: string; market?: string }) {
  const [fields, setFields] = useState<StrategyField[]>([])
  const [presets, setPresets] = useState<StrategyPreset[]>([])
  const [playbooks, setPlaybooks] = useState<StrategyPlaybook[]>([])
  const [check, setCheck] = useState<StrategyCheck | null>(null)
  const [result, setResult] = useState<BacktestResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [editing, setEditing] = useState(false)
  const [err, setErr] = useState('')
  const [appliedPreset, setAppliedPreset] = useState<StrategyPreset | null>(null)

  const [buy, setBuy] = useState<StrategyCondition[]>([])
  const [sell, setSell] = useState<StrategyCondition[]>([])
  const [avoid, setAvoid] = useState<StrategyCondition[]>([])
  const [name, setName] = useState('')
  const [scope, setScope] = useState<'global' | 'stock'>('stock')

  const own = playbooks.find((p) => p.scope === 'stock' && p.symbol === symbol)
  const global = playbooks.find((p) => p.scope === 'global')
  const effective = own || global

  const load = useCallback(async () => {
    try {
      const list = await strategyApi.playbooks()
      setFields(list.fields)
      setPresets(list.presets || [])
      setPlaybooks(list.items)
      const mine = list.items.find((p) => p.scope === 'stock' && p.symbol === symbol)
      const g = list.items.find((p) => p.scope === 'global')
      const base = mine || g
      setBuy((base?.buy as StrategyCondition[]) || list.defaults.buy)
      setSell((base?.sell as StrategyCondition[]) || list.defaults.sell)
      setAvoid((base?.avoid as StrategyCondition[]) || list.defaults.avoid || [])
      setName(base?.name || '')
      setScope(mine ? 'stock' : 'global')
      if (base?.note && base.name) {
        setAppliedPreset(
          (prev) => prev || (list.presets || []).find((p) => p.name === base.name) || null,
        )
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : '策略加载失败')
    }
  }, [symbol])

  const runCheck = useCallback(async () => {
    try {
      setCheck(await strategyApi.check(symbol, market))
    } catch {
      setCheck(null)
    }
  }, [symbol, market])

  const runBacktest = useCallback(
    async (
      nextBuy: StrategyCondition[] = buy,
      nextSell: StrategyCondition[] = sell,
      nextAvoid: StrategyCondition[] = avoid,
    ) => {
      setLoading(true)
      setErr('')
      try {
        const r = await strategyApi.backtest({
          symbol,
          market,
          buy: nextBuy,
          sell: nextSell,
          avoid: nextAvoid,
          days: 500,
        })
        setResult(r)
        if (r.error) setErr(r.error)
      } catch (e) {
        setErr(e instanceof Error ? e.message : '回测失败')
      } finally {
        setLoading(false)
      }
    },
    [symbol, market, buy, sell, avoid],
  )

  useEffect(() => {
    void load()
    void runCheck()
    setResult(null)
    setAppliedPreset(null)
  }, [load, runCheck])

  const save = useCallback(
    async (
      nextBuy: StrategyCondition[] = buy,
      nextSell: StrategyCondition[] = sell,
      nextAvoid: StrategyCondition[] = avoid,
      nextName: string = name,
      nextScope: 'global' | 'stock' = scope,
      nextNote: string = appliedPreset?.note || '',
    ) => {
      setErr('')
      setSaving(true)
      try {
        await strategyApi.save({
          scope: nextScope,
          symbol: nextScope === 'stock' ? symbol : '',
          name: nextName || (nextScope === 'stock' ? `${symbol} 策略` : '全局策略'),
          buy: nextBuy,
          sell: nextSell,
          avoid: nextAvoid,
          note: nextNote,
        })
        await load()
        await runCheck()
        await runBacktest(nextBuy, nextSell, nextAvoid)
        return true
      } catch (e) {
        setErr(e instanceof Error ? e.message : '保存失败')
        return false
      } finally {
        setSaving(false)
      }
    },
    [scope, symbol, name, buy, sell, avoid, appliedPreset, load, runCheck, runBacktest],
  )

  const applyPreset = useCallback(
    async (id: string) => {
      const preset = presets.find((p) => p.id === id)
      if (!preset) return
      setBuy(preset.buy)
      setSell(preset.sell)
      setAvoid(preset.avoid)
      setName(preset.name)
      setScope('stock')
      setAppliedPreset(preset)
      setEditing(false)
      await save(preset.buy, preset.sell, preset.avoid, preset.name, 'stock', preset.note)
    },
    [presets, save],
  )

  const verdictClass = (v: string) =>
    v === '符合买入'
      ? 'text-stock-up'
      : v === '符合卖出' || v === '禁止买入'
        ? 'text-stock-down'
        : v === '接近买入'
          ? 'text-amber-500'
          : 'text-muted-foreground'

  const chart = useMemo(() => buildChart(result), [result])
  const rules = result?.rules
  const metrics = result?.metrics
  // 只有当前策略名仍是模板名时才显示模板的一句话解读,避免手动改名后残留旧描述
  const presetHint =
    appliedPreset && appliedPreset.name === (effective?.name || name)
      ? appliedPreset.description
      : ''

  return (
    <div className="space-y-3">
      <div className="card p-3">
        <div className="flex flex-wrap items-center gap-2">
          <div className="text-[12px] font-medium">策略检查</div>
          {check && (
            <span className={`text-[12px] font-medium ${verdictClass(check.verdict)}`}>
              {check.verdict}
            </span>
          )}
          <span className="min-w-0 truncate text-[11px] text-muted-foreground">
            {effective ? effective.name || (effective.scope === 'stock' ? '个股策略' : '全局策略') : '未配置策略'}
          </span>
          {check?.price != null && (
            <span className="text-[10px] text-muted-foreground">
              {check.price} {check.estimated ? '（盘中估算）' : ''}
            </span>
          )}
          <div className="ml-auto flex items-center gap-1.5">
            {presets.length > 0 && (
              <select
                value=""
                onChange={(e) => {
                  if (e.target.value) void applyPreset(e.target.value)
                }}
                className="h-7 rounded border bg-background px-1.5 text-[11px] text-muted-foreground"
                title="一键套用策略模板并回测"
              >
                <option value="">套用模板…</option>
                {presets.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            )}
            {effective && (
              <Button variant="outline" size="sm" className="h-7 px-2 text-[11px]" onClick={() => void runCheck()}>
                重新检查
              </Button>
            )}
            <Button variant="outline" size="sm" className="h-7 px-2 text-[11px]" onClick={() => setEditing((v) => !v)}>
              {editing ? '收起编辑' : '编辑策略'}
            </Button>
          </div>
        </div>

        {check?.pattern?.tags?.length ? (
          <div className="mt-2 flex flex-wrap gap-1">
            {check.pattern.tags.map((tag) => (
              <span key={tag} className="rounded bg-muted/50 px-1.5 py-0.5 text-[10px] text-muted-foreground">
                {tag}
              </span>
            ))}
          </div>
        ) : null}

        {check && check.has_playbook && (
          <div className="mt-2 space-y-1.5">
            <CheckRows title="买入（全部满足）" items={check.buy_checks} />
            <CheckRows title="卖出（任一满足）" items={check.sell_checks} />
            <CheckRows title="禁买（任一命中即不买）" items={check.avoid_checks || []} invert />
            {check.pattern?.risk_hint && (
              <div className="rounded bg-amber-500/10 px-2 py-1 text-[10px] text-amber-600 dark:text-amber-400">
                {check.pattern.risk_hint}
              </div>
            )}
            {check.estimated && (
              <div className="text-[10px] text-muted-foreground">盘中以现价估算，收盘后复核</div>
            )}
          </div>
        )}
        {check && !check.has_playbook && (
          <div className="mt-2 text-[11px] text-muted-foreground">
            还没有策略。点「编辑策略」配一条，或从右上角「套用模板」一键开始。
          </div>
        )}
      </div>

      {editing && (
        <div className="card space-y-2 p-3">
          <div className="flex flex-wrap items-center gap-2 text-[11px]">
            <select
              value={scope}
              onChange={(e) => setScope(e.target.value as 'global' | 'stock')}
              className="rounded border bg-background px-2 py-1"
            >
              <option value="stock">仅本股生效</option>
              <option value="global">全局默认</option>
            </select>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="策略名称"
              className="w-40 rounded border bg-background px-2 py-1"
            />
            <Button size="sm" className="h-7 px-2 text-[11px]" disabled={saving} onClick={() => void save()}>
              {saving ? '保存中…' : '保存并回测'}
            </Button>
          </div>
          <ConditionEditor title="买入条件（全部满足）" value={buy} onChange={setBuy} fields={fields} />
          <ConditionEditor title="卖出条件（任一满足）" value={sell} onChange={setSell} fields={fields} />
          <ConditionEditor title="禁买条件（任一命中即不买）" value={avoid} onChange={setAvoid} fields={fields} />
          <div className="text-[10px] text-muted-foreground">
            个股策略会完全替换全局策略；禁买条件用来排除「高开低走未修复、大幅高开」这类形态，保存后立即用于检查与回测。
          </div>
        </div>
      )}

      <div className="card p-3">
        <div className="flex flex-wrap items-center gap-2">
          <div className="text-[12px] font-medium">策略回测</div>
          <span className="text-[11px] text-muted-foreground">近 2 年 · 次日开盘成交 · 含费用</span>
          <Button
            size="sm"
            className="ml-auto h-7 px-3 text-[11px]"
            disabled={loading}
            onClick={() => void runBacktest()}
          >
            {loading ? '回测中…' : '运行回测'}
          </Button>
        </div>

        {err && <div className="mt-2 text-[11px] text-destructive">{err}</div>}

        {(rules || effective) && (
          <div className="mt-2 space-y-0.5 rounded-md border border-border/50 bg-muted/20 p-2 text-[10px] text-muted-foreground">
            <div>
              <span className="text-stock-up">买入</span>：
              {rulesText(rules?.buy || effective?.buy, fields)}
            </div>
            <div>
              <span className="text-stock-down">卖出</span>：
              {rulesText(rules?.sell || effective?.sell, fields)}
            </div>
            {(rules?.avoid?.length || effective?.avoid?.length) ? (
              <div>
                <span className="text-amber-500">禁买</span>：
                {rulesText(rules?.avoid || effective?.avoid, fields, '未设置')}
              </div>
            ) : null}
            {presetHint && (
              <div className="pt-0.5 text-foreground/70">交易解读：{presetHint}</div>
            )}
          </div>
        )}

        {result && !result.error && (
          <>
            <div className="mt-2 grid grid-cols-3 gap-1.5">
              <Metric label="当前状态" value={result.current_status} />
              <Metric label="胜率" value={`${result.win_rate}%`} />
              <Metric
                label="区间收益"
                value={`${result.total_return > 0 ? '+' : ''}${result.total_return}%`}
                tone={result.total_return >= 0 ? 'up' : 'down'}
              />
              <Metric
                label="跑赢个股"
                value={`${result.excess_return > 0 ? '+' : ''}${result.excess_return}%`}
                tone={result.excess_return >= 0 ? 'up' : 'down'}
              />
              <Metric
                label="最大回撤"
                value={metrics ? `${metrics.max_drawdown}%` : '--'}
                tone={metrics && metrics.max_drawdown < -20 ? 'down' : undefined}
              />
              <Metric
                label="年化"
                value={metrics ? `${metrics.annual_return > 0 ? '+' : ''}${metrics.annual_return}%` : '--'}
                tone={metrics && metrics.annual_return >= 0 ? 'up' : 'down'}
              />
            </div>
            <div className="mt-1.5 text-[10px] text-muted-foreground">
              {result.start} ~ {result.end} · {result.trade_count} 笔 · 买入持有{' '}
              {result.buy_hold_return > 0 ? '+' : ''}
              {result.buy_hold_return}%
              {metrics ? ` · 平均持有 ${metrics.avg_hold_days} 天` : ''}
              {metrics?.profit_factor != null ? ` · 盈亏比 ${metrics.profit_factor}` : ''}
            </div>
            {chart && <Chart data={chart} />}
            <div className="mt-1.5 flex flex-wrap gap-3 text-[10px] text-muted-foreground">
              <span className="flex items-center gap-1">
                <i className="inline-block h-0.5 w-4 bg-sky-400" /> 区间股价
              </span>
              <span className="flex items-center gap-1">
                <i className="inline-block h-0.5 w-4 bg-amber-400" /> 策略收益
              </span>
              <span className="flex items-center gap-1">
                <i className="inline-block h-2 w-2 rounded-full bg-stock-up" /> 买入
              </span>
              <span className="flex items-center gap-1">
                <i className="inline-block h-2 w-2 rounded-full bg-stock-down" /> 卖出
              </span>
            </div>
            {result.explain && (
              <div className="mt-2 rounded-md border border-border/50 bg-muted/20 p-2 text-[11px] leading-relaxed text-muted-foreground">
                <div className="mb-0.5 text-[10px] font-medium text-foreground">交易解读</div>
                {result.explain}
              </div>
            )}
            {result.trades.length > 0 && (
              <div className="mt-2 max-h-40 overflow-auto text-[11px]">
                <table className="w-full">
                  <thead className="text-[10px] text-muted-foreground">
                    <tr className="border-b">
                      <th className="py-1 text-left font-normal">买入</th>
                      <th className="py-1 text-left font-normal">卖出</th>
                      <th className="py-1 text-right font-normal">收益</th>
                      <th className="py-1 text-right font-normal">持有</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.trades.slice().reverse().map((t, i) => (
                      <tr key={`${t.entry_date}-${i}`} className="border-b last:border-0">
                        <td className="py-0.5">{t.entry_date.slice(5)}</td>
                        <td className="py-0.5">{t.exit_date.slice(5)}</td>
                        <td className={`py-0.5 text-right ${t.pnl_pct >= 0 ? 'text-stock-up' : 'text-stock-down'}`}>
                          {t.pnl_pct > 0 ? '+' : ''}
                          {t.pnl_pct}%
                        </td>
                        <td className="py-0.5 text-right text-muted-foreground">{t.hold_days}天</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: 'up' | 'down' }) {
  return (
    <div className="rounded-md border bg-muted/20 px-2 py-1.5">
      <div className="text-[10px] text-muted-foreground">{label}</div>
      <div className={`text-[13px] font-medium ${tone === 'up' ? 'text-stock-up' : tone === 'down' ? 'text-stock-down' : ''}`}>
        {value}
      </div>
    </div>
  )
}

function CheckRows({
  title,
  items,
  invert = false,
}: {
  title: string
  items: StrategyCheck['buy_checks']
  invert?: boolean
}) {
  if (!items.length) return null
  return (
    <div>
      <div className="text-[10px] text-muted-foreground">{title}</div>
      {items.map((c) => {
        const good = invert ? !c.ok : c.ok
        return (
          <div key={`${title}-${c.field}`} className="flex items-center gap-2 text-[11px]">
            <span className={good ? 'text-stock-up' : 'text-muted-foreground'}>
              {invert ? (c.ok ? '✗' : '✓') : c.ok ? '✓' : '✗'}
            </span>
            <span className="min-w-0 flex-1 truncate">{c.label}</span>
            <span className="text-muted-foreground">{c.expect}</span>
            <span className={good ? 'text-foreground' : 'text-muted-foreground'}>{c.actual}</span>
          </div>
        )
      })}
    </div>
  )
}

function ConditionEditor({
  title,
  value,
  onChange,
  fields,
}: {
  title: string
  value: StrategyCondition[]
  onChange: (v: StrategyCondition[]) => void
  fields: StrategyField[]
}) {
  const update = (i: number, patch: Partial<StrategyCondition>) =>
    onChange(value.map((c, idx) => (idx === i ? { ...c, ...patch } : c)))

  const groups = useMemo(() => {
    const map = new Map<string, StrategyField[]>()
    for (const f of fields) {
      const key = f.group || '其它'
      const list = map.get(key) || []
      list.push(f)
      map.set(key, list)
    }
    return Array.from(map.entries())
  }, [fields])

  return (
    <div>
      <div className="mb-1 flex items-center gap-2 text-[10px] text-muted-foreground">
        {title}
        <button
          type="button"
          className="rounded border px-1.5 text-[10px] hover:bg-accent"
          onClick={() => onChange([...value, { field: fields[0]?.key || 'runup_3d', op: 'gte', value: 0 }])}
        >
          + 条件
        </button>
      </div>
      {value.length === 0 && <div className="text-[10px] text-muted-foreground/70">未设置（此方向不做限制）</div>}
      {value.map((c, i) => (
        <div key={i} className="mb-1 flex flex-wrap items-center gap-1 text-[11px]">
          <select
            value={c.field}
            onChange={(e) => update(i, { field: e.target.value })}
            className="max-w-[132px] rounded border bg-background px-1.5 py-0.5"
          >
            {groups.map(([group, items]) => (
              <optgroup key={group} label={group}>
                {items.map((f) => (
                  <option key={f.key} value={f.key}>
                    {f.label}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
          <select
            value={c.op}
            onChange={(e) => update(i, { op: e.target.value })}
            className="rounded border bg-background px-1.5 py-0.5"
          >
            {OPS.map(([op, label]) => (
              <option key={op} value={op}>
                {label}
              </option>
            ))}
          </select>
          {c.field === 'gap_drop_flag' && c.op === 'eq' ? (
            <select
              value={String(c.value)}
              onChange={(e) => update(i, { value: Number(e.target.value) })}
              className="rounded border bg-background px-1.5 py-0.5"
            >
              <option value="1">是</option>
              <option value="0">否</option>
            </select>
          ) : (
            <input
              value={
                Array.isArray(c.value) ? (c.value as unknown[]).join(',') : String(c.value ?? '')
              }
              onChange={(e) => {
                const raw = e.target.value
                if (c.op === 'between') {
                  const parts = raw.split(',').map((v) => Number(v.trim()))
                  update(i, {
                    value: parts.every((v) => Number.isFinite(v)) && parts.length === 2 ? parts : raw,
                  })
                  return
                }
                const num = Number(raw)
                update(i, {
                  value: Number.isNaN(num) || raw.trim() === '' ? raw : num,
                })
              }}
              placeholder={c.op === 'between' ? '-5,0' : '0'}
              className="w-24 rounded border bg-background px-1.5 py-0.5"
            />
          )}
          <span className="text-[10px] text-muted-foreground">
            {fields.find((f) => f.key === c.field)?.unit || ''}
          </span>
          <button
            type="button"
            className="text-muted-foreground hover:text-destructive"
            onClick={() => onChange(value.filter((_, idx) => idx !== i))}
          >
            ×
          </button>
        </div>
      ))}
    </div>
  )
}

interface ChartPoint {
  x: number
  priceY: number
  equityY: number
  buy: boolean
  sell: boolean
}

interface ChartData {
  points: ChartPoint[]
  ticks: number[]
  y: (v: number) => number
  xLabels: Array<{ x: number; label: string }>
}

const CHART = { W: 340, H: 180, PL: 38, PR: 6, PT: 8, PB: 18 }

function niceTicks(min: number, max: number): number[] {
  const span = max - min || 1
  const raw = span / 4
  const mag = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1e-6))))
  const step =
    [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= 5.5) || mag * 10
  const start = Math.ceil(min / step) * step
  const out: number[] = []
  for (let v = start; v <= max + 1e-9; v += step) {
    out.push(Math.round(v * 10) / 10)
  }
  return out
}

function buildChart(result: BacktestResult | null): ChartData | null {
  if (!result || !result.curve?.length) return null
  const { W, H, PL, PR, PT, PB } = CHART
  const values = result.curve.flatMap((p) => [p.price_pct, p.equity_pct])
  const min = Math.min(...values, 0)
  const max = Math.max(...values, 0)
  const ticks = niceTicks(min, max)
  const lo = Math.min(min, ticks[0] ?? min)
  const hi = Math.max(max, ticks[ticks.length - 1] ?? max)
  const span = hi - lo || 1
  const y = (v: number) => PT + ((hi - v) / span) * (H - PT - PB)
  const n = result.curve.length
  const px = (i: number) => PL + (i / Math.max(1, n - 1)) * (W - PL - PR)
  const buys = new Set(result.trades.map((t) => t.entry_date))
  const sells = new Set(result.trades.map((t) => t.exit_date))
  const points = result.curve.map((p, i) => ({
    x: px(i),
    priceY: y(p.price_pct),
    equityY: y(p.equity_pct),
    buy: p.buy ?? buys.has(p.date),
    sell: p.sell ?? sells.has(p.date),
  }))
  const idx = [0, Math.floor((n - 1) / 2), n - 1]
  const xLabels = idx.map((i) => ({
    x: px(i),
    label: (result.curve[i]?.date || '').slice(5),
  }))
  return { points, ticks, y, xLabels }
}

function Chart({ data }: { data: ChartData }) {
  const { W, H, PL, PR, PB } = CHART
  const pricePath = data.points
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)},${p.priceY.toFixed(1)}`)
    .join(' ')
  const equityPath = data.points
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)},${p.equityY.toFixed(1)}`)
    .join(' ')
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="mt-2 h-auto w-full"
      preserveAspectRatio="xMidYMid meet"
      role="img"
      aria-label="策略收益曲线"
    >
      {data.ticks.map((tick) => (
        <g key={tick}>
          <line
            x1={PL}
            y1={data.y(tick)}
            x2={W - PR}
            y2={data.y(tick)}
            stroke="currentColor"
            strokeOpacity={tick === 0 ? 0.28 : 0.08}
            strokeWidth={0.8}
          />
          <text
            x={PL - 4}
            y={data.y(tick) + 3}
            textAnchor="end"
            fontSize="8"
            fill="currentColor"
            fillOpacity="0.55"
          >
            {tick}%
          </text>
        </g>
      ))}
      <path d={pricePath} fill="none" stroke="#38bdf8" strokeWidth="1.2" />
      <path d={equityPath} fill="none" stroke="#fbbf24" strokeWidth="1.4" />
      {data.points.map((p, i) =>
        p.buy ? (
          <circle key={`b${i}`} cx={p.x} cy={p.priceY} r="2.6" fill="#F04438" stroke="#fff" strokeWidth="0.6" />
        ) : null,
      )}
      {data.points.map((p, i) =>
        p.sell ? (
          <circle key={`s${i}`} cx={p.x} cy={p.priceY} r="2.6" fill="#12B76A" stroke="#fff" strokeWidth="0.6" />
        ) : null,
      )}
      {data.xLabels.map((item, i) => (
        <text
          key={`x${i}`}
          x={item.x}
          y={H - PB + 10}
          textAnchor={i === 0 ? 'start' : i === data.xLabels.length - 1 ? 'end' : 'middle'}
          fontSize="8"
          fill="currentColor"
          fillOpacity="0.55"
        >
          {item.label}
        </text>
      ))}
    </svg>
  )
}
