import { useMemo, useState } from 'react'
import { ArrowLeft, Check, Plus, Search, X } from 'lucide-react'
import {
  KIND_LIST,
  defaultPlace,
  indicators,
  titleOf,
  valid,
  type Kind,
  type Study,
} from '@/lib/studio-indicators'

type Props = { studies: Study[]; change: (s: Study[]) => void }

type Spec = {
  title: string
  args: string[]
  defaults: number[]
  price: boolean
  limits?: Array<[number, number]>
  decimal?: boolean[]
}

/** 常用周期档位；超出当前指标范围的会被过滤掉 */
const PERIOD_LADDER = [2, 3, 5, 6, 9, 10, 12, 14, 20, 24, 26, 30, 50, 60, 120, 250]

const specOf = (kind: Kind) => indicators[kind] as unknown as Spec

function periodOptions(kind: Kind, index: number, current: number) {
  const spec = specOf(kind)
  const [min, max] = spec.limits?.[index] ?? [2, 250]
  const pool = new Set(PERIOD_LADDER.filter((v) => v >= min && v <= max))
  pool.add(spec.defaults[index])
  if (Number.isFinite(current) && current >= min && current <= max) pool.add(current)
  return [...pool].sort((a, b) => a - b)
}

/**
 * 指标面板：列表 ↔ 参数 两段式。
 *
 * 参考观潮的「指标列表 → 独立设置窗」：列表里只负责选指标，参数在同层第二页
 * 用「标签 + 控件」的表单行展示，避免参数区夹在卡片网格里上下跳动。
 * 周期类参数给下拉档位（含「自定义…」），倍数/偏离这类小数用数字输入。
 */
export default function StudioIndicatorMenu({ studies, change }: Props) {
  const [step, setStep] = useState<'list' | 'config'>('list')
  const [kind, setKind] = useState<Kind>('MA')
  const [values, setValues] = useState<string[]>(['20'])
  const [place, setPlace] = useState<Study['place']>('main')
  const [editing, setEditing] = useState('')
  const [query, setQuery] = useState('')
  const [error, setError] = useState('')
  const [custom, setCustom] = useState<Record<number, boolean>>({})

  const spec = specOf(kind)
  const entries = useMemo(
    () =>
      KIND_LIST.filter((id) => {
        const word = query.trim().toLowerCase()
        if (!word) return true
        return (
          id.toLowerCase().includes(word) ||
          indicators[id].title.toLowerCase().includes(word)
        )
      }),
    [query],
  )

  function openConfig(next: Kind, study?: Study) {
    setKind(next)
    setValues(
      study ? study.args.map(String) : specOf(next).defaults.map(String),
    )
    setPlace(study ? study.place : defaultPlace(next))
    setEditing(study?.id ?? '')
    setCustom({})
    setError('')
    setStep('config')
  }

  /** 无参指标（成交量、OBV）点一下直接加，不进设置页 */
  function pick(next: Kind) {
    if (specOf(next).defaults.length === 0) {
      change([
        ...studies,
        { id: String(Date.now()), kind: next, args: [], place: defaultPlace(next) },
      ])
      return
    }
    openConfig(next)
  }

  function submit() {
    const item: Study = {
      id: editing || String(Date.now()),
      kind,
      args: values.map(Number),
      place,
    }
    if (!valid(item)) {
      setError('参数超出范围：周期 2–250 的整数；倍数/偏离需在允许区间内。')
      return
    }
    change(
      editing ? studies.map((s) => (s.id === editing ? item : s)) : [...studies, item],
    )
    setStep('list')
    setEditing('')
    setError('')
  }

  /* ------------------------------------------------------------ 参数设置页 */
  if (step === 'config') {
    return (
      <div className="st-ind">
        <header className="st-ind-head">
          <button
            type="button"
            className="st-ind-back"
            aria-label="返回指标列表"
            onClick={() => {
              setStep('list')
              setEditing('')
            }}
          >
            <ArrowLeft size={16} />
          </button>
          <div>
            <b>{spec.title}</b>
            <small>
              {kind} · {spec.price ? '主图' : '副图'}
            </small>
          </div>
        </header>

        <div className="st-ind-rows">
          {spec.args.map((label, index) => {
            const [min, max] = spec.limits?.[index] ?? [2, 250]
            const isDecimal = spec.decimal?.[index] === true
            const current = Number(values[index])
            const options = periodOptions(kind, index, current)
            const useCustom = custom[index] === true || (isDecimal ? false : !options.includes(current))
            return (
              <label className="st-ind-row" key={label + index}>
                <span>{label}</span>
                {isDecimal ? (
                  <input
                    type="number"
                    step="any"
                    min={min}
                    max={max}
                    value={values[index] ?? ''}
                    onChange={(event) =>
                      setValues((list) =>
                        list.map((v, i) => (i === index ? event.target.value : v)),
                      )
                    }
                  />
                ) : (
                  <span className="st-ind-pick">
                    <select
                      aria-label={label}
                      value={useCustom ? 'custom' : String(values[index] ?? '')}
                      onChange={(event) => {
                        if (event.target.value === 'custom') {
                          setCustom((map) => ({ ...map, [index]: true }))
                          return
                        }
                        setCustom((map) => ({ ...map, [index]: false }))
                        setValues((list) =>
                          list.map((v, i) =>
                            i === index ? event.target.value : v,
                          ),
                        )
                      }}
                    >
                      {options.map((option) => (
                        <option key={option} value={String(option)}>
                          {option}
                        </option>
                      ))}
                      <option value="custom">自定义…</option>
                    </select>
                    {useCustom && (
                      <input
                        type="number"
                        aria-label={label + ' 自定义值'}
                        min={min}
                        max={max}
                        step="1"
                        value={values[index] ?? ''}
                        onChange={(event) =>
                          setValues((list) =>
                            list.map((v, i) =>
                              i === index ? event.target.value : v,
                            ),
                          )
                        }
                      />
                    )}
                  </span>
                )}
              </label>
            )
          })}

          <div className="st-ind-row">
            <span>显示位置</span>
            <div className="st-ind-place" role="group" aria-label="显示位置">
              <button
                type="button"
                aria-pressed={place === 'main'}
                onClick={() => setPlace('main')}
              >
                主图叠加
              </button>
              <button
                type="button"
                aria-pressed={place === 'pane'}
                onClick={() => setPlace('pane')}
              >
                独立副图
              </button>
            </div>
          </div>
        </div>

        {error && (
          <p className="st-ind-error" role="alert">
            {error}
          </p>
        )}

        <div className="st-ind-foot">
          <button
            type="button"
            className="st-ind-cancel"
            onClick={() => {
              setStep('list')
              setEditing('')
              setError('')
            }}
          >
            取消
          </button>
          <button type="button" className="st-ind-submit" onClick={submit}>
            {editing ? <Check size={14} /> : <Plus size={14} />}
            {editing ? '保存修改' : '添加到图表'}
          </button>
        </div>
      </div>
    )
  }

  /* ------------------------------------------------------------ 指标列表页 */
  return (
    <div className="st-ind">
      <div className="st-ind-search">
        <Search size={14} />
        <input
          aria-label="搜索指标"
          placeholder="搜索指标，例如 RSI / 布林"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        {query && (
          <button type="button" aria-label="清空搜索" onClick={() => setQuery('')}>
            <X size={13} />
          </button>
        )}
      </div>

      {studies.length > 0 && (
        <section className="st-ind-block">
          <header>
            <h4>已添加</h4>
            <span>{studies.length}</span>
          </header>
          <div className="st-ind-chips">
            {studies.map((study) => (
              <span
                key={study.id}
                className="st-ind-chip"
                data-place={study.place}
              >
                <button
                  type="button"
                  onClick={() => openConfig(study.kind, study)}
                  title="点击编辑参数"
                >
                  {titleOf(study)}
                </button>
                <button
                  type="button"
                  aria-label={'移除 ' + titleOf(study)}
                  onClick={() => change(studies.filter((item) => item.id !== study.id))}
                >
                  <X size={12} />
                </button>
              </span>
            ))}
          </div>
        </section>
      )}

      <section className="st-ind-block">
        <header>
          <h4>可选指标</h4>
          <span>{entries.length}</span>
        </header>
        <div className="st-ind-list" role="listbox" aria-label="指标列表">
          {entries.map((id) => {
            const item = specOf(id)
            const added = studies.some((study) => study.kind === id)
            return (
              <button
                key={id}
                type="button"
                role="option"
                aria-selected={added}
                onClick={() => pick(id)}
              >
                <b>
                  {item.title}
                  {added && <i aria-hidden="true">·</i>}
                </b>
                <small>
                  {id} · {item.price ? '主图' : '副图'}
                  {item.defaults.length ? ' · ' + item.defaults.join('/') : ''}
                </small>
              </button>
            )
          })}
          {entries.length === 0 && <p className="st-ind-empty">没有匹配的指标</p>}
        </div>
      </section>
    </div>
  )
}
