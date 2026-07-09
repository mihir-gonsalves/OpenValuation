import { describe, expect, it } from 'vitest'
import { coerceDecimals } from './client'

describe('coerceDecimals', () => {
  it('converts numeric-string Decimal fields to numbers', () => {
    const out = coerceDecimals({
      price: '220.50',
      revenue: '451442000000',
      value: '8.26',
    }) as Record<string, unknown>
    expect(out.price).toBe(220.5)
    expect(out.revenue).toBe(451442000000)
    expect(out.value).toBe(8.26)
  })

  it('preserves numeric-looking identifiers and dates as strings', () => {
    const out = coerceDecimals({
      cik_10: '0000320193',
      sic: '3571',
      ticker: 'AAPL',
      period_end: '2024-09-28',
      data_as_of: '2026-06-16T12:00:00Z',
    }) as Record<string, unknown>
    expect(out.cik_10).toBe('0000320193')
    expect(out.sic).toBe('3571')
    expect(out.ticker).toBe('AAPL')
    expect(out.period_end).toBe('2024-09-28')
    expect(out.data_as_of).toBe('2026-06-16T12:00:00Z')
  })

  it('passes through nulls and recurses into arrays and nested objects', () => {
    const out = coerceDecimals({
      price: null,
      periods: [{ multiples: { pe: { value: '33.1', label: 'P/E' } } }],
    }) as { price: null; periods: { multiples: { pe: { value: number; label: string } } }[] }
    expect(out.price).toBeNull()
    expect(out.periods[0].multiples.pe.value).toBe(33.1)
    expect(out.periods[0].multiples.pe.label).toBe('P/E')
  })
})
