/**
 * Hand-crafted, type-accurate API fixtures for tests and local development.
 *
 * These mirror the real backend response shape (see src/api/types.ts) but are
 * trimmed to a few periods and tuned to exercise the cases the UI must handle:
 * full data, an N/A multiple, the basic-EPS label flip, and per-period warnings.
 */
import type { FinancialsResponse, MultipleSet, SearchResponse, TTMPeriod, Warning } from '@/api/types'

function emptyExtracted(period_end: string, filing_date: string) {
  return {
    filing_date,
    period_end,
    price: null,
    shares_outstanding: null,
    eps_diluted: null,
    revenue: null,
    operating_income: null,
    depreciation_and_amortization: null,
    net_income: null,
    operating_cash_flow: null,
    capex: null,
    total_assets: null,
    stockholders_equity: null,
    long_term_debt: null,
    short_term_borrowings: null,
    current_portion_lt_debt: null,
    finance_lease_current: null,
    finance_lease_noncurrent: null,
    minority_interest: null,
    preferred_stock: null,
    cash: null,
    audit: [],
    warnings: [],
  }
}

function multipleSet(values: Partial<Record<keyof MultipleSet, number | null>>): MultipleSet {
  const mv = (label: string, value: number | null = null, warnings: Warning[] = []) => ({
    value,
    label,
    warnings,
  })
  return {
    ev_revenue: mv('EV/Revenue', values.ev_revenue ?? null),
    ev_ebitda: mv('EV/EBITDA', values.ev_ebitda ?? null),
    ev_ebit: mv('EV/EBIT', values.ev_ebit ?? null),
    pe: mv('P/E', values.pe ?? null),
    pfcf: mv('P/FCF', values.pfcf ?? null),
    ps: mv('P/S', values.ps ?? null),
    pb: mv('P/B', values.pb ?? null),
  }
}

function aaplPeriod(
  period_end: string,
  filing_date: string,
  label: string,
  vals: Partial<Record<keyof MultipleSet, number>>,
): TTMPeriod {
  return {
    filing_date,
    period_end,
    label,
    price: 220,
    multiples: multipleSet(vals),
    ev_components: {
      market_cap: 3.4e12,
      long_term_debt: 9.5e10,
      short_term_borrowings: 1e10,
      current_portion_lt_debt: 1e10,
      finance_lease_current: null,
      finance_lease_noncurrent: null,
      minority_interest: null,
      preferred_stock: null,
      cash: 3e10,
      enterprise_value: 3.49e12,
    },
    extracted: {
      ...emptyExtracted(period_end, filing_date),
      price: 220,
      shares_outstanding: 1.52e10,
      eps_diluted: 6.6,
      revenue: 3.9e11,
      operating_income: 1.2e11,
      depreciation_and_amortization: 1.1e10,
      net_income: 1e11,
      operating_cash_flow: 1.1e11,
      capex: 1e10,
      total_assets: 3.6e11,
      stockholders_equity: 7e10,
      audit: [
        {
          concept: 'Revenue',
          xbrl_tag: 'RevenueFromContractWithCustomerExcludingAssessedTax',
          is_fallback: false,
          unit: 'USD',
          entity_context: 'consolidated',
          value: 3.9e11,
        },
        {
          concept: 'Diluted EPS',
          xbrl_tag: 'EarningsPerShareDiluted',
          is_fallback: false,
          unit: 'USD/shares',
          entity_context: 'consolidated',
          value: 6.6,
        },
      ],
      warnings: [],
    },
    warnings: [],
  }
}

export const aaplFinancials: FinancialsResponse = {
  company: {
    cik_10: '0000320193',
    name: 'Apple Inc.',
    ticker: 'AAPL',
    sic: '3571',
    sic_description: 'Electronic Computers',
    exchange: 'Nasdaq',
    is_financial: false,
    is_capital_intensive: true,
  },
  periods: [
    aaplPeriod('2024-09-28', '2024-11-01', 'Q4 FY24', {
      ev_revenue: 7.8,
      ev_ebitda: 21.4,
      ev_ebit: 25.1,
      pe: 33.1,
      pfcf: 26.3,
      ps: 8.5,
      pb: 45.2,
    }),
    aaplPeriod('2024-06-29', '2024-08-02', 'Q3 FY24', {
      ev_revenue: 7.4,
      ev_ebitda: 20.1,
      ev_ebit: 23.9,
      pe: 31.2,
      pfcf: 24.9,
      ps: 8.1,
      pb: 43.0,
    }),
    aaplPeriod('2024-03-30', '2024-05-03', 'Q2 FY24', {
      ev_revenue: 7.0,
      ev_ebitda: 18.9,
      ev_ebit: 22.2,
      pe: 28.4,
      pfcf: 23.1,
      ps: 7.6,
      pb: 40.1,
    }),
  ],
  cached_at: '2026-06-16T12:00:00Z',
  data_as_of: '2026-06-16T12:01:00Z',
}

/** MSFT: EV/EBITDA is N/A (D&A missing) and P/E uses basic EPS (label flip). */
export const msftFinancials: FinancialsResponse = {
  company: {
    cik_10: '0000789019',
    name: 'Microsoft Corporation',
    ticker: 'MSFT',
    sic: '7372',
    sic_description: 'Prepackaged Software',
    exchange: 'Nasdaq',
    is_financial: false,
    is_capital_intensive: false,
  },
  periods: [
    {
      filing_date: '2024-10-30',
      period_end: '2024-09-30',
      label: 'Q1 FY25',
      price: 420,
      multiples: {
        ev_revenue: { value: 12.1, label: 'EV/Revenue', warnings: [] },
        ev_ebitda: { value: null, label: 'EV/EBITDA', warnings: [] },
        ev_ebit: { value: 27.0, label: 'EV/EBIT', warnings: [] },
        pe: {
          value: 36.2,
          label: 'P/E (basic)',
          warnings: [
            { code: 'fallback_tag', message: 'Primary tag absent, fallback used for EPS (EarningsPerShareBasic).' },
          ],
        },
        pfcf: { value: 38.0, label: 'P/FCF', warnings: [] },
        ps: { value: 13.0, label: 'P/S', warnings: [] },
        pb: { value: 11.4, label: 'P/B', warnings: [] },
      },
      ev_components: {
        market_cap: 3.1e12,
        long_term_debt: 4.2e10,
        short_term_borrowings: null,
        current_portion_lt_debt: null,
        finance_lease_current: null,
        finance_lease_noncurrent: null,
        minority_interest: null,
        preferred_stock: null,
        cash: 7.5e10,
        enterprise_value: 3.07e12,
      },
      extracted: {
        ...emptyExtracted('2024-09-30', '2024-10-30'),
        price: 420,
        revenue: 2.5e11,
        operating_income: 1.1e11,
        depreciation_and_amortization: null,
        audit: [
          {
            concept: 'Diluted EPS',
            xbrl_tag: 'EarningsPerShareBasic',
            is_fallback: true,
            unit: 'USD/shares',
            entity_context: 'consolidated',
            value: 11.6,
          },
        ],
        warnings: [
          { code: 'fallback_tag', message: 'Primary tag absent, fallback used for EPS (EarningsPerShareBasic).' },
        ],
      },
      // fallback_tag also rides the pe cell above, so the badge filter
      // hides it here, ttm_annualized is period-level and stays visible.
      warnings: [
        { code: 'fallback_tag', message: 'Primary tag absent, fallback used for EPS (EarningsPerShareBasic).' },
        { code: 'ttm_annualized', message: 'Prior-year data unavailable for Revenue. TTM annualized from current YTD.' },
      ],
    },
  ],
  cached_at: '2026-06-16T12:00:00Z',
  data_as_of: '2026-06-16T12:01:00Z',
}

export const appleSearch: SearchResponse = {
  results: [
    { cik_10: '0000320193', name: 'Apple Inc.', ticker: 'AAPL' },
    { cik_10: '0001640147', name: 'Snowflake Inc.', ticker: 'SNOW' },
  ],
}

export const financialsByCik: Record<string, FinancialsResponse> = {
  '0000320193': aaplFinancials,
  '0000789019': msftFinancials,
}
