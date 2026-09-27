import { fetchAPI } from './client'

/** 操作计划里的一个情景。文本字段给人看,量化字段驱动提醒。 */
export interface OperationScenario {
  title: string
  condition: string
  action_type: 'buy' | 'add' | 'reduce' | 'stop_loss' | 'observe'
  action: string
  position: string
  buy_price_range: string
  stop_loss: string
  target1: string
  target2: string
  strategy: string
  is_best: boolean
  // 量化价位(元)。0 表示该情景不监控此项,界面上不提供"加入提醒"按钮。
  entry_low: number
  entry_high: number
  stop_loss_num: number
  target1_low: number
  target1_high: number
  target2_low: number
  target2_high: number
}

export interface OperationDiscipline {
  principle: string
  detail: string
}

export interface OperationPlan {
  id: number
  stock_symbol: string
  stock_name: string
  market: string
  plan_date: string
  plan_end_date: string
  agent_name: string
  overall_judgment: string
  scenarios: OperationScenario[]
  discipline: OperationDiscipline[]
  summary: string
  risk_warning: string
  content: string
  status: 'pending' | 'executing' | 'completed' | 'expired'
  remarks: string
  created_at: string
  updated_at: string
  /** 该计划已创建的提醒数(仅 /latest 返回) */
  alert_count?: number
  /** 该计划创建的提醒明细(仅 /{id} 返回) */
  alerts?: Array<{
    id: number
    name: string
    source_label: string
    enabled: boolean
    condition_group: unknown
    expire_at: string | null
  }>
}

/** 要加入提醒的价位:情景下标 + 价位类型。 */
export interface PlanAlertItem {
  scenario_index: number
  level: 'stop_loss' | 'target1' | 'target2' | 'entry'
}

export interface PlanAlertResult {
  created: Array<{
    id: number
    name: string
    source_label?: string
    /**
     * 该提醒的判定条件当前是否已经成立。提醒引擎是状态判定而非穿越判定,条件一成立
     * 下一轮扫描就会触发 —— 可能正是你要的(已跌穿止损),也可能是误导(尚未突破时
     * 的移动止损),故如实标注、由你判断。
     */
    already_true?: boolean
    condition_group?: { op: string; items: Array<{ type: string; op: string; value: unknown }> }
  }>
  /** 被跳过的项及原因(如"现价已达成该目标"),用于如实告知用户 */
  rejected: Array<{ level: string; title?: string; reason: string }>
  current_price: number | null
  expire_at: string
}

export const operationPlansApi = {
  /** 历史留档:按计划日期倒序。不传 symbol 即全部。 */
  list: (params: {
    symbol?: string
    market?: string
    start_date?: string
    end_date?: string
    limit?: number
  } = {}) => {
    const q = new URLSearchParams()
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && String(v).trim()) q.set(k, String(v))
    })
    const s = q.toString()
    return fetchAPI<OperationPlan[]>(`/operation-plans${s ? `?${s}` : ''}`)
  },

  /** 某只股票最近一份计划;没有则返回 null。 */
  latest: (symbol: string, market: string) =>
    fetchAPI<OperationPlan | null>(
      `/operation-plans/latest?symbol=${encodeURIComponent(symbol)}&market=${encodeURIComponent(market)}`
    ),

  get: (id: number) => fetchAPI<OperationPlan>(`/operation-plans/${id}`),

  remove: (id: number, deleteAlerts = false) =>
    fetchAPI<{ ok: boolean; removed_alerts: number }>(
      `/operation-plans/${id}?delete_alerts=${deleteAlerts}`,
      { method: 'DELETE' }
    ),

  setStatus: (id: number, status: string, remarks = '') =>
    fetchAPI<OperationPlan>(
      `/operation-plans/${id}/status?status=${encodeURIComponent(status)}&remarks=${encodeURIComponent(remarks)}`,
      { method: 'PUT' }
    ),

  /** 一键把计划里的价位加进提醒。整批原子提交,返回被跳过的项及原因。 */
  addAlerts: (planId: number, items: PlanAlertItem[]) =>
    fetchAPI<PlanAlertResult>('/price-alerts/from-plan', {
      method: 'POST',
      body: JSON.stringify({ plan_id: planId, items }),
    }),
}
