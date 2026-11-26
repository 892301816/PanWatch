import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import StrategyRuleEditor from '@panwatch/biz-ui/components/StrategyRuleEditor'

const preset = {
  id: 'macd_below_zero_revival',
  name: 'MACD 零下回升',
  params: [],
  paramRows: [
    { key: 'low_window', label: '新低回看天数', control: 'number', op: '≥', unit: '天', default: 20 },
    { key: 'use_x', label: '要求收盘价在 MA60 上方', control: 'toggle', default: true },
  ],
  buy: [],
  sell: [],
  avoid: [],
} as never

const draft = {
  name: 'MACD 零下回升',
  scope: 'global',
  symbol: '',
  market: 'CN',
  note: '',
  buy: [],
  sell: [],
  avoid: [],
  questions: [],
  config: { template_id: 'macd_below_zero_revival', params: { low_window: 20, use_x: true } },
} as never

describe('模板条件行', () => {
  it('数字行渲染出可编辑输入框，布尔行渲染出勾选框', () => {
    render(
      <StrategyRuleEditor
        value={draft}
        onChange={() => undefined}
        fields={[]}
        presets={[preset]}
      />,
    )
    const numbers = screen.getAllByLabelText('新低回看天数') as HTMLInputElement[]
    const number = numbers.find(n => n.type === 'number') as HTMLInputElement
    expect(number).toBeTruthy()
    expect(number.type).toBe('number')
    expect(number.value).toBe('20')
    expect(number.disabled).toBe(false)
    expect(number.style.width).toBe('88px')
    expect(number.style.border).toContain('hsl(var(--border))')
    const toggles = screen.getAllByLabelText('要求收盘价在 MA60 上方') as HTMLInputElement[]
    const toggle = toggles.find(n => n.type === 'checkbox') as HTMLInputElement
    expect(toggle.type).toBe('checkbox')
  })
})
