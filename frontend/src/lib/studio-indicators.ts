import type { StudioBar } from '@panwatch/api'

type Spec = {
  /** 面板里显示的中文名 */
  title: string
  /** 参数名，顺序与 defaults 一致 */
  args: string[]
  defaults: number[]
  /** true = 叠加到主图，false = 独立副图 */
  price: boolean
  /** 每个参数的取值范围，缺省 [2, 250] */
  limits?: Array<[number, number]>
  /** 该参数允许小数（如布林倍数、偏离百分比） */
  decimal?: boolean[]
}

export const indicators = {
  /* ---- 主图叠加 ---- */
  MA: { title: '简单均线', args: ['周期'], defaults: [20], price: true },
  EMA: { title: '指数均线', args: ['周期'], defaults: [12], price: true },
  WMA: { title: '加权均线', args: ['周期'], defaults: [20], price: true },
  BOLL: {
    title: '布林带',
    args: ['周期', '倍数'],
    defaults: [20, 2],
    price: true,
    limits: [
      [2, 250],
      [0.1, 10],
    ],
    decimal: [false, true],
  } as Spec,
  ENV: {
    title: '轨道线 ENV',
    args: ['周期', '偏离%'],
    defaults: [14, 6],
    price: true,
    limits: [
      [2, 250],
      [0.1, 50],
    ],
    decimal: [false, true],
  } as Spec,
  BBI: {
    title: '多空指数 BBI',
    args: ['周期1', '周期2', '周期3', '周期4'],
    defaults: [3, 6, 12, 24],
    price: true,
    limits: [
      [2, 250],
      [2, 250],
      [2, 250],
      [2, 250],
    ],
  } as Spec,

  /* ---- 副图指标 ---- */
  VOL: { title: '成交量', args: [], defaults: [], price: false },
  VMA: { title: '成交量均线', args: ['周期'], defaults: [5], price: false },
  MACD: {
    title: '趋势动量 MACD',
    args: ['快', '慢', '信号'],
    defaults: [12, 26, 9],
    price: false,
  },
  RSI: { title: '相对强弱 RSI', args: ['周期'], defaults: [14], price: false },
  KDJ: {
    title: '随机指标 KDJ',
    args: ['周期', 'K平滑', 'D平滑'],
    defaults: [9, 3, 3],
    price: false,
  },
  WR: { title: '威廉指标 WR', args: ['周期'], defaults: [14], price: false },
  CCI: { title: '顺势指标 CCI', args: ['周期'], defaults: [14], price: false },
  ATR: { title: '真实波幅 ATR', args: ['周期'], defaults: [14], price: false },
  OBV: { title: '能量潮 OBV', args: [], defaults: [], price: false },
  BIAS: { title: '乖离率 BIAS', args: ['周期'], defaults: [6], price: false },
  ROC: { title: '变动率 ROC', args: ['周期'], defaults: [12], price: false },
  MTM: {
    title: '动量 MTM',
    args: ['周期', '均线'],
    defaults: [12, 6],
    price: false,
  },
  DMA: {
    title: '平行线差 DMA',
    args: ['快', '慢', '均线'],
    defaults: [10, 50, 10],
    price: false,
  },
  TRIX: {
    title: '三重指数 TRIX',
    args: ['周期', '均线'],
    defaults: [12, 9],
    price: false,
  },
  PSY: {
    title: '心理线 PSY',
    args: ['周期', '均线'],
    defaults: [12, 6],
    price: false,
  },
  DMI: {
    title: '趋向指标 DMI',
    args: ['周期', 'ADX平滑'],
    defaults: [14, 6],
    price: false,
  },
} as const satisfies Record<string, Spec>

export type Kind = keyof typeof indicators
export type Study = {
  id: string
  kind: Kind
  args: number[]
  place: 'main' | 'pane'
}
export type Track = { name: string; values: (number | null)[]; hist?: boolean }

export const KIND_LIST = Object.keys(indicators) as Kind[]

export const titleOf = (s: Study) =>
  s.kind + (s.args.length ? '(' + s.args.join(',') + ')' : '')

const specOf = (kind: Kind): Spec => indicators[kind] as unknown as Spec

export function valid(s: Study) {
  const spec = indicators[s?.kind]
  if (
    !spec ||
    typeof s.id !== 'string' ||
    !['pane', 'main'].includes(s.place) ||
    !Array.isArray(s.args) ||
    s.args.length !== spec.defaults.length
  )
    return false
  const view = specOf(s.kind)
  const ok = s.args.every((v, i) => {
    if (!Number.isFinite(v)) return false
    const [min, max] = view.limits?.[i] ?? [2, 250]
    if (v < min || v > max) return false
    return view.decimal?.[i] ? true : Number.isInteger(v)
  })
  if (!ok) return false
  if (s.kind === 'MACD' && s.args[0] >= s.args[1]) return false
  if (s.kind === 'DMA' && s.args[0] >= s.args[1]) return false
  return true
}

export function readStudies(): Study[] {
  try {
    const s = JSON.parse(localStorage.getItem('pw:studies:v1') || 'null')
    if (Array.isArray(s) && s.every(valid)) return s
  } catch {}
  return [{ id: 'volume', kind: 'VOL', args: [], place: 'pane' }]
}

/* ------------------------------------------------------------------ 基础算子 */

type Series = (number | null)[]

/** 简单移动平均：只输出完整窗口 */
function ma(values: Series, n: number): Series {
  let sum = 0
  return values.map((v, i) => {
    sum += v ?? 0
    if (i >= n) sum -= values[i - n] ?? 0
    return i >= n - 1 && v !== null ? sum / n : null
  })
}

/** 加权移动平均（最新一根权重最大） */
function wma(values: Series, n: number): Series {
  const weight = (n * (n + 1)) / 2
  return values.map((_, i) => {
    if (i < n - 1) return null
    let total = 0
    for (let k = 0; k < n; k++) {
      const item = values[i - k]
      if (item === null) return null
      total += item * (n - k)
    }
    return total / weight
  })
}

/** EMA，用前 n 个点的均值做种子（与原有指标口径一致） */
function ema(values: Series, n: number): Series {
  let sum = 0
  let count = 0
  let prev: number | null = null
  return values.map((v) => {
    if (v === null) return null
    if (count < n) {
      sum += v
      count++
      if (count === n) prev = sum / n
    } else prev = prev! + ((v - prev!) * 2) / (n + 1)
    return prev
  })
}

/** 通达信 SMA(X, N, 1)：Y = (X + (N-1)·Y') / N */
function wilder(values: Series, n: number): Series {
  let prev: number | null = null
  return values.map((v) => {
    if (v === null) return null
    prev = prev === null ? v : (v + (n - 1) * prev) / n
    return prev
  })
}

function hhv(values: Series, n: number): Series {
  return values.map((_, i) => {
    if (i < n - 1) return null
    let max = -Infinity
    for (let k = i - n + 1; k <= i; k++) {
      const item = values[k]
      if (item === null) return null
      if (item > max) max = item
    }
    return max
  })
}

function llv(values: Series, n: number): Series {
  return values.map((_, i) => {
    if (i < n - 1) return null
    let min = Infinity
    for (let k = i - n + 1; k <= i; k++) {
      const item = values[k]
      if (item === null) return null
      if (item < min) min = item
    }
    return min
  })
}

function sumN(values: Series, n: number): Series {
  let sum = 0
  return values.map((v, i) => {
    sum += v ?? 0
    if (i >= n) sum -= values[i - n] ?? 0
    return i >= n - 1 ? sum : null
  })
}

function diff(a: Series, b: Series): Series {
  return a.map((v, i) => (v === null || b[i] === null ? null : v - b[i]!))
}

function ratio(a: Series, b: Series, scale = 1): Series {
  return a.map((v, i) =>
    v === null || b[i] === null || b[i] === 0 ? null : (v / b[i]!) * scale,
  )
}

function trueRange(bars: StudioBar[]): Series {
  return bars.map((bar, i) => {
    if (!i) return bar.high - bar.low
    const prev = bars[i - 1].close
    return Math.max(
      bar.high - bar.low,
      Math.abs(bar.high - prev),
      Math.abs(bar.low - prev),
    )
  })
}

/* -------------------------------------------------------------------- 计算 */

export function calculate(s: Study, bars: StudioBar[]): Track[] {
  const spec = specOf(s.kind)
  const [n = 0, m = 0, k = 0] = s.args
  const name = titleOf(s)
  const close: number[] = bars.map((b) => b.close)
  const high: number[] = bars.map((b) => b.high)
  const low: number[] = bars.map((b) => b.low)
  const volume: number[] = bars.map((b) => b.volume)

  switch (s.kind) {
    case 'VOL':
      return [{ name: '成交量', values: volume, hist: true }]

    case 'VMA':
      return [
        { name: '成交量', values: volume, hist: true },
        { name: '量均线' + m, values: ma(volume, m || n) },
      ]

    case 'MA':
      return [{ name, values: ma(close, n) }]

    case 'EMA':
      return [{ name, values: ema(close, n) }]

    case 'WMA':
      return [{ name, values: wma(close, n) }]

    case 'BBI': {
      const periods = s.args.length ? s.args : spec.defaults
      const lines = periods.map((p) => ma(close, p))
      const values = close.map((_, i) => {
        const ready = lines.every((line) => line[i] !== null)
        if (!ready) return null
        return lines.reduce((sum, line) => sum + line[i]!, 0) / lines.length
      })
      return [{ name, values }]
    }

    case 'BOLL': {
      let sum = 0
      let squares = 0
      const upper: Series = []
      const lower: Series = []
      const mean = close.map((v, i) => {
        sum += v
        squares += v * v
        if (i >= n) {
          sum -= close[i - n]
          squares -= close[i - n] ** 2
        }
        const ready = i >= n - 1
        const mid = sum / n
        if (ready) {
          const band = m * Math.sqrt(Math.max(0, squares / n - mid * mid))
          upper.push(mid + band)
          lower.push(mid - band)
        } else {
          upper.push(null)
          lower.push(null)
        }
        return ready ? mid : null
      })
      return [
        { name: '中轨', values: mean },
        { name: '上轨', values: upper },
        { name: '下轨', values: lower },
      ]
    }

    case 'ENV': {
      const base = ma(close, n)
      const pct = m / 100
      return [
        { name: '中轨', values: base },
        { name: '上轨', values: base.map((v) => (v === null ? null : v * (1 + pct))) },
        { name: '下轨', values: base.map((v) => (v === null ? null : v * (1 - pct))) },
      ]
    }

    case 'MACD': {
      const quick = ema(close, n)
      const slow = ema(close, m)
      const dif = diff(quick, slow)
      const dea = ema(dif, k)
      return [
        {
          name: 'MACD',
          hist: true,
          values: dif.map((v, i) => (v === null || dea[i] === null ? null : 2 * (v - dea[i]!))),
        },
        { name: 'DIF', values: dif },
        { name: 'DEA', values: dea },
      ]
    }

    case 'RSI': {
      let gain = 0
      let loss = 0
      const values = close.map((v, i) => {
        if (!i) return null
        const delta = v - close[i - 1]!
        const up = Math.max(delta, 0)
        const down = Math.max(-delta, 0)
        gain = i <= n ? gain + up / n : ((n - 1) * gain + up) / n
        loss = i <= n ? loss + down / n : ((n - 1) * loss + down) / n
        if (i < n) return null
        return gain + loss === 0 ? 50 : (100 * gain) / (gain + loss)
      })
      return [{ name, values }]
    }

    case 'KDJ': {
      const hh = hhv(high, n)
      const ll = llv(low, n)
      const rsv: Series = close.map((c, i) => {
        if (hh[i] === null || ll[i] === null) return null
        const span = hh[i]! - ll[i]!
        return span === 0 ? 50 : ((c - ll[i]!) / span) * 100
      })
      const K = wilder(rsv, m || 3)
      const D = wilder(K, k || 3)
      const J = K.map((v, i) => (v === null || D[i] === null ? null : 3 * v - 2 * D[i]!))
      return [
        { name: 'K', values: K },
        { name: 'D', values: D },
        { name: 'J', values: J },
      ]
    }

    case 'WR': {
      const hh = hhv(high, n)
      const ll = llv(low, n)
      const values = close.map((c, i) => {
        if (hh[i] === null || ll[i] === null) return null
        const span = hh[i]! - ll[i]!
        return span === 0 ? 50 : ((hh[i]! - c) / span) * 100
      })
      return [{ name, values }]
    }

    case 'CCI': {
      const tp: number[] = bars.map((b) => (b.high + b.low + b.close) / 3)
      const mid = ma(tp, n)
      const deviation = tp.map((_, i) => {
        if (mid[i] === null) return null
        let total = 0
        for (let j = i - n + 1; j <= i; j++) total += Math.abs(tp[j]! - mid[i]!)
        return total / n
      })
      const values = tp.map((v, i) => {
        if (mid[i] === null || deviation[i] === null) return null
        if (deviation[i] === 0) return 0
        return (v - mid[i]!) / (0.015 * deviation[i]!)
      })
      return [{ name, values }]
    }

    case 'ATR':
      return [{ name, values: wilder(trueRange(bars), n) }]

    case 'OBV': {
      let total = 0
      const values = bars.map((bar, i) => {
        if (i) {
          const delta = bar.close - bars[i - 1].close
          if (delta > 0) total += bar.volume
          else if (delta < 0) total -= bar.volume
        }
        return total
      })
      return [{ name: 'OBV', values }]
    }

    case 'BIAS': {
      const base = ma(close, n)
      return [{ name, values: ratio(diff(close, base), base, 100) }]
    }

    case 'ROC': {
      const values = close.map((c, i) => {
        if (i < n) return null
        const past = close[i - n]
        return past === null || past === 0 ? null : ((c - past) / past) * 100
      })
      return [{ name, values }]
    }

    case 'MTM': {
      const values = close.map((c, i) => (i < n ? null : c - close[i - n]!))
      return [
        { name: 'MTM', values },
        { name: 'MTMMA' + (m || 6), values: ma(values, m || 6) },
      ]
    }

    case 'DMA': {
      const fast = ma(close, n)
      const slow = ma(close, m)
      const dif = diff(fast, slow)
      return [
        { name: 'DIF', values: dif },
        { name: 'AMA' + (k || 10), values: ma(dif, k || 10) },
      ]
    }

    case 'TRIX': {
      const triple = ema(ema(ema(close, n), n), n)
      const trix = triple.map((v, i) => {
        if (i < 1 || v === null || triple[i - 1] === null || triple[i - 1] === 0)
          return null
        return ((v - triple[i - 1]!) / triple[i - 1]!) * 100
      })
      return [
        { name: 'TRIX', values: trix },
        { name: 'MATRIX' + (m || 9), values: ma(trix, m || 9) },
      ]
    }

    case 'PSY': {
      const values = close.map((_, i) => {
        if (i < n) return null
        let up = 0
        for (let j = i - n + 1; j <= i; j++) {
          if (close[j]! > close[j - 1]!) up++
        }
        return (up / n) * 100
      })
      return [
        { name: 'PSY', values },
        { name: 'PSYMA' + (m || 6), values: ma(values, m || 6) },
      ]
    }

    case 'DMI': {
      const tr = trueRange(bars)
      const plusDM: Series = bars.map((bar, i) => {
        if (!i) return 0
        const up = bar.high - bars[i - 1].high
        const down = bars[i - 1].low - bar.low
        return up > down && up > 0 ? up : 0
      })
      const minusDM: Series = bars.map((bar, i) => {
        if (!i) return 0
        const up = bar.high - bars[i - 1].high
        const down = bars[i - 1].low - bar.low
        return down > up && down > 0 ? down : 0
      })
      const trSum = sumN(tr, n)
      const diPlus = ratio(sumN(plusDM, n), trSum, 100)
      const diMinus = ratio(sumN(minusDM, n), trSum, 100)
      const dx = diPlus.map((p, i) => {
        const neg = diMinus[i]
        if (p === null || neg === null || p + neg === 0) return null
        return (Math.abs(p - neg) / (p + neg)) * 100
      })
      const adx = ma(dx, m || 6)
      const adxr = adx.map((v, i) => {
        const lag = i - (m || 6)
        if (v === null || lag < 0 || adx[lag] === null) return null
        return (v + adx[lag]!) / 2
      })
      return [
        { name: '+DI', values: diPlus },
        { name: '-DI', values: diMinus },
        { name: 'ADX', values: adx },
        { name: 'ADXR', values: adxr },
      ]
    }

    default:
      return [{ name, values: close.map(() => null) }]
  }
}

/** 指标默认落点：价格类叠加主图，其余进副图 */
export const defaultPlace = (kind: Kind): Study['place'] =>
  indicators[kind].price ? 'main' : 'pane'
