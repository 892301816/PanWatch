import { useMemo, useRef, useState } from 'react'
import {
  ArrowLeft,
  ChevronDown,
  Code2,
  Play,
  SlidersHorizontal,
  Table2,
} from 'lucide-react'
import type {
  BacktestOrder,
  ChartCompileResult,
  ChartPreviewResult,
  StudioBar,
  StudioInputMeta,
  StudioPlotSpec,
  StudioStats,
  StudioTrade,
} from '@panwatch/api'
import StudioChart from '@/components/StudioChart'
import StudioPineInputs from '@/components/StudioPineInputs'
import StudioPineStats from '@/components/StudioPineStats'
import type { StudioScriptKind } from '@/components/StudioTopbar'

/**
 * 手机端独立骨架。
 *
 * 与桌面壳（StrategyStudio 的桌面 JSX）共用同一份状态与回调，但 DOM 结构
 * 完全为触屏重画：单行顶栏 → 图表吃满 → 底部操作条 → 三个全屏视图切换
 * （图表 / 脚本 / 回测结果）。刻意不用 Dialog，避免手机上出现「弹窗套弹窗」。
 */

type Draft = { name: string; source: string }

type Props = {
  instrument: { name: string; symbol: string; market: string }
  symbolOptions: Array<{ value: string; label: string }>
  onSymbol: (value: string) => void
  period: string
  periods: Array<{ value: string; label: string }>
  onPeriod: (value: string) => void

  bars: StudioBar[]
  orders: BacktestOrder[]
  plots?: StudioPlotSpec[]
  focus?: { date: string }
  onPick: (date: string) => void
  storageKey: string
  loading: boolean
  failure: string
  onReload: () => void

  scriptName: string
  scriptKind: StudioScriptKind
  busy: boolean
  canBacktest: boolean
  backtestHint?: string
  favorite: boolean
  indicatorCount: number

  onBacktest: () => void
  onFavorite: () => void
  onGallery: () => void
  onKnowledge: () => void
  onIndicators: () => void

  draft: Draft
  onDraft: (next: Draft) => void
  inputs: StudioInputMeta[]
  params: Record<string, unknown>
  onParams: (next: Record<string, unknown>) => void
  compile?: ChartCompileResult
  compiling: boolean
  onCompile: () => void
  onGoToLine: (line: number, column?: number) => void
  onSave: () => void
  onApply: () => void

  dockState: 'idle' | 'running' | 'done' | 'error'
  stats?: StudioStats
  trades?: StudioTrade[]
  equity?: number[]
  dates?: string[]
  result?: React.ReactNode
  notice?: string
  preview?: ChartPreviewResult
  onTemplate: () => void
}

type View = 'chart' | 'code' | 'report'

export default function StudioMobileWorkbench(props: Props) {
  const [view, setView] = useState<View>('chart')
  const [symbolOpen, setSymbolOpen] = useState(false)
  const [periodOpen, setPeriodOpen] = useState(false)
  const area = useRef<HTMLTextAreaElement>(null)

  const scriptBadge =
    props.scriptKind === 'strategy'
      ? 'Pine 策略'
      : props.scriptKind === 'indicator'
        ? 'Pine 指标'
        : '四语句脚本'

  const diagnostics = props.compile?.diagnostics ?? []
  const warnings = useMemo(
    () => props.compile?.warnings ?? props.preview?.warnings ?? [],
    [props.compile, props.preview],
  )
  const lineCount = useMemo(
    () => Math.max(1, props.draft.source.split(/\r?\n/).length),
    [props.draft.source],
  )

  /** 点诊断跳行：把 textarea 光标放到对应位置并聚焦。 */
  function jump(line: number, column = 1) {
    props.onGoToLine(line, column)
    const node = area.current
    if (!node) return
    const lines = node.value.split(/\r?\n/)
    let offset = 0
    for (let i = 0; i < Math.min(line - 1, lines.length); i += 1) {
      offset += lines[i].length + 1
    }
    node.focus()
    node.setSelectionRange(offset + Math.max(0, column - 1), offset + Math.max(0, column - 1))
    node.scrollTop = Math.max(0, (line - 4) * 18)
  }

  /* ---------------------------------------------------------------- 顶栏 */
  const topbar = (
    <div className="stm-top">
      <button
        type="button"
        className="stm-symbol"
        onClick={() => setSymbolOpen((open) => !open)}
        aria-expanded={symbolOpen}
      >
        <b>{props.instrument.name}</b>
        {props.instrument.name !== props.instrument.symbol && (
          <small>{props.instrument.symbol}</small>
        )}
        <ChevronDown size={14} />
      </button>
      <div className="stm-periods">
        {props.periods.map((item) => (
          <button
            key={item.value}
            type="button"
            aria-pressed={props.period === item.value}
            onClick={() => props.onPeriod(item.value)}
          >
            {item.label}
          </button>
        ))}
      </div>
      <button
        type="button"
        className="stm-icon"
        onClick={props.onIndicators}
        aria-label="指标"
      >
        <SlidersHorizontal size={16} />
      </button>
      <button
        type="button"
        className="stm-icon"
        onClick={() => setPeriodOpen((open) => !open)}
        aria-label="更多周期"
      >
        <ChevronDown size={16} />
      </button>
    </div>
  )

  /* ------------------------------------------------------------ 标的抽屉 */
  const symbolSheet = symbolOpen && (
    <div className="stm-sheet" role="dialog" aria-label="切换标的">
      <div className="stm-sheet-inner">
        <header>
          <b>切换标的</b>
          <button type="button" onClick={() => setSymbolOpen(false)}>
            关闭
          </button>
        </header>
        <div className="stm-sheet-list">
          {props.symbolOptions.map((item) => (
            <button
              key={item.value}
              type="button"
              aria-pressed={item.value === undefined}
              onClick={() => {
                props.onSymbol(item.value)
                setSymbolOpen(false)
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  )

  const periodSheet = periodOpen && (
    <div className="stm-sheet" role="dialog" aria-label="切换周期">
      <div className="stm-sheet-inner">
        <header>
          <b>时间周期</b>
          <button type="button" onClick={() => setPeriodOpen(false)}>
            关闭
          </button>
        </header>
        <div className="stm-sheet-list">
          {props.periods.map((item) => (
            <button
              key={item.value}
              type="button"
              aria-pressed={props.period === item.value}
              onClick={() => {
                props.onPeriod(item.value)
                setPeriodOpen(false)
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  )

  /* ---------------------------------------------------------- 视图：图表 */
  const chartView = (
    <>
      <div className="stm-chart">
        {props.bars.length > 0 ? (
          <StudioChart
            bars={props.bars}
            orders={props.orders}
            storageKey={props.storageKey}
            focus={props.focus}
            onPick={props.onPick}
            plots={props.plots}
          />
        ) : (
          <div className="stm-empty">
            {props.loading ? '正在读取行情…' : props.failure || '暂无K线'}
            {!props.loading && (
              <button type="button" onClick={props.onReload}>
                重新读取
              </button>
            )}
          </div>
        )}
      </div>
      <div className="stm-actions">
        <button type="button" onClick={() => setView('code')}>
          <Code2 size={17} />
          <span>脚本</span>
        </button>
        <button
          type="button"
          className="stm-primary"
          disabled={props.busy || !props.canBacktest}
          onClick={props.onBacktest}
          title={props.canBacktest ? '开始回测' : props.backtestHint}
        >
          <Play size={16} />
          {props.busy ? '回测中…' : '开始回测'}
        </button>
        <button
          type="button"
          onClick={() => setView('report')}
          aria-label="回测结果"
          data-state={props.dockState}
        >
          <Table2 size={17} />
          <span>结果</span>
        </button>
      </div>
    </>
  )

  /* ---------------------------------------------------------- 视图：脚本 */
  const codeView = (
    <div className="stm-panel">
      <header className="stm-panel-head">
        <button type="button" className="stm-back" onClick={() => setView('chart')}>
          <ArrowLeft size={18} />
        </button>
        <input
          aria-label="脚本名称"
          value={props.draft.name}
          maxLength={60}
          onChange={(event) => props.onDraft({ ...props.draft, name: event.target.value })}
        />
        <span className="stm-badge">{scriptBadge}</span>
      </header>

      <div className="stm-editor">
        <pre className="stm-gutter" aria-hidden="true">
          {Array.from({ length: lineCount }, (_, index) => String(index + 1)).join('\n')}
        </pre>
        <textarea
          ref={area}
          aria-label="Pine 脚本"
          spellCheck={false}
          maxLength={20000}
          value={props.draft.source}
          onChange={(event) => props.onDraft({ ...props.draft, source: event.target.value })}
        />
      </div>

      {diagnostics.length > 0 && (
        <ul className="stm-diagnostics" aria-label="语法诊断">
          {diagnostics.map((item, index) => (
            <li key={index}>
              <button type="button" onClick={() => jump(item.line, item.column)}>
                第 {item.line} 行：{item.message}
              </button>
            </li>
          ))}
        </ul>
      )}
      {warnings.length > 0 && (
        <ul className="stm-warnings">
          {warnings.slice(0, 4).map((item, index) => (
            <li key={index}>{item}</li>
          ))}
        </ul>
      )}

      {props.inputs.length > 0 && (
        <div className="stm-inputs">
          <StudioPineInputs
            inputs={props.inputs}
            values={props.params as Record<string, never>}
            onChange={(next) => props.onParams(next as Record<string, unknown>)}
          />
        </div>
      )}

      <div className="stm-panel-actions">
        <button type="button" onClick={props.onCompile} disabled={props.compiling}>
          {props.compiling ? '检查中…' : '检查语法'}
        </button>
        <button type="button" onClick={props.onSave}>
          保存
        </button>
        <button type="button" onClick={props.onApply}>
          应用到图表
        </button>
        <button type="button" className="stm-primary" onClick={props.onBacktest} disabled={props.busy}>
          回测
        </button>
      </div>
    </div>
  )

  /* ---------------------------------------------------------- 视图：结果 */
  const reportView = (
    <div className="stm-panel">
      <header className="stm-panel-head">
        <button type="button" className="stm-back" onClick={() => setView('chart')}>
          <ArrowLeft size={18} />
        </button>
        <b>回测结果</b>
        <span className="stm-badge">{scriptBadge}</span>
      </header>
      <div className="stm-report">
        {props.dockState === 'running' && <p className="stm-hint">回测运行中…</p>}
        {props.dockState === 'error' && (
          <p className="stm-error">{props.notice || '回测失败'}</p>
        )}
        {props.dockState === 'idle' && (
          <div className="stm-hint">
            <p>还没有运行结果</p>
            <p>点击底部「开始回测」查看回测报告</p>
            <div className="stm-hint-actions">
              <button type="button" onClick={() => setView('code')}>
                脚本编辑
              </button>
              <button type="button" onClick={props.onTemplate}>
                从模板开始
              </button>
            </div>
          </div>
        )}
        {props.dockState === 'done' && (
          <>
            {props.stats && (
              <StudioPineStats
                stats={props.stats}
                trades={props.trades ?? []}
                equity={props.equity ?? []}
                dates={props.dates ?? []}
              />
            )}
            {props.result}
          </>
        )}
      </div>
    </div>
  )

  return (
    <div className="stm">
      {view === 'chart' && (
        <>
          {topbar}
          {chartView}
        </>
      )}
      {view === 'code' && codeView}
      {view === 'report' && reportView}
      {symbolSheet}
      {periodSheet}
    </div>
  )
}
