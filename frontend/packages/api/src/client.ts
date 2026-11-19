const API_BASE = '/api'
const DEFAULT_TIMEOUT_MS = 20000

interface ApiResponse<T> {
  code: number
  success?: boolean
  data: T
  message: string
}

export function getToken(): string | null {
  return localStorage.getItem('token')
}

export function logout() {
  localStorage.removeItem('token')
  localStorage.removeItem('token_expires')
  window.location.href = '/login'
}

export function isAuthenticated(): boolean {
  const token = getToken()
  if (!token) return false

  const expires = localStorage.getItem('token_expires')
  if (expires && new Date(expires) < new Date()) {
    logout()
    return false
  }
  return true
}

export interface ApiRequestOptions extends RequestInit {
  timeoutMs?: number
}

export async function fetchAPI<T>(path: string, options?: ApiRequestOptions): Promise<T> {
  const headers: Record<string, string> = {}

  const token = getToken()
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }

  if (options?.body) {
    headers['Content-Type'] = 'application/json'
  }

  const timeoutController = new AbortController()
  const callerSignal = options?.signal
  const abortFromCaller = () => timeoutController.abort(callerSignal?.reason)
  if (callerSignal?.aborted) abortFromCaller()
  else callerSignal?.addEventListener('abort', abortFromCaller, { once: true })
  const timeoutMs = typeof options?.timeoutMs === 'number' && options.timeoutMs > 0
    ? options.timeoutMs
    : DEFAULT_TIMEOUT_MS
  const timeoutId = window.setTimeout(() => timeoutController.abort(), timeoutMs)

  try {
    const { timeoutMs: _timeoutMs, ...requestOptions } = options || {}
    const res = await fetch(`${API_BASE}${path}`, {
      ...requestOptions,
      headers: {
        ...headers,
        ...(requestOptions.headers as Record<string, string> | undefined),
      },
      signal: timeoutController.signal,
    })

    if (res.status === 401) {
      logout()
      throw new Error('登录已过期')
    }

    const contentType = res.headers.get('content-type') || ''
    if (contentType.includes('text/html')) {
      throw new Error('接口返回了网页，服务可能尚未更新。请刷新页面；若仍失败，请更新并重启本地服务。')
    }
    let body: ApiResponse<T>
    try {
      body = await res.json()
    } catch (error) {
      if (timeoutController.signal.aborted) throw error
      throw new Error(res.ok ? '接口返回格式异常，请稍后重试' : `请求失败（HTTP ${res.status}）`)
    }
    if (!body || typeof body.code !== 'number') {
      throw new Error('接口返回格式异常，请刷新页面并确认服务已更新')
    }
    if (!res.ok || body.code !== 0 || body.success === false) {
      throw new Error(body.message || `请求失败（HTTP ${res.status}）`)
    }
    return body.data
  } catch (error: any) {
    if (timeoutController.signal.aborted && !callerSignal?.aborted) {
      throw new Error('请求超时，请稍后重试')
    }
    throw error
  } finally {
    window.clearTimeout(timeoutId)
    callerSignal?.removeEventListener('abort', abortFromCaller)
  }
}

export const apiClient = {
  request: fetchAPI,
}
