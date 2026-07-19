import { describe, expect, it } from 'vitest'
import {
  formatAuditValue,
  formatCompactCurrency,
  formatDate,
  formatDateTime,
  formatMultiple,
  NA,
} from './format'

describe('formatMultiple', () => {
  it('renders one decimal with an x suffix', () => {
    expect(formatMultiple(33.14)).toBe('33.1')
  })
  it('preserves negatives', () => {
    expect(formatMultiple(-12.3)).toBe('-12.3')
  })
  it('shows NA for null/NaN/undefined', () => {
    expect(formatMultiple(null)).toBe(NA)
    expect(formatMultiple(undefined)).toBe(NA)
    expect(formatMultiple(NaN)).toBe(NA)
  })
})

describe('formatCompactCurrency', () => {
  it('scales to T/B/M', () => {
    expect(formatCompactCurrency(3.49e12)).toBe('$3.49T')
    expect(formatCompactCurrency(9.5e10)).toBe('$95.00B')
    expect(formatCompactCurrency(-3e7)).toBe('-$30.00M')
  })
  it('shows NA for null', () => {
    expect(formatCompactCurrency(null)).toBe(NA)
  })
})

describe('formatAuditValue', () => {
  it('formats USD as compact currency', () => {
    expect(formatAuditValue(3.9e11, 'USD')).toBe('$390.00B')
  })
  it('formats shares compactly', () => {
    expect(formatAuditValue(1.52e10, 'shares')).toBe('15.2B')
  })
  it('formats per-share to 2 decimals', () => {
    expect(formatAuditValue(6.6, 'USD/shares')).toBe('$6.60')
  })
})

describe('dates', () => {
  it('formats an ISO date in UTC', () => {
    expect(formatDate('2024-09-28')).toBe('Sep 28, 2024')
  })
  it('shows NA for null/invalid', () => {
    expect(formatDate(null)).toBe(NA)
    expect(formatDate('not-a-date')).toBe(NA)
  })
})

describe('formatDateTime', () => {
  it('formats an ISO datetime in Eastern time (honoring DST)', () => {
    expect(formatDateTime('2024-09-28T14:32:00Z')).toBe('Sep 28, 2024 at 10:32 AM ET')
  })
  it('shows NA for null/invalid', () => {
    expect(formatDateTime(null)).toBe(NA)
    expect(formatDateTime('not-a-date')).toBe(NA)
  })
})
