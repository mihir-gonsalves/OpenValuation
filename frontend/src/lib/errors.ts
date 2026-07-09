/** Maps error codes to friendly user-facing copy. */
import type { ErrorCode } from '@/api/types'

/** Friendly, user-facing copy for request-level errors. */
export const ERROR_COPY: Record<ErrorCode, { title: string; body: string }> = {
  edgar_not_found: {
    title: 'No SEC filer found',
    body: 'No company on SEC EDGAR matches that identifier. Try a different name or ticker.',
  },
  invalid_cik: {
    title: 'Invalid company identifier',
    body: 'That CIK is not valid. Search by name or ticker to pick a company.',
  },
  ifrs_filer: {
    title: 'Unsupported filer',
    body: 'This company reports under IFRS. OpenValuation supports U.S. GAAP filers only.',
  },
  unsupported_taxonomy: {
    title: 'Unsupported filing data',
    body: 'This company has no usable U.S. GAAP XBRL facts, so multiples cannot be computed.',
  },
  edgar_timeout: {
    title: 'SEC EDGAR timed out',
    body: 'SEC EDGAR did not respond in time. Please try again in a moment.',
  },
  edgar_rate_limit: {
    title: 'Rate limited by SEC EDGAR',
    body: 'SEC EDGAR is rate limiting requests right now. Please try again shortly.',
  },
  not_implemented: {
    title: 'Not available yet',
    body: 'This feature is not available yet.',
  },
  internal_error: {
    title: 'Something went wrong',
    body: 'An unexpected error occurred on the server. Please try again.',
  },
}

export function errorCopy(code: ErrorCode | 'http_error'): { title: string; body: string } {
  if (code === 'http_error') {
    return {
      title: 'Could not reach the server',
      body: 'The server may be waking up and can take ~30-50s. Please retry.',
    }
  }
  return ERROR_COPY[code] ?? ERROR_COPY.internal_error
}
