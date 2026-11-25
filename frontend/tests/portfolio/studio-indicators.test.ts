import { describe, expect, it } from 'vitest'
import {
  calculate,
  valid,
  type Kind,
  type Study,
} from '@/lib/studio-indicators'
import type { StudioBar } from '@panwatch/api'

const bars = (closes: number[]): StudioBar[] =>
  closes.map((close, i) => ({
    date: String(i),
    open: close,
    high: close,
    low: close,
    close,
    volume: i + 1,
  }))
const study = (kind: Kind, args: number[]): Study => ({
  id: 'sample',
  kind,
  args,
  place: 'main',
})
describe('chart study calculations', () => {
  it('uses complete windows for MA, SMA-seeded EMA and population Bollinger bands', () => {
    const rows = bars([1, 2, 3, 7])
    expect(calculate(study('MA', [3]), rows)[0].values).toEqual([
      null,
      null,
      2,
      4,
    ])
    expect(calculate(study('EMA', [3]), rows)[0].values).toEqual([
      null,
      null,
      2,
      4.5,
    ])
    const bands = calculate(study('BOLL', [3, 2]), rows)
    expect(bands[1].values[2]).toBeCloseTo(2 + 2 * Math.sqrt(2 / 3))
    expect(bands[2].values[2]).toBeCloseTo(2 - 2 * Math.sqrt(2 / 3))
  })
  it('keeps RSI and MACD warm-up gaps and handles flat prices', () => {
    const rows = bars([8, 8, 8, 8, 8, 8, 8])
    expect(calculate(study('RSI', [3]), rows)[0].values).toEqual([
      null,
      null,
      null,
      50,
      50,
      50,
      50,
    ])
    expect(calculate(study('MACD', [2, 4, 3]), rows)[0].values).toEqual([
      null,
      null,
      null,
      null,
      null,
      0,
      0,
    ])
    expect(calculate(study('RSI', [2]), bars([1, 2, 3]))[0].values[2]).toBe(100)
    expect(valid(study('MACD', [12, 10, 3]))).toBe(false)
  })
})

const ohlc = (closes: number[]): StudioBar[] =>
  closes.map((close, i) => ({
    date: String(i),
    open: close,
    high: close + 1,
    low: close - 1,
    close,
    volume: i + 1,
  }))

describe('expanded indicator registry', () => {
  it('validates each kind against its own argument bounds', () => {
    expect(valid(study('BOLL', [20, 2.5]))).toBe(true)
    expect(valid(study('BOLL', [20, 0]))).toBe(false)
    expect(valid(study('ENV', [14, 6.5]))).toBe(true)
    expect(valid(study('BBI', [3, 6, 12]))).toBe(false)
    expect(valid(study('DMA', [50, 10, 10]))).toBe(false)
    expect(valid(study('KDJ', [9, 3, 3]))).toBe(true)
    expect(valid(study('WMA', [20]))).toBe(true)
    expect(valid(study('OBV', []))).toBe(true)
  })

  it('matches the classic KDJ / WR / BIAS formulas', () => {
    const rows = ohlc([10, 11, 12, 13])
    const kdj = calculate(study('KDJ', [3, 3, 3]), rows)
    expect(kdj.map((t) => t.name)).toEqual(['K', 'D', 'J'])
    // RSV(3) at i=2: (12 - 9) / (13 - 9) * 100 = 75
    expect(kdj[0].values.slice(0, 3)).toEqual([null, null, 75])
    expect(kdj[2].values[2]).toBe(75)
    // WR(3) at i=2: (13 - 12) / (13 - 9) * 100 = 25
    expect(calculate(study('WR', [3]), rows)[0].values[2]).toBeCloseTo(25)
    // BIAS(3) at i=2: (12 - 11) / 11 * 100
    expect(calculate(study('BIAS', [3]), rows)[0].values[2]).toBeCloseTo((100 / 11) * 1)
  })

  it('accumulates OBV by close direction', () => {
    const rows = bars([10, 11, 10, 13])
    expect(calculate(study('OBV', []), rows)[0].values).toEqual([0, 2, -1, 3])
  })

  it('returns every track for multi-line indicators', () => {
    const rows = ohlc([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25])
    const counts: Array<[Kind, number[], number]> = [
      ['BOLL', [5, 2], 3],
      ['ENV', [3, 5], 3],
      ['MACD', [3, 6, 3], 3],
      ['KDJ', [5, 3, 3], 3],
      ['MTM', [5, 3], 2],
      ['DMA', [3, 6, 3], 2],
      ['TRIX', [3, 3], 2],
      ['PSY', [5, 3], 2],
      ['DMI', [5, 3], 4],
      ['VMA', [3], 2],
      ['BBI', [3, 6, 12, 24], 1],
    ]
    for (const [kind, args, count] of counts) {
      const tracks = calculate(study(kind, args), rows)
      expect(tracks, kind).toHaveLength(count)
      for (const track of tracks) expect(track.values).toHaveLength(rows.length)
    }
  })

  it('never emits NaN for the new indicators', () => {
    const rows = ohlc([12, 12, 12, 12, 12, 12, 12, 12])
    const kinds: Array<[Kind, number[]]> = [
      ['WMA', [3]],
      ['ENV', [3, 5]],
      ['BBI', [3, 6, 12, 24]],
      ['KDJ', [5, 3, 3]],
      ['WR', [5]],
      ['CCI', [5]],
      ['ATR', [5]],
      ['BIAS', [5]],
      ['ROC', [5]],
      ['MTM', [5, 3]],
      ['DMA', [3, 6, 3]],
      ['TRIX', [3, 3]],
      ['PSY', [5, 3]],
      ['DMI', [5, 3]],
      ['VMA', [3]],
    ]
    for (const [kind, args] of kinds) {
      for (const track of calculate(study(kind, args), rows)) {
        expect(track.values.every((v) => v === null || Number.isFinite(v)), kind).toBe(true)
      }
    }
  })
})
