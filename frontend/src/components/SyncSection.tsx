import { useCallback, useEffect, useRef, useState, type ElementType } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  Copy,
  FileSpreadsheet,
  ImagePlus,
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
  type ImageCommitResult,
  type ImageExtract,
  type ImageRow,
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

const POSITION_STATUS_LABEL: Record<string, string> = {
  create: '新建持仓',
  new_stock: '新建自选',
  update: '成本更新',
  add: '加仓',
  reduce: '减仓',
  unchanged: '无变化',
  close: '清仓',
  unresolved: '未识别',
}

const POSITION_STATUS_CHIP: Record<string, string> = {
  create: 'bg-stock-up/15 text-stock-up',
  new_stock: 'bg-sky-500/15 text-sky-600',
  update: 'bg-amber-500/15 text-amber-600',
  add: 'bg-stock-up/15 text-stock-up',
  reduce: 'bg-stock-down/15 text-stock-down',
  unchanged: 'bg-accent/40 text-muted-foreground',
  close: 'bg-stock-down/15 text-stock-down',
  unresolved: 'bg-amber-500/15 text-amber-600',
}

const moneyText = (value?: number | null) =>
  value == null || !Number.isFinite(value) ? '--' : value.toFixed(2)

/** 把截图压到最长边 1600、JPEG 0.85，降低上传与视觉模型 token。 */
async function imageToBase64(file: File, maxSide = 1600): Promise<string> {
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(reader.error || new Error('读取图片失败'))
    reader.readAsDataURL(file)
  })
  const fallback = dataUrl.includes(',') ? dataUrl.split(',')[1] || '' : ''
  try {
    const image = await new Promise<HTMLImageElement>((resolve, reject) => {
      const img = new Image()
      img.onload = () => resolve(img)
      img.onerror = () => reject(new Error('图片无法解码'))
      img.src = dataUrl
    })
    const scale = Math.min(1, maxSide / Math.max(image.width, image.height))
    const canvas = document.createElement('canvas')
    canvas.width = Math.max(1, Math.round(image.width * scale))
    canvas.height = Math.max(1, Math.round(image.height * scale))
    const ctx = canvas.getContext('2d')
    if (!ctx) return fallback
    ctx.drawImage(image, 0, 0, canvas.width, canvas.height)
    const jpeg = canvas.toDataURL('image/jpeg', 0.85)
    return jpeg.includes(',') ? jpeg.split(',')[1] || fallback : fallback
  } catch {
    return fallback
  }
}

/** 交易同步面板：令牌、文本试解析、交割单/截图导入（理财页弹窗内嵌，也可单独成卡片）。 */
export default function SyncSection({
  embedded = false,
  onSynced,
}: {
  embedded?: boolean
  onSynced?: () => void
} = {}) {
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

  // 截图导入：持仓截图 or 成交截图
  const [shotMode, setShotMode] = useState<'positions' | 'trades'>('positions')
  const [shot, setShot] = useState<ImageExtract | null>(null)
  const [shotResult, setShotResult] = useState<ImageCommitResult | null>(null)
  const [shotBusy, setShotBusy] = useState(false)
  const [shotName, setShotName] = useState('')
  const [removeMissing, setRemoveMissing] = useState(false)
  const [syncCash, setSyncCash] = useState(true)
  const [recordTrades, setRecordTrades] = useState(true)
  const shotRef = useRef<HTMLInputElement>(null)

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
        onSynced?.()
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
      onSynced?.()
    } catch (e) {
      toast(e instanceof Error ? e.message : '导入失败', 'error')
    } finally {
      setImportBusy(false)
    }
  }

  const runImagePreview = async (file: File) => {
    setShot(null)
    setShotResult(null)
    setShotName(file.name || '截图')
    setShotBusy(true)
    try {
      const base64 = await imageToBase64(file)
      if (!base64) {
        toast('图片读取失败，请换一张截图', 'error')
        return
      }
      const result = await syncApi.imagePreview({
        image_base64: base64,
        mode: shotMode,
        account_id: accountId,
      })
      setShot(result)
      if (!result.ok) {
        toast(result.hint || result.warnings?.[0] || '没识别到数据行', 'error')
      }
    } catch (e) {
      toast(e instanceof Error ? e.message : '图片识别失败', 'error')
    } finally {
      setShotBusy(false)
    }
  }

  const commitImage = async () => {
    if (!shot || shot.rows.length === 0) return
    setShotBusy(true)
    try {
      const available =
        shot.mode === 'positions' && syncCash ? shot.account_summary?.available : undefined
      const result = await syncApi.imageCommit({
        mode: shot.mode,
        rows: shot.rows,
        account_id: accountId,
        remove_missing: shot.mode === 'positions' ? removeMissing : false,
        available_funds: typeof available === 'number' ? available : undefined,
        record_trades: shot.mode === 'positions' ? recordTrades : false,
      })
      setShotResult(result)
      const detail =
        shot.mode === 'positions'
          ? `新建 ${result.created}，更新 ${result.updated ?? 0}，加仓 ${result.buy_trades ?? 0} 笔，减仓 ${result.sell_trades ?? 0} 笔，清仓 ${result.closed ?? 0}，失败 ${result.failed ?? 0}${result.removed ? `，删除 ${result.removed}` : ''}${result.cash_updated ? '，已同步可用资金' : ''}`
          : `新增 ${result.created}，重复 ${result.duplicate ?? 0}，失败 ${result.failed ?? 0}`
      toast(`同步完成：${detail}`, 'success')
      setShot(null)
      onSynced?.()
    } catch (e) {
      toast(e instanceof Error ? e.message : '同步失败', 'error')
    } finally {
      setShotBusy(false)
    }
  }

  const updateShotRow = (index: number, patch: Partial<ImageRow>) => {
    setShot((prev) =>
      prev
        ? { ...prev, rows: prev.rows.map((row, i) => (i === index ? { ...row, ...patch } : row)) }
        : prev,
    )
  }

  const onPasteImage = (event: React.ClipboardEvent) => {
    const item = Array.from(event.clipboardData.items).find((entry) => entry.type.startsWith('image/'))
    if (!item) return
    const file = item.getAsFile()
    if (!file) return
    event.preventDefault()
    void runImagePreview(file)
  }

  const onDropImage = (event: React.DragEvent) => {
    event.preventDefault()
    const file = Array.from(event.dataTransfer.files).find((entry) =>
      entry.type.startsWith('image/'),
    )
    if (file) void runImagePreview(file)
  }

  // 弹窗里任意位置 Ctrl+V 都能粘贴截图（不必先点中某个输入框）
  const runImageRef = useRef<(file: File) => void>(() => {})
  runImageRef.current = (file: File) => {
    void runImagePreview(file)
  }
  useEffect(() => {
    const handler = (event: ClipboardEvent) => {
      const items = event.clipboardData?.items
      if (!items) return
      const item = Array.from(items).find((entry) => entry.type.startsWith('image/'))
      const file = item?.getAsFile()
      if (!file) return
      event.preventDefault()
      runImageRef.current(file)
    }
    window.addEventListener('paste', handler)
    return () => window.removeEventListener('paste', handler)
    // runImageRef 每次渲染都会更新，监听只注册一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

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
      onPaste={onPasteImage}
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

      {/* 截图导入：持仓 / 成交 */}
      <div className="mt-4 rounded-lg border border-border/60 p-3">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <ImagePlus className="h-3.5 w-3.5 text-primary" />
          <span className="text-[12px] font-medium">截图导入</span>
          <span className="text-[10px] text-muted-foreground">AI 识别，预览后同步</span>
          <div className="ml-auto flex overflow-hidden rounded border border-border text-[10px]">
            <button
              type="button"
              className={`px-2 py-1 ${shotMode === 'positions' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground'}`}
              onClick={() => {
                setShotMode('positions')
                setShot(null)
                setShotResult(null)
              }}
            >
              持仓截图
            </button>
            <button
              type="button"
              className={`px-2 py-1 ${shotMode === 'trades' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground'}`}
              onClick={() => {
                setShotMode('trades')
                setShot(null)
                setShotResult(null)
              }}
            >
              成交截图
            </button>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-[11px]">
          <input
            ref={shotRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0]
              if (file) void runImagePreview(file)
              e.target.value = ''
            }}
          />
          <Button
            size="sm"
            variant="secondary"
            className="h-8"
            disabled={shotBusy}
            onClick={() => shotRef.current?.click()}
          >
            <ImagePlus className="h-3.5 w-3.5" /> 选择截图
          </Button>
          <span className="text-[10px] text-muted-foreground">手机可从相册/相机选；电脑可拖拽或 Ctrl+V 粘贴</span>
          {shotName && <span className="text-[10px] text-muted-foreground">{shotName}</span>}
          {shotBusy && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />}
        </div>
        <div
          role="button"
          tabIndex={0}
          onClick={() => shotRef.current?.click()}
          onPaste={onPasteImage}
          onDrop={onDropImage}
          onDragOver={(event) => event.preventDefault()}
          onKeyDown={(event) => {
            if (event.key === 'Enter' || event.key === ' ') shotRef.current?.click()
          }}
          className="mt-2 cursor-pointer rounded-lg border border-dashed border-border/70 bg-muted/10 px-3 py-3 text-center text-[10px] text-muted-foreground transition-colors hover:border-primary/50 hover:text-primary"
        >
          拖拽截图到这里，或点这里选择；弹窗内任意位置按 Ctrl+V 也能粘贴
        </div>
        <div className="mt-1 text-[10px] leading-relaxed text-muted-foreground">
          截图会发送给你配置的 AI 视觉模型；持仓截图只同步数量与成本，不写交易流水、不动可用资金。
        </div>

        {shot?.warnings?.length ? (
          <div className="mt-1 flex items-start gap-1 text-[10px] text-amber-600">
            <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
            {shot.warnings.join('；')}
          </div>
        ) : null}

        {shot?.mode === 'positions' && shot.account_summary && (
          <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 rounded border border-border/60 bg-background/50 p-2 text-[11px] sm:grid-cols-4">
            <div>
              <span className="text-muted-foreground">券商：</span>
              {shot.account_summary.broker || '--'}
            </div>
            <div>
              <span className="text-muted-foreground">总资产：</span>
              {moneyText(shot.account_summary.total_assets)}
            </div>
            <div>
              <span className="text-muted-foreground">总市值：</span>
              {moneyText(shot.account_summary.market_value)}
            </div>
            <div>
              <span className="text-muted-foreground">可用：</span>
              {moneyText(shot.account_summary.available)}
            </div>
            <div>
              <span className="text-muted-foreground">可取：</span>
              {moneyText(shot.account_summary.withdrawable)}
            </div>
            <div>
              <span className="text-muted-foreground">浮动盈亏：</span>
              {moneyText(shot.account_summary.floating_pnl)}
            </div>
            <div>
              <span className="text-muted-foreground">当日盈亏：</span>
              {moneyText(shot.account_summary.daily_pnl)}
              {shot.account_summary.daily_pnl_pct != null
                ? `（${moneyText(shot.account_summary.daily_pnl_pct)}%）`
                : ''}
            </div>
            <div>
              <span className="text-muted-foreground">仓位：</span>
              {shot.account_summary.position_ratio != null
                ? `${moneyText(shot.account_summary.position_ratio)}%`
                : '--'}
            </div>
          </div>
        )}

        {shot && shot.rows.length > 0 && (
          <div className="mt-2">
            <div className="flex flex-wrap items-center gap-2 text-[11px]">
              <span className="rounded bg-accent/40 px-1.5 py-0.5">识别 {shot.rows.length} 行</span>
              {shot.mode === 'positions' ? (
                <>
                  <span className="rounded bg-stock-up/15 px-1.5 py-0.5 text-stock-up">
                    新建 {shot.summary.create ?? 0}
                  </span>
                  <span className="rounded bg-stock-up/15 px-1.5 py-0.5 text-stock-up">
                    加仓 {shot.summary.add ?? 0}
                  </span>
                  <span className="rounded bg-stock-down/15 px-1.5 py-0.5 text-stock-down">
                    减仓 {shot.summary.reduce ?? 0}
                  </span>
                  {(shot.summary.update ?? 0) > 0 && (
                    <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-amber-600">
                      成本更新 {shot.summary.update}
                    </span>
                  )}
                  <span className="rounded bg-accent/40 px-1.5 py-0.5 text-muted-foreground">
                    无变化 {shot.summary.unchanged ?? 0}
                  </span>
                  <span className="rounded bg-stock-down/15 px-1.5 py-0.5 text-stock-down">
                    清仓 {shot.summary.close ?? 0}
                  </span>
                  <span className="rounded bg-sky-500/15 px-1.5 py-0.5 text-sky-600">
                    新建自选 {shot.summary.new_stock ?? 0}
                  </span>
                </>
              ) : (
                <>
                  <span className="rounded bg-stock-up/15 px-1.5 py-0.5 text-stock-up">
                    可导入 {shot.summary.ok ?? 0}
                  </span>
                  <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-amber-600">
                    重复 {shot.summary.duplicate ?? 0}
                  </span>
                  <span className="rounded bg-stock-down/15 px-1.5 py-0.5 text-stock-down">
                    跳过 {shot.summary.invalid ?? 0}
                  </span>
                </>
              )}
              <div className="ml-auto flex flex-wrap items-center gap-2">
                <Button
                  size="sm"
                  className="h-8"
                  disabled={shotBusy || shot.rows.length === 0}
                  onClick={() => void commitImage()}
                >
                  确认同步
                </Button>
              </div>
            </div>
            {shot.mode === 'positions' && (
              <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-muted-foreground">
                <label className="flex items-center gap-1">
                  <input type="checkbox" checked={syncCash} onChange={(e) => setSyncCash(e.target.checked)} />
                  同步可用资金
                  {shot.account_summary?.available != null
                    ? `（设为 ${moneyText(shot.account_summary.available)}）`
                    : '（截图未识别到）'}
                </label>
                <label className="flex items-center gap-1">
                  <input
                    type="checkbox"
                    checked={recordTrades}
                    onChange={(e) => setRecordTrades(e.target.checked)}
                  />
                  按变动生成交易流水（加仓/减仓/清仓）
                </label>
                <label className="flex items-center gap-1">
                  <input
                    type="checkbox"
                    checked={removeMissing}
                    onChange={(e) => setRemoveMissing(e.target.checked)}
                  />
                  删除截图里没有的持仓（截图不完整时别勾）
                </label>
              </div>
            )}
            <div className="mt-1.5 max-h-64 overflow-auto rounded border border-border/50">
              <table className="w-full text-[11px]">
                <thead className="sticky top-0 bg-muted/60 text-[10px] text-muted-foreground">
                  {shot.mode === 'positions' ? (
                    <tr>
                      <th className="px-2 py-1 text-left font-normal">代码</th>
                      <th className="px-2 py-1 text-left font-normal">名称</th>
                      <th className="px-2 py-1 text-right font-normal">当前→截图</th>
                      <th className="px-2 py-1 text-right font-normal">变动</th>
                      <th className="px-2 py-1 text-right font-normal">成本/现价</th>
                      <th className="px-2 py-1 text-right font-normal">推算价（可改）</th>
                      <th className="px-2 py-1 text-left font-normal">状态</th>
                    </tr>
                  ) : (
                    <tr>
                      <th className="px-2 py-1 text-left font-normal">日期</th>
                      <th className="px-2 py-1 text-left font-normal">代码</th>
                      <th className="px-2 py-1 text-left font-normal">名称</th>
                      <th className="px-2 py-1 text-left font-normal">方向</th>
                      <th className="px-2 py-1 text-right font-normal">数量</th>
                      <th className="px-2 py-1 text-right font-normal">价格</th>
                      <th className="px-2 py-1 text-right font-normal">费用</th>
                      <th className="px-2 py-1 text-left font-normal">状态</th>
                    </tr>
                  )}
                </thead>
                <tbody>
                  {shot.rows.map((row, index) =>
                    shot.mode === 'positions' ? (
                      <tr key={`p-${row.symbol}-${index}`} className="border-t border-border/40">
                        <td className="px-2 py-0.5 font-mono">{row.symbol}</td>
                        <td className={`px-2 py-0.5 ${row.closing ? 'text-muted-foreground' : ''}`}>
                          {row.name}
                        </td>
                        <td className="px-2 py-0.5 text-right text-muted-foreground">
                          {row.current_quantity ?? '--'} → {row.quantity}
                          {row.available != null ? `/${row.available}` : ''}
                        </td>
                        <td
                          className={`px-2 py-0.5 text-right ${
                            (row.delta ?? 0) > 0
                              ? 'text-stock-up'
                              : (row.delta ?? 0) < 0
                                ? 'text-stock-down'
                                : 'text-muted-foreground'
                          }`}
                        >
                          {row.closing
                            ? '清仓'
                            : row.delta
                              ? `${row.delta > 0 ? '+' : ''}${row.delta}`
                              : '--'}
                        </td>
                        <td className="px-2 py-0.5 text-right">
                          {row.cost_price} / {row.last_price ?? '--'}
                        </td>
                        <td className="px-2 py-0.5 text-right">
                          {row.trade_action ? (
                            <input
                              type="number"
                              step="0.001"
                              value={row.trade_price ?? ''}
                              onChange={(e) =>
                                updateShotRow(index, {
                                  trade_price: e.target.value === '' ? null : Number(e.target.value),
                                })
                              }
                              className="w-16 rounded border border-border bg-background px-1 py-0.5 text-right text-[11px]"
                            />
                          ) : (
                            <span className="text-muted-foreground">--</span>
                          )}
                        </td>
                        <td className="px-2 py-0.5">
                          <span
                            className={`rounded px-1 py-0.5 text-[10px] ${
                              POSITION_STATUS_CHIP[row.status || ''] || 'bg-accent/40 text-muted-foreground'
                            }`}
                          >
                            {POSITION_STATUS_LABEL[row.status || ''] || row.status || '--'}
                          </span>
                        </td>
                      </tr>
                    ) : (
                      <tr key={`t-${row.symbol}-${index}`} className="border-t border-border/40">
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
                          <span className={`rounded px-1 py-0.5 text-[10px] ${statusChip(row.status || '')}`}>
                            {STATUS_LABEL[row.status || ''] || row.status || '--'}
                          </span>
                          {row.message && (
                            <span className="ml-1 text-[10px] text-muted-foreground">{row.message}</span>
                          )}
                        </td>
                      </tr>
                    ),
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {shotResult && (
          <div className="mt-2 rounded border border-border/60 bg-muted/20 p-2 text-[11px]">
            {shotResult.mode === 'positions'
              ? `持仓同步完成：新建 ${shotResult.created}，更新 ${shotResult.updated ?? 0}，加仓 ${shotResult.buy_trades ?? 0} 笔，减仓 ${shotResult.sell_trades ?? 0} 笔，清仓 ${shotResult.closed ?? 0}，失败 ${shotResult.failed ?? 0}${shotResult.removed ? `，删除 ${shotResult.removed}` : ''}。`
              : `成交同步完成：新增 ${shotResult.created}，重复 ${shotResult.duplicate ?? 0}，失败 ${shotResult.failed ?? 0}。`}
            {shotResult.mode === 'positions' && shotResult.cash_updated ? ' 可用资金已更新。' : ''}
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
