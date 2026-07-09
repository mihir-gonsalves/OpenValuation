/**
 * Typed fetch wrappers for the OpenValuation backend.
 *
 * Base URL comes from VITE_API_BASE. In development it is empty, so requests go
 * to a same-origin "/api/..." path that Vite proxies to http://localhost:8000.
 * In production it is the Render backend URL.
 */
import type { ErrorCode, FinancialsResponse, SearchResponse } from './types'

const API_BASE = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '')

if (import.meta.env.PROD && !API_BASE) {
  // Fail loud at startup rather than producing HTML-parsed-as-JSON errors later.
  console.error('VITE_API_BASE is empty in a production build, API calls will hit the SPA rewrite.')
}

/**
 * Pydantic v2 serializes Decimal fields as JSON *strings* (e.g. "8.26",
 * "451442000000"). They must be coerced back to numbers at the boundary so the rest
 * of the app can treat money/ratios as `number | null` (see types.ts).
 *
 * This uses a denylist of keys that are legitimately strings - including
 * numeric-looking ones that must NOT be coerced (cik_10, sic) and ISO dates.
 * Every other numeric-looking string is converted.
 */
const STRING_KEYS = new Set([
  'cik_10',
  'name',
  'ticker',
  'sic',
  'sic_description',
  'exchange',
  'code',
  'message',
  'concept',
  'label',
  'xbrl_tag',
  'unit',
  'entity_context',
  'filing_date',
  'period_end',
  'cached_at',
  'data_as_of',
])

export function coerceDecimals(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(coerceDecimals)
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      if (
        typeof v === 'string' &&
        !STRING_KEYS.has(k) &&
        v.trim() !== '' &&
        Number.isFinite(Number(v))
      ) {
        out[k] = Number(v)
      } else {
        out[k] = coerceDecimals(v)
      }
    }
    return out
  }
  return value
}

/** A normalized, typed error thrown for any non-2xx API response. */
export class ApiError extends Error {
  readonly status: number
  readonly code: ErrorCode | 'http_error'

  constructor(status: number, code: ErrorCode | 'http_error', message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }

  /** 4xx errors are caused by the request and should not be retried. */
  get isClientError(): boolean {
    return this.status >= 400 && this.status < 500
  }
}

/**
 * Normalize the many error body shapes FastAPI can return into ApiError.
 *  - Explicit errors:  { detail: { error, message } }  (conventional)
 *  - Or top-level:     { error, message }
 *  - Validation (422):  { detail: [{ msg, ... }] }
 */
export function toApiError(status: number, body: unknown): ApiError {
  const detail = (body as { detail?: unknown } | null)?.detail ?? body

  if (detail && typeof detail === 'object' && 'error' in detail) {
    const d = detail as { error: ErrorCode; message?: string }
    return new ApiError(status, d.error, d.message ?? d.error)
  }
  if (Array.isArray(detail) && detail[0]?.msg) {
    return new ApiError(status, 'invalid_cik', String(detail[0].msg))
  }
  return new ApiError(status, 'http_error', `Request failed (${status})`)
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(`${API_BASE}${path}`, init)
  } catch {
    // Network failure / CORS / server unreachable (incl. a long cold start that
    // exceeded the browser's patience).
    throw new ApiError(0, 'http_error', 'Could not reach the server.')
  }

  if (!res.ok) {
    const body = await res.json().catch(() => null)
    throw toApiError(res.status, body)
  }
  return (await res.json()) as T
}

/**
 * GET /health - liveness probe used to detect a cold start before the
 * user commits a search. Returns true once the backend answers - throws (like any
 * request) while it is still spinning up, so callers can retry.
 */
export async function pingHealth(): Promise<boolean> {
  await request<unknown>('/health')
  return true
}

/** POST /api/search - resolve a name or ticker to up to 5 CIK candidates. */
export function search(query: string): Promise<SearchResponse> {
  return request<SearchResponse>('/api/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query }),
  })
}

/** GET /api/financials/{cik_10} - TTM multiples for a company. */
export async function getFinancials(cik10: string): Promise<FinancialsResponse> {
  const raw = await request<unknown>(`/api/financials/${cik10}`)
  return coerceDecimals(raw) as FinancialsResponse
}

/** Absolute URL for the Excel export endpoint (used by the download action). */
export function getExportUrl(cik10: string): string {
  return `${API_BASE}/api/export/${cik10}`
}
