import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Database,
  Loader2,
  Play,
  RefreshCw,
  Save,
  XCircle,
} from 'lucide-react'
import { datasetApi, type DatasetStatus } from '@panwatch/api'

const fmtNum = (v: number | null | undefined) =>
  v == null ? '--' : v >= 10000 ? `${(v / 10000).toFixed(1)} 万` : String(v)

export default function DatasetPanel() {
  const [status, setStatus] = useState<DatasetStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [syncDays, setSyncDays] = useState(1095)
  const [forceProvider, setForceProvider] = useState('')
  const [scheduleHour, setScheduleHour] = useState(15)
  const [scheduleMinute, setScheduleMinute] = useState(45)
  const [scheduleEnabled, setScheduleEnabled] = useState(true)
  const [savingSchedule, setSavingSchedule] = useState(false)
  const pollRef = useRef<number | null>(null)

  const load = useCallback(async () => {
    try {
      const data = await datasetApi.status()
      setStatus(data)
      setError('')
      return data
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败')
      return null
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (!status) return
    setScheduleHour(status.schedule.hour)
    setScheduleMinute(status.schedule.minute)
    setScheduleEnabled(status.schedule.enabled)
  }, [status?.schedule.hour, status?.schedule.minute, status?.schedule.enabled])

  const running = !!status?.progress?.running

  useEffect(() => {
    if (!running) {
      if (pollRef.current) {
        window.clearInterval(pollRef.current)
        pollRef.current = null
      }
      return
    }
    if (pollRef.current) return
    pollRef.current = window.setInterval(async () => {
      const data = await datasetApi.status().catch(() => null)
      if (data) {
        setStatus(data)
        if (!data.progress.running && pollRef.current) {
          window.clearInterval(pollRef.current)
          pollRef.current = null
        }
      }
    }, 2500)
    return () => {
      if (pollRef.current) {
        window.clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
  }, [running])

  const providers = status?.providers || []

  const startSync = async (mode: 'incremental' | 'full') => {
    try {
      setError('')
      await datasetApi.startSync({
        mode,
        days: mode === 'full' ? syncDays : undefined,
        provider: forceProvider || null,
        allow_fallback: true,
      })
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : '同步启动失败')
    }
  }

  const saveSchedule = async () => {
    setSavingSchedule(true)
    try {
      await datasetApi.updateSchedule({
        enabled: scheduleEnabled,
        hour: scheduleHour,
        minute: scheduleMinute,
      })
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSavingSchedule(false)
    }
  }

  const pct = (() => {
    const p = status?.progress
    if (!p || !p.running) return 0
    if (!p.total) return 8
    return Math.min(100, Math.max(5, Math.round((p.done / p.total) * 100)))
  })()

  return (
    <div className="card p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Database className="h-4 w-4 text-primary" />
        <h2 className="text-[13px] font-semibold text-muted-foreground">日K本地库</h2>
        <span className="text-[11px] text-muted-foreground">
          数据源在「K线数据」分区管理，这里只负责同步与定时
        </span>
        <button
          onClick={() => load()}
          className="ml-auto flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] hover:bg-accent"
        >
          <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
          刷新
        </button>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
        {[
          ['本地行数', fmtNum(status?.stats.rows)],
          ['交易日', `${status?.stats.days ?? '--'} 天`],
          ['覆盖个股', `${status?.stats.symbols ?? '--'} 只`],
          ['最新日期', status?.sync.last_date || '--'],
        ].map(([k, v]) => (
          <div key={k} className="rounded-md border bg-muted/20 p-2">
            <div className="text-[10px] text-muted-foreground">{k}</div>
            <div className="mt-0.5 text-sm font-semibold">{v}</div>
          </div>
        ))}
      </div>

      {status?.stale && (
        <div className="mt-2 flex items-center gap-2 rounded-md border border-amber-500/30 bg-amber-500/5 p-2 text-[11px] text-amber-600 dark:text-amber-400">
          <AlertTriangle className="h-3.5 w-3.5" />
          本地数据落后于最近交易日，建议执行一次增量同步
        </div>
      )}
      {error && <div className="mt-2 text-[11px] text-destructive">{error}</div>}

      <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
        <select
          value={forceProvider}
          onChange={(e) => setForceProvider(e.target.value)}
          className="rounded border bg-background px-2 py-1"
        >
          <option value="">自动（按 K 线数据源优先级）</option>
          {providers.map((p) => (
            <option key={p.id} value={p.provider}>
              强制 {p.provider}
            </option>
          ))}
        </select>
        <select
          value={syncDays}
          onChange={(e) => setSyncDays(Number(e.target.value))}
          className="rounded border bg-background px-2 py-1"
        >
          <option value={365}>全量 1 年</option>
          <option value={1095}>全量 3 年</option>
          <option value={3650}>全量 10 年</option>
        </select>
        <button
          onClick={() => startSync('incremental')}
          disabled={running}
          className="flex items-center gap-1 rounded bg-primary px-3 py-1 text-primary-foreground disabled:opacity-50"
        >
          <Play className="h-3 w-3" />
          增量同步
        </button>
        <button
          onClick={() => startSync('full')}
          disabled={running}
          className="rounded border px-3 py-1 hover:bg-accent disabled:opacity-50"
        >
          全量同步
        </button>
      </div>

      <div className="mt-3">
        <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
          {running ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : status?.sync.status === 'error' ? (
            <XCircle className="h-3 w-3 text-destructive" />
          ) : (
            <CheckCircle2 className="h-3 w-3 text-emerald-500" />
          )}
          <span>
            {running
              ? status?.progress.message || '同步中…'
              : status?.sync.status === 'error'
                ? `上次失败：${status.sync.error}`
                : status?.sync.finished_at
                  ? `上次同步 ${status.sync.last_mode} · ${status.sync.rows} 行 · ${status.sync.finished_at.replace('T', ' ').slice(0, 16)}`
                  : '尚未同步'}
          </span>
          {running && status?.progress.provider && (
            <span className="rounded bg-muted px-1.5 py-0.5">{status.progress.provider}</span>
          )}
        </div>
        <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted">
          <div
            className="h-full rounded-full bg-primary transition-all"
            style={{ width: `${running ? pct : status?.sync.status === 'ok' ? 100 : 0}%` }}
          />
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3 border-t pt-3 text-xs">
        <span className="flex items-center gap-1 text-[11px] text-muted-foreground">
          <Clock className="h-3.5 w-3.5" />
          每日自动同步
        </span>
        <label className="flex items-center gap-1.5">
          <input
            type="checkbox"
            checked={scheduleEnabled}
            onChange={(e) => setScheduleEnabled(e.target.checked)}
          />
          启用
        </label>
        <div className="flex items-center gap-1">
          <input
            type="number"
            min={0}
            max={23}
            value={scheduleHour}
            onChange={(e) => setScheduleHour(Number(e.target.value))}
            className="w-16 rounded border bg-background px-2 py-1"
          />
          <span className="text-muted-foreground">:</span>
          <input
            type="number"
            min={0}
            max={59}
            value={scheduleMinute}
            onChange={(e) => setScheduleMinute(Number(e.target.value))}
            className="w-16 rounded border bg-background px-2 py-1"
          />
        </div>
        <button
          onClick={saveSchedule}
          disabled={savingSchedule}
          className="flex items-center gap-1 rounded bg-primary px-3 py-1 text-primary-foreground disabled:opacity-50"
        >
          {savingSchedule ? <Loader2 className="h-3 w-3 animate-spin" /> : <Save className="h-3 w-3" />}
          保存
        </button>
        <span className="text-[11px] text-muted-foreground">
          {status?.schedule.next_run_at
            ? `下次执行 ${status.schedule.next_run_at.replace('T', ' ').slice(0, 16)}`
            : '未安排'}
        </span>
      </div>

      <div className="mt-3 border-t pt-3 text-[10px] leading-relaxed text-muted-foreground">
        日K按「K线数据」分区的优先级自动选源（同花顺 → TickFlow → 通达信），失败自动切换。
        日K为原始价，成交量单位为手。数据仅供研究参考，不构成投资建议。
      </div>
    </div>
  )
}
