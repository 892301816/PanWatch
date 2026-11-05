import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import WealthSummary from '@/components/WealthSummary'

afterEach(cleanup)

const total = {
  total_assets: 528640,
  total_market_value: 412800,
  available_funds: 115840,
  total_daily_pnl: -2386,
  total_pnl: 28640,
  total_pnl_pct: 7.46,
}

describe('WealthSummary', () => {
  it('shows the selected account scope and formats negative PnL with the sign before the currency', () => {
    render(<WealthSummary total={total} accounts={1} positions={5} scopeLabel="长期配置" />)
    expect(screen.getByText('长期配置')).toBeTruthy()
    expect(screen.getByText('−¥2,386.00')).toBeTruthy()
    expect(screen.getByText('+¥28,640.00')).toBeTruthy()
    expect(screen.getByText('¥528,640.00')).toBeTruthy()
    expect(screen.getByText(/仓位 78.1%/)).toBeTruthy()
  })

  it('supports local privacy and removes value and directional hints until the user reveals them', () => {
    const onPrivacyChange = vi.fn()
    const { container } = render(<WealthSummary total={total} accounts={2} positions={5} onPrivacyChange={onPrivacyChange} />)
    fireEvent.click(screen.getByRole('button', { name: '隐藏金额' }))
    expect(onPrivacyChange).toHaveBeenCalledWith(true)
    expect(screen.queryByText('¥528,640.00')).toBeNull()
    expect(screen.queryByText('−¥2,386.00')).toBeNull()
    expect(screen.queryByText(/78.1%/)).toBeNull()
    expect(container.querySelector('.wealth-positive')).toBeNull()
    expect(container.querySelector('.wealth-negative')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '显示金额' }))
    expect(screen.getByText('¥528,640.00')).toBeTruthy()
  })

  it('leaves controlled privacy to the page so one setting can cover summaries and lists', () => {
    const onPrivacyChange = vi.fn()
    const { rerender } = render(<WealthSummary total={total} accounts={2} positions={5} privateMode onPrivacyChange={onPrivacyChange} />)
    fireEvent.click(screen.getByRole('button', { name: '显示金额' }))
    expect(onPrivacyChange).toHaveBeenCalledWith(false)
    expect(screen.queryByText('¥528,640.00')).toBeNull()
    rerender(<WealthSummary total={total} accounts={2} positions={5} privateMode={false} onPrivacyChange={onPrivacyChange} />)
    expect(screen.getByText('¥528,640.00')).toBeTruthy()
  })

  it('represents missing or invalid amounts and percentages as unknown without leaking NaN or infinity', () => {
    const invalid = { total_assets: null, total_market_value: undefined, available_funds: Number.POSITIVE_INFINITY, total_daily_pnl: Number.NaN, total_pnl: null, total_pnl_pct: Number.NEGATIVE_INFINITY }
    const { container } = render(<WealthSummary total={invalid} accounts={0} positions={0} />)
    expect(screen.getAllByText('--')).toHaveLength(4)
    expect(container.textContent).toContain('持仓市值 --')
    expect(container.textContent).toContain('仓位 --')
    expect(container.textContent).not.toMatch(/NaN|Infinity|¥-/)
  })
})
