# OpenValuation: Architecture & Decision Record

This document explains the architectural decisions, accepted tradeoffs, and non-obvious implementation choices in `OpenValuation`. Intended for contributors, reviewers, and future reference.



## Guiding Principles

1. **Free over scalable** — every infrastructure decision is filtered through a zero-cost requirement. Where cost is the limiting factor, the gap is documented.
2. **Transparent over polished** — missing data surfaces as labeled `N/A`. Uncertain values carry warnings. No silent assumptions.
3. **Isolated external dependencies** — each data source lives in its own module. Replacing a source means editing one file.
4. **Computation in Python** — all financial logic lives in the backend. The frontend renders results, it does not compute them.



## Architecture: FastAPI + React

A monolithic Next.js app was rejected for the following reasons:

- Python's ecosystem (`yfinance`, `pandas`, `openpyxl`) is more mature and more widely used for financial data work than Node equivalents.
- `openpyxl` supports real Excel formulas, named ranges, and formatted cells directly. The moment Excel export became a core feature, Python was the right backend choice.
- The separation is clean: React manages stateful UI and loading states. FastAPI handles async HTTP, validation, and computation. No SSR, no hydration, nothing shared between sides.

**Deployment:** React -> Vercel, FastAPI -> Render (free tier).



## Excel Export Structure

The Excel workbook is designed to be fully auditable and mirrors the API output.

Three sheets are generated:

### 1. Summary
- Final multiples table (12 TTM periods)
- Company name, ticker, CIK
- Data retrieval timestamp
- Active warnings per period

### 2. Raw Financials
- All extracted XBRL values by period
- Fields:
  - Concept name
  - XBRL tag used
  - Fallback status (primary vs fallback)
  - Unit (USD)
  - Entity context (consolidated vs segment)
  - Period end date

### 3. Calculations
- Each multiple expressed as a live Excel formula
- References cells in the Raw Financials sheet
- No hardcoded values, all results recompute if inputs are edited

This structure ensures complete transparency: every output number can be traced to its raw input and calculation logic.



## Caching

EDGAR's `companyfacts` payload can be 5–10 MB per company. Fetching it on every request is wasteful and slow.

**Design:** module-level dict keyed by CIK, 24-hour TTL.

```python
_cache: dict[str, CacheEntry] = {}
CACHE_TTL_HOURS = 24

def get(cik: str) -> FinancialData | None:
    entry = _cache.get(cik)
    if entry and (datetime.utcnow() - entry.cached_at).total_seconds() < CACHE_TTL_HOURS * 3600:
        return entry.data
    return None
```

**Why CIK, not ticker?** CIK is EDGAR's stable internal identifier. Tickers can change on renames or relistings, CIK never does.

**Why not Redis?** Redis costs money on every managed hosting platform. For a personal project with likely low usage, in-process memory is sufficient.

**Known tradeoff:** Render's free tier spins down after 15 minutes of inactivity. Every cold start empties the cache. The first request for each company after a cold start pays the full EDGAR fetch cost (~1–3 seconds). This is disclosed in the README and the UI.



## Price Service

All price logic is isolated in `price.py`. Input: ticker + date. Output: `Decimal` or `None`.

**Why the next trading day's close?** EDGAR filings are typically submitted after 4:00 PM ET. The filing day's own close means the market had not yet seen the data. The next trading day is the first session where the disclosed information could be fully priced in — the standard in academic and professional valuation practice.

Edge case: filings submitted before 9:30 AM ET could technically use the same trading day's close. This is not handled, the tool always uses the next trading day. Impact is minimal.

**Why adjusted close?** Stock splits distort raw prices over time. `yfinance`'s `history(auto_adjust=True)` returns split-adjusted close, making prices comparable across periods.

**Why a 14-day window?** The US market can be closed for 4–5 consecutive calendar days around Christmas–New Year. A 7-day window starting December 26 could return zero trading days. 14 days guarantees at least 4 trading days regardless of holiday placement. The cost is a marginally wider `yfinance` fetch, this is negligible.

**Ticker normalization:** EDGAR stores `BRK.A`, yfinance expects `BRK-A`. The service normalizes `.` -> `-` before querying. If no price is returned after normalization, `price_unavailable` is surfaced rather than a silent wrong value.

**Timeouts:** All outbound HTTP calls — both EDGAR and yfinance — are made with an explicit timeout. For EDGAR calls via `httpx`, a 15-second timeout is set at the client level:

```python
async with httpx.AsyncClient(timeout=15.0) as client:
    response = await client.get(url, headers=headers)
```

If EDGAR does not respond within 15 seconds, an `httpx.TimeoutException` is caught and re-raised as a structured `503` response with message: *"EDGAR is taking longer than usual. Please try again in a moment."* The same pattern applies to any synchronous HTTP calls made inside the yfinance wrapper. Silent hangs that eventually surface as platform-level 502s are worse than a clean, user-readable timeout message.

**Shares outstanding** are drawn from the filing's XBRL data (`CommonStockSharesOutstanding`), not from the price service. This ensures the share count matches the exact period being valued.



## XBRL Extraction

### Tag Fallback Logic

Companies use slightly different XBRL tag names for the same concept depending on their industry and when they adopted XBRL. Tags are tried in priority order, the first match is used.

```python
REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
]
```

When a fallback fires, the `warnings` array includes a structured note (e.g., `{ code: "fallback_revenue", message: "Primary tag absent, using Revenues" }`). The Input Audit Panel shows which tag was used.

If no tag matches at all: value returns as `None`, UI displays `N/A`

### Amended Filings

When both an original filing (e.g., `10-Q`, `10-K`) and an amended version (`10-Q/A`, `10-K/A`) exist for the same reporting period, the original filing is always used.

Rationale:
- Amended filings often restate only specific line items, not the full financial set
- Mixing amended and original data introduces internal inconsistency across concepts

A structured warning `amendment_exists` is attached to the affected period to indicate that an amended version is available but was not used.

### Unit Validation

Only facts with `unitRef: USD` are accepted. Non-USD facts are rejected at extraction. A 1,000x scale error propagating to a multiple is worse than an `N/A`.



## Enterprise Value

```
EV = Market Cap
   + Long-Term Debt
   + Short-Term Borrowings
   + Current Portion of LT Debt
   + Finance Lease Liabilities (Current + Non-Current)
   + Minority Interest
   + Preferred Stock
   − Cash & Cash Equivalents
```

### Debt Components

Each component maps to primary and fallback XBRL tags. Missing individual components are treated as zero — an absent tag almost always means zero balance, not a data error. If *all* financial debt tags are absent, EV is still computed (Market Cap − Cash) but `ev_debt_missing` is set: *"All financial debt tags were absent. EV may be understated."*

### Finance Lease Liabilities

Finance leases are economically identical to debt: the lessee controls the asset and owes fixed future payments. They are recognized under ASC 842 (effective for most public companies from fiscal years beginning after December 15, 2018).

Consistency with the denominators matters:
- Finance lease expense splits into a **depreciation component** (hits D&A, affecting EBITDA and EBIT) and an **interest component** (below operating income).
- EV includes `FinanceLeaseLiabilityNoncurrent` + `FinanceLeaseLiabilityCurrent` in the numerator.
- EBITDA adds back D&A, which includes the depreciation component of finance lease expense.
- EBIT includes the amortization of the lease asset but excludes the interest portion.

This maintains internal consistency across EV/EBITDA and EV/EBIT multiples.

If both finance lease tags are absent, they are treated as zero. A warning is set only for capital-intensive SIC sectors (manufacturing 2000–3999, transportation/utilities 4000–4999), where material finance leases are more likely. Absence is legitimate for most companies.

### Cash Fallback

Primary tag: `CashAndCashEquivalentsAtCarryingValue`. Fallback: `CashCashEquivalentsAndShortTermInvestments`, which includes short-term investments. When the fallback fires, `cash_fallback_includes_investments` warning is set, as the wider definition can overstate the cash deduction.

### Operating Lease Exclusion

Operating lease liabilities are excluded from EV. Under ASC 842, operating lease expense flows through operating expenses as a single line — it is not split into interest and depreciation the way finance lease expense is. Adding operating lease liabilities to EV without stripping lease expense from the denominators would overstate EV/EBIT and EV/EBITDA. Correcting for this (the EBITDAR methodology) requires non-GAAP adjustments not available from EDGAR XBRL. Excluded and disclosed.



## Multiples

All multiples are computed from reported GAAP figures. No non-GAAP adjustments.

| Multiple | Formula |
|---|---|
| P/E | Price ÷ Diluted EPS (TTM) |
| EV/EBITDA | EV ÷ (EBIT + D&A) (TTM) |
| EV/EBIT | EV ÷ Operating Income (TTM) |
| EV/Revenue | EV ÷ Revenue (TTM) |
| P/S | Market Cap ÷ Revenue (TTM) |
| P/B | Market Cap ÷ Stockholders' Equity (point-in-time) |
| P/FCF | Market Cap ÷ (Operating Cash Flow − CapEx) (TTM) |

### Key Rules

- **Negative results** are displayed as negative numbers (negative P/E for loss-making companies, negative EV for net-cash companies). `N/A` means data is unavailable, not that the result is negative — these are categorically different.
- **Near-zero denominators** return `N/A` with `denominator_near_zero`.
- **Negative FCF** returns `N/A` with an explanatory note. This differs from negative P/E: negative FCF is ambiguous and not reported by professional databases as a negative multiple.
- **P/E fallback:** if diluted EPS is absent, falls back to basic EPS, labeled "P/E (basic)" with `fallback_eps_basic` warning.
- **CapEx sign normalization:** CapEx is sometimes reported as a negative cash outflow, sometimes positive. The tool takes the absolute value if necessary and sets `capex_sign_normalised`.
- **EV/EBIT vs EV/EBITDA:** both are shown so users can see the D&A impact. For capital-intensive companies they will diverge materially, for asset-light companies they will be close.
- **Near-zero denominators:** if `abs(denominator) < 0.01`, return `N/A` with `denominator_near_zero`. This avoids unstable or non-meaningful multiples from numerically insignificant denominators.

All computations are pure functions in `multiples.py`. Each returns `{ value: Decimal | None, warnings: [...] }`. All edge cases are covered in `test_multiples.py`.



## TTM (Trailing Twelve Months)

### Structure

12 TTM periods derived from 16 quarterly filings, stepping back one quarter at a time. Period 1 is the most recent quarter, Period 12 is 11 quarters prior. Each column is labeled `TTM [quarter end date]`. The filing date is shown separately so the price fetch date is verifiable.

12 periods provides enough history to see trend direction without requiring excessive historical EDGAR data.

### TTM Bridge

Income statement and cash flow items:

```
TTM = Most Recent Annual + Current YTD − Prior Year YTD
```

"YTD" is the single cumulative value reported in the 10-Q for that quarter — not a sum of individual quarters.

Balance sheet items are always point-in-time (balance sheet date of the quarter end).

### YTD vs. Single-Quarter Identification

XBRL facts are classified by comparing start and end date durations. YTD facts span from fiscal year start to a quarter end, single-quarter facts span exactly one quarter.

### Fallback

If prior-year YTD is unavailable (e.g., company recently went public, or changed fiscal year):

```
TTM ≈ Most Recent Annual + (Current YTD / quarters in YTD) × 4
```

Labeled with `ttm_annualized` warning and the message: *"Prior-year YTD unavailable, annualized from current YTD. TTM may be less precise."* 

If a company changed fiscal year and the prior-year YTD stub does not exist, the bridge degrades to this fallback rather than producing an incorrect result.

### Price Per Period

Each TTM period fetches a price as of the next trading day after the most recent quarterly filing's submission timestamp.



## Input Audit Panel

Displays for each extracted concept:
- XBRL tag name matched
- Fallback status (primary vs. fallback)
- Unit
- Entity context (consolidated vs. segment)

Makes the data source traceable without requiring the user to inspect raw filings. Collapsible in the UI to reduce clutter.



## Error Handling

**Principle: a wrong answer is worse than no answer.**

| Situation | Behavior |
|---|---|
| XBRL tag missing | `None` returned; UI shows `N/A` with tooltip explaining the tag was not found |
| Negative result (valid) | Displayed as negative with note |
| Invalid price | Structured `price_unavailable` error |
| All debt tags absent | EV computed with debt=0; `ev_debt_missing` warning |
| Non-USD unit | Fact rejected at extraction |
| Ambiguous fact (multiple contexts) | Deterministic rule applied; if ambiguous remains, `None` + `ambiguous_fact` |
| Period mismatch (>3 days) | Fact rejected; `period_mismatch` warning |
| EDGAR 429 | Retry once with exponential backoff; `503` to client if it fails |
| EDGAR timeout (>15s) | `503` with user-readable message |
| Negative FCF | `N/A` with explanatory note |



## Testing Strategy

### Fixture-Based Extraction Tests

Real `companyfacts.json` responses (Apple, Microsoft, small-cap, airline, retailer) are stored in `tests/fixtures/`. Extraction functions accept the parsed JSON dict directly — no HTTP mocking needed for extraction tests. This validates behavior against real API responses, not just that the correct URL was called.

Coverage includes: tag fallback behavior, finance lease handling, CapEx sign normalization, missing tag handling, non-USD rejection, period mismatch rejection.

### TTM Tests

Constructed `ExtractedFinancials` objects covering: normal bridge, prior-year YTD absent (annualization fallback), fiscal year change mid-period (bridge degrades to fallback, not wrong result), 12-period rolling window correctness.

### Calculation Tests

Pure function tests on `multiples.py`: positive/negative/zero cases, near-zero denominators, negative EV (net-cash companies), negative FCF, EV composition with and without finance lease liabilities, EV/EBIT vs EV/EBITDA divergence on capital-intensive fixtures.

### Price Service Tests

`yfinance` library mocked directly. Coverage: output mapping, date validation (returned date >= requested), price > 0 check, ticker normalization.

### Frontend

Thin rendering layer. Unit tests on pure utility functions (formatters, display logic), manual verification otherwise. Deliberate tradeoff, not an oversight.



## CORS

```python
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",")
```

- **Local dev:** `http://localhost:5173`
- **Production:** Vercel deployment URL only

Wildcard (`*`) is never used in a deployed environment.



## URL State

Results are mapped to `/?cik={CIK}`. CIK-based URLs are stable and shareable. A "Copy Link" button copies the current URL to the clipboard via `navigator.clipboard.writeText(window.location.href)`. Button briefly shows "Copied!" for 1.5 seconds on success. Degrades gracefully if the clipboard API is unavailable.



## Intentionally Excluded

| Feature | Reason |
|---|---|
| User accounts | Stateless by design; no persistence needed |
| Sector-normalized multiples | Requires a peer database; out of scope for v1 |
| Non-GAAP / adjusted figures | Company-specific; not available from EDGAR XBRL |
| Operating lease liabilities in EV | Requires EBITDAR adjustment; non-GAAP |
| IFRS support | Different taxonomy; EDGAR handles US filers only |
| EDGAR XBRL deep-linking | EDGAR does not support stable tag-level URLs |



## Tradeoffs Summary

| Area | Decision | Tradeoff |
|---|---|---|
| Hosting | Render free tier | Cache lost on cold start |
| Cache | In-process memory | Lost on restart; no horizontal scale |
| Price data | `yfinance` | Unofficial; can fail intermittently |
| Figures | GAAP only | No adjusted EBITDA or non-GAAP metrics |
| Scope | U.S. SEC filers | No IFRS / foreign private issuer support |
