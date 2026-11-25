import type {
  IChartApi,
  ISeriesApi,
  ISeriesPrimitivePaneRenderer,
} from 'lightweight-charts'
import type { BacktestOrder, StudioBar } from '@panwatch/api'

type Mark = { index: number; price: number; buy: boolean; fill: boolean }

export function drawTradeMarks(
  chart: IChartApi,
  candle: ISeriesApi<'Candlestick'>,
  bars: StudioBar[],
  orders: BacktestOrder[],
  signals: boolean,
  fills: boolean,
  dark: boolean,
) {
  const byDate = new Map(bars.map((bar, index) => [bar.date, index]))
  const marks = new Map<string, Mark>()
  function add(date: string, buy: boolean, fill: boolean, price?: number) {
    const index = byDate.get(date)
    if (index === undefined) return
    const bar = bars[index]
    marks.set([date, buy, fill].join('/'), {
      index,
      buy,
      fill,
      price: price ?? (buy ? bar.low : bar.high),
    })
  }
  if (signals)
    for (const b of bars) {
      if (b.template_entry) add(b.date, true, false)
      if (b.template_exit) add(b.date, false, false)
    }
  for (const o of orders) {
    if (signals) add(o.signal_date, o.side === 'buy', false)
    if (
      fills &&
      o.status === 'filled' &&
      o.execution_date &&
      o.fill_price != null
    )
      add(o.execution_date, o.side === 'buy', true, o.fill_price)
  }
  const ordered = [...marks.values()].sort((a, b) => a.index - b.index)
  const renderer: ISeriesPrimitivePaneRenderer = {
    draw(target) {
      target.useMediaCoordinateSpace(({ context: c, mediaSize }) => {
        const scale = chart.timeScale(),
          range = scale.getVisibleLogicalRange()
        if (!range) return
        const lastLabel = [-100, -100]
        c.save()
        c.font = 'bold 11px system-ui'
        c.textAlign = 'center'
        c.textBaseline = 'middle'
        c.lineJoin = 'round'
        for (const m of ordered) {
          if (m.index < range.from - 1 || m.index > range.to + 1) continue
          const x = scale.timeToCoordinate(bars[m.index].date),
            y = candle.priceToCoordinate(m.price)
          if (x == null || y == null) continue
          const color = m.buy
            ? dark
              ? '#ff979d'
              : '#bb3440'
            : dark
              ? '#75d9ae'
              : '#117455'
          c.lineWidth = 1.5
          c.strokeStyle = dark ? '#19191a' : 'white'
          if (m.fill) {
            c.fillStyle = dark ? '#edf0fa' : '#253651'
            c.beginPath()
            c.moveTo(x, y - 5)
            c.lineTo(x + 5, y)
            c.lineTo(x, y + 5)
            c.lineTo(x - 5, y)
            c.closePath()
            c.fill()
            c.stroke()
            continue
          }
          const direction = m.buy ? 1 : -1,
            tip = y + direction * 4,
            side = m.buy ? 0 : 1
          if (x - lastLabel[side] < 22) {
            c.fillStyle = color
            c.beginPath()
            c.moveTo(x, tip)
            c.lineTo(x - 3, tip + direction * 5)
            c.lineTo(x + 3, tip + direction * 5)
            c.closePath()
            c.fill()
            continue
          }
          lastLabel[side] = x
          const cy = Math.max(
              10,
              Math.min(mediaSize.height - 10, tip + direction * 14),
            ),
            edge = cy - direction * 8
          c.fillStyle = color
          c.beginPath()
          if (typeof c.roundRect === 'function')
            c.roundRect(x - 9, cy - 8, 18, 16, 3)
          else c.rect(x - 9, cy - 8, 18, 16)
          c.fill()
          c.stroke()
          c.beginPath()
          c.moveTo(x - 3, edge)
          c.lineTo(x, tip)
          c.lineTo(x + 3, edge)
          c.fill()
          c.fillStyle = dark ? '#161821' : 'white'
          c.fillText(m.buy ? 'B' : 'S', x, cy + 0.5)
        }
        c.restore()
      })
    },
  }
  const views = [{ zOrder: () => 'top' as const, renderer: () => renderer }]
  candle.attachPrimitive({ paneViews: () => views })
}
