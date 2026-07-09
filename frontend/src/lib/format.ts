/** Display formatting helpers. Pure functions, no side effects. */
import type { Warning } from '@/api/types'

/** Collapse warnings sharing a code, keeps first occurrence. Stable keys + honest counts. */
export function dedupeWarnings(warnings: Warning[]): Warning[] {
  const seen = new Set<string>()
  const out: Warning[] = []
  for (const w of warnings) {
    if (seen.has(w.code)) continue
    seen.add(w.code)
    out.push(w)
  }
  return out
}

/** Dash used wherever a value is unavailable (null). */
export const NA = '—'

/**
 * Format a valuation multiple. Null -> dash. Otherwise one decimal place.
 * Negatives are preserved (the backend already maps the non-interpretable
 * negatives to null).
 */
export function formatMultiple(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NA
  let v = value
  if (Object.is(v, -0) || Math.abs(v) < 0.05) v = Math.abs(v)
  return `${v.toFixed(1)}`
}

/** Compact currency for EV components and audit values: $3.49T, $95.0B, $1.2M. */
export function formatCompactCurrency(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NA
  const sign = value < 0 ? '-' : ''
  const abs = Math.abs(value)
  const units: [number, string][] = [
    [1e12, 'T'],
    [1e9, 'B'],
    [1e6, 'M'],
    [1e3, 'K'],
  ]
  for (const [threshold, suffix] of units) {
    if (abs >= threshold) return `${sign}$${(abs / threshold).toFixed(2)}${suffix}`
  }
  return `${sign}$${abs.toFixed(2)}`
}

/** Per-share / small numeric values (EPS, price). */
export function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NA
  return value.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

/** Compact magnitude for non-currency counts (e.g. shares): 15.2B, 1.5M. */
export function formatCompactNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NA
  return value.toLocaleString('en-US', { notation: 'compact', maximumFractionDigits: 2 })
}

/**
 * Format an audit value according to its XBRL unit: USD as compact currency,
 * share counts compactly, per-share figures to 2 decimals.
 */
export function formatAuditValue(value: number | null | undefined, unit: string | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NA
  if (unit === 'USD') return formatCompactCurrency(value)
  if (unit === 'shares') return formatCompactNumber(value)
  return formatNumber(value, 2)
}

function parseISO(iso: string | null | undefined): Date | null {
  if (!iso) return null
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d
}

/** ISO date string -> "Sep 28, 2024". Returns NA for null/invalid. */
export function formatDate(iso: string | null | undefined): string {
  const d = parseISO(iso)
  if (!d) return NA
  return d.toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  })
}

/** ISO datetime string -> "Sep 28, 2024 at 10:32 AM ET". Returns NA for null/invalid. */
export function formatDateTime(iso: string | null | undefined): string {
  const d = parseISO(iso)
  if (!d) return NA
  return (
    d
      .toLocaleString('en-US', {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        timeZone: 'America/New_York',
        hour12: true,
      })
      .replace(/(\d{4}), /, '$1 at ') + ' ET'
  )
}
