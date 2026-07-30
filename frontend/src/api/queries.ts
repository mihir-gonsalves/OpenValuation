/**
 * TanStack Query hooks over the API client.
 *
 * Cold-start note: the backend runs on Render's free tier, which spins down
 * after inactivity. The first request can take 30-50s. Therefore a retry runs a
 * few times with backoff on server/network errors, but never on 4xx (a bad CIK
 * or unknown company will not become valid on retry).
 */
import { keepPreviousData, useMutation, useQuery } from '@tanstack/react-query'
import { ApiError, getExportUrl, getFinancials, pingHealth, search, toApiError, unreachableError } from './client'
import type { FinancialsResponse, SearchResponse } from './types'

function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.isClientError) return false
  return failureCount < 10
}
const coldStartDelay = (attempt: number) => Math.min(1000 * 3 ** attempt, 6000)

/**
 * Polls /api/health until the backend answers. Used by the landing page to show a
 * "waking up" notice during a cold start rather than letting searches fail silently.
 */
export function useServerHealth() {
  return useQuery<boolean>({
    queryKey: ['health'],
    queryFn: pingHealth,
    refetchInterval: (query) => (query.state.data ? false : 5000),
    retry: false,
    staleTime: Infinity,
  })
}

export function useSearch(query: string) {
  const trimmed = query.trim()
  return useQuery<SearchResponse>({
    queryKey: ['search', trimmed],
    queryFn: () => search(trimmed),
    enabled: trimmed.length >= 1,
    placeholderData: keepPreviousData,
    staleTime: 60 * 1000,
    retry: shouldRetry,
    retryDelay: coldStartDelay,
  })
}

export function useFinancials(cik10: string | null) {
  return useQuery<FinancialsResponse>({
    queryKey: ['financials', cik10],
    queryFn: () => getFinancials(cik10 as string),
    enabled: !!cik10,
    retry: shouldRetry,
    retryDelay: coldStartDelay,
  })
}

export function useExport() {
  return useMutation<void, ApiError, { cik10: string; filename: string }>({
    mutationFn: async ({ cik10, filename }) => {
      let res: Response
      try {
        res = await fetch(getExportUrl(cik10))
      } catch {
        throw unreachableError()
      }
      if (!res.ok) {
        if (res.status === 501) {
          throw new ApiError(501, 'not_implemented', 'Excel export is not available yet.')
        }
        const body = await res.json().catch(() => null)
        throw toApiError(res.status, body)
      }
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      a.remove()
      // Defer revoke so the browser has started the download before the URL is freed.
      setTimeout(() => URL.revokeObjectURL(url), 0)
    },
  })
}
