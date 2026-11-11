import { useMemo } from 'react'
import type { StrategyCondition, StrategyField } from '@panwatch/api'

/** 条件编辑器与「条件 → 中文」的共用实现（回测页与全局策略设置共用）。 */

export const OPS: Array<[string, string]> = [
  ['gte', '≥'],
  ['lte', '≤'],
  ['gt', '>'],
  ['lt', '<'],
  ['eq', '='],
  ['between', '介于'],
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
}: {
  title: string
  hint?: string
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
      <div className="mb-1 flex flex-wrap items-center gap-2 text-[10px] text-muted-foreground">
        <span>{title}</span>
        <button
          type="button"
          className="rounded border px-1.5 text-[10px] hover:bg-accent"
          onClick={() => onChange([...value, { field: fields[0]?.key || 'runup_3d', op: 'gte', value: 0 }])}
        >
          + 条件
        </button>
        {hint && <span className="text-[10px] text-muted-foreground/70">{hint}</span>}
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
              value={Array.isArray(c.value) ? (c.value as unknown[]).join(',') : String(c.value ?? '')}
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
