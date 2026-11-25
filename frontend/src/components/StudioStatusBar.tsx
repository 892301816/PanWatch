type Props = {
  state: 'ready' | 'running' | 'error'
  detail?: string
  line: number
  column: number
}

const labels = {
  ready: '就绪',
  running: '运行中',
  error: '失败',
} as const

export default function StudioStatusBar({
  state,
  detail,
  line,
  column,
}: Props) {
  return (
    <div className="st-statusbar" data-state={state}>
      <span className="st-status-dot" aria-hidden="true" />
      <span
        className="st-status-text"
        role="status"
        aria-live="polite"
        title={detail}
      >
        {labels[state]}
        {detail ? <small> · {detail}</small> : null}
      </span>
      <span className="st-status-cursor">
        行 {Math.max(1, line)}, 列 {Math.max(1, column)}
      </span>
    </div>
  )
}
