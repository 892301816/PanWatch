import { useEffect, useRef, useState } from 'react'
import type { StudioStats, StudioTrade } from '@panwatch/api'

type Props = {
  stats: StudioStats
  trades: StudioTrade[]
  equity: number[]
  dates: string[]
  /** 回测 dock 的「统计」页只复用指标网格；默认仍显示全部内容。 */
  showTrades?: boolean
  showEquity?: boolean
  /** 统计来源不是 Pine 预览时，用报告口径替换标题与说明。 */
  heading?: string
  note?: string
  label?: string
}

const number = (value: unknown, digits = 2) =>
  typeof value === 'number' && Number.isFinite(value)
    ? value.toLocaleString('zh-CN', { maximumFractionDigits: digits })
    : '—'
/** 比率类指标保留两位小数，缺失时显示 —。 */
const ratio = (value: unknown) => number(value, 2)
const percent = (value: unknown) =>
  typeof value === 'number' && Number.isFinite(value)
    ? value.toFixed(2) + '%'
    : '—'
/** 权益曲线纵轴用「万」压缩宽度，避免大资金把图挤扁。 */
const axisMoney = (value: number) =>
  Math.abs(value) >= 10000
    ? (value / 10000).toFixed(Math.abs(value) >= 100000 ? 0 : 1) + '万'
    : value.toFixed(0)

function EquityCurve({ equity, dates }: { equity: number[]; dates: string[] }) {
  const box = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(680)
  useEffect(() => {
    const node = box.current
    if (!node || typeof ResizeObserver === 'undefined') return
    const resize = new ResizeObserver(() => {
      if (node.clientWidth) setWidth(Math.max(240, node.clientWidth))
    })
    resize.observe(node)
    return () => resize.disconnect()
  }, [])
  const points = equity.filter((value) => Number.isFinite(value))
  if (points.length < 2)
    return <p className="st-pine-empty">本次没有可绘制的权益曲线。</p>
  const w = width,
    h = width < 480 ? 150 : 210,
    left = 58,
    top = 12,
    bottom = 26
  const low = Math.min(...points),
    high = Math.max(...points),
    span = high - low || 1
  const x = (index: number) =>
    left + (index / Math.max(1, equity.length - 1)) * (w - left - 16)
  const y = (value: number) => top + ((high - value) / span) * (h - top - bottom)
  const path = equity
    .map((value, index) => `${index ? 'L' : 'M'}${x(index)},${y(value)}`)
    .join(' ')
  const label = (index: number) => dates[index] || ''
  const ticks = [0, 0.25, 0.5, 0.75, 1]
  return (
    <div ref={box} className="st-pine-curve">
      <svg
        viewBox={`0 0 ${w} ${h}`}
        role="img"
        aria-label="Pine 策略账户权益曲线"
      >
        {ticks.map((tick) => {
          const value = low + tick * span
          return (
            <g key={tick}>
              <line
                x1={left}
                x2={w - 16}
                y1={y(value)}
                y2={y(value)}
                stroke="currentColor"
                opacity={0.12}
              />
              <text
                x={left - 6}
                y={y(value) + 4}
                fontSize="13"
                textAnchor="end"
                fill="currentColor"
              >
                {axisMoney(value)}
              </text>
            </g>
          )
        })}
        <path d={path} fill="none" stroke="#2563eb" strokeWidth="2" />
        {[0, Math.floor((equity.length - 1) / 2), equity.length - 1].map(
          (index, tick) =>
            label(index) ? (
              <text
                key={tick}
                x={x(index)}
                y={h - 7}
                fontSize="13"
                textAnchor={tick === 0 ? 'start' : tick === 2 ? 'end' : 'middle'}
                fill="currentColor"
              >
                {label(index)}
              </text>
            ) : null,
        )}
      </svg>
      <p className="st-pine-legend">
        蓝线：账户权益 · 初始 {number(equity[0])} 元 · 期末{' '}
        {number(equity[equity.length - 1])} 元
      </p>
    </div>
  )
}

/** 成交明细表：dock 的「成交」页复用同一份实现，避免复制列定义。 */
export function StudioPineTrades({ trades }: { trades: StudioTrade[] }) {
  return (
    <div className="st-pine-trades">
      <header>
        <b>成交明细</b>
        <small>{trades.length} 笔</small>
      </header>
      {trades.length ? (
        <div className="st-pine-table">
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>方向</th>
                <th>开仓</th>
                <th>平仓</th>
                <th>数量</th>
                <th>盈亏</th>
                <th>持仓根数</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((trade, index) => (
                <tr key={trade.entry_id || index}>
                  <td>{index + 1}</td>
                  <td>{trade.direction === -1 ? '做空' : '做多'}</td>
                  <td>
                    {trade.entry_date}
                    <small>{number(trade.entry_price)}</small>
                  </td>
                  <td>
                    {trade.exit_date}
                    <small>{number(trade.exit_price)}</small>
                  </td>
                  <td>{number(trade.size, 0)}</td>
                  <td
                    className={
                      trade.profit > 0
                        ? 'st-up'
                        : trade.profit < 0
                          ? 'st-down'
                          : undefined
                    }
                  >
                    {trade.profit > 0 ? '+' : ''}
                    {number(trade.profit)}
                  </td>
                  <td>{number(trade.bars, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="st-pine-empty">该脚本未产生成交</p>
      )}
    </div>
  )
}

export default function StudioPineStats({
  stats,
  trades,
  equity,
  dates,
  showTrades = true,
  showEquity = true,
  heading = '策略统计',
  note = '口径与观潮一致 · 历史结果不代表未来表现',
  label = 'Pine 策略统计',
}: Props) {
  // 老报告（非 Pine）的统计字段不全：缺数据的卡片直接不渲染，
  // 不要显示成「— 元」这种误导性占位。
  const has = (value: unknown) =>
    typeof value === 'number' && Number.isFinite(value)
  const money = (value: unknown, unit = '元') =>
    has(value) ? number(value) + ' ' + unit : ''
  const cards: Array<[string, string, string?]> = (
    [
      [
        '总收益',
        percent(stats.total_return_pct),
        '账户权益相对初始资金',
      ],
      ['年化收益', percent(stats.annualized_return_pct)],
      [
        '最大回撤',
        percent(stats.max_drawdown_pct),
        // 只有 Pine 口径才有初始资金字段，用它区分是不是老报告，避免把百分比当金额
        has(stats.initial_capital) ? money(stats.max_drawdown) : undefined,
      ],
      ['夏普', ratio(stats.sharpe)],
      ['索提诺', ratio(stats.sortino)],
      ['卡玛', ratio(stats.calmar)],
      ['盈亏比', ratio(stats.profit_factor)],
      [
        '胜率',
        percent(stats.win_rate_pct),
        has(stats.wins) || has(stats.losses)
          ? (stats.wins ?? 0) + ' 胜 / ' + (stats.losses ?? 0) + ' 负'
          : undefined,
      ],
      ['交易次数', has(stats.trades) ? number(stats.trades, 0) : ''],
      ['平均持仓', has(stats.avg_holding_bars) ? number(stats.avg_holding_bars, 1) + ' 根' : ''],
      ['手续费合计', money(stats.total_fees)],
      ['期望值', money(stats.expectancy, '元/笔')],
    ] as Array<[string, string, string?]>
  ).filter(([, value]) => value !== '' && value !== '—')
  return (
    <section className="st-pine-stats" aria-label={label}>
      <header>
        <b>{heading}</b>
        <small>{note}</small>
      </header>
      <div className="st-pine-metrics">
        {cards.map(([label, value, note]) => (
          <div key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
            {note && <small>{note}</small>}
          </div>
        ))}
      </div>
      {showEquity && <EquityCurve equity={equity} dates={dates} />}
      {showTrades && <StudioPineTrades trades={trades} />}
    </section>
  )
}
