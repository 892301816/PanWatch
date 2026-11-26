import type { StrategyCondition } from '@panwatch/api'

/** 字段 → Pine 表达式。value 是阈值；不支持的直接返回 null（生成时降级为注释）。 */
const SIMPLE: Record<string, (value: number, op: string) => string | null> = {
  change_pct: (v, op) => `chg ${opPine(op)} ${v}`,
  open_gap: (v, op) => `gap ${opPine(op)} ${v}`,
  body_pct: (v, op) => `body ${opPine(op)} ${v}`,
  close_pos: (v, op) => `closePos ${opPine(op)} ${v}`,
  volume_ratio_5d: (v, op) => `volRatio ${opPine(op)} ${v}`,
  turnover_rate: (v, op) => `turnover ${opPine(op)} ${v}`,
  rsi_14: (v, op) => `rsi ${opPine(op)} ${v}`,
  ma5_dev: (v, op) => `ma5Dev ${opPine(op)} ${v}`,
  ma20_dev: (v, op) => `ma20Dev ${opPine(op)} ${v}`,
  momentum_20d: (v, op) => `momentum20 ${opPine(op)} ${v}`,
  boll_width: (v, op) => `bollWidth ${opPine(op)} ${v}`,
  annual_vol_20d: (v, op) => `annualVol ${opPine(op)} ${v}`,
  above_ma5: () => 'aboveMa5',
  above_ma20: () => 'aboveMa20',
  above_ma60: () => 'aboveMa60',
  ma_golden_cross: () => 'maGoldenCross',
  ma_bull_alignment: () => 'maBullAlignment',
  ma20_breakout: () => 'ma20Breakout',
  macd_golden_cross: () => 'macdGoldenCross',
  boll_breakout: () => 'bollBreakout',
  close_above_prev: () => 'closeAbovePrev',
}

const LABEL: Record<string, string> = {
  change_pct: '当日涨幅', open_gap: '开盘跳空', body_pct: '日内容体', close_pos: '收盘位置',
  volume_ratio_5d: '量能比', turnover_rate: '换手率', rsi_14: 'RSI(14)', ma5_dev: 'MA5 偏离',
  ma20_dev: 'MA20 偏离', momentum_20d: '20日动量', boll_width: '布林带宽', annual_vol_20d: '年化波动率',
  above_ma5: '收盘价在 MA5 上方', above_ma20: '收盘价在 MA20 上方', above_ma60: '收盘价在 MA60 上方',
  ma_golden_cross: 'MA5 上穿 MA20', ma_bull_alignment: 'MA5>MA20>MA60', ma20_breakout: '突破 MA20',
  macd_golden_cross: 'MACD 金叉', boll_breakout: '突破布林上轨', close_above_prev: '收盘高于昨收',
  consecutive_limit_ups: '连板数', limit_up_locked: '当日涨停', limit_up_count_60d: '近60日涨停次数',
}

const OPS: Record<string, string> = {
  gte: '>=', gt: '>', lte: '<=', lt: '<', eq: '==', ne: '!=',
}

function opPine(op: string): string {
  return OPS[op] || '=='
}

/**
 * 把条件行翻译成 Pine 脚本。
 * 只翻译本引擎能表达的部分；其余字段生成注释，避免脚本看起来可用、实际口径不同。
 */
export function conditionsToPine(
  buy: StrategyCondition[],
  sell: StrategyCondition[],
  avoid: StrategyCondition[],
  name = '条件策略',
): { code: string; unsupported: string[] } {
  const unsupported = new Set<string>()
  const lines: string[] = []
  const flat = (rows: StrategyCondition[]) =>
    rows.flatMap((row) => (row.children?.length ? row.children : [row]))

  const render = (row: StrategyCondition): string | null => {
    const field = String(row.field || '')
    const value = Number(row.value)
    const simple = SIMPLE[field]
    if (!simple) {
      unsupported.add(LABEL[field] || field)
      return null
    }
    const expr = simple(value, String(row.op || 'gte'))
    return expr ?? null
  }

  const buyExprs = flat(buy).map(render).filter((x): x is string => !!x)
  const sellExprs = flat(sell).map(render).filter((x): x is string => !!x)
  const avoidExprs = flat(avoid).map(render).filter((x): x is string => !!x)

  lines.push('//@version=5')
  lines.push(`strategy("${name}", overlay=true)`)
  lines.push('')
  lines.push('// ---- 基础序列 ----')
  lines.push('chg = (close / close[1] - 1) * 100')
  lines.push('gap = (open / close[1] - 1) * 100')
  lines.push('body = (close - open) / open * 100')
  lines.push('closePos = (close - low) / (high - low) * 100')
  lines.push('volRatio = volume / ta.sma(volume, 5)')
  if (flat(buy).concat(flat(sell), flat(avoid)).some((r) => r.field === 'turnover_rate')) {
    lines.push('// 注意：换手率需要流通股本，Pine 无法直接得到；下面用 0 占位，请自行替换')
    lines.push('turnover = 0.0')
  }
  if (flat(buy).concat(flat(sell), flat(avoid)).some((r) => r.field === 'rsi_14')) {
    lines.push('rsi = ta.rsi(close, 14)')
  }
  lines.push('ma5 = ta.sma(close, 5)')
  lines.push('ma20 = ta.sma(close, 20)')
  lines.push('ma60 = ta.sma(close, 60)')
  lines.push('ma5Dev = (close / ma5 - 1) * 100')
  lines.push('ma20Dev = (close / ma20 - 1) * 100')
  lines.push('momentum20 = (close / close[20] - 1) * 100')
  lines.push('mid = ta.sma(close, 20)')
  lines.push('dev = ta.stdev(close, 20)')
  lines.push('bollWidth = dev * 4 / mid * 100')
  lines.push('annualVol = ta.stdev(close / close[1] - 1, 20) * math.sqrt(252) * 100')
  lines.push('aboveMa5 = close > ma5')
  lines.push('aboveMa20 = close > ma20')
  lines.push('aboveMa60 = close > ma60')
  lines.push('maGoldenCross = ta.crossover(ma5, ma20)')
  lines.push('maBullAlignment = ma5 > ma20 and ma20 > ma60')
  lines.push('ma20Breakout = ta.crossover(close, ma20)')
  lines.push('macdGoldenCross = ta.crossover(ta.ema(close, 12) - ta.ema(close, 26), ta.ema(ta.ema(close, 12) - ta.ema(close, 26), 9))')
  lines.push('bollBreakout = close > mid + 2 * dev')
  lines.push('closeAbovePrev = close > close[1]')
  lines.push('')
  lines.push('// ---- 条件 ----')
  lines.push(`longCondition = ${buyExprs.length ? buyExprs.join(' and ') : 'false'}`)
  lines.push(`exitCondition = ${sellExprs.length ? sellExprs.join(' or ') : 'false'}`)
  lines.push(`avoidCondition = ${avoidExprs.length ? avoidExprs.join(' or ') : 'false'}`)
  lines.push('')
  lines.push('if longCondition and not avoidCondition')
  lines.push('    strategy.entry("L", strategy.long)')
  lines.push('if exitCondition')
  lines.push('    strategy.close("L")')
  if (unsupported.size) {
    lines.push('')
    lines.push('// ---- 以下字段本引擎能算，但 Pine 无法等价表达，未翻译 ----')
    for (const item of unsupported) lines.push(`// ${item}`)
  }
  return { code: lines.join('\n'), unsupported: [...unsupported] }
}
