import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchAPI } from '@panwatch/api/client'

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

describe('API response and deadline handling', () => {
  it('explains an HTML 200 response instead of reporting HTTP 200', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('<html>SPA</html>', { headers: { 'Content-Type': 'text/html' } }))
    await expect(fetchAPI('/strategy/library')).rejects.toThrow('接口返回了网页')
  })

  it('rejects malformed success data and preserves a real API failure message', async () => {
    const request = vi.spyOn(globalThis, 'fetch')
    request.mockResolvedValueOnce(new Response(JSON.stringify({ items: [] })))
    await expect(fetchAPI('/strategy/library')).rejects.toThrow('接口返回格式异常')
    request.mockResolvedValueOnce(new Response(JSON.stringify({ code: 404, success: false, message: '接口不存在' }), { status: 404 }))
    await expect(fetchAPI('/strategy/library')).rejects.toThrow('接口不存在')
  })

  it('keeps the deadline when the caller supplies a cancellation signal', async () => {
    vi.useFakeTimers()
    vi.spyOn(globalThis, 'fetch').mockImplementation((_url, options) => new Promise((_resolve, reject) => {
      options?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
    }))
    const caller = new AbortController()
    const assertion = expect(fetchAPI('/quotes/batch', { signal: caller.signal, timeoutMs: 100 })).rejects.toThrow('请求超时')
    await vi.advanceTimersByTimeAsync(100)
    await assertion
    expect(caller.signal.aborted).toBe(false)
  })

  it('preserves caller cancellation rather than calling it a timeout', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((_url, options) => new Promise((_resolve, reject) => {
      options?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
    }))
    const caller = new AbortController()
    const assertion = expect(fetchAPI('/stocks', { signal: caller.signal })).rejects.toMatchObject({ name: 'AbortError' })
    caller.abort()
    await assertion
  })

  it('covers reading the response body with the same deadline', async () => {
    vi.useFakeTimers()
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, options) => ({
      status: 200, ok: true, headers: new Headers({ 'Content-Type': 'application/json' }),
      json: () => new Promise((_resolve, reject) => options?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))),
    }) as Response)
    const assertion = expect(fetchAPI('/stocks', { timeoutMs: 100 })).rejects.toThrow('请求超时')
    await vi.advanceTimersByTimeAsync(100)
    await assertion
  })
})
