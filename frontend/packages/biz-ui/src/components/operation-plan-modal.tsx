/**
 * AI 每日操作计划弹窗。
 *
 * 一份计划 = 总体判断 + 若干情景(每个情景带触发条件/仓位/买入区间/止损价/目标价)
 * + 操作纪律 + 一句话总结 + 风险提示。
 *
 * 核心交互是每个价位旁边那颗「加入提醒」按钮 —— 这是把"计划"变成"纪律"的环节:
 * 计划写完没人盯等于没写,加成提醒后才会有东西在破位时叫你。
 *
 * 两个刻意的设计:
 * 1. 量化价位为 0 的项不显示按钮(模型没给这个价,别让用户点了个寂寞);
 * 2. 后端会拒绝"已达成"的目标价并给出原因,这里如实展示被跳过的项,不静默吞掉。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@panwatch/base-ui/components/ui/dialog'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import {
  operationPlansApi,
  type OperationPlan,
  type OperationScenario,
  type PlanAlertItem,
} from '@panwatch/api'

type Level = PlanAlertItem['level']

const ACTION_STYLE: Record<string, { label: string; cls: string }> = {
  buy: { label: '买入', cls: 'bg-red-500/10 text-red-600 border-red-500/30' },
  add: { label: '加仓', cls: 'bg-red-500/10 text-red-600 border-red-500/30' },
  reduce: { label: '减仓', cls: 'bg-green-500/10 text-green-600 border-green-500/30' },
  stop_loss: { label: '止损', cls: 'bg-amber-500/10 text-amber-600 border-amber-500/30' },
  observe: { label: '观察', cls: 'bg-muted text-muted-foreground border-border' },
}

const STATUS_LABEL: Record<string, string> = {
  pending: '待执行',
  executing: '执行中',
  completed: '已完成',
  expired: '已过期',
}

interface Props {
  open: boolean
  onOpenChange: (v: boolean) => void
  symbol: string
  market: string
  stockName?: string
  /** 外部已取到的计划(可选,避免重复请求) */
  plan?: OperationPlan | null
  /** 生成/加入提醒后通知外部刷新 */
  onChanged?: () => void
}

export default function OperationPlanModal({
  open,
  onOpenChange,
  symbol,
  market,
  stockName,
  plan: planProp,
  onChanged,
}: Props) {
  const { toast } = useToast()
  const [plan, setPlan] = useState<OperationPlan | null>(planProp ?? null)
  const [loading, setLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [rejected, setRejected] = useState<Array<{ level: string; title?: string; reason: string }>>([])
  const [alreadyTrue, setAlreadyTrue] = useState<string[]>([])

  const load = useCallback(async () => {
    if (!symbol) return
    setLoading(true)
    try {
      setPlan(await operationPlansApi.latest(symbol, market || 'CN'))
    } catch (e) {
      toast(e instanceof Error ? e.message : '加载操作计划失败', 'error')
    } finally {
      setLoading(false)
    }
  }, [symbol, market, toast])

  useEffect(() => {
    if (!open) return
    setRejected([])
    setAlreadyTrue([])
    if (planProp !== undefined) {
      setPlan(planProp)
    } else {
      void load()
    }
  }, [open, planProp, load])

  /** 该情景中可加入提醒的价位。量化值为 0 的不列 —— 模型没给这个价。 */
  const levelsOf = (s: OperationScenario): Array<{ level: Level; text: string }> => {
    const out: Array<{ level: Level; text: string }> = []
    if (s.stop_loss_num > 0) out.push({ level: 'stop_loss', text: `止损 ${s.stop_loss_num}` })
    if (s.target1_low > 0 || s.target1_high > 0) {
      const lo = s.target1_low, hi = s.target1_high
      out.push({ level: 'target1', text: `第一目标 ${lo && hi ? `${lo}-${hi}` : lo || hi}` })
    }
    if (s.target2_low > 0 || s.target2_high > 0) {
      const lo = s.target2_low, hi = s.target2_high
      out.push({ level: 'target2', text: `第二目标 ${lo && hi ? `${lo}-${hi}` : lo || hi}` })
    }
    if (s.entry_low > 0 && s.entry_high > 0) {
      out.push({ level: 'entry', text: `买入区间 ${s.entry_low}-${s.entry_high}` })
    }
    return out
  }

  const submit = useCallback(
    async (items: PlanAlertItem[]) => {
      if (!plan || !items.length) return
      setSubmitting(true)
      try {
        const res = await operationPlansApi.addAlerts(plan.id, items)
        setRejected(res.rejected || [])
        setAlreadyTrue(
          (res.created || [])
            .filter(c => c.already_true)
            .map(c => c.source_label || c.name)
        )
        if (res.created.length) {
          toast(
            `已加入 ${res.created.length} 条提醒${res.rejected.length ? `，${res.rejected.length} 条被跳过` : ''}`,
            'success'
          )
        } else {
          toast('没有可加入的提醒，请查看下方跳过原因', 'error')
        }
        onChanged?.()
        await load()
      } catch (e) {
        toast(e instanceof Error ? e.message : '加入提醒失败', 'error')
      } finally {
        setSubmitting(false)
      }
    },
    [plan, toast, onChanged, load]
  )

  const allItems = useMemo<PlanAlertItem[]>(() => {
    if (!plan?.scenarios) return []
    const items: PlanAlertItem[] = []
    plan.scenarios.forEach((s, i) => {
      levelsOf(s).forEach(({ level }) => items.push({ scenario_index: i, level }))
    })
    return items
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plan])

  const alertCount = plan?.alerts?.length ?? plan?.alert_count ?? 0

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[94vw] max-w-4xl max-h-[88vh] overflow-y-auto p-5 md:p-6">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            操作计划
            {plan && (
              <span className="text-sm font-normal text-muted-foreground">
                {stockName || plan.stock_name}（{plan.stock_symbol}） · {plan.plan_date}
              </span>
            )}
          </DialogTitle>
          <DialogDescription>
            把计划里的价位加入提醒，破位时才会有东西叫你 —— 计划写完没人盯，等于没写。
          </DialogDescription>
        </DialogHeader>

        {loading && <div className="py-10 text-center text-sm text-muted-foreground">加载中…</div>}

        {!loading && !plan && (
          <div className="py-10 text-center text-sm text-muted-foreground">
            还没有该股票的操作计划。
            <div className="mt-2 text-xs">
              可在「智能体」页为它绑定「操作计划」Agent，或手动触发一次生成。
            </div>
          </div>
        )}

        {!loading && plan && (
          <div className="space-y-4">
            {/* 顶部状态条 */}
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="rounded border border-border px-2 py-0.5 text-muted-foreground">
                {STATUS_LABEL[plan.status] || plan.status}
              </span>
              <span className="rounded border border-border px-2 py-0.5 text-muted-foreground">
                已加入 {alertCount} 条提醒
              </span>
              {allItems.length > 0 && (
                <Button
                  size="sm"
                  variant="outline"
                  className="ml-auto h-7 text-xs"
                  disabled={submitting}
                  onClick={() => submit(allItems)}
                >
                  全部加入提醒（{allItems.length}）
                </Button>
              )}
            </div>

            {/* 总体判断 */}
            {plan.overall_judgment && (
              <section>
                <h3 className="mb-1.5 text-sm font-semibold">总体判断</h3>
                <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-muted-foreground">
                  {plan.overall_judgment}
                </p>
              </section>
            )}

            {/* 情景方案 */}
            {plan.scenarios?.length > 0 && (
              <section>
                <h3 className="mb-2 text-sm font-semibold">具体操作方案</h3>
                <div className="space-y-3">
                  {plan.scenarios.map((s, i) => {
                    const style = ACTION_STYLE[s.action_type] || ACTION_STYLE.observe
                    const levels = levelsOf(s)
                    return (
                      <div
                        key={i}
                        className={`rounded-lg border p-3 ${
                          s.is_best ? 'border-red-500/40 bg-red-500/[0.03]' : 'border-border'
                        }`}
                      >
                        <div className="mb-1.5 flex flex-wrap items-center gap-2">
                          <span className={`rounded border px-1.5 py-0.5 text-[11px] ${style.cls}`}>
                            {style.label}
                          </span>
                          <span className="text-[13px] font-medium">{s.title}</span>
                          {s.is_best && (
                            <span className="text-[11px] text-red-600">最理想</span>
                          )}
                        </div>

                        <dl className="space-y-1 text-[12px] text-muted-foreground">
                          {s.condition && (
                            <div className="flex gap-1.5">
                              <dt className="shrink-0 text-foreground/70">触发条件</dt>
                              <dd>{s.condition}</dd>
                            </div>
                          )}
                          <div className="flex flex-wrap gap-x-4 gap-y-1">
                            {s.action && (
                              <span>
                                <span className="text-foreground/70">动作 </span>
                                {s.action}
                              </span>
                            )}
                            {s.position && (
                              <span>
                                <span className="text-foreground/70">仓位 </span>
                                {s.position}
                              </span>
                            )}
                          </div>
                          <div className="flex flex-wrap gap-x-4 gap-y-1">
                            {s.buy_price_range && (
                              <span>
                                <span className="text-foreground/70">买入区间 </span>
                                {s.buy_price_range}
                              </span>
                            )}
                            {s.stop_loss && (
                              <span className="text-amber-600">
                                <span className="text-foreground/70">止损 </span>
                                {s.stop_loss}
                              </span>
                            )}
                          </div>
                          {s.target1 && (
                            <div>
                              <span className="text-foreground/70">第一目标 </span>
                              {s.target1}
                            </div>
                          )}
                          {s.target2 && (
                            <div>
                              <span className="text-foreground/70">第二目标 </span>
                              {s.target2}
                            </div>
                          )}
                          {s.strategy && (
                            <div className="text-muted-foreground/80">{s.strategy}</div>
                          )}
                        </dl>

                        {levels.length > 0 ? (
                          <div className="mt-2 flex flex-wrap gap-1.5 border-t border-border/50 pt-2">
                            {levels.map(({ level, text }) => (
                              <Button
                                key={level}
                                size="sm"
                                variant="outline"
                                className="h-6 px-2 text-[11px]"
                                disabled={submitting}
                                onClick={() => submit([{ scenario_index: i, level }])}
                              >
                                + {text}
                              </Button>
                            ))}
                          </div>
                        ) : (
                          <div className="mt-2 border-t border-border/50 pt-2 text-[11px] text-muted-foreground">
                            该情景未给出可量化的价位，无法加入提醒
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              </section>
            )}

            {/* 条件当前已成立的提醒 —— 会在下一轮扫描立刻触发,如实告知 */}
            {alreadyTrue.length > 0 && (
              <section className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-3">
                <h3 className="mb-1 text-[13px] font-semibold text-amber-600">
                  以下提醒的条件当前已成立，下一轮扫描（60 秒内）就会触发
                </h3>
                <ul className="space-y-0.5 text-[12px] text-muted-foreground">
                  {alreadyTrue.map((t, i) => (
                    <li key={i}>{t}</li>
                  ))}
                </ul>
                <div className="mt-1 text-[11px] text-muted-foreground/80">
                  如果价格已经跌穿止损，这正是你要的提醒；如果是「突破后才生效」的移动止损，
                  说明标的还没突破，建议先停用该条。
                </div>
              </section>
            )}

            {/* 被跳过的项 */}
            {rejected.length > 0 && (
              <section className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-3">
                <h3 className="mb-1 text-[13px] font-semibold text-amber-600">
                  以下价位未加入提醒
                </h3>
                <ul className="space-y-0.5 text-[12px] text-muted-foreground">
                  {rejected.map((r, i) => (
                    <li key={i}>
                      {r.title ? `${r.title} · ` : ''}
                      {r.reason}
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {/* 操作纪律 —— 这块是给纪律用的,放在显眼位置 */}
            {plan.discipline?.length > 0 && (
              <section className="rounded-lg border border-border bg-muted/30 p-3">
                <h3 className="mb-1.5 text-sm font-semibold">操作纪律</h3>
                <ol className="list-decimal space-y-1 pl-5 text-[13px] text-muted-foreground">
                  {plan.discipline.map((d, i) => (
                    <li key={i}>
                      {d.principle && <span className="font-medium text-foreground">{d.principle}：</span>}
                      {d.detail}
                    </li>
                  ))}
                </ol>
              </section>
            )}

            {plan.summary && (
              <section>
                <h3 className="mb-1.5 text-sm font-semibold">一句话总结</h3>
                <p className="text-[13px] font-medium">{plan.summary}</p>
              </section>
            )}

            {plan.risk_warning && (
              <section>
                <h3 className="mb-1.5 text-sm font-semibold">风险提示</h3>
                <div className="prose prose-sm max-w-none text-[13px] text-muted-foreground">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{plan.risk_warning}</ReactMarkdown>
                </div>
              </section>
            )}

            {/* 完整报告(折叠) */}
            {plan.content && (
              <details className="rounded-lg border border-border p-3">
                <summary className="cursor-pointer text-[13px] font-medium">
                  查看完整报告原文
                </summary>
                <div className="prose prose-sm mt-2 max-w-none text-[13px]">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{plan.content}</ReactMarkdown>
                </div>
              </details>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
