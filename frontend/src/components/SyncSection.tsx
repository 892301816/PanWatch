import { useCallback, useEffect, useRef, useState, type ElementType } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  Copy,
  FileSpreadsheet,
  KeyRound,
  Loader2,
  RefreshCw,
  Smartphone,
  Upload,
} from 'lucide-react'
import {
  fetchAPI,
  patsApi,
  syncApi,
  type ImportCommit,
  type ImportPreview,
  type SyncParsed,
  type SyncResult,
} from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { useToast } from '@panwatch/base-ui/components/ui/toast'

type AccountOption = { id: number; name: string }

const SAMPLE_TEXT =
  '【华泰证券】您尾号1234账户于2026年11月11日10:31:05以29.50元买入002943宇晶股份200股，成交手续费5.00元。'

function toBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer)
  let binary = ''
  const chunk = 0x8000
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk))
  }
  return btoa(binary)
}

function decodeText(buffer: ArrayBuffer): string {
  const utf8 = new TextDecoder('utf-8').decode(buffer)
  if (!utf8.includes('\uFFFD')) return utf8
  try {
    return new TextDecoder('gb18030').decode(buffer)
  } catch {
    return utf8
  }
}

function statusChip(status: string) {
  if (status === 'ok') return 'bg-stock-up/15 text-stock-up'
  if (status === 'new_stock') return 'bg-sky-500/15 text-sky-600'
  if (status === 'duplicate') return 'bg-amber-500/15 text-amber-600'
  return 'bg-stock-down/15 text-stock-down'
}

const STATUS_LABEL: Record<string, string> = {
  ok: '可导入',
  new_stock: '可导入（建自选）',
  duplicate: '重复',
  invalid: '跳过',
}

/** 交易同步面板：令牌、文本试解析、交割单导入（理财页弹窗内嵌，也可单独成卡片）。 */
export default function SyncSection({ embedded = false }: { embedded?: boolean } = {}) {
  const { toast } = useToast()
  const [accounts, setAccounts] = useState<AccountOption[]>([])
  const [accountId, setAccountId] = useState<number | null>(null)
  const [token, setToken] = useState<string | null>(null)
  const [tokenName, setTokenName] = useState('手机成交同步')
  const [creating, setCreating] = useState(false)

  const [text, setText] = useState('')
  const [parsed, setParsed] = useState<SyncParsed | null>(null)
  const [textResult, setTextResult] = useState<SyncResult | null>(null)
  const [textBusy, setTextBusy] = useState(false)

  const [fileName, setFileName] = useState('')
  const [filePayload, setFilePayload] = useState<{ content?: string; base64?: string; filename: string } | null>(null)
  const [preview, setPreview] = useState<ImportPreview | null>(null)
  const [commitResult, setCommitResult] = useState<ImportCommit | null>(null)
  const [importBusy, setImportBusy] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  const loadAccounts = useCallback(async () => {
    try {
      const rows = await fetchAPI<AccountOption[]>('/accounts')
      setAccounts(rows || [])
      setAccountId((prev) => prev ?? rows?.[0]?.id ?? null)
    } catch (e) {
      toast(e instanceof Error ? e.message : '账户列表加载失败', 'error')
    }
  }, [toast])

  useEffect(() => {
    void loadAccounts()
  }, [loadAccounts])

  const createToken = async () => {
    setCreating(true)
    try {
      const created = await patsApi.create({
        name: tokenName.trim() || '手机成交同步',
        scopes: ['trade:write'],
        expires_in_days: 365,
      })
      setToken(created.token)
      toast('同步令牌已创建，明文只显示这一次', 'success')
    } catch (e) {
      toast(e instanceof Error ? e.message : '创建失败', 'error')
    } finally {
      setCreating(false)
    }
  }

  const copy = (value: string) => {
    navigator.clipboard?.writeText(value)
    toast('已复制到剪贴板', 'success')
  }

  const ingest = async (dryRun: boolean) => {
    if (!text.trim()) {
      toast('先粘贴一条成交短信/通知', 'error')
      return
    }
    setTextBusy(true)
    try {
      const result = await syncApi.ingest({
        text: text.trim(),
        account_id: accountId,
        dry_run: dryRun,
      })
      setTextResult(result)
      setParsed(result.parsed || null)
      if (!result.ok) {
        toast(result.reason || result.error || '解析失败', 'error')
        return
      }
      if (dryRun) {
        toast(result.duplicate ? '这笔成交已经记过（重复）' : '解析成功，确认后可记账', 'success')
        return
      }
      if (result.duplicate) {
        toast('这笔成交已经记过，未重复写入', 'success')
      } else {
        toast('已记账', 'success')
        setText('')
        setParsed(null)
      }
    } catch (e) {
      toast(e instanceof Error ? e.message : '请求失败', 'error')
    } finally {
      setTextBusy(false)
    }
  }

  const pickFile = async (file: File) => {
    setFileName(file.name)
    setPreview(null)
    setCommitResult(null)
    const isBinary = /\.(xlsx|xls)$/i.test(file.name)
    const buffer = await file.arrayBuffer()
    const payload = isBinary
      ? { base64: toBase64(buffer), filename: file.name }
      : { content: decodeText(buffer), filename: file.name }
    setFilePayload(payload)
    await runPreview(payload)
  }

  const runPreview = async (payload = filePayload) => {
    if (!payload) return
    setImportBusy(true)
    try {
      const result = await syncApi.preview({ ...payload, account_id: accountId })
      setPreview(result)
      if (!result.summary.total) toast('文件里没有解析到成交行', 'error')
    } catch (e) {
      toast(e instanceof Error ? e.message : '预览失败', 'error')
    } finally {
      setImportBusy(false)
    }
  }

  const commit = async () => {
    if (!filePayload) return
    setImportBusy(true)
    try {
      const result = await syncApi.commit({ ...filePayload, account_id: accountId })
      setCommitResult(result)
      toast(`导入完成：新增 ${result.created}，重复 ${result.duplicate}，失败 ${result.failed}`, 'success')
      setPreview(null)
    } catch (e) {
      toast(e instanceof Error ? e.message : '导入失败', 'error')
    } finally {
      setImportBusy(false)
    }
  }

  const curlToken = token || 'pwmcp_你的同步令牌'
  const curl = `curl -X POST '${window.location.origin}/api/portfolio/trades/ingest' \\
  -H 'Authorization: Bearer ${curlToken}' \\
  -H 'Content-Type: application/json' \\
  -d '{"text":"${SAMPLE_TEXT}","account_id":${accountId ?? 1}}'`

  const Wrapper: ElementType = embedded ? 'div' : 'section'
  return (
    <Wrapper
      id={embedded ? undefined : 'sec-sync'}
      className={embedded ? 'space-y-4' : 'card p-4 md:p-6 lg:col-span-12'}
    >
      {!embedded && (
        <div className="mb-4 flex items-start gap-2">
          <Smartphone className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
          <div>
            <h3 className="text-[12px] font-medium text-foreground md:text-[13px]">交易同步</h3>
            <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
              把手机上的券商成交短信/推送或同花顺/券商导出的交割单同步进账本。写入的是「交易流水」，
              会按同一套费率与成本逻辑更新持仓和可用资金；不会下单。
            </p>
          </div>
        </div>
      )}

      {/* 账户 */}
      <div className="mb-4 flex flex-wrap items-center gap-2 text-[11px]">
        <span className="text-muted-foreground">记账账户</span>
        <select
          value={accountId ?? ''}
          onChange={(e) => setAccountId(e.target.value ? Number(e.target.value) : null)}
          className="rounded border bg-background px-2 py-1"
        >
          {accounts.length === 0 && <option value="">还没有账户</option>}
          {accounts.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
        <Button variant="ghost" size="sm" className="h-7 px-2" onClick={() => void loadAccounts()}>
          <RefreshCw className="h-3.5 w-3.5" />
        </Button>
        <span className="text-muted-foreground">只有一个账户时可省略 account_id</span>
      </div>

      {/* 令牌 */}
      <div className="mb-4 rounded-lg border border-border/60 bg-muted/20 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <KeyRound className="h-3.5 w-3.5 text-primary" />
          <span className="text-[12px] font-medium">手机同步令牌</span>
          <span className="text-[10px] text-muted-foreground">scope: trade:write，只能写交易流水</span>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Input
            value={tokenName}
            onChange={(e) => setTokenName(e.target.value)}
            className="h-8 max-w-[220px] text-[12px]"
            placeholder="令牌备注"
          />
          <Button size="sm" className="h-8" onClick={() => void createToken()} disabled={creating}>
            {creating ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : '创建同步令牌'}
          </Button>
          {token && (
            <Button variant="secondary" size="sm" className="h-8" onClick={() => copy(token)}>
              <Copy className="h-3.5 w-3.5" /> 复制明文
            </Button>
          )}
        </div>
        {token && (
          <div className="mt-2 rounded bg-background/70 px-2 py-1 font-mono text-[11px] break-all">
            {token}
            <span className="ml-2 font-sans text-[10px] text-amber-600">只显示这一次，请保存到手机自动化里</span>
          </div>
        )}
        <div className="mt-2 text-[10px] text-muted-foreground">
          已有令牌可在「设置 → MCP 令牌」里查看/吊销；到期时间 365 天。
        </div>
      </div>

      {/* 文本试解析 */}
      <div className="mb-4">
        <div className="mb-1.5 flex items-center gap-2 text-[12px] font-medium">
          <CheckCircle2 className="h-3.5 w-3.5 text-primary" />
          粘贴短信/推送试解析
        </div>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={3}
          placeholder={SAMPLE_TEXT}
          className="w-full rounded-lg border border-border bg-background px-2.5 py-2 text-[12px] leading-relaxed"
        />
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Button size="sm" variant="secondary" className="h-8" onClick={() => void ingest(true)} disabled={textBusy}>
            {textBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : '解析预览'}
          </Button>
          <Button
            size="sm"
            className="h-8"
            onClick={() => void ingest(false)}
            disabled={textBusy || !parsed?.ok}
          >
            确认记账
          </Button>
          {textResult?.duplicate && <span className="text-[11px] text-amber-600">这笔已经记过</span>}
          {textResult && !textResult.ok && (
            <span className="text-[11px] text-stock-down">{textResult.reason || textResult.error}</span>
          )}
        </div>

        {parsed && (
          <div className="mt-2 rounded-lg border border-border/60 bg-background/50 p-2.5 text-[11px]">
            <div className="grid grid-cols-2 gap-x-3 gap-y-1 sm:grid-cols-4">
              <div>
                <span className="text-muted-foreground">方向：</span>
                {parsed.action === 'buy' ? '买入' : parsed.action === 'sell' ? '卖出' : '--'}
              </div>
              <div>
                <span className="text-muted-foreground">代码：</span>
                {parsed.symbol || '--'}
              </div>
              <div>
                <span className="text-muted-foreground">名称：</span>
                {parsed.name || '--'}
              </div>
              <div>
                <span className="text-muted-foreground">日期：</span>
                {parsed.trade_date}
                {parsed.time ? ` ${parsed.time}` : ''}
              </div>
              <div>
                <span className="text-muted-foreground">数量：</span>
                {parsed.quantity}
              </div>
              <div>
                <span className="text-muted-foreground">价格：</span>
                {parsed.price}
              </div>
              <div>
                <span className="text-muted-foreground">费用：</span>
                {parsed.fees?.total ?? 0}
              </div>
              <div>
                <span className="text-muted-foreground">置信度：</span>
                {Math.round((parsed.confidence || 0) * 100)}%
              </div>
            </div>
            {parsed.warnings?.length > 0 && (
              <div className="mt-1 flex items-start gap-1 text-[10px] text-amber-600">
                <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                {parsed.warnings.join('；')}
              </div>
            )}
          </div>
        )}
      </div>

      {/* 交割单导入 */}
      <div>
        <div className="mb-1.5 flex items-center gap-2 text-[12px] font-medium">
          <FileSpreadsheet className="h-3.5 w-3.5 text-primary" />
          交割单导入
        </div>
        <div className="flex flex-wrap items-center gap-2 text-[11px]">
          <input
            ref={fileRef}
            type="file"
            accept=".csv,.txt,.tsv,.xls,.xlsx"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0]
              if (file) void pickFile(file)
              e.target.value = ''
            }}
          />
          <Button size="sm" variant="secondary" className="h-8" onClick={() => fileRef.current?.click()}>
            <Upload className="h-3.5 w-3.5" /> 选择文件
          </Button>
          {fileName && <span className="text-muted-foreground">{fileName}</span>}
          <Button
            size="sm"
            variant="ghost"
            className="h-8"
            onClick={() => void runPreview()}
            disabled={!filePayload || importBusy}
          >
            <RefreshCw className="h-3.5 w-3.5" /> 重新预览
          </Button>
          <Button size="sm" className="h-8" onClick={() => void commit()} disabled={!preview || importBusy}>
            确认导入
          </Button>
          {importBusy && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />}
        </div>
        <div className="mt-1 text-[10px] text-muted-foreground">
          支持 CSV/TSV、xlsx 与「伪装成 xls 的 HTML 表格」；老式二进制 .xls 需要服务器装有 xlrd，失败时请另存为 CSV。
          导入按成交时间排序，重复行自动跳过。
        </div>

        {preview && (
          <div className="mt-2">
            <div className="flex flex-wrap gap-2 text-[11px]">
              <span className="rounded bg-accent/40 px-1.5 py-0.5">共 {preview.summary.total} 行</span>
              <span className="rounded bg-stock-up/15 px-1.5 py-0.5 text-stock-up">
                可导入 {preview.summary.ok + preview.summary.new_stock}
              </span>
              <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-amber-600">
                重复 {preview.summary.duplicate}
              </span>
              <span className="rounded bg-stock-down/15 px-1.5 py-0.5 text-stock-down">
                跳过 {preview.summary.invalid}
              </span>
            </div>
            <div className="mt-1.5 max-h-64 overflow-auto rounded border border-border/50">
              <table className="w-full text-[11px]">
                <thead className="sticky top-0 bg-muted/60 text-[10px] text-muted-foreground">
                  <tr>
                    <th className="px-2 py-1 text-left font-normal">行</th>
                    <th className="px-2 py-1 text-left font-normal">日期</th>
                    <th className="px-2 py-1 text-left font-normal">代码</th>
                    <th className="px-2 py-1 text-left font-normal">名称</th>
                    <th className="px-2 py-1 text-left font-normal">方向</th>
                    <th className="px-2 py-1 text-right font-normal">数量</th>
                    <th className="px-2 py-1 text-right font-normal">价格</th>
                    <th className="px-2 py-1 text-right font-normal">费用</th>
                    <th className="px-2 py-1 text-left font-normal">状态</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.rows.map((row) => (
                    <tr key={`${row.line}-${row.external_id}-${row.symbol}`} className="border-t border-border/40">
                      <td className="px-2 py-0.5 text-muted-foreground">{row.line}</td>
                      <td className="px-2 py-0.5">{row.trade_date}</td>
                      <td className="px-2 py-0.5 font-mono">{row.symbol}</td>
                      <td className="px-2 py-0.5">{row.name}</td>
                      <td className="px-2 py-0.5">
                        {row.action === 'buy' ? '买入' : row.action === 'sell' ? '卖出' : '--'}
                      </td>
                      <td className="px-2 py-0.5 text-right">{row.quantity}</td>
                      <td className="px-2 py-0.5 text-right">{row.price}</td>
                      <td className="px-2 py-0.5 text-right">{row.fees?.total ?? 0}</td>
                      <td className="px-2 py-0.5">
                        <span className={`rounded px-1 py-0.5 text-[10px] ${statusChip(row.status)}`}>
                          {STATUS_LABEL[row.status] || row.status}
                        </span>
                        {row.message && <span className="ml-1 text-[10px] text-muted-foreground">{row.message}</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {commitResult && (
          <div className="mt-2 rounded border border-border/60 bg-muted/20 p-2 text-[11px]">
            导入完成：新增 {commitResult.created}，重复 {commitResult.duplicate}，失败 {commitResult.failed}。
            {commitResult.failed > 0 && (
              <span className="ml-1 text-stock-down">
                {commitResult.results.filter((r) => r.status === 'error').slice(0, 2).map((r) => `第${r.line}行：${r.message}`).join('；')}
              </span>
            )}
          </div>
        )}
      </div>

      {/* curl 示例 */}
      <div className="mt-4">
        <div className="mb-1 text-[11px] font-medium">手机/电脑转发示例（curl）</div>
        <pre className="overflow-auto whitespace-pre-wrap break-all rounded-lg border border-border/60 bg-muted/30 p-2.5 text-[10px] leading-relaxed">
{curl}
        </pre>
        <div className="mt-1 flex items-center gap-2 text-[10px] text-muted-foreground">
          <Button variant="ghost" size="sm" className="h-6 px-1.5 text-[10px]" onClick={() => copy(curl)}>
            <Copy className="h-3 w-3" /> 复制
          </Button>
          iOS/Android 的自动化步骤见 docs/trade-sync-guide.md。
        </div>
      </div>
    </Wrapper>
  )
}
