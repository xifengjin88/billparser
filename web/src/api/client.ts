import type { components } from './schema'

export type Schemas = components['schemas']
export type DocumentItem = Schemas['DocumentItem']
export type DocumentView = Schemas['DocumentView']
export type ChargeRow = Schemas['ChargeRow']
export type ChargeDetail = Schemas['ChargeDetail']
export type CiteOut = Schemas['CiteOut']
export type GapRow = Schemas['GapRow']
export type ParseTask = Schemas['ParseTask']
export type SourceInfo = Schemas['SourceInfo']
export type SourceTable = Schemas['SourceTable']
export type SourceLine = Schemas['SourceLine']
export type Settings = Schemas['Settings']
export type ReviewEdits = Schemas['ReviewEdits']
export type ReviewPreview = Schemas['ReviewPreview']
export type ApproveRequest = Schemas['ApproveRequest']
export type HeaderEdit = Schemas['HeaderEdit']
export type CostsOut = Schemas['CostsOut']
export type MappingPage = Schemas['MappingPage']
export type MappingRow = Schemas['MappingRow']
export type Resolution = Schemas['Resolution']
export type Me = Schemas['Me']

/** Sent when the API answers 401: the sign-in check runs again. */
export const AUTH_REQUIRED = 'auth:required'

/** An HTTP error from the API, with the server's message (FastAPI's `detail`). */
export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(`/api${path}`, init)
  } catch {
    throw new ApiError(0, 'The app could not reach the server. Check your connection and try again.')
  }
  if (!res.ok) {
    // the session ended or was never made: the app shows its sign-in screen
    if (res.status === 401 && !path.startsWith('/auth/')) window.dispatchEvent(new Event(AUTH_REQUIRED))
    let message = res.statusText
    try {
      const body = await res.json()
      message = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail ?? body)
    } catch {
      // not JSON: keep the status text
    }
    if (!message || res.status >= 502) {
      // a proxy error has no useful text: the API was restarting or did not answer
      message = res.status >= 500
        ? `The server did not answer (HTTP ${res.status}). It may be restarting: try again in a few seconds.`
        : message || `The request failed (HTTP ${res.status}).`
    }
    throw new ApiError(res.status, message)
  }
  return (await res.json()) as T
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: 'POST',
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  upload: <T>(path: string, form: FormData) => request<T>(path, { method: 'POST', body: form }),
}

/** Query string from the defined values only. */
export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const out = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') out.set(k, String(v))
  }
  const s = out.toString()
  return s ? `?${s}` : ''
}

/** Save a JSON response of the API as a file. */
export async function downloadJson(path: string, filename: string) {
  const data = await api.get<unknown>(path)
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }))
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}
