import { useEffect, useMemo, useRef, useState } from 'react'
import type { ReactElement } from 'react'
import type { StudioInputMeta } from '@panwatch/api'

export type PineInputValue = number | string | boolean

type Props = {
  inputs: StudioInputMeta[]
  values: Record<string, PineInputValue>
  onChange: (values: Record<string, PineInputValue>) => void
}

/** 没有声明 options 的 timeframe 参数用这套常用周期。 */
const TIMEFRAMES = ['1', '5', '15', '30', '60', 'D', 'W', 'M']
const asText = (value: unknown) => (value == null ? '' : String(value))
const isNumeric = (kind: string) => kind === 'int' || kind === 'float'
const isSelect = (meta: StudioInputMeta) =>
  meta.kind === 'timeframe' || !!meta.options?.length
/** options 里的项可能是裸值，也可能是 {text,value} 这类描述对象。 */
function optionValue(option: unknown): unknown {
  if (option && typeof option === 'object') {
    const row = option as {
      value?: unknown
      text?: unknown
      label?: unknown
      title?: unknown
    }
    return row.value ?? row.text ?? row.label ?? row.title ?? ''
  }
  return option
}
function optionText(option: unknown): string {
  if (option && typeof option === 'object') {
    const row = option as {
      value?: unknown
      text?: unknown
      label?: unknown
      title?: unknown
    }
    return asText(row.text ?? row.label ?? row.title ?? row.value)
  }
  return asText(option)
}
/** <input type="color"> 只接受 #rrggbb；Pine 的 #rrggbbaa 先去掉透明度再取色。 */
function hexOf(value: unknown) {
  const text = asText(value).trim()
  if (/^#[0-9a-f]{8}$/i.test(text)) return text.slice(0, 7).toLowerCase()
  if (/^#[0-9a-f]{6}$/i.test(text)) return text.toLowerCase()
  if (/^#[0-9a-f]{3}$/i.test(text))
    return '#' + text.slice(1).split('').map((c) => c + c).join('')
  return '#000000'
}

/** 单个参数控件：数字/文本保留本地输入态，避免防抖回写打断输入。 */
function PineField({
  meta,
  value,
  commit,
}: {
  meta: StudioInputMeta
  value: PineInputValue | undefined
  commit: (next: PineInputValue) => void
}) {
  const current = value === undefined ? (meta.default as PineInputValue) : value
  const [text, setText] = useState(() => asText(current))
  // 外部值（切换脚本、恢复默认）变化时同步；等价数值不打断「3.」这类输入。
  useEffect(() => {
    if (isNumeric(meta.kind)) {
      if (!Number.isFinite(Number(text)) || Number(text) !== Number(current))
        setText(asText(current))
    } else if (text !== asText(current)) setText(asText(current))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current, meta.id])

  const low = meta.minval ?? undefined
  const high = meta.maxval ?? undefined
  let hint = ''
  if (isNumeric(meta.kind) && text !== '') {
    const parsed = Number(text)
    if (!Number.isFinite(parsed)) hint = '请输入数字，当前值不会生效'
    else if (meta.kind === 'int' && !Number.isInteger(parsed))
      hint = '需要整数，仍会按当前值发送'
    else if (low !== undefined && parsed < low) hint = `低于建议下限 ${low}`
    else if (high !== undefined && parsed > high) hint = `高于建议上限 ${high}`
  }
  const title = meta.tooltip || undefined
  const label = (
    <span className="st-pine-label">
      {meta.name}
      {low !== undefined || high !== undefined ? (
        <small>
          {low ?? '—'}–{high ?? '—'}
        </small>
      ) : null}
    </span>
  )
  let control: ReactElement
  if (isSelect(meta)) {
    const options = meta.options?.length ? meta.options : TIMEFRAMES
    const currentText = asText(current)
    const matched = options.some((o) => optionText(o) === currentText)
    control = (
      <select
        aria-label={meta.name}
        value={currentText}
        onChange={(e) => {
          const hit = options.find((o) => optionText(o) === e.target.value)
          const next = hit === undefined ? e.target.value : optionValue(hit)
          setText(asText(next))
          commit(next as PineInputValue)
        }}
      >
        {!matched && currentText !== '' && (
          <option value={currentText}>{currentText}</option>
        )}
        {options.map((option, i) => (
          <option key={i} value={optionText(option)}>
            {optionText(option)}
          </option>
        ))}
      </select>
    )
  } else if (meta.kind === 'bool') {
    return (
      <label className="st-pine-field st-pine-check" title={title}>
        <input
          type="checkbox"
          checked={!!current}
          onChange={(e) => commit(e.target.checked)}
        />
        {label}
      </label>
    )
  } else if (meta.kind === 'color') {
    control = (
      <input
        type="color"
        aria-label={meta.name}
        value={hexOf(current)}
        onChange={(e) => {
          setText(e.target.value)
          commit(e.target.value)
        }}
      />
    )
  } else if (meta.kind === 'text') {
    control = (
      <textarea
        aria-label={meta.name}
        rows={3}
        value={text}
        onChange={(e) => {
          setText(e.target.value)
          commit(e.target.value)
        }}
      />
    )
  } else if (isNumeric(meta.kind)) {
    control = (
      <input
        type="number"
        aria-label={meta.name}
        aria-invalid={!!hint}
        inputMode="decimal"
        step={meta.step ?? (meta.kind === 'int' ? 1 : 'any')}
        min={low}
        max={high}
        value={text}
        onChange={(e) => {
          setText(e.target.value)
          const parsed = Number(e.target.value)
          if (e.target.value !== '' && Number.isFinite(parsed)) commit(parsed)
        }}
      />
    )
  } else {
    control = (
      <input
        aria-label={meta.name}
        value={text}
        onChange={(e) => {
          setText(e.target.value)
          commit(e.target.value)
        }}
      />
    )
  }
  return (
    <label className="st-pine-field" title={title}>
      {label}
      {control}
      {hint && <em className="st-pine-hint">{hint}</em>}
    </label>
  )
}

export default function StudioPineInputs({ inputs, values, onChange }: Props) {
  const timer = useRef<number>()
  const pending = useRef<Record<string, PineInputValue>>({})
  const latest = useRef(values)
  latest.current = values
  useEffect(() => () => window.clearTimeout(timer.current), [])

  // 按 group 分组；没有 group 的排在最前面。
  const groups = useMemo(() => {
    const map = new Map<string, StudioInputMeta[]>()
    for (const meta of inputs) {
      const key = meta.group || ''
      const rows = map.get(key)
      if (rows) rows.push(meta)
      else map.set(key, [meta])
    }
    return [...map.entries()].sort((a, b) =>
      a[0] === '' ? -1 : b[0] === '' ? 1 : 0,
    )
  }, [inputs])

  // 300ms 防抖：连续拖动/输入只让父级请求一次。
  function commit(id: string, next: PineInputValue) {
    pending.current = { ...pending.current, [id]: next }
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => {
      const patch = pending.current
      pending.current = {}
      onChange({ ...latest.current, ...patch })
    }, 300)
  }

  if (!inputs.length) return null
  return (
    <section className="st-pine-inputs" aria-label="Pine 参数面板">
      <header>
        <b>参数</b>
        <small>{inputs.length} 项 · 修改后自动重算图表</small>
      </header>
      {groups.map(([group, rows]) => (
        <fieldset key={group || 'default'}>
          {group && <legend>{group}</legend>}
          <div className="st-pine-grid">
            {rows.map((meta) => (
              <PineField
                key={meta.id}
                meta={meta}
                value={values[meta.id]}
                commit={(next) => commit(meta.id, next)}
              />
            ))}
          </div>
        </fieldset>
      ))}
    </section>
  )
}
