import { useEffect, useRef, useState } from 'react'
import {
  CandlestickChart,
  ChevronDown,
  History,
  LineChart,
  Settings,
  SlidersHorizontal,
  Star,
} from 'lucide-react'

/** 工作台图表类型：本轮只把 candle / line 做成可选状态，图表内核下一轮接入。 */
export type StudioChartType = 'candle' | 'hollow' | 'line' | 'area'
/** 脚本类型：Pine 策略 / Pine 指标 / 旧版四语句脚本。 */
export type StudioScriptKind = 'strategy' | 'indicator' | 'v1'

type Option = { value: string; label: string }

type Props = {
  symbol: string
  name: string
  starred: boolean
  onStar: () => void
  chartType: StudioChartType
  onChartType: (type: StudioChartType) => void
  interval: string
  intervals: string[]
  onInterval: (interval: string) => void
  indicatorCount: number
  onIndicators: () => void
  strategyName: string
  scriptKind: StudioScriptKind
  onScriptEdit: () => void
  onBacktest: () => void
  onSettings: () => void
  /** 以下为保持工作台原有“切换标的 / 布局”能力追加的可选入参。 */
  instrumentValue?: string
  instrumentOptions?: Option[]
  onInstrumentChange?: (value: string) => void
  onGallery?: (view: 'templates' | 'mine') => void
  layout?: 'default' | 'compact'
  onLayout?: (layout: 'default' | 'compact') => void
}

/** 周期代码到中文标签：后端目前只支持 1d / 1w / 1m。 */
const intervalLabels: Record<string, string> = {
  '1d': '日线',
  '1w': '周线',
  '1m': '月线',
  '15m': '15分',
  '60m': '1小时',
}
const intervalText = (value: string) => intervalLabels[value] || value

const chartTypes: Array<[StudioChartType, string, boolean]> = [
  ['candle', '蜡烛图', true],
  ['hollow', '空心蜡烛', false],
  ['line', '线图', true],
  ['area', '面积图', false],
]

export default function StudioTopbar({
  symbol,
  name,
  starred,
  onStar,
  chartType,
  onChartType,
  interval,
  intervals,
  onInterval,
  indicatorCount,
  onIndicators,
  onSettings,
  instrumentValue,
  instrumentOptions,
  onInstrumentChange,
}: Props) {
  const root = useRef<HTMLDivElement>(null)
  const [menu, setMenu] = useState<'chart' | 'more' | null>(null)
  const close = () => setMenu(null)
  const toggle = (next: Exclude<typeof menu, null>) =>
    setMenu((current) => (current === next ? null : next))

  // 点击组件外部关闭所有小弹层，保持和项目里其他下拉一致的操作习惯。
  useEffect(() => {
    if (!menu) return
    const onPointerDown = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) close()
    }
    document.addEventListener('mousedown', onPointerDown)
    return () => document.removeEventListener('mousedown', onPointerDown)
  }, [menu])

  const visible = intervals.slice(0, 2)
  const extra = intervals.slice(2)
  const moreActive = extra.includes(interval)

  return (
    <div className="st-topbar" ref={root}>
      <div className="st-topbar-symbol">
        {instrumentOptions?.length && onInstrumentChange ? (
          <select
            className="st-topbar-instrument"
            aria-label="工作台标的"
            title={`${name} ${symbol}`}
            value={instrumentValue}
            onChange={(event) => onInstrumentChange(event.target.value)}
          >
            {instrumentOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
            <option value="manual">按代码打开其他标的…</option>
          </select>
        ) : (
          <>
            <b className="st-topbar-name" title={`${name} ${symbol}`}>
              {name}
            </b>
            <span className="st-topbar-code">{symbol}</span>
          </>
        )}
        <button
          type="button"
          className="st-topbar-star"
          aria-pressed={starred}
          aria-label={starred ? '取消收藏标的' : '收藏标的'}
          title={starred ? '取消收藏' : '收藏标的'}
          onClick={onStar}
        >
          <Star size={14} fill={starred ? 'currentColor' : 'none'} />
        </button>
      </div>

      <span className="st-topbar-sep" aria-hidden="true" />

      <div className="st-topbar-pop">
        <button
          type="button"
          className="st-topbar-icon"
          aria-label="图表类型"
          aria-expanded={menu === 'chart'}
          title="图表类型"
          onClick={() => toggle('chart')}
        >
          {chartType === 'line' ? (
            <LineChart size={16} />
          ) : (
            <CandlestickChart size={16} />
          )}
        </button>
        {menu === 'chart' && (
          <div className="st-pop" role="menu" aria-label="图表类型">
            {chartTypes.map(([value, label, ready]) => (
              <button
                key={value}
                type="button"
                role="menuitemradio"
                aria-checked={chartType === value}
                className={chartType === value ? 'st-pop-active' : undefined}
                disabled={!ready}
                title={ready ? label : label + '待图表内核接入（P1）'}
                onClick={() => {
                  if (!ready) return
                  onChartType(value)
                  close()
                }}
              >
                {label}
                {!ready && <small>P1</small>}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="st-topbar-intervals" role="group" aria-label="K线周期">
        {visible.map((value) => (
          <button
            key={value}
            type="button"
            aria-pressed={interval === value}
            onClick={() => onInterval(value)}
          >
            {intervalText(value)}
          </button>
        ))}
        <div className="st-topbar-pop">
          <button
            type="button"
            aria-expanded={menu === 'more'}
            aria-pressed={moreActive}
            onClick={() => toggle('more')}
          >
            更多
            <ChevronDown size={13} />
          </button>
          {menu === 'more' && (
            <div className="st-pop" role="menu" aria-label="更多周期">
              {extra.map((value) => (
                <button
                  key={value}
                  type="button"
                  role="menuitemradio"
                  aria-checked={interval === value}
                  className={interval === value ? 'st-pop-active' : undefined}
                  onClick={() => {
                    onInterval(value)
                    close()
                  }}
                >
                  {intervalText(value)}
                </button>
              ))}
              <small>分钟线周期待后端行情接口支持（P1）</small>
            </div>
          )}
        </div>
      </div>

      <button
        type="button"
        className="st-topbar-icon"
        aria-label="工作台设置"
        title="回测参数"
        onClick={onSettings}
      >
        <Settings size={16} />
      </button>

      <button
        type="button"
        className="st-topbar-icon"
        disabled
        title="历史回放待接入（P1）"
        aria-label="回放"
      >
        <History size={16} />
        <span className="st-topbar-label">回放</span>
      </button>

      <button
        type="button"
        className="st-topbar-indicators"
        onClick={onIndicators}
        title="图表指标"
      >
        <SlidersHorizontal size={15} />
        指标
        <span className="st-topbar-count">{indicatorCount}</span>
      </button>

    </div>
  )
}
