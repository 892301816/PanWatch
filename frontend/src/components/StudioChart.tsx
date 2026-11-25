import { useEffect, useMemo, useRef, useState } from 'react'
import {
  ColorType,
  createChart,
  LineStyle,
  LineType,
  type IChartApi,
  type ISeriesApi,
  type Time,
} from 'lightweight-charts'
import {
  MousePointer2,
  Minus,
  TrendingUp,
  Undo2,
  PencilRuler,
  SlidersHorizontal,
  X,
} from 'lucide-react'
import type { BacktestOrder, StudioBar, StudioPlotSpec } from '@panwatch/api'

import { drawTradeMarks } from '@/lib/studio-markers'
import StudioIndicatorMenu from './StudioIndicatorMenu'
import {
  calculate,
  indicators,
  readStudies,
  titleOf,
  type Study,
} from '@/lib/studio-indicators'

function dateOf(t: Time | undefined) {
  if (typeof t === 'string') return t
  if (typeof t === 'number')
    return new Date(t * 1000).toISOString().slice(0, 10)
  return t
    ? [
        t.year,
        String(t.month).padStart(2, '0'),
        String(t.day).padStart(2, '0'),
      ].join('-')
    : ''
}
/** 空 plot 列表的稳定引用：避免默认参数每次渲染都换新数组触发图表重建。 */
const noPlots: StudioPlotSpec[] = []
/** Pine plot 取值：非数值一律当空点，保留时间轴上的空档。 */
function plotValue(bar: StudioBar | undefined, name: string): number | null {
  const value = bar?.[name]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}
/** lightweight-charts 线宽只接受 1–4。 */
function plotWidth(value: number): 1 | 2 | 3 | 4 {
  const width = Math.round(Number(value) || 1)
  return Math.min(4, Math.max(1, width)) as 1 | 2 | 3 | 4
}
/** Pine 的 plot 风格映射：阶梯线用 lineType，圆点用 pointMarkers。 */
function plotLineOptions(style: string) {
  if (style === 'stepline' || style === 'steplinebr' || style === 'stepline_diamond')
    return { lineType: LineType.WithSteps }
  if (style === 'circles') return { pointMarkersVisible: true }
  return {}
}
/** 直方图/柱状风格改用 histogram 系列。 */
const isHistogramStyle = (style: string) =>
  style === 'histogram' || style === 'columns'
const formatValue = (n: number | null | undefined) =>
  n == null ? '—' : n.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
type Point = { date: string; price: number }
type Line = { start: Point; end?: Point }
type Tool = 'inspect' | 'level' | 'trend' | 'start' | 'end'
type Props = {
  bars: StudioBar[]
  storageKey: string
  focus?: { date: string }
  orders: BacktestOrder[]
  onPick: (date: string) => void
  /** Pine 脚本声明的绘图：overlay 画主图，其余各占一个副图 pane。 */
  plots?: StudioPlotSpec[]
}
function readLines(key: string): Line[] {
  try {
    const list: Line[] = JSON.parse(localStorage.getItem(key) || '[]')
    const valid = (p: Point) =>
      p && typeof p.date === 'string' && Number.isFinite(p.price)
    return Array.isArray(list)
      ? list.filter((l) => valid(l.start) && (!l.end || valid(l.end)))
      : []
  } catch {
    return []
  }
}
export default function StudioChart({
  bars,
  storageKey,
  focus,
  orders,
  onPick,
  plots = noPlots,
}: Props) {
  const [studies, setStudies] = useState(readStudies)
  const [menu, setMenu] = useState(false)
  // 每个副图自己的高度（px）；拖拽写入 localStorage，双击恢复自动比例
  const [paneHeights, setPaneHeights] = useState<Record<string, number>>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem('pw:studio:pane-h') || '{}')
      if (saved && typeof saved === 'object')
        return saved as Record<string, number>
    } catch {}
    return {}
  })
  function persistPanes(map: Record<string, number>) {
    try {
      localStorage.setItem('pw:studio:pane-h', JSON.stringify(map))
    } catch {}
  }
  function startPaneDrag(
    event: {
      clientY: number
      preventDefault: () => void
      stopPropagation: () => void
    },
    key: string,
  ) {
    event.preventDefault()
    event.stopPropagation()
    const section = paneHosts.current.get(key)?.parentElement
    if (!section) return
    const startY = event.clientY
    const startH = section.getBoundingClientRect().height
    let moved = false
    const move = (moveEvent: PointerEvent) => {
      moved = true
      // 不设上限：副图可以拖到任意高，超出可视区由外层滑轨滚
      const next = Math.max(56, Math.round(startH + (startY - moveEvent.clientY)))
      setPaneHeights((map) => ({ ...map, [key]: next }))
    }
    const up = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      document.body.classList.remove('st-dock-dragging')
      // 双击复位时也会触发一次 down/up，没真正移动就不能把高度写回去
      if (!moved) return
      setPaneHeights((map) => {
        persistPanes(map)
        return map
      })
    }
    document.body.classList.add('st-dock-dragging')
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }
  function resetPane(key: string) {
    setPaneHeights((map) => {
      const next = { ...map }
      delete next[key]
      persistPanes(next)
      return next
    })
  }
  // 指标浮层用 fixed 定位（图表区可滚动，absolute 会被裁掉）
  const legendAnchor = useRef<HTMLDivElement>(null)
  const [popPos, setPopPos] = useState<{ top: number; left: number } | null>(null)
  // 点浮层外部 / 按 Esc 关闭
  useEffect(() => {
    if (!menu) return
    const onDown = (event: MouseEvent) => {
      if (!legendAnchor.current?.contains(event.target as Node)) setMenu(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenu(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [menu])
  const [dark, setDark] = useState(() =>
    document.documentElement.classList.contains('dark'),
  )
  const [tip, setTip] = useState<{ x: number; y: number } | null>(null)
  const [showSignals, setShowSignals] = useState(true),
    [showFills, setShowFills] = useState(true)
  const paneHosts = useRef(new Map<string, HTMLDivElement>())
  const computed = useMemo(
    () => studies.map((study) => ({ study, tracks: calculate(study, bars) })),
    [studies, bars],
  )
  const indexByDate = useMemo(
    () => new Map(bars.map((b, i) => [b.date, i])),
    [bars],
  )
  const palette = dark
    ? ['#e7bd74', '#93b8ff', '#c7a3ec', '#70c7c4']
    : ['#936216', '#476cba', '#8c56b3', '#167d7a']
  /** plot 自带的颜色优先，缺失时按索引轮换默认色板。 */
  const plotColor = (plot: StudioPlotSpec, index: number) =>
    typeof plot.color === 'string' && plot.color
      ? plot.color
      : palette[index % palette.length]
  // Pine 副图：按 plot 名分配 pane，同名 plot 叠在同一个 pane 里。
  const pinePanes = useMemo(() => {
    const map = new Map<string, StudioPlotSpec[]>()
    for (const plot of plots) {
      if (plot.overlay) continue
      const rows = map.get(plot.name)
      if (rows) rows.push(plot)
      else map.set(plot.name, [plot])
    }
    return [...map.entries()]
  }, [plots])
  const chartBg = dark ? '#19191a' : '#ffffff',
    axis = dark ? '#acacb2' : '#595962',
    grid = dark ? '#2b2b2f' : '#ededf0'
  function configure(next: Study[]) {
    setStudies(next)
    try {
      localStorage.setItem('pw:studies:v1', JSON.stringify(next))
    } catch {
      setHint('本浏览器未能保存指标布局')
    }
  }
  useEffect(() => {
    const root = document.documentElement
    const watch = new MutationObserver(() =>
      setDark(root.classList.contains('dark')),
    )
    watch.observe(root, { attributes: true, attributeFilter: ['class'] })
    return () => watch.disconnect()
  }, [])
  const host = useRef<HTMLDivElement>(null)
  const api = useRef<IChartApi>()
  const price = useRef<ISeriesApi<'Candlestick'>>()
  const [cursor, setCursor] = useState('')
  const [touchDate, setTouchDate] = useState('')
  const tapOrigin = useRef<{ x: number; y: number; id: number }>()
  const retainedRange = useRef<{ from: number; to: number; last: string }>()
  const [toolsOpen, setToolsOpen] = useState(false)
  const [tool, setTool] = useState<Tool>('inspect')
  const [start, setStart] = useState<Point>()
  const [selected, setSelected] = useState(0)
  const [lines, setLines] = useState(() => readLines(storageKey))
  const history = useRef<Line[][]>([])
  const [hint, setHint] = useState('')
  const [chartError, setChartError] = useState('')
  function store(next: Line[], remember = true) {
    if (remember) history.current = [...history.current.slice(-24), lines]
    setLines(next)
    try {
      localStorage.setItem(storageKey, JSON.stringify(next))
    } catch {
      setHint('本机存储不可用，画线仅在本次页面保留')
    }
    setStart(undefined)
    setTool('inspect')
  }
  const click = useRef((_p: Point) => {})
  click.current = (p) => {
    if (tool === 'inspect') {
      setCursor(p.date)
      onPick(p.date)
      return
    }
    if (tool === 'trend' && !start) {
      setStart(p)
      return
    }
    if (tool === 'trend' && start?.date === p.date) {
      setHint('请选择另一个交易日')
      return
    }
    if (tool === 'start' || tool === 'end') {
      const other =
        tool === 'end' ? lines[selected]?.start : lines[selected]?.end
      if (other?.date === p.date) {
        setHint('趋势线两端需要不同的日期')
        return
      }
      store(lines.map((l, i) => (i === selected ? { ...l, [tool]: p } : l)))
    } else {
      setSelected(lines.length)
      store([
        ...lines,
        tool === 'level' ? { start: p } : { start: start!, end: p },
      ])
    }
  }
  useEffect(() => {
    if (!host.current || !bars.length) return
    const container = host.current
    let chart: IChartApi
    try {
      chart = createChart(container, {
        width: container.clientWidth,
        height: container.clientHeight,
        layout: {
          background: { type: ColorType.Solid, color: chartBg },
          textColor: axis,
          attributionLogo: true,
        },
        grid: {
          vertLines: { color: grid },
          horzLines: { color: grid },
        },
        rightPriceScale: {
          borderVisible: false,
          minimumWidth: 65,
          scaleMargins: { top: 0.13, bottom: 0.16 },
        },
        timeScale: { borderVisible: false, rightOffset: 3 },
        localization: { locale: 'zh-CN' },
        handleScroll: {
          vertTouchDrag: false,
          horzTouchDrag: true,
          pressedMouseMove: true,
          mouseWheel: false,
        },
      })
    } catch {
      setChartError('图表初始化失败，请刷新后重试')
      return
    }
    api.current = chart
    const candle = chart.addCandlestickSeries({
      upColor: '#ec887e',
      downColor: '#56b9a1',
      borderVisible: false,
      wickUpColor: '#ec887e',
      wickDownColor: '#56b9a1',
    })
    price.current = candle
    candle.setData(bars.map((b) => ({ ...b, time: b.date })))
    if (plots.length) {
      // Pine：主图按 plot 名取 bar 上的动态字段画线
      for (const [index, plot] of plots.entries()) {
        if (!plot.overlay) continue
        const line = chart.addLineSeries({
          color: plotColor(plot, index),
          lineWidth: plotWidth(plot.linewidth),
          priceLineVisible: false,
          lastValueVisible: false,
          ...plotLineOptions(plot.style),
        })
        line.setData(
          bars.map((b) => {
            const value = plotValue(b, plot.name)
            return value == null ? { time: b.date } : { time: b.date, value }
          }),
        )
      }
    } else {
      // 老的四语句脚本：固定只有快线/慢线两列
      for (const [field, color] of [
        ['fast', palette[0]],
        ['slow', palette[1]],
      ] as const) {
        const line = chart.addLineSeries({
          color,
          lineWidth: 2,
          priceLineVisible: false,
          lastValueVisible: false,
        })
        line.setData(
          bars.flatMap((b) =>
            typeof b[field] === 'number'
              ? [{ time: b.date, value: b[field] as number }]
              : [],
          ),
        )
      }
    }
    drawTradeMarks(chart, candle, bars, orders, showSignals, showFills, dark)
    type Peer = {
      chart: IChartApi
      node: HTMLDivElement
      first:
        | ISeriesApi<'Line'>
        | ISeriesApi<'Histogram'>
        | ISeriesApi<'Candlestick'>
      value: (i: number) => number | null
    }
    const peers: Peer[] = [
      { chart, node: container, first: candle, value: (i) => bars[i].close },
    ]
    computed.forEach(({ study, tracks }, index) => {
      const panel = study.place === 'pane',
        node = paneHosts.current.get(study.id)
      if (panel && !node) return
      const target = panel
        ? createChart(node!, {
            width: node!.clientWidth,
            height: node!.clientHeight,
            layout: {
              background: { type: ColorType.Solid, color: chartBg },
              textColor: axis,
              attributionLogo: false,
            },
            grid: { horzLines: { color: grid }, vertLines: { color: grid } },
            rightPriceScale: { minimumWidth: 65, borderVisible: false },
            timeScale: { visible: false, borderVisible: false },
            localization: { locale: 'zh-CN' },
            handleScroll: {
              vertTouchDrag: false,
              horzTouchDrag: true,
              pressedMouseMove: true,
              mouseWheel: false,
            },
          })
        : chart
      const separateScale = !panel && !indicators[study.kind].price
      const scaleId = separateScale ? 'overlay-' + study.id : 'right'
      tracks.forEach((track, j) => {
        const color = palette[(index + 2 + j) % palette.length]
        const options = {
          priceScaleId: scaleId,
          color,
          lastValueVisible: false,
          priceLineVisible: false,
        }
        const series = track.hist
          ? target.addHistogramSeries({
              ...options,
              priceFormat: { type: study.kind === 'VOL' ? 'volume' : 'price' },
            })
          : target.addLineSeries({ ...options, lineWidth: 1 })
        series.setData(
          bars.map((b, i) => {
            const v = track.values[i]
            if (v === null) return { time: b.date }
            return {
              time: b.date,
              value: v,
              ...(track.hist
                ? {
                    color: (study.kind === 'VOL' ? b.close >= b.open : v >= 0)
                      ? 'rgba(216,76,84,.55)'
                      : 'rgba(26,150,123,.55)',
                  }
                : {}),
            }
          }),
        )
        if (panel && j === 0)
          peers.push({
            chart: target,
            node: node!,
            first: series,
            value: (i) => track.values[i],
          })
      })
      if (separateScale)
        target.priceScale(scaleId).applyOptions({
          scaleMargins: {
            top: study.kind === 'VOL' ? 0.8 : 0.22,
            bottom: 0.06,
          },
          visible: false,
        })
    })
    // Pine 副图：每个 plot 名一个 pane，宿主与指标副图同样按需创建
    pinePanes.forEach(([name, group]) => {
      const node = paneHosts.current.get('pine:' + name)
      if (!node) return
      const target = createChart(node, {
        width: node.clientWidth,
        height: node.clientHeight,
        layout: {
          background: { type: ColorType.Solid, color: chartBg },
          textColor: axis,
          attributionLogo: false,
        },
        grid: { horzLines: { color: grid }, vertLines: { color: grid } },
        rightPriceScale: { minimumWidth: 65, borderVisible: false },
        timeScale: { visible: false, borderVisible: false },
        localization: { locale: 'zh-CN' },
        handleScroll: {
          vertTouchDrag: false,
          horzTouchDrag: true,
          pressedMouseMove: true,
          mouseWheel: false,
        },
      })
      group.forEach((plot, j) => {
        const color = plotColor(plot, j)
        // 直方图/柱状按 plot 风格换系列类型，其余都画折线
        const series = isHistogramStyle(plot.style)
          ? target.addHistogramSeries({
              color,
              lastValueVisible: false,
              priceLineVisible: false,
            })
          : target.addLineSeries({
              color,
              lineWidth: plotWidth(plot.linewidth),
              lastValueVisible: false,
              priceLineVisible: false,
              ...plotLineOptions(plot.style),
            })
        series.setData(
          bars.map((b) => {
            const value = plotValue(b, plot.name)
            return value == null ? { time: b.date } : { time: b.date, value }
          }),
        )
        if (j === 0)
          peers.push({
            chart: target,
            node,
            first: series,
            value: (i) => plotValue(bars[i], plot.name),
          })
      })
    })
    let syncing = false
    for (const peer of peers) {
      peer.chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
        if (syncing || !range) return
        syncing = true
        try {
          for (const other of peers)
            if (other !== peer)
              other.chart.timeScale().setVisibleLogicalRange(range)
        } finally {
          syncing = false
        }
      })
      peer.chart.subscribeCrosshairMove((e) => {
        if (syncing) return
        const date = dateOf(e.time),
          i = indexByDate.get(date)
        setCursor(date)
        if (peer.chart === chart && e.point && date) {
          // 提示框跟随鼠标，并夹在画布内（右下/左下都不越界）
          const boxW = 176
          const boxH = 168
          const maxX = Math.max(8, container.clientWidth - boxW - 8)
          const maxY = Math.max(8, container.clientHeight - boxH - 8)
          setTip({
            x: Math.min(Math.max(e.point.x + 16, 8), maxX),
            y: Math.min(Math.max(e.point.y + 16, 8), maxY),
          })
        } else {
          setTip(null)
        }
        syncing = true
        try {
          for (const other of peers)
            if (other !== peer) {
              const v = i === undefined ? null : other.value(i)
              if (v != null)
                other.chart.setCrosshairPosition(v, date, other.first)
              else other.chart.clearCrosshairPosition()
            }
        } finally {
          syncing = false
        }
      })
    }
    chart.subscribeClick((e) => {
      const date = dateOf(e.time)
      if (!date || !e.point) return
      const value = candle.coordinateToPrice(e.point.y)
      if (value !== null) click.current({ date, price: Number(value) })
    })
    if (retainedRange.current?.last === bars[bars.length - 1].date)
      chart.timeScale().setVisibleLogicalRange(retainedRange.current)
    else
      chart.timeScale().setVisibleLogicalRange({
        from: Math.max(
          0,
          bars.length - (container.clientWidth < 500 ? 48 : 100),
        ),
        to: bars.length + 3,
      })
    const resize = new ResizeObserver(() => {
      for (const p of peers)
        p.chart.resize(p.node.clientWidth, p.node.clientHeight)
    })
    peers.forEach((p) => resize.observe(p.node))
    return () => {
      const range = chart.timeScale().getVisibleLogicalRange()
      if (range)
        retainedRange.current = { ...range, last: bars[bars.length - 1].date }
      resize.disconnect()
      api.current = undefined
      price.current = undefined
      syncing = true
      peers.forEach((p) => p.chart.remove())
    }
  }, [bars, orders, dark, computed, showSignals, showFills, plots, pinePanes])
  useEffect(() => {
    const chart = api.current,
      candle = price.current
    if (!chart || !candle) return
    const cleanups: Array<() => void> = []
    for (const [i, l] of lines.entries()) {
      const color = i === selected ? palette[0] : axis
      if (l.end) {
        const line = chart.addLineSeries({
          color,
          lineWidth: 2,
          lastValueVisible: false,
          priceLineVisible: false,
          autoscaleInfoProvider: () => null,
        })
        const anchors = [l.start, l.end].sort((a, b) =>
          a.date.localeCompare(b.date),
        )
        // Off-range anchors must not add dates to the instrument's time scale.
        const ia = bars.findIndex((b) => b.date === anchors[0].date),
          ib = bars.findIndex((b) => b.date === anchors[1].date)
        if (ia >= 0 && ib > ia)
          line.setData(anchors.map((p) => ({ time: p.date, value: p.price })))
        cleanups.push(() => chart.removeSeries(line))
      } else {
        const line = candle.createPriceLine({
          price: l.start.price,
          color,
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title: '水平线',
        })
        cleanups.push(() => candle.removePriceLine(line))
      }
    }
    return () => {
      if (api.current === chart) cleanups.forEach((fn) => fn())
    }
  }, [bars, orders, lines, selected, dark, computed, showSignals, showFills, plots, pinePanes])
  useEffect(() => {
    if (!focus || !api.current) return
    const i = bars.findIndex((b) => b.date === focus.date)
    if (i < 0) return
    const half = (host.current?.clientWidth || 600) < 500 ? 18 : 38
    api.current
      .timeScale()
      .setVisibleLogicalRange({ from: i - half, to: i + half })
    if (price.current)
      api.current.setCrosshairPosition(bars[i].close, focus.date, price.current)
    setCursor(focus.date)
    setTouchDate('')
  }, [focus, bars])
  const index = indexByDate.get(touchDate || cursor) ?? bars.length - 1,
    current = bars[index],
    prev = bars[index - 1]?.close
  const change = prev > 0 ? (current.close / prev - 1) * 100 : null
  const changeText =
    change == null ? '—' : (change > 0 ? '+' : '') + change.toFixed(2) + '%'
  const signals = [
    ...(current?.template_entry ? ['买入信号'] : []),
    ...(current?.template_exit ? ['卖出信号'] : []),
    ...orders
      .filter((o) => o.execution_date === current?.date)
      .map(
        (o) =>
          (o.side === 'buy' ? '买' : '卖') +
          (o.status === 'filled'
            ? '成 ' + formatValue(o.fill_price)
            : '未成交'),
      ),
  ]
  function readTracks(study: (typeof computed)[number]) {
    const group = computed.indexOf(study)
    return study.tracks.map((t, j) => (
      <span key={t.name} className="st-track">
        <i
          style={{
            backgroundColor: t.hist
              ? 'var(--hint)'
              : palette[(group + 2 + j) % palette.length],
          }}
        />
        {study.tracks.length > 1 ? t.name + ' ' : ''}
        {formatValue(t.values[index])}
      </span>
    ))
  }
  function zoom(factor: number) {
    const scale = api.current?.timeScale(),
      visible = scale?.getVisibleLogicalRange()
    if (!visible || !scale) return
    const width = Math.min(
      bars.length + 4,
      Math.max(10, (visible.to - visible.from) * factor),
    )
    scale.setVisibleLogicalRange({ from: visible.to - width, to: visible.to })
  }
  function choose(t: Tool) {
    setTool(t)
    setStart(undefined)
    setHint('')
  }
  return (
    <section className="st-chart">
      <header className="st-read">
        <span>{current?.date || '—'}</span>
        {current &&
          (['open', 'high', 'low', 'close'] as const).map((k, i) => (
            <span key={k}>
              {['开', '高', '低', '收'][i]} <b>{current[k].toFixed(2)}</b>
            </span>
          ))}
        <span>
          涨跌{' '}
          <b className={change != null && change < 0 ? 'st-down' : 'st-up'}>
            {changeText}
          </b>
        </span>
        <span>
          量 <b>{formatValue(current?.volume)}</b>
        </span>
      </header>
      <div className="st-legend">
        {plots.length ? (
          // Pine：图例按 plot 名展示当前 bar 的取值
          plots
            .filter((plot) => plot.overlay)
            .map((plot, index) => (
              <span key={plot.name + index} style={{ color: plotColor(plot, index) }}>
                ━ {plot.name} {formatValue(plotValue(current, plot.name))}
              </span>
            ))
        ) : (
          <>
            <span>━ 快线 {current?.fast?.toFixed(2) ?? '—'}</span>
            <span>━ 慢线 {current?.slow?.toFixed(2) ?? '—'}</span>
          </>
        )}
        <div className="st-legend-anchor" ref={legendAnchor}>
          <button
            aria-expanded={menu}
            onClick={() => {
              if (menu) {
                setMenu(false)
                return
              }
              const rect = legendAnchor.current?.getBoundingClientRect()
              if (rect) {
                setPopPos({
                  top: rect.bottom + 6,
                  left: Math.max(8, Math.min(rect.left, window.innerWidth - 396)),
                })
              }
              setMenu(true)
            }}
            title="添加或编辑指标"
          >
            <SlidersHorizontal size={15} />
            指标
          </button>
          {menu && (
            <div
              className="st-popover"
              role="dialog"
              aria-label="指标与标记"
              style={popPos ? { top: popPos.top, left: popPos.left } : undefined}
            >
              <header>
                <b>指标与标记</b>
                <button
                  type="button"
                  aria-label="关闭"
                  className="st-popover-close"
                  onClick={() => setMenu(false)}
                >
                  <X size={15} />
                </button>
              </header>
              <div className="st-popover-body">
                <StudioIndicatorMenu studies={studies} change={configure} />
                <div className="st-mark-options">
                  <label>
                    <input
                      type="checkbox"
                      checked={showSignals}
                      onChange={(e) => setShowSignals(e.target.checked)}
                    />{' '}
                    买卖信号
                  </label>
                  <label>
                    <input
                      type="checkbox"
                      checked={showFills}
                      onChange={(e) => setShowFills(e.target.checked)}
                    />{' '}
                    实际成交
                  </label>
                </div>
              </div>
            </div>
          )}
        </div>
        <button aria-label="放大K线" onClick={() => zoom(0.7)}>
          ＋
        </button>
        <button aria-label="缩小K线" onClick={() => zoom(1.4)}>
          −
        </button>
        <button
          aria-expanded={toolsOpen}
          onClick={() => setToolsOpen(!toolsOpen)}
        >
          <PencilRuler size={15} />
          画线{lines.length ? ' · ' + lines.length : ''}
        </button>
      </div>
      <div className="st-signal-key" aria-label="标记图例">
        {showSignals && (
          <>
            <span>
              <b>B</b>买入信号
            </span>
            <span>
              <b>S</b>卖出信号
            </span>
          </>
        )}
        {showFills && orders.some((o) => o.status === 'filled') && (
          <span>◆ 实际成交价</span>
        )}
      </div>
      {computed.some((s) => s.study.place === 'main') && (
        <div className="st-overlay-values">
          {computed
            .filter((s) => s.study.place === 'main')
            .map((s) => (
              <span key={s.study.id}>
                {titleOf(s.study)}
                {!indicators[s.study.kind].price ? '（独立刻度）' : ''}{' '}
                {readTracks(s)}
              </span>
            ))}
        </div>
      )}
      {toolsOpen && (
        <div className="st-tools" aria-label="画线工具">
          {(
            [
              ['inspect', MousePointer2, '查看'],
              ['level', Minus, '水平线'],
              ['trend', TrendingUp, '趋势线'],
            ] as const
          ).map(([id, Icon, label]) => (
            <button
              key={id}
              aria-pressed={tool === id}
              onClick={() => choose(id)}
            >
              <Icon size={14} />
              {label}
            </button>
          ))}
          {!!lines.length && (
            <>
              <select
                aria-label="选择画线"
                value={Math.min(selected, lines.length - 1)}
                onChange={(e) => {
                  setSelected(+e.target.value)
                  choose('inspect')
                }}
              >
                {lines.map((l, i) => (
                  <option key={i} value={i}>
                    {l.end ? '趋势线' : '水平线'} {i + 1}
                  </option>
                ))}
              </select>
              <button onClick={() => choose('start')}>调整起点</button>
              {lines[selected]?.end && (
                <button onClick={() => choose('end')}>调整终点</button>
              )}
              <button
                onClick={() => {
                  store(lines.filter((_, i) => i !== selected))
                  setSelected(0)
                }}
              >
                删除
              </button>
            </>
          )}
          <button
            disabled={!start && !history.current.length}
            onClick={() => {
              if (start) choose('inspect')
              else {
                store(history.current.pop()!, false)
                setSelected(0)
              }
            }}
          >
            <Undo2 size={14} />
            撤销
          </button>
        </div>
      )}
      {tool !== 'inspect' && (
        <p role="status">
          {tool === 'trend'
            ? start
              ? '点选终点'
              : '点选起点'
            : '在图上点选新位置'}
        </p>
      )}
      {hint && <small>{hint}</small>}
      <div
        className="st-plot-wrap"
        onPointerMoveCapture={(e) => {
          if (e.pointerType === 'mouse' && touchDate) setTouchDate('')
        }}
        onPointerDownCapture={(e) => {
          setTouchDate('')
          tapOrigin.current =
            e.pointerType === 'touch' && tool === 'inspect' && e.isPrimary
              ? { x: e.clientX, y: e.clientY, id: e.pointerId }
              : undefined
        }}
        onPointerCancelCapture={() => {
          tapOrigin.current = undefined
        }}
        onPointerUpCapture={(e) => {
          const origin = tapOrigin.current
          tapOrigin.current = undefined
          if (
            !origin ||
            origin.id !== e.pointerId ||
            Math.hypot(origin.x - e.clientX, origin.y - e.clientY) > 9 ||
            !api.current ||
            !host.current
          )
            return
          const rect = host.current.getBoundingClientRect(),
            x = e.clientX - rect.left,
            scale = api.current.timeScale()
          if (x < 0 || x >= scale.width()) return
          const logical = scale.coordinateToLogical(x)
          const bar = logical == null ? undefined : bars[Math.round(logical)]
          if (bar) {
            setTouchDate(bar.date)
            onPick(bar.date)
            if (price.current)
              api.current.setCrosshairPosition(
                bar.close,
                bar.date,
                price.current,
              )
          }
        }}
      >
        <div
          className="st-canvas"
          ref={host}
          aria-label="价格、策略信号与成交量"
        />
        {tip && current && tool === 'inspect' && (
          <div
            className="st-tooltip"
            style={{ left: tip.x, top: tip.y }}
          >
            <strong>{current.date}</strong>
            <dl>
              {[
                ['开盘', current.open],
                ['最高', current.high],
                ['最低', current.low],
                ['收盘', current.close],
                ['成交量', current.volume],
              ].map(([k, v]) => (
                <div key={k}>
                  <dt>{k}</dt>
                  <dd>{formatValue(Number(v))}</dd>
                </div>
              ))}
              <div>
                <dt>涨跌幅</dt>
                <dd>{changeText}</dd>
              </div>
            </dl>
            {signals.length > 0 && <small>{signals.join(' · ')}</small>}
          </div>
        )}
        {tool !== 'inspect' && (
          <div
            className="st-draw-layer"
            aria-label="画线定位区域"
            onPointerUp={(event) => {
              const rect = event.currentTarget.getBoundingClientRect()
              const scale = api.current?.timeScale()
              const x = event.clientX - rect.left
              if (!scale || x > scale.width()) return
              const logical = scale.coordinateToLogical(x)
              const value = price.current?.coordinateToPrice(
                event.clientY - rect.top,
              )
              if (logical == null || value == null) return
              const bar = bars[Math.round(logical)]
              if (bar) click.current({ date: bar.date, price: Number(value) })
            }}
          />
        )}
      </div>
      {computed
        .filter((s) => s.study.place === 'pane')
        .map((s) => (
          <section
            className="st-subplot"
            key={s.study.id}
            aria-label={titleOf(s.study) + ' 副图'}
            style={
              paneHeights[s.study.id]
                ? { flex: '0 0 auto', height: paneHeights[s.study.id] }
                : undefined
            }
          >
            <span
              className="st-pane-resize"
              role="separator"
              aria-orientation="horizontal"
              aria-label={'调整 ' + titleOf(s.study) + ' 副图高度'}
              title="拖动调整高度，双击恢复自动比例"
              onPointerDown={(event) => startPaneDrag(event, s.study.id)}
              onDoubleClick={() => resetPane(s.study.id)}
            />
            <header>
              <b>{titleOf(s.study)}</b>
              <small>{readTracks(s)}</small>
            </header>
            <div
              ref={(node) => {
                if (node) paneHosts.current.set(s.study.id, node)
                else paneHosts.current.delete(s.study.id)
              }}
            />
          </section>
        ))}
      {pinePanes.length > 0 && (
        <div className="st-pine-panes">
          {pinePanes.map(([name, group]) => (
            <section
              className="st-subplot"
              key={'pine:' + name}
              aria-label={name + ' 副图'}
              style={
                paneHeights['pine:' + name]
                  ? { flex: '0 0 auto', height: paneHeights['pine:' + name] }
                  : undefined
              }
            >
              <span
                className="st-pane-resize"
                role="separator"
                aria-orientation="horizontal"
                aria-label={'调整 ' + name + ' 副图高度'}
                title="拖动调整高度，双击恢复自动比例"
                onPointerDown={(event) => startPaneDrag(event, 'pine:' + name)}
                onDoubleClick={() => resetPane('pine:' + name)}
              />
              <header>
                <b>{name}</b>
                <small>
                  {group.map((plot, j) => (
                    <span key={plot.style + j} className="st-track">
                      <i style={{ backgroundColor: plotColor(plot, j) }} />
                      {formatValue(plotValue(current, plot.name))}
                    </span>
                  ))}
                </small>
              </header>
              <div
                ref={(node) => {
                  if (node) paneHosts.current.set('pine:' + name, node)
                  else paneHosts.current.delete('pine:' + name)
                }}
              />
            </section>
          ))}
        </div>
      )}
      {chartError && <p role="alert">{chartError}</p>}
    </section>
  )
}
