import type { ApiErrorShape } from './types'
import { ElMessage } from 'element-plus'

export class ApiError extends Error {
  code: string
  details: unknown
  traceId: string

  constructor(payload: ApiErrorShape, status: number) {
    super(payload.message || `请求失败（${status}）`)
    this.code = payload.code || 'request_failed'
    this.details = payload.details
    this.traceId = payload.trace_id || ''
  }
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  headers.set('Accept', 'application/json')
  const response = await fetch(`/api/v1${path}`, { ...options, headers })
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event('erp-session-expired'))
    let payload: ApiErrorShape
    try { payload = await response.json() } catch { payload = { code: 'request_failed', message: `请求失败（${response.status}）` } }
    if (payload.code === 'pilot_scope_only') ElMessage.warning(payload.message || '此操作尚未接通多人版本')
    throw new ApiError(payload, response.status)
  }
  return response.json() as Promise<T>
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  patch: <T>(path: string, body: unknown) => request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  put: <T>(path: string, body: unknown) => request<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  upload: <T>(path: string, form: FormData) => request<T>(path, { method: 'POST', body: form }),
}
