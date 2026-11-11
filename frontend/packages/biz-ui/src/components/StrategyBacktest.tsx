import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  strategyApi,
  type BacktestResult,
  type StrategyCheck,
  type StrategyCondition,
  type StrategyField,
  type StrategyPlaybook,
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

/** 单票策略检查 + 回测(同花顺「策略回测」的简化版)。 */
export default function StrategyBacktest({ symbol }: { symbol: string }) {
  const [fields, setFields] = useState<StrategyField[]>([])
  const [playbooks, setPlaybooks] = useState<StrategyPlaybook[]>([])
  const [check, setCheck] = useState<StrategyCheck | null>(null)
  const [result, setResult] = useState<BacktestResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [editing, setEditing] = useState(false)
  const [err, setErr] = useState('')

  const [buy, setBuy] = useState<StrategyCondition[]>([])
  const [sell, setSell] = useState<StrategyCondition[]>([])
  const [name, setName] = useState('')
  const [scope, setScope] = useState<'global' | 'stock'>('stock')

  const own = playbooks.find((p) => p.scope === 'stock' && p.symbol === symbol)
  const global = playbooks.find((p) => p.scope === 'global')
  const effective = own || global

  const load = useCallback(async () => {
    try {
      const list = await strategyApi.playbooks()
      setFields(list.fields)
      setPlaybooks(list.items)
      const mine = list.items.find((p) => p.scope === 'stock' && p.symbol === symbol)
      const g = list.items.find((p) => p.scope === 'global')
      const base = mine || g
      setBuy((base?.buy as StrategyCondition[]) || list.defaults.buy)
      setSell((base?.sell as StrategyCondition[]) || list.defaults.sell)
      setName(base?.name || '')
      setScope(mine ? 'stock' : 'global')
    } catch (e) {
      setErr(e instanceof Error ? e.message : '策略加载失败')
    }
  }, [symbol])

  const runCheck = useCallback(async () => {
    try {
      setCheck(await strategyApi.check(symbol))
    } catch {
      setCheck(null)
    }
  }, [symbol])

  useEffect(() => {
    void load()
    void runCheck()
    setResult(null)
  }, [load, runCheck])

  const runBacktest = useCallback(async () => {
    setLoading(true)
    setErr('')
    try {
      const r = await strategyApi.backtest({
        symbol,
        buy: effective?.buy as StrategyCondition[] | undefined,
        sell: effective?.sell as StrategyCondition[] | undefined,
        days: 500,
      })
      setResult(r)
      if (r.error) setErr(r.error)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '回测失败')
    } finally {
      setLoading(false)
    }
  }, [symbol, effective])

  const save = useCallback(async () => {
    setErr('')
    try {
      await strategyApi.save({
        scope,
        symbol: scope === 'stock' ? symbol : '',
        name: name || (scope === 'stock' ? `${symbol} 策略` : '全局策略'),
        buy,
        sell,
      })
      setEditing(false)
      await load()
      await runCheck()
    } catch (e) {
      setErr(e instanceof Error ? e.message : '保存失败')
    }
  }, [scope, symbol, name, buy, sell, load, runCheck])

  const verdictClass = (v: string) =>
    v === '符合买入'
      ? 'text-stock-up'
      : v === '符合卖出'
        ? 'text-stock-down'
        : v === '接近买入'
          ? 'text-amber-500'
          : 'text-muted-foreground'

  const chart = useMemo(() => buildChart(result), [result])

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
          <span className="text-[11px] text-muted-foreground">
            {effective ? effective.name || (effective.scope === 'stock' ? '个股策略' : '全局策略') : '未配置策略'}
          </span>
          <div className="ml-auto flex items-center gap-1.5">
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

        {check && check.has_playbook && (
          <div className="mt-2 space-y-1.5">
            <CheckRows title="买入" items={check.buy_checks} />
            <CheckRows title="卖出" items={check.sell_checks} />
            {check.estimated && (
              <div className="text-[10px] text-muted-foreground">盘中以现价估算，收盘后复核</div>
            )}
          </div>
        )}
        {check && !check.has_playbook && (
          <div className="mt-2 text-[11px] text-muted-foreground">
            还没有策略。点「编辑策略」配一条，或直接用下方回测的默认规则。
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
            <Button size="sm" className="h-7 px-2 text-[11px]" onClick={() => void save()}>
              保存
            </Button>
          </div>
          <ConditionEditor title="买入条件（全部满足）" value={buy} onChange={setBuy} fields={fields} />
          <ConditionEditor title="卖出条件（任一满足）" value={sell} onChange={setSell} fields={fields} />
          <div className="text-[10px] text-muted-foreground">
            个股策略会完全替换全局策略；保存后立即用于检查与回测。
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

        {result && !result.error && (
          <>
            <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
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
            </div>
            <div className="mt-2 text-[10px] text-muted-foreground">
              {result.start} ~ {result.end} · {result.trade_count} 笔交易 · 买入持有{' '}
              {result.buy_hold_return > 0 ? '+' : ''}
              {result.buy_hold_return}%
            </div>
            {chart && <Chart data={chart} />}
            <div className="mt-2 flex flex-wrap gap-3 text-[10px] text-muted-foreground">
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
      <div className={`text-sm font-medium ${tone === 'up' ? 'text-stock-up' : tone === 'down' ? 'text-stock-down' : ''}`}>
        {value}
      </div>
    </div>
  )
}

function CheckRows({ title, items }: { title: string; items: StrategyCheck['buy_checks'] }) {
  if (!items.length) return null
  return (
    <div>
      <div className="text-[10px] text-muted-foreground">{title}</div>
      {items.map((c) => (
        <div key={`${title}-${c.field}`} className="flex items-center gap-2 text-[11px]">
          <span className={c.ok ? 'text-stock-up' : 'text-muted-foreground'}>{c.ok ? '✓' : '✗'}</span>
          <span className="min-w-0 flex-1 truncate">{c.label}</span>
          <span className="text-muted-foreground">{c.expect}</span>
          <span className={c.ok ? 'text-foreground' : 'text-muted-foreground'}>{c.actual}</span>
        </div>
      ))}
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
      {value.map((c, i) => (
        <div key={i} className="mb-1 flex flex-wrap items-center gap-1 text-[11px]">
          <select
            value={c.field}
            onChange={(e) => update(i, { field: e.target.value })}
            className="rounded border bg-background px-1.5 py-0.5"
          >
            {fields.map((f) => (
              <option key={f.key} value={f.key}>
                {f.label}
              </option>
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
          <input
            value={
              Array.isArray(c.value) ? (c.value as unknown[]).join(',') : String(c.value ?? '')
            }
            onChange={(e) => {
              const raw = e.target.value
              const num = Number(raw)
              update(i, {
                value: Number.isNaN(num) || raw.trim() === '' ? raw : num,
              })
            }}
            className="w-24 rounded border bg-background px-1.5 py-0.5"
          />
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

function buildChart(result: BacktestResult | null): ChartPoint[] | null {
  if (!result || !result.curve.length) return null
  const W = 560
  const H = 200
  const pad = 8
  const values = result.curve.flatMap((p) => [p.price_pct, p.equity_pct])
  const min = Math.min(...values, 0)
  const max = Math.max(...values, 0)
  const span = max - min || 1
  const n = result.curve.length
  const y = (v: number) => pad + ((max - v) / span) * (H - pad * 2)
  const buys = new Set(result.trades.map((t) => t.entry_date))
  const sells = new Set(result.trades.map((t) => t.exit_date))
  return result.curve.map((p, i) => ({
    x: pad + (i / Math.max(1, n - 1)) * (W - pad * 2),
    priceY: y(p.price_pct),
    equityY: y(p.equity_pct),
    buy: buys.has(p.date),
    sell: sells.has(p.date),
  }))
}

function Chart({ data }: { data: ChartPoint[] }) {
  const pricePath = data.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)},${p.priceY.toFixed(1)}`).join(' ')
  const equityPath = data.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)},${p.equityY.toFixed(1)}`).join(' ')
  return (
    <svg viewBox="0 0 560 200" className="mt-2 h-[200px] w-full" preserveAspectRatio="none">
      <line x1="8" y1="100" x2="552" y2="100" stroke="currentColor" strokeOpacity="0.12" strokeWidth="0.5" />
      <path d={pricePath} fill="none" stroke="#38bdf8" strokeWidth="1.2" />
      <path d={equityPath} fill="none" stroke="#fbbf24" strokeWidth="1.2" />
      {data.map((p, i) =>
        p.buy ? <circle key={`b${i}`} cx={p.x} cy={p.priceY} r="2.4" fill="#F04438" /> : null
      )}
      {data.map((p, i) =>
        p.sell ? <circle key={`s${i}`} cx={p.x} cy={p.priceY} r="2.4" fill="#12B76A" /> : null
      )}
    </svg>
  )
}
