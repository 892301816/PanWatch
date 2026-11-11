import { useCallback, useEffect, useRef, useState } from 'react'
import { Loader2, RefreshCw, ShieldAlert, ShieldCheck, ShieldQuestion } from 'lucide-react'
import { strategyApi, type StrategyCheckItem, type StrategyGate } from '@panwatch/api'

/**
 * 买卖前体检:打开交易弹窗时自动跑一次「规则 + 形态 + AI」。
 *
 * - 结论只来自后端 gate:策略不满足 / 禁买命中 → 不建议,AI 只能下调不能上调;
 * - 没配 AI 也能用,只显示规则结论与形态标签;
 * - 价格 / 数量变化时自动重跑(180 秒内同价档命中后端缓存,不再等 AI)。
 */
export default function TradeGate({
  symbol,
  side,
  market = 'CN',
  price,
  quantity,
  enabled = true,
  onGate,
}: {
  symbol: string
  side: 'buy' | 'sell'
  market?: string
  price?: number
  quantity?: number
  enabled?: boolean
  onGate?: (gate: StrategyGate | null) => void
}) {
  const [gate, setGate] = useState<StrategyGate | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [detailOpen, setDetailOpen] = useState(false)
  const seqRef = useRef(0)
  const onGateRef = useRef(onGate)
  onGateRef.current = onGate

  const run = useCallback(
    async (useAi: boolean) => {
      if (!symbol || !enabled) return
      const seq = ++seqRef.current
      setLoading(true)
      setError('')
      try {
        const result = await strategyApi.gate({
          symbol,
          side,
          market,
          price: price && price > 0 ? price : undefined,
          quantity: quantity && quantity > 0 ? quantity : undefined,
          use_ai: useAi,
        })
        if (seq !== seqRef.current) return
        setGate(result)
        onGateRef.current?.(result)
      } catch (e) {
        if (seq !== seqRef.current) return
        setGate(null)
        onGateRef.current?.(null)
        setError(e instanceof Error ? e.message : '体检失败')
      } finally {
        if (seq === seqRef.current) setLoading(false)
      }
    },
    [symbol, side, market, price, quantity, enabled],
  )

  useEffect(() => {
    if (!enabled || !symbol) return
    // 价格/数量在弹窗里会被连续输入,防抖 350ms 再请求;后端还有 180s 同价档缓存
    const timer = window.setTimeout(() => {
      void run(true)
    }, 350)
    return () => {
      window.clearTimeout(timer)
      seqRef.current += 1
    }
  }, [run, enabled, symbol])

  if (!enabled) return null

  const verdict = gate?.verdict || ''
  const tone =
    verdict === '可操作'
      ? 'up'
      : verdict === '接近'
        ? 'warn'
        : verdict === '不建议'
          ? 'down'
          : 'flat'
  const color =
    tone === 'up'
      ? 'text-stock-up'
      : tone === 'warn'
        ? 'text-amber-500'
        : tone === 'down'
          ? 'text-stock-down'
          : 'text-muted-foreground'
  const border =
    tone === 'up'
      ? 'border-stock-up/30 bg-stock-up/5'
      : tone === 'warn'
        ? 'border-amber-500/30 bg-amber-500/5'
        : tone === 'down'
          ? 'border-stock-down/30 bg-stock-down/5'
          : 'border-border bg-muted/20'
  const Icon = tone === 'up' ? ShieldCheck : tone === 'down' ? ShieldAlert : ShieldQuestion
  const checks: Array<{ item: StrategyCheckItem; avoid: boolean }> =
    side === 'buy'
      ? [
          ...(gate?.buy_checks || []).map((item) => ({ item, avoid: false })),
          ...(gate?.avoid_checks || []).map((item) => ({ item, avoid: true })),
        ]
      : (gate?.sell_checks || []).map((item) => ({ item, avoid: false }))
  const ai = gate?.ai
  const aiTone = ai?.tone === '支撑' ? 'up' : ai?.tone === '反对' ? 'down' : 'warn'

  return (
    <div className={`rounded-lg border p-2.5 text-[11px] ${border}`}>
      <div className="flex items-center gap-1.5">
        <Icon className={`h-3.5 w-3.5 shrink-0 ${color}`} />
        <span className="font-medium text-foreground">
          {side === 'buy' ? '买入前体检' : '卖出前体检'}
        </span>
        {loading && !gate ? (
          <span className="flex items-center gap-1 text-muted-foreground">
            <Loader2 className="h-3 w-3 animate-spin" />
            分析中…
          </span>
        ) : (
          <span className={`font-medium ${color}`}>{verdict || '—'}</span>
        )}
        {gate?.cached && <span className="text-muted-foreground">（缓存）</span>}
        <span className="ml-auto flex shrink-0 items-center gap-1.5">
          {gate && gate.has_playbook && (
            <span className="max-w-[92px] truncate text-muted-foreground" title={gate.playbook_name}>
              {gate.playbook_name || '策略卡'}
            </span>
          )}
          <button
            type="button"
            className="rounded p-0.5 text-muted-foreground hover:text-primary disabled:opacity-50"
            title="重新分析（含 AI）"
            disabled={loading}
            onClick={() => void run(true)}
          >
            {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
          </button>
        </span>
      </div>

      {error && <div className="mt-1 text-destructive">{error}</div>}

      {gate && (
        <>
          <div className="mt-1.5 leading-relaxed text-foreground/90">{gate.summary}</div>

          {ai && ai.ok && (
            <div className="mt-1.5 rounded border border-border/50 bg-background/40 p-1.5">
              <span
                className={`font-medium ${
                  aiTone === 'up' ? 'text-stock-up' : aiTone === 'down' ? 'text-stock-down' : 'text-amber-500'
                }`}
              >
                AI {ai.verdict}
              </span>
              {ai.reason && <span className="text-foreground/90">：{ai.reason}</span>}
              {ai.risk && <div className="mt-0.5 text-muted-foreground">风险：{ai.risk}</div>}
            </div>
          )}
          {ai && !ai.ok && ai.enabled && (
            <div className="mt-1 text-muted-foreground">
              AI 暂未返回（{ai.error || '超时'}），先按规则与形态判断。
            </div>
          )}
          {ai && !ai.enabled && (
            <div className="mt-1 text-muted-foreground">
              未配置 AI 模型，本次只按规则与形态判断。
            </div>
          )}

          {gate.pattern?.tags?.length ? (
            <div className="mt-1.5 flex flex-wrap gap-1">
              {gate.pattern.tags.map((tag) => (
                <span
                  key={tag}
                  className="rounded bg-background/60 px-1.5 py-0.5 text-[10px] text-muted-foreground"
                >
                  {tag}
                </span>
              ))}
            </div>
          ) : null}

          {gate.reasons.length > 0 && (
            <ul className="mt-1.5 space-y-0.5 text-muted-foreground">
              {gate.reasons.slice(0, 4).map((reason, i) => (
                <li key={i} className="leading-snug">
                  · {reason}
                </li>
              ))}
            </ul>
          )}

          {checks.length > 0 && (
            <div className="mt-1.5">
              <button
                type="button"
                className="text-[10px] text-muted-foreground hover:text-primary"
                onClick={() => setDetailOpen((v) => !v)}
              >
                {detailOpen ? '收起逐条对照' : `逐条对照（${checks.length} 条）`}
              </button>
              {detailOpen && (
                <div className="mt-1 space-y-0.5">
                  {checks.map(({ item, avoid }) => (
                    <CheckRow key={`${item.field}-${item.expect}-${avoid}`} item={item} avoid={avoid} />
                  ))}
                </div>
              )}
            </div>
          )}

          {gate.pattern?.summary && (
            <div className="mt-1.5 leading-snug text-muted-foreground">{gate.pattern.summary}</div>
          )}

          <div className="mt-1 text-[10px] text-muted-foreground/80">
            {gate.as_of ? `${gate.as_of}${gate.estimated ? '（盘中估算）' : ''}` : ''}
            {gate.elapsed_ms ? ` · ${(gate.elapsed_ms / 1000).toFixed(1)}s` : ''}
          </div>
        </>
      )}

      {loading && gate && (
        <div className="mt-1 flex items-center gap-1 text-[10px] text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" /> 正在重新分析…
        </div>
      )}
    </div>
  )
}

/** 结论是否为「不建议」:交易弹窗据此要求二次确认。 */
export function isGateBlocked(gate: StrategyGate | null | undefined): boolean {
  return gate?.verdict === '不建议'
}

function CheckRow({ item, avoid }: { item: StrategyCheckItem; avoid: boolean }) {
  // 买入条件:命中是好事(绿勾);禁买条件:命中是坏事(红叉)
  const good = avoid ? !item.ok : item.ok
  return (
    <div className="flex items-start gap-1.5 leading-snug">
      <span className={`shrink-0 ${good ? 'text-stock-up' : 'text-muted-foreground'}`}>
        {avoid ? (item.ok ? '✗' : '✓') : item.ok ? '✓' : '✗'}
      </span>
      <span className="min-w-0 flex-1">
        {avoid && <span className="mr-1 text-[9px] text-muted-foreground">禁买</span>}
        <span className="text-foreground/90">{item.label}</span>
        <span className="text-muted-foreground">
          {' '}
          {item.actual} <span className="opacity-70">（要求 {item.expect}）</span>
        </span>
      </span>
    </div>
  )
}
