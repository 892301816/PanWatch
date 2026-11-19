import type { TradeIntentTarget, RecordIntent } from '@/components/TradeDecisionDialog'

const prefix = 'panwatch_trade_transfer:'
export type TradeTransfer = RecordIntent & { target: TradeIntentTarget }
export function saveTradeTransfer(value: TradeTransfer): string {
  const id = crypto.randomUUID()
  sessionStorage.setItem(prefix + id, JSON.stringify({ value, expires: Date.now() + 30 * 60 * 1000 }))
  return id
}
export function consumeTradeTransfer(id: string): TradeTransfer {
  const raw = sessionStorage.getItem(prefix + id)
  if (!raw) throw new Error('待记账参数不存在，请从策略中心重新进入')
  sessionStorage.removeItem(prefix + id)
  const stored = JSON.parse(raw)
  if (!stored.value || stored.expires < Date.now()) throw new Error('待记账参数已过期，请重新核对成交信息')
  return stored.value
}
