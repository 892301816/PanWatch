import { useState } from 'react'
import { Eye, EyeOff } from 'lucide-react'

type Amount = number | null | undefined

interface Totals {
  total_assets: Amount
  total_market_value: Amount
  available_funds: Amount
  total_daily_pnl: Amount
  total_pnl: Amount
  total_pnl_pct: Amount
}

interface WealthSummaryProps {
  total: Totals
  accounts: number
  positions: number
  scopeLabel?: string
  privateMode?: boolean
  onPrivacyChange?: (value: boolean) => void
}

const isAmount = (value: Amount): value is number => typeof value === 'number' && Number.isFinite(value)
const numberFormat = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

export default function WealthSummary({
  total,
  accounts,
  positions,
  scopeLabel = '全部账户',
  privateMode: controlledPrivateMode,
  onPrivacyChange,
}: WealthSummaryProps) {
  const [localPrivateMode, setLocalPrivateMode] = useState(false)
  const privateMode = controlledPrivateMode ?? localPrivateMode

  const togglePrivacy = () => {
    const next = !privateMode
    if (controlledPrivateMode === undefined) setLocalPrivateMode(next)
    onPrivacyChange?.(next)
  }

  const money = (value: Amount, signed = false) => {
    if (!isAmount(value)) return '--'
    if (privateMode) return '••••••'
    const sign = value < 0 ? '−' : signed && value > 0 ? '+' : ''
    return `${sign}¥${numberFormat.format(Math.abs(value))}`
  }

  const percent = (value: Amount, signed = false) => {
    if (!isAmount(value)) return '--'
    if (privateMode) return '••••'
    const sign = value < 0 ? '−' : signed && value > 0 ? '+' : ''
    return `${sign}${Math.abs(value).toFixed(signed ? 2 : 1)}%`
  }

  const pnlClass = (value: Amount) => {
    if (privateMode || !isAmount(value) || value === 0) return ''
    return value > 0 ? 'wealth-positive' : 'wealth-negative'
  }

  const ratio = isAmount(total.total_assets) && total.total_assets > 0 && isAmount(total.total_market_value)
    ? total.total_market_value / total.total_assets * 100
    : null

  return (
    <section className="wealth-summary" aria-label="资产概况">
      <div className="wealth-stat wealth-stat-total">
        <div className="wealth-stat-label">
          <span>总资产 <span className="wealth-summary-scope" title={scopeLabel}>{scopeLabel}</span></span>
          <button
            type="button"
            className="wealth-privacy"
            onClick={togglePrivacy}
            aria-pressed={privateMode}
            aria-label={privateMode ? '显示金额' : '隐藏金额'}
            title={privateMode ? '显示金额' : '隐藏金额'}
          >
            {privateMode ? <EyeOff size={15} aria-hidden="true" /> : <Eye size={15} aria-hidden="true" />}
          </button>
        </div>
        <strong className="wealth-stat-value">{money(total.total_assets)}</strong>
        <span className="wealth-stat-note">持仓市值 {money(total.total_market_value)}</span>
      </div>
      <div className="wealth-stat">
        <div className="wealth-stat-label">今日盈亏</div>
        <strong className={`wealth-stat-value ${pnlClass(total.total_daily_pnl)}`}>{money(total.total_daily_pnl, true)}</strong>
        <span className="wealth-stat-note">人民币折算 · 持仓口径</span>
      </div>
      <div className="wealth-stat">
        <div className="wealth-stat-label">持仓浮动盈亏</div>
        <strong className={`wealth-stat-value ${pnlClass(total.total_pnl)}`}>{money(total.total_pnl, true)}</strong>
        <span className={`wealth-stat-note ${pnlClass(total.total_pnl_pct)}`}>相对持仓成本 {percent(total.total_pnl_pct, true)}</span>
      </div>
      <div className="wealth-stat">
        <div className="wealth-stat-label">可用资金</div>
        <strong className="wealth-stat-value">{money(total.available_funds)}</strong>
        <span className="wealth-stat-note">仓位 {percent(ratio)} · {accounts} 个账户 / {positions} 只持仓</span>
      </div>
    </section>
  )
}
