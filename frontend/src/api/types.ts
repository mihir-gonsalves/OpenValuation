/**
 * TypeScript mirror of the backend Pydantic contract.
 *
 * Serialization facts (verified against a live response - see PHASE_4_SPEC §1):
 *  - These are final, backend-computed display values - no client-side decimal math is needed.
 *  - `date` / `datetime` serialize as ISO-8601 *strings* and are preserved.
 *  - `null` means "data unavailable" - distinct from 0 or a valid negative.
 *
 * Keep this file in sync with backend/app/models/{company,financials,errors}.py.
 */

// --- Warning & error codes (mirror app/models/errors.py) ---

export type WarningCode =
  | 'fallback_tag'
  | 'ev_debt_missing'
  | 'debt_deduplicated'
  | 'cash_fallback_includes_investments'
  | 'capex_sign_normalized'
  | 'lease_pre_asc842'
  | 'finance_lease_missing_capital_intensive'
  | 'amendment_exists'
  | 'ttm_annualized'
  | 'period_mismatch'
  | 'ambiguous_fact'
  | 'denominator_near_zero'
  | 'negative_book_value'
  | 'negative_fcf'
  | 'input_missing'
  | 'price_unavailable'

export type ErrorCode =
  | 'edgar_timeout'
  | 'edgar_rate_limit'
  | 'edgar_not_found'
  | 'ifrs_filer'
  | 'unsupported_taxonomy'
  | 'invalid_cik'
  | 'internal_error'
  | 'not_implemented'

export interface Warning {
  code: WarningCode
  message: string
  concept?: string | null
}

// --- Search (mirror app/models/company.py) ---

export interface CompanyCandidate {
  cik_10: string
  name: string
  ticker: string
}

export interface SearchResponse {
  results: CompanyCandidate[]
}

export interface CompanyMeta {
  cik_10: string
  name: string
  ticker: string | null
  sic: string | null
  sic_description: string | null
  exchange: string | null
  is_financial: boolean
  is_capital_intensive: boolean
}

// --- Financials (mirror app/models/financials.py) ---

export interface AuditEntry {
  concept: string
  xbrl_tag: string | null
  is_fallback: boolean
  unit: string | null
  entity_context: string | null
  value: number | null
}

export interface ExtractedFinancials {
  filing_date: string | null
  period_end: string | null
  price: number | null
  shares_outstanding: number | null
  eps_diluted: number | null
  revenue: number | null
  operating_income: number | null
  depreciation_and_amortization: number | null
  net_income: number | null
  operating_cash_flow: number | null
  capex: number | null
  total_assets: number | null
  stockholders_equity: number | null
  long_term_debt: number | null
  short_term_borrowings: number | null
  current_portion_lt_debt: number | null
  finance_lease_current: number | null
  finance_lease_noncurrent: number | null
  cash: number | null
  minority_interest: number | null
  preferred_stock: number | null
  audit: AuditEntry[]
  warnings: Warning[]
}

export interface MultipleValue {
  value: number | null
  label: string
  warnings: Warning[]
}

/** The seven multiples, in canonical display order. */
export interface MultipleSet {
  ev_revenue: MultipleValue
  ev_ebitda: MultipleValue
  ev_ebit: MultipleValue
  pe: MultipleValue
  pfcf: MultipleValue
  ps: MultipleValue
  pb: MultipleValue
}

/** Stable key order for iterating MultipleSet rows. */
export const MULTIPLE_KEYS = [
  'ev_revenue',
  'ev_ebitda',
  'ev_ebit',
  'pe',
  'pfcf',
  'ps',
  'pb',
] as const

export type MultipleKey = (typeof MULTIPLE_KEYS)[number]

export interface EVComponents {
  market_cap: number | null
  long_term_debt: number | null
  short_term_borrowings: number | null
  current_portion_lt_debt: number | null
  finance_lease_current: number | null
  finance_lease_noncurrent: number | null
  minority_interest: number | null
  preferred_stock: number | null
  cash: number | null
  enterprise_value: number | null
}

export interface TTMPeriod {
  filing_date: string | null
  period_end: string
  label: string
  price: number | null
  multiples: MultipleSet
  ev_components: EVComponents
  extracted: ExtractedFinancials
  warnings: Warning[]
}

export interface FinancialsResponse {
  company: CompanyMeta
  periods: TTMPeriod[]
  cached_at: string | null
  data_as_of: string
}
