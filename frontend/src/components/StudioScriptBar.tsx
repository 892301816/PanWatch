import {
  Braces,
  ChevronDown,
  Download,
  LayoutGrid,
  Play,
  Star,
  Table2,
} from 'lucide-react'
import type { StudioScriptKind } from './StudioTopbar'

type Props = {
  strategyName: string
  scriptKind: StudioScriptKind
  busy: boolean
  onEdit: () => void
  onBacktest: () => void
  onBacktestOptions: () => void
  onFavorite: () => void
  onGallery: () => void
  onKnowledge: () => void
  favorite: boolean
  /** 追加：当前标的不支持回测时禁用主按钮，用 title 说明原因。 */
  canBacktest?: boolean
  backtestHint?: string
  /** 追加：打开回测结果弹窗 */
  onReport?: () => void
  reportState?: 'idle' | 'running' | 'done' | 'error'
  /** 策略下拉：脚本与条件策略都列出 */
  strategies?: Array<{ id: number; name: string; kind: 'script' | 'conditions' }>
  strategyValue?: number
  onStrategyChange?: (id: number) => void
}

const kindLabel = (kind: StudioScriptKind) =>
  kind === 'strategy'
    ? 'Pine Script · 策略'
    : kind === 'indicator'
      ? 'Pine Script · 指标'
      : '四语句脚本'

export default function StudioScriptBar({
  strategyName,
  scriptKind,
  busy,
  onEdit,
  onBacktest,
  onBacktestOptions,
  onFavorite,
  onGallery,
  onKnowledge,
  favorite,
  canBacktest = true,
  strategies = [],
  strategyValue,
  onStrategyChange,
  backtestHint,
  onReport,
  reportState = 'idle',
}: Props) {
  return (
    <div className="st-scriptbar" aria-label="脚本工具条">
      <div className="st-scriptbar-actions">
        <select
          aria-label="选择策略"
          className="st-strategy-select"
          value={strategyValue ?? ''}
          onChange={(event) => onStrategyChange?.(Number(event.target.value))}
          title="切换图表与回测使用的策略（脚本 / 条件都在这里）"
        >
          <option value="" disabled>选择策略</option>
          <optgroup label="条件策略">
            {strategies.filter((item) => item.kind === 'conditions').map((item) => (
              <option key={item.id} value={item.id}>{item.name}</option>
            ))}
          </optgroup>
          <optgroup label="脚本策略">
            {strategies.filter((item) => item.kind === 'script').map((item) => (
              <option key={item.id} value={item.id}>{item.name}</option>
            ))}
          </optgroup>
        </select>
        <button type="button" onClick={onEdit} title="打开脚本编辑器">
          <Braces size={15} />
          编辑
        </button>
        <div className="st-split">
          <button
            type="button"
            className="st-split-main"
            disabled={busy || !canBacktest}
            title={!canBacktest ? backtestHint : '按当前脚本与参数开始回测（Ctrl+Enter）'}
            onClick={onBacktest}
          >
            <Play size={14} />
            {busy ? '回测中…' : '开始回测'}
          </button>
          <button
            type="button"
            className="st-split-more"
            aria-label="回测参数"
            title="回测参数：区间、资金、仓位与成交时点"
            onClick={onBacktestOptions}
          >
            <ChevronDown size={14} />
          </button>
        </div>
        <button
          type="button"
          aria-pressed={favorite}
          onClick={onFavorite}
          title={favorite ? '取消收藏当前脚本' : '收藏当前脚本到草稿架'}
        >
          <Star size={15} fill={favorite ? 'currentColor' : 'none'} />
          {favorite ? '已收藏' : '收藏'}
        </button>
        <button type="button" onClick={onGallery} title="打开我的策略">
          <LayoutGrid size={15} />
          我的策略
        </button>
        {onReport && (
          <button
            type="button"
            onClick={onReport}
            title="打开回测结果"
            data-state={reportState}
          >
            <Table2 size={15} />
            回测结果
          </button>
        )}
        <button
          type="button"
          onClick={onKnowledge}
          title="下载 Pine 函数知识库"
        >
          <Download size={15} />
          下载函数知识库
        </button>
      </div>
      <div className="st-scriptbar-title">
        <b title={strategyName}>{strategyName}</b>
        <span className={'st-badge' + (scriptKind === 'v1' ? '' : ' pine')}>
          {kindLabel(scriptKind)}
        </span>
      </div>
    </div>
  )
}
