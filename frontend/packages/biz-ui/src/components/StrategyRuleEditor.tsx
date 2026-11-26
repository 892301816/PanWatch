import { useState } from 'react'
import type { StrategyDraftInput, StrategyField, StrategyPreset } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { ConditionEditor, ConditionValueInput } from './strategy-rules'
import { strategySummary, templateSignalDescription, templateCaveat } from './strategy-description'

export function validateRuleDraft(value: StrategyDraftInput, fields: StrategyField[]): string[] {
  const errors: string[] = []
  const walk=(rows:StrategyDraftInput['buy'],prefix:string)=>rows.forEach(row=>{
    if(row.children){if(!row.children.length)errors.push(`${prefix}条件组不能为空`);walk(row.children,prefix);return}
    if(!fields.some(f=>f.key===row.field))errors.push(`${prefix}字段不受支持`)
    const values=Array.isArray(row.value)?row.value:[row.value]
    if(!values.length||values.some(n=>typeof n!=='number'||!Number.isFinite(n)))errors.push(`${prefix}需要填写有效数值`)
  })
  for (const [group, rows] of [['买入', value.buy], ['卖出', value.sell], ['禁买', value.avoid]] as const) {
    rows.forEach((row, index) => {
      if(row.children){walk(row.children,group);return}
      const prefix = `${group}第 ${index + 1} 条`
      if (!fields.some(field => field.key === row.field)) errors.push(`${prefix}字段不受支持`)
      if (!['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'between', 'in', 'not_in'].includes(row.op)) errors.push(`${prefix}比较方式不受支持`)
      const numbers = Array.isArray(row.value) ? row.value : [row.value]
      if (!numbers.length || numbers.some(number => typeof number !== 'number' || !Number.isFinite(number))) errors.push(`${prefix}需要填写有效数值`)
      if (row.op === 'between' && (!Array.isArray(row.value) || row.value.length !== 2 || Number(row.value[0]) > Number(row.value[1]))) errors.push(`${prefix}区间需要填写从小到大的两个数值`)
    })
  }
  for(const n of Object.values(value.config?.execution||{})){if(typeof n!=='number'||!Number.isFinite(n))errors.push('执行参数必须是有效数字')}
  if(Object.values(value.config?.scoring||{}).some(v=>typeof v!=='number'||!Number.isFinite(v)||v<=0))errors.push('评分权重必须为正数')
  if (value.questions?.length) errors.push('请先处理 AI 提出的待确认问题，再保存或应用可执行规则')
  return errors
}

export default function StrategyRuleEditor({ value, onChange, fields, presets, busy = false, onAiDraft, initialMode = '', guided = false }: {
  value: StrategyDraftInput
  onChange: (value: StrategyDraftInput) => void
  fields: StrategyField[]
  presets: StrategyPreset[]
  busy?: boolean
  initialMode?: string
  guided?: boolean
  onAiDraft?: (text: string) => Promise<void>
}) {
  const [idea, setIdea] = useState('')
  const [buyOpen, setBuyOpen] = useState(!value.buy.length)
  const [ideaOpen, setIdeaOpen] = useState(initialMode === 'idea')
  const text = strategySummary(value, fields, presets)
  const template=presets.find(p=>p.id===value.config?.template_id)
  const updateConfig=(patch:Partial<NonNullable<StrategyDraftInput['config']>>)=>onChange({...value,config:{...value.config,...patch}})
  return <fieldset disabled={busy} className="min-w-0 space-y-5">
    <div className="grid gap-4 sm:grid-cols-2">
      <label className="space-y-1.5 text-sm"><span className="block font-medium">草稿名称</span><input aria-label="草稿名称" className="w-full rounded-lg border border-border bg-background px-3 py-2" value={value.name} onChange={e => onChange({ ...value, name: e.target.value })} placeholder="例如：缩量回踩，不追高" /></label>
      <label className="space-y-1.5 text-sm"><span className="block font-medium">从模板开始</span><select aria-label="载入策略模板" className="w-full rounded-lg border border-border bg-background px-3 py-2" value="" onChange={e => { const preset = presets.find(p => p.id === e.target.value); if (preset) onChange({ ...value, name: preset.name, buy: preset.buy, sell: preset.sell, avoid: preset.avoid, note: preset.note, config:preset.config||{}, questions: [] }) }}><option value="">选择模板，仅修改草稿</option>{presets.map(preset => <option key={preset.id} value={preset.id}>{preset.name}</option>)}</select></label>
    </div>
    {onAiDraft && <details open={ideaOpen} onToggle={e=>setIdeaOpen(e.currentTarget.open)} className="rounded-xl border border-border p-4"><summary className="cursor-pointer text-sm font-medium">让 AI 把我的想法写成规则</summary><p className="mt-3 text-sm leading-6 text-muted-foreground">描述想买、想卖和绝不买的情形。AI 只生成待确认草稿，不会改变正在使用的策略。</p><label className="mt-3 block"><span className="sr-only">策略想法</span><textarea className="min-h-24 w-full rounded-lg border border-border bg-background p-3 text-sm" aria-label="策略想法" value={idea} onChange={e => setIdea(e.target.value)} placeholder="例如：缩量回踩20日线时考虑买入；放量高开低走且没有修复时不追。" /></label><Button type="button" variant="secondary" className="mt-2" disabled={!idea.trim() || busy} onClick={() => void onAiDraft(idea.trim())}>{busy ? '正在生成…' : '生成规则草稿'}</Button></details>}
    {!!value.questions?.length && <div className="rounded-xl border border-amber-500/40 bg-amber-500/5 p-4 text-sm" role="alert"><p className="font-medium">这些想法还不能直接执行</p><ul className="mt-2 list-disc space-y-1 pl-5">{value.questions.map((question, index) => <li key={index}>{question}</li>)}</ul><p className="mt-3 text-muted-foreground">请补充或调整下面的规则。清除问题不代表 AI 判断正确，应用前仍需核对。</p><Button type="button" size="sm" variant="secondary" className="mt-3" onClick={() => onChange({ ...value, questions: [] })}>已核对并完善规则，清除待确认项</Button></div>}
    {template&&<><TemplateConditionRows template={template} value={value} onChange={onChange} /><section aria-label="模板信号" className="rounded-xl border border-border p-4"><h3 className="font-medium">模板信号（只读）</h3><div className="my-2 text-sm leading-7 text-muted-foreground"><p>入场：{templateSignalDescription(value.config, 'buy')}</p><p>退出：{templateSignalDescription(value.config, 'sell')}</p><p>{templateCaveat(template.id)}</p></div><p className="text-xs text-muted-foreground">这段由模板定义，不能在这里改写；要改逻辑请用「脚本编辑」。</p></section></>}
    {template ? <><section className="space-y-3 border-y border-border py-4" aria-label="可读规则说明"><h3 className="font-medium">完整规则</h3><p className="text-sm">买入：{text.buy}</p><p className="text-sm">退出：{text.sell}</p><p className="text-sm">禁买：{text.avoid}</p></section><details><summary className="min-h-11 cursor-pointer font-medium">附加条件与高级编辑</summary><p className="my-3 text-xs text-muted-foreground">保留模板信号可继续叠加纪律；移除信号会改变策略的含义</p>    <ConditionEditor title="买入：全部满足才入场" emptyHint="不设置买入条件：不产生新买入信号。" value={value.buy} onChange={buy => onChange({ ...value, buy })} fields={fields} />
    <ConditionEditor title="卖出：任一满足即退出" emptyHint="不设置卖出条件：不会根据策略主动退出，期末持仓继续计价。" value={value.sell} onChange={sell => onChange({ ...value, sell })} fields={fields} />
    <ConditionEditor title="禁买：任一命中就不买" emptyHint="不设置禁买条件：不附加禁买限制。" value={value.avoid} onChange={avoid => onChange({ ...value, avoid })} fields={fields} />
</details></> : <>{guided ? <><section className="grid gap-3 sm:grid-cols-3" aria-label="可读规则说明"><div className="rounded-xl border border-border bg-muted/20 p-4"><p className="text-xs text-muted-foreground">什么时候买入</p><p className="mt-2 text-sm leading-7">{text.buy}</p></div><div className="rounded-xl border border-border bg-muted/20 p-4"><p className="text-xs text-muted-foreground">什么时候退出</p><p className="mt-2 text-sm leading-7">{text.sell}</p></div><div className="rounded-xl border border-border bg-muted/20 p-4"><p className="text-xs text-muted-foreground">哪些时候不买</p><p className="mt-2 text-sm leading-7">{text.avoid}</p></div></section><details open={buyOpen} onToggle={e=>setBuyOpen(e.currentTarget.open)}><summary className="flex min-h-11 cursor-pointer items-center gap-2">编辑买入条件<span className="text-xs text-muted-foreground">{value.buy.length ? `（${value.buy.length} 条）` : "（未设置）"}</span></summary>    <ConditionEditor title="买入：全部满足才入场" emptyHint="不设置买入条件：不产生新买入信号。" value={value.buy} onChange={buy => onChange({ ...value, buy })} fields={fields} />
</details><details><summary className="flex min-h-11 cursor-pointer items-center gap-2">编辑退出条件<span className="text-xs text-muted-foreground">{value.sell.length ? `（${value.sell.length} 条）` : "（未设置）"}</span></summary>    <ConditionEditor title="卖出：任一满足即退出" emptyHint="不设置卖出条件：不会根据策略主动退出，期末持仓继续计价。" value={value.sell} onChange={sell => onChange({ ...value, sell })} fields={fields} />
</details><details><summary className="flex min-h-11 cursor-pointer items-center gap-2">编辑禁买条件<span className="text-xs text-muted-foreground">{value.avoid.length ? `（${value.avoid.length} 条）` : "（未设置）"}</span></summary>    <ConditionEditor title="禁买：任一命中就不买" emptyHint="不设置禁买条件：不附加禁买限制。" value={value.avoid} onChange={avoid => onChange({ ...value, avoid })} fields={fields} />
</details></> : <>    <ConditionEditor title="买入：全部满足才入场" emptyHint="不设置买入条件：不产生新买入信号。" value={value.buy} onChange={buy => onChange({ ...value, buy })} fields={fields} />
    <ConditionEditor title="卖出：任一满足即退出" emptyHint="不设置卖出条件：不会根据策略主动退出，期末持仓继续计价。" value={value.sell} onChange={sell => onChange({ ...value, sell })} fields={fields} />
    <ConditionEditor title="禁买：任一命中就不买" emptyHint="不设置禁买条件：不附加禁买限制。" value={value.avoid} onChange={avoid => onChange({ ...value, avoid })} fields={fields} />
</>}</>}
    {template&&<details className="rounded-xl border border-border p-4"><summary className="cursor-pointer font-medium">候选评分与排名</summary><p className="my-3 text-xs text-muted-foreground">用于机会排序和组合选股。按同日股票池做最小最大归一化，权重归一后加总；方向由你指定。单股信号检查不冒充全市场排名。</p>{Object.entries(value.config?.scoring||template.config?.scoring||{}).map(([id,w])=><div key={id} className="mb-3 flex flex-wrap items-center gap-3"><span className="text-sm">{fields.find(f=>f.source_id===id||f.key===`tf_${id}`)?.label||id}</span><ConditionValueInput label={`${id}评分权重`} op="eq" value={w} onChange={n=>updateConfig({scoring:{...value.config?.scoring,[id]:n as number}})}/><select aria-label={`${id}评分方向`} className="min-h-10 rounded border bg-card px-3" value={value.config?.directions?.[id]||'high'} onChange={e=>updateConfig({directions:{...value.config?.directions,[id]:e.target.value as 'high'|'low'}})}><option value="high">越高越优先</option><option value="low">越低越优先</option></select><button type="button" onClick={()=>{const scoring={...value.config?.scoring},directions={...value.config?.directions};delete scoring[id];delete directions[id];updateConfig({scoring,directions})}}>移除</button></div>)}<select aria-label="添加评分因子" value="" className="min-h-10 max-w-full rounded border bg-card px-3" onChange={e=>{if(e.target.value)updateConfig({scoring:{...value.config?.scoring,[e.target.value]:1}})}}><option value="">添加评分因子（最多4个）</option>{fields.filter(f=>f.source_id||f.key.startsWith('tf_')).map(f=><option key={f.key} value={f.source_id||f.key.slice(3)}>{f.label}</option>)}</select></details>}
    <section aria-label="风险与执行" className="rounded-xl border border-border p-4"><h3 className="font-medium">风险与执行</h3><p className="my-2 text-xs text-muted-foreground">按策略建仓成本及交易日计算。日线风险在收盘确认，下一交易日开盘尝试退出，可能跳空且受涨跌停限制。留空关闭对应规则。</p><div className="grid gap-3 sm:grid-cols-3">{([['stop_loss_pct','止损幅度','%'],['take_profit_pct','止盈幅度','%'],['max_hold_days','最长持有',' 个交易日']] as const).map(([key,label,unit])=><label key={key} className="space-y-1 rounded-lg border border-border px-3 py-2"><span className="block text-xs text-muted-foreground">{label}{unit}</span><ConditionValueInput label={label} value={value.config?.execution?.[key]??''} op="eq" onChange={n=>{const execution={...value.config?.execution};if(n==='')delete execution[key];else execution[key]=n as number;updateConfig({execution})}}/></label>)}</div></section>
    <label className="block space-y-1.5 text-sm"><span className="block font-medium">策略说明</span><textarea aria-label="策略说明" rows={2} className="w-full rounded-lg border border-border bg-background p-3" value={value.note} onChange={e => onChange({ ...value, note: e.target.value })} /><span className="block text-xs text-muted-foreground">说明供 AI 理解；只有上面的结构化条件会参与确定性回测。</span></label>
  </fieldset>
}

/** 模板条件行：所有模板统一为「开关 + 字段 + 比较符 + 数值 + 单位」。
 *  注意：这里全部用内联样式，避免新增 Tailwind 类被构建漏掉（开关会塌成白圆）。 */
function TemplateConditionRows({ template, value, onChange }: {
  template: { name: string; paramRows?: Array<{ key: string; gate?: string | null; label: string; control: 'toggle' | 'number'; op?: string; unit?: string; default?: number | boolean; hint?: string }> }
  value: StrategyDraftInput
  onChange: (value: StrategyDraftInput) => void
}) {
  const rows = template.paramRows || []
  if (!rows.length) return null
  const params = value.config?.params || {}
  const set = (key: string, next: unknown) => onChange({ ...value, config: { ...value.config, params: { ...params, [key]: next } } })
  const switchStyle = (on: boolean): React.CSSProperties => ({
    position: 'relative', width: 36, height: 20, borderRadius: 999, border: '1px solid ' + (on ? 'var(--primary, #5b5ef4)' : 'var(--border, #d4d4d8)'),
    background: on ? 'var(--primary, #5b5ef4)' : 'var(--muted, #e5e7eb)', cursor: 'pointer', flex: '0 0 auto', padding: 0,
  })
  const knobStyle = (on: boolean): React.CSSProperties => ({
    position: 'absolute', top: 2, left: on ? 18 : 2, width: 14, height: 14, borderRadius: '50%',
    background: '#fff', transition: 'left .15s', pointerEvents: 'none',
  })
  return <section aria-label="模板条件" className="rounded-xl border border-border p-4">
    <h3 className="font-medium">{template.name} · 条件设置</h3>
    <p className="my-2 text-xs text-muted-foreground">每一行都是一条条件；关掉左侧开关表示这条不参与判断，改动数值后需要重新验证。</p>
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {rows.map(row => {
        const gateOn = row.gate ? Boolean(params[row.gate] ?? true) : true
        const raw = params[row.key] ?? row.default ?? (row.control === 'toggle' ? true : 0)
        return <div key={row.key} className="rounded-lg border border-border" style={{ display: 'flex', alignItems: 'center', gap: 12, minHeight: 44, padding: '8px 12px', opacity: gateOn ? 1 : 0.45 }}>
          {row.gate ? <button type="button" role="switch" aria-checked={gateOn} aria-label={row.hint || row.label} style={switchStyle(gateOn)} onClick={() => set(row.gate as string, !gateOn)}><span style={knobStyle(gateOn)} /></button>
            : row.control === 'toggle' ? <input type="checkbox" aria-label={row.label} style={{ width: 16, height: 16, flex: '0 0 auto' }} checked={Boolean(raw)} onChange={e => set(row.key, e.target.checked)} />
            : <span style={{ width: 36, flex: '0 0 auto' }} />}
          <span style={{ flex: '1 1 auto', minWidth: 0, fontSize: 13 }}>{row.label}</span>
          {row.control === 'number' ? <>
            <span style={{ flex: '0 0 auto', fontSize: 12, color: 'var(--muted-foreground, #71717a)' }}>{row.op || '≥'}</span>
            <input
              aria-label={row.label}
              type="number"
              disabled={!gateOn}
              style={{ width: 88, height: 30, flex: '0 0 auto', textAlign: 'center', borderRadius: 8, border: '1px solid var(--border, #d4d4d8)', background: 'var(--background, #fff)', fontSize: 13 }}
              value={String(raw)}
              onChange={e => { const next = Number(e.target.value); if (Number.isFinite(next)) set(row.key, next) }}
            />
            <span style={{ width: 40, flex: '0 0 auto', fontSize: 11, color: 'var(--muted-foreground, #71717a)' }}>{row.unit || ''}</span>
          </> : null}
        </div>
      })}
    </div>
  </section>
}