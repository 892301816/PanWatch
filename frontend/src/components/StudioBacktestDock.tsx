import { useEffect, useRef, useState } from 'react'
import type {
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
  ReactNode,
} from 'react'
import { ChevronDown, ChevronUp, Loader2 } from 'lucide-react'
import type { StudioStats, StudioTrade } from '@panwatch/api'
import StudioPineStats, { StudioPineTrades } from './StudioPineStats'

type Props = {
  state: 'idle' | 'running' | 'done' | 'error'
  error?: string | null
  stats?: StudioStats | null
  /** 统计页标题/说明：Pine 预览用默认观潮口径，回测报告用报告口径。 */
  statsHeading?: string
  statsNote?: string
  trades?: StudioTrade[]
  equity?: number[]
  dates?: string[]
  /** 现有 ResultChart + 成交行，作为「收益」tab 的主体。 */
  result?: ReactNode
  onEdit: () => void
  onTemplate: () => void
  height: number
  onHeight: (height: number) => void
  /** 追加：失败时允许直接重试；不传则只保留编辑入口。 */
  onBacktest?: () => void
}

export const DOCK_HEIGHT_KEY = 'pw:studio:dock-height'
export const DOCK_MIN_HEIGHT = 120
export const DOCK_DEFAULT_HEIGHT = 280

/** 拖动时的高度上限：60vh，与 CSS 的 max-height 保持一致。 */
const maxHeight = () =>
  typeof window === 'undefined'
    ? 720
    : Math.max(DOCK_MIN_HEIGHT, Math.round(window.innerHeight * 0.6))

const clamp = (value: number) =>
  Math.min(maxHeight(), Math.max(DOCK_MIN_HEIGHT, Math.round(value)))

type DockTab = 'return' | 'trades' | 'stats'

export default function StudioBacktestDock({
  state,
  error,
  stats,
  statsHeading,
  statsNote,
  trades,
  equity,
  dates,
  result,
  onEdit,
  onTemplate,
  height,
  onHeight,
  onBacktest,
}: Props) {
  const [tab, setTab] = useState<DockTab>('return')
  const [dragging, setDragging] = useState(false)
  const drag = useRef<{ startY: number; startHeight: number }>()
  const heightRef = useRef(height)
  heightRef.current = height
  const lastHeight = useRef(height)
  const onHeightRef = useRef(onHeight)
  onHeightRef.current = onHeight

  useEffect(() => {
    if (!dragging) return
    const move = (event: PointerEvent) => {
      const start = drag.current
      if (!start) return
      // 向上拖动增大高度：起点高度 + (起点 Y - 当前 Y)
      lastHeight.current = clamp(
        start.startHeight + (start.startY - event.clientY),
      )
      onHeightRef.current(lastHeight.current)
    }
    const stop = () => {
      drag.current = undefined
      setDragging(false)
      try {
        localStorage.setItem(DOCK_HEIGHT_KEY, String(lastHeight.current))
      } catch {}
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', stop)
    window.addEventListener('pointercancel', stop)
    return () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', stop)
      window.removeEventListener('pointercancel', stop)
    }
  }, [dragging])

  function startDrag(event: ReactPointerEvent<HTMLButtonElement>) {
    event.preventDefault()
    lastHeight.current = height
    drag.current = { startY: event.clientY, startHeight: height }
    setDragging(true)
  }

  function resizeBy(delta: number) {
    const next = clamp(heightRef.current + delta)
    lastHeight.current = next
    onHeight(next)
    try {
      localStorage.setItem(DOCK_HEIGHT_KEY, String(next))
    } catch {}
  }

  function onHandleKey(event: ReactKeyboardEvent<HTMLButtonElement>) {
    if (event.key === 'ArrowUp') {
      event.preventDefault()
      resizeBy(24)
    } else if (event.key === 'ArrowDown') {
      event.preventDefault()
      resizeBy(-24)
    } else if (event.key === 'Home') {
      event.preventDefault()
      resizeBy(DOCK_MIN_HEIGHT - heightRef.current)
    }
  }

  return (
    <section
      className={
        'st-dock' + (dragging ? ' st-dock-dragging' : '')
      }
      aria-label="回测结果"
      data-state={state}
      style={{ height: clamp(height) }}
    >
      <div className="st-dock-bar">
        <button
          type="button"
          className="st-dock-handle"
          role="separator"
          aria-orientation="horizontal"
          aria-label="拖动调整回测结果高度"
          aria-valuemin={DOCK_MIN_HEIGHT}
          aria-valuemax={maxHeight()}
          aria-valuenow={clamp(height)}
          title="拖到想要的边界即可；双击在展开/收起之间切换"
          onPointerDown={startDrag}
          onKeyDown={onHandleKey}
          onDoubleClick={() => {
            const expanded = Math.round(
              (typeof window === 'undefined' ? 900 : window.innerHeight) * 0.55,
            )
            onHeight(
              clamp(height) > DOCK_MIN_HEIGHT + 12 ? DOCK_MIN_HEIGHT : expanded,
            )
          }}
        />
        <button
          type="button"
          className="st-dock-toggle"
          aria-label={clamp(height) > DOCK_MIN_HEIGHT + 12 ? '收起回测面板' : '展开回测面板'}
          title={clamp(height) > DOCK_MIN_HEIGHT + 12 ? '收起' : '展开'}
          onClick={() => {
            const expanded = Math.round(
              (typeof window === 'undefined' ? 900 : window.innerHeight) * 0.55,
            )
            onHeight(
              clamp(height) > DOCK_MIN_HEIGHT + 12 ? DOCK_MIN_HEIGHT : expanded,
            )
          }}
        >
          {clamp(height) > DOCK_MIN_HEIGHT + 12 ? (
            <ChevronDown size={13} />
          ) : (
            <ChevronUp size={13} />
          )}
        </button>
      </div>

      {state === 'idle' && (
        <div className="st-dock-body">
          <div className="st-empty-state">
            <h3>还没有运行结果</h3>
            <p>加载模板或编写脚本后，点击「开始回测」查看回测报告</p>
            <p className="st-empty-hint">也可使用快捷键 Ctrl+Enter</p>
            <div className="st-empty-actions">
              <button type="button" onClick={onEdit}>
                脚本编辑
              </button>
              <button type="button" className="st-accent" onClick={onTemplate}>
                从模板开始
              </button>
            </div>
          </div>
        </div>
      )}

      {state === 'running' && (
        <div className="st-dock-body">
          <div className="st-dock-loading" role="status">
            <Loader2 size={18} className="st-spin" />
            回测运行中…
          </div>
        </div>
      )}

      {state === 'error' && (
        <div className="st-dock-body">
          <div className="st-empty-state" role="alert">
            <h3>回测失败</h3>
            <p className="st-empty-error">{error || '未知错误，请稍后重试。'}</p>
            <div className="st-empty-actions">
              <button type="button" onClick={onEdit}>
                检查脚本
              </button>
              {onBacktest && (
                <button type="button" className="st-accent" onClick={onBacktest}>
                  重新回测
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      {state === 'done' && (
        <>
          <div className="st-dock-tabs" role="tablist" aria-label="回测结果视图">
            {(
              [
                ['return', '收益'],
                ['trades', '成交'],
                ['stats', '统计'],
              ] as const
            ).map(([id, label]) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={tab === id}
                onClick={() => setTab(id)}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="st-dock-body" role="tabpanel">
            {tab === 'return' &&
              (result || (
                <p className="st-pine-empty">本次回测没有可展示的收益曲线。</p>
              ))}
            {tab === 'trades' &&
              (trades?.length ? (
                <StudioPineTrades trades={trades} />
              ) : (
                <p className="st-pine-empty">该脚本未产生成交</p>
              ))}
            {tab === 'stats' &&
              (stats ? (
                <StudioPineStats
                  stats={stats}
                  trades={trades ?? []}
                  equity={equity ?? []}
                  dates={dates ?? []}
                  showTrades={false}
                  showEquity={!!equity?.length}
                  heading={statsHeading}
                  note={statsNote}
                  label={statsHeading}
                />
              ) : (
                <p className="st-pine-empty">当前脚本没有统计口径。</p>
              ))}
          </div>
        </>
      )}
    </section>
  )
}
