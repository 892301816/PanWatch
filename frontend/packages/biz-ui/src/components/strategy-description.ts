import type { StrategyConfig, StrategyDraftInput, StrategyField, StrategyPreset } from '@panwatch/api'
import { rulesText } from './strategy-rules'

// These descriptions name signal families. Editable thresholds are shown separately,
// so changing a template parameter cannot leave a default threshold in the summary.
const families: Record<string, [string, string, string]> = {
  active_limit_gene: ['短线动量', '涨停活跃度、当日涨幅与量能', '近60日涨停次数低于当前最少次数减一'],
  boll_breakout: ['趋势突破', '布林上轨突破与量能', '收盘价低于布林下轨'],
  breakout_new_high_60d: ['趋势突破', '突破前60日高点、涨幅与量能', '收盘价低于20日均线'],
  broken_board_recovery: ['短线动量', '断板后的反包形态、涨幅与量能', '收盘价从上向下跌破20日均线'],
  bullish_alignment: ['趋势跟随', '均线多头排列与正向动量', 'MA5下穿MA20，或收盘价向下跌破MA20'],
  consecutive_limit_ups: ['短线动量', '当日涨停与连续涨停次数', '无技术退出信号，按风险退出和持有期限处理'],
  factor_rank_research: ['多因子选股', '同日股票池的因子评分、分数门槛与排名', '评分降至退出阈值或以下'],
  high_turnover_surge: ['短线动量', '换手活跃度与当日涨幅', '收盘价从上向下跌破20日均线'],
  limit_up_momentum: ['短线动量', '涨停动量、当日涨幅与连板次数', '无技术退出信号，按风险退出和持有期限处理'],
  long_lower_shadow_reversal: ['回调反弹', '前期回落、长下影与实体修复', '收盘价低于5日均线'],
  low_volatility_leader: ['趋势跟随', '正向动量、低波动与均线位置', '收盘价从上向下跌破20日均线'],
  ma_convergence_breakout: ['趋势突破', '均线收拢后的价格与量能突破', '收盘价低于10日均线'],
  ma_golden_cross: ['趋势跟随', 'MA5与MA20交叉、量能和长期均线过滤', 'MA5从上向下穿过MA20'],
  macd_below_zero_revival: ['回调反弹', '价格新低与MACD动能修复', 'MACD DIF从下向上越过零轴'],
  macd_golden: ['趋势跟随', 'MACD金叉与量能过滤', 'MACD出现死叉'],
  n_day_low_reversal: ['回调反弹', '阶段新低后的阳线与放量修复', '收盘价从上向下跌破20日均线'],
  near_limit_up: ['短线动量', '当日涨幅与距离涨停的幅度', '收盘价从上向下跌破20日均线'],
  oversold_bounce: ['回调反弹', 'RSI超卖、阳线与量能', '收盘价从上向下跌破20日均线'],
  oversold_reversal: ['回调反弹', 'RSI超卖、涨幅与5日均线位置', '收盘价从上向下跌破20日均线'],
  platform_consolidation_breakout: ['趋势突破', '窄幅整理后的平台与量能突破', '收盘价低于20日均线'],
  pullback_ma20_bounce: ['回调反弹', '20日均线附近的回踩与趋势过滤', '收盘价向下跌破MA20，或MA5下穿MA20'],
  pullback_to_support: ['回调反弹', '20日均线附近的缩量与动量过滤', '收盘价从上向下跌破20日均线'],
  rsi_midline_pullback: ['回调反弹', '长期趋势中的RSI中轴回踩', '收盘价低于20日均线'],
  strong_open: ['短线动量', '高开、收盘位置与涨幅过滤', '收盘价从上向下跌破20日均线'],
  trend_breakout: ['趋势突破', '长期均线、新高与量能过滤', '收盘价从上向下跌破20日均线'],
  volume_price_surge: ['趋势突破', '20日均线突破、阳线与量能', '收盘价从上向下跌破20日均线'],
}

export function templateCategory(id?: string) { return families[id || '']?.[0] || '交易纪律' }
export function templateCaveat(id?: string) {
  const group = templateCategory(id)
  return ({ 趋势跟随: '震荡行情可能反复触发，注意交易次数和成本', 趋势突破: '突破可能失效，需要核对退出与跳空风险', 回调反弹: '下跌趋势可能继续，回调并不代表已经企稳', 短线动量: '波动、涨跌停和流动性会影响成交', 多因子选股: '需要同日股票池，单股回测不能验证排名效果' } as Record<string, string>)[group] || '先核对可执行条件，再用历史数据验证'
}

export function templateSignalDescription(config: StrategyConfig | undefined, direction: 'buy' | 'sell') {
  const family = families[config?.template_id || '']
  return family ? family[direction === 'buy' ? 1 : 2] : '查看模板定义与当前参数'
}

export function strategySummary(draft: Pick<StrategyDraftInput, 'buy' | 'sell' | 'avoid' | 'config'>, fields: StrategyField[], presets: StrategyPreset[]) {
  const preset = presets.find(p => p.id === draft.config?.template_id)
  const describe = (direction: 'buy' | 'sell' | 'avoid') => {
    const enriched = fields.map(f => preset && (f.key === 'template_entry' || f.key === 'template_exit') ? { ...f, label: `${preset.name} · ${templateSignalDescription(draft.config, f.key === 'template_entry' ? 'buy' : 'sell')}` } : f)
    return rulesText(draft[direction], enriched, direction === 'buy' ? '尚未设置买入条件' : direction === 'sell' ? '未设置技术卖出条件' : '无额外禁买条件', direction === 'buy' ? ' 且 ' : ' 或 ')
  }
  return { buy: describe('buy'), sell: describe('sell'), avoid: describe('avoid') }
}
