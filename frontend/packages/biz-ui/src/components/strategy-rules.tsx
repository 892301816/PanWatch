import { useEffect, useMemo, useRef, useState } from 'react'
import type { StrategyCondition, StrategyField } from '@panwatch/api'
import StrategyFactorPicker from './StrategyFactorPicker'

/** 条件编辑器与「条件 → 中文」的共用实现（回测页与全局策略设置共用）。 */

export const OPS: Array<[string, string]> = [
  ['gte', '≥'],
  ['lte', '≤'],
  ['gt', '>'],
  ['lt', '<'],
  ['eq', '='],
  ['ne', '≠'],
  ['between', '介于'],
  ['in', '属于'],
  ['not_in', '不属于'],
]

export const OP_TEXT: Record<string, string> = {
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

export function conditionText(cond: StrategyCondition, fields: StrategyField[]): string {
  if (cond.children) return `（${cond.children.map(c=>conditionText(c,fields)).join(cond.logic==='any'?' 或 ':' 且 ')}）`
  const field = fields.find((f) => f.key === cond.field)
  const when = cond.offset ? `${cond.offset}个交易日前 · ` : ''
  const repeat = (cond.consecutive || 1) > 1 ? `连续${cond.consecutive}个交易日 · ` : ''
  const label = `${when}${repeat}${field?.label || cond.field}`
  const unit = field?.unit || ''
  const value = cond.value
  if ((cond.field === 'gap_drop_flag' || field?.kind === 'boolean' || cond.field.startsWith('template_')) && cond.op === 'eq') {
    return `${label} = ${Number(value) === 1 ? '是' : '否'}`
  }
  if (cond.op === 'between' && Array.isArray(value) && value.length === 2) {
    return `${label} 介于 ${value[0]} ~ ${value[1]}${unit}`
  }
  return `${label} ${OP_TEXT[cond.op] || cond.op} ${Array.isArray(value) ? value.join(',') : value}${unit}`
}

export function rulesText(
  conds: StrategyCondition[] | undefined,
  fields: StrategyField[],
  empty = '未设置',
  joiner = ' 且 ',
): string {
  if (!conds || conds.length === 0) return empty
  return conds.map((c) => conditionText(c, fields)).join(joiner)
}

export function ConditionEditor({
  title,
  hint,
  value,
  onChange,
  fields,
  emptyHint = '未设置',
  depth = 0,
}: {
  title: string
  hint?: string
  value: StrategyCondition[]
  onChange: (v: StrategyCondition[]) => void
  fields: StrategyField[]
  emptyHint?: string
  depth?: number
}) {
  const [pickerOpen,setPickerOpen]=useState(false)
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
      <div className="mb-2 flex flex-wrap items-center gap-3 text-sm font-medium">
        <span>{title}</span>
        <button
          type="button"
          className="min-h-9 rounded-lg border border-border px-3 text-xs hover:bg-accent"
          onClick={() => setPickerOpen(true)}
        >
          + 条件
        </button>
        {depth<3&&<button type="button" className="min-h-9 rounded-lg border border-border px-3 text-xs" onClick={()=>onChange([...value,{field:'__group__',op:'eq',value:1,logic:'all',children:[{field:fields[0]?.key||'runup_3d',op:'gte',value:0}]}])}>＋ 条件组</button>}
        {hint && <span className="text-[10px] text-muted-foreground/70">{hint}</span>}
      </div>
      {value.length === 0 && <p className="text-sm leading-6 text-muted-foreground">{emptyHint}</p>}
      <StrategyFactorPicker open={pickerOpen} onClose={()=>setPickerOpen(false)} fields={fields} onSelect={f=>onChange([...value,{field:f.key,op:f.kind==='boolean'?'eq':'gte',value:f.kind==='boolean'?1:0}])}/>
      {value.map((c, i) => c.children ? <div key={i} className="mb-3 rounded-lg border border-border p-3"><div className="mb-3 flex justify-between gap-3"><select aria-label={`${title}第${i+1}组逻辑`} className="min-h-10 bg-card border border-border rounded-lg px-3" value={c.logic||'all'} onChange={e=>update(i,{logic:e.target.value as 'all'|'any'})}><option value="all">全部满足</option><option value="any">任一满足</option></select><button type="button" aria-label={`删除${title}第${i+1}组`} onClick={()=>onChange(value.filter((_,j)=>j!==i))}>删除组</button></div><ConditionEditor title={`${title}条件组${i+1}`} value={c.children} onChange={children=>update(i,{children})} fields={fields} depth={depth+1}/></div> : (
        <div key={i} className="mb-2 flex flex-wrap items-center gap-2 text-sm">
          <select
            value={c.field}
            aria-label={`${title}第${i + 1}条字段`}
            onChange={(e) => update(i, { field: e.target.value })}
            className="min-h-10 max-w-full rounded-lg border border-border bg-background px-3"
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
            aria-label={`${title}第${i + 1}条比较方式`}
            onChange={(e) => update(i, { op: e.target.value })}
            className="min-h-10 rounded-lg border border-border bg-background px-3"
          >
            {OPS.map(([op, label]) => (
              <option key={op} value={op}>
                {label}
              </option>
            ))}
          </select>
          {(c.field === 'gap_drop_flag' || fields.find(f=>f.key===c.field)?.kind==='boolean') && c.op === 'eq' ? (
            <select
              value={String(c.value)}
              aria-label={`${title}第${i + 1}条数值`}
              onChange={(e) => update(i, { value: Number(e.target.value) })}
              className="min-h-10 rounded-lg border border-border bg-background px-3"
            >
              <option value="1">是</option>
              <option value="0">否</option>
            </select>
          ) : (
            <ConditionValueInput
              key={`${c.field}:${c.op}`}
              value={c.value}
              op={c.op}
              label={`${title}第${i + 1}条数值`}
              onChange={next => update(i, { value: next })}
            />
          )}
          <span className="text-[10px] text-muted-foreground">
            {fields.find((f) => f.key === c.field)?.unit === 'ratio' ? '比值（0.05 = 5%）' : fields.find((f) => f.key === c.field)?.unit || ''}
          </span>
          <button
            type="button"
            aria-label={`删除${title}第${i + 1}条`}
            className="min-h-10 min-w-10 rounded-lg text-muted-foreground hover:bg-accent hover:text-destructive"
            onClick={() => onChange(value.filter((_, idx) => idx !== i))}
          >
            ×
          </button>
          <details className="basis-full text-xs text-muted-foreground"><summary className="cursor-pointer">时序与公式说明</summary><p className="my-2">{fields.find(f=>f.key===c.field)?.description || '原有量价条件，继续使用原来的计算口径。'}</p><label>前几个交易日 <input aria-label={`${title}第${i+1}条历史偏移`} type="number" min={0} max={240} value={c.offset||0} className="w-20 min-h-10 bg-background border rounded px-2" onChange={e=>update(i,{offset:Number(e.target.value)})}/></label><label className="ml-3">连续满足 <input aria-label={`${title}第${i+1}条连续天数`} type="number" min={1} max={60} value={c.consecutive||1} className="w-20 min-h-10 bg-background border rounded px-2" onChange={e=>update(i,{consecutive:Number(e.target.value)})}/> 个交易日</label></details>
        </div>
      ))}
    </div>
  )
}

const valueText = (value: unknown) => Array.isArray(value) ? value.join(',') : String(value ?? '')
const numericToken = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i

function parseValue(raw: string, op: string): unknown {
  const tokens = raw.split(',').map(token => token.trim())
  if (tokens.some(token => !numericToken.test(token) || !Number.isFinite(Number(token)))) return raw
  if (['between', 'in', 'not_in'].includes(op)) {
    return op !== 'between' || tokens.length === 2 ? tokens.map(Number) : raw
  }
  return tokens.length === 1 ? Number(tokens[0]) : raw
}

/** Keep edit text such as "0." and "-" while publishing valid numeric values immediately. */
export function ConditionValueInput({ value, op, label, onChange }: { value: unknown; op: string; label: string; onChange: (value: unknown) => void }) {
  const [raw, setRaw] = useState(() => valueText(value))
  const emitted = useRef(value)
  useEffect(() => {
    if (JSON.stringify(value) !== JSON.stringify(emitted.current)) {
      emitted.current = value
      setRaw(valueText(value))
    }
  }, [value])
  const publish = (text: string) => {
    const next = parseValue(text, op)
    emitted.current = next
    onChange(next)
    return next
  }
  return <input
    value={raw}
    aria-label={label}
    inputMode="decimal"
    onChange={event => { setRaw(event.target.value); publish(event.target.value) }}
    onBlur={() => { const next = publish(raw); if (typeof next !== 'string') setRaw(valueText(next)) }}
    placeholder={op === 'between' ? '-5,0' : '0'}
    className="min-h-10 w-28 rounded-lg border border-border bg-background px-3"
  />
}
