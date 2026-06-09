# OpenValuation: Architecture & Decision Record

This document explains the architectural decisions, tradeoffs, and implementation details behind `OpenValuation`. Intended for contributors, reviewers, and future reference.



## Guiding Principles

**1. Zero-cost constraint**

All infrastructure decisions are bounded by a strict no-cost requirement. Any resulting limitations are explicitly documented.

**2. Transparent over polished**

Missing data is surfaced as `N/A`. Fallbacks and uncertain values carry warnings and are labeled. No silent assumptions.

**3. Isolated dependencies**

Each external data source is encapsulated in its own module. Replacing a provider means editing one file.

**4. Computation in Python**

All financial logic lives in the backend. The frontend is strictly a rendering layer.



## System Architecture: React + FastAPI

A monolithic framework (e.g., Next.js) was intentionally avoided.

**Rationale:**
- Python has a stronger ecosystem for financial data workflows (`yfinance`, `pandas`, `openpyxl`)
- `openpyxl` enables native Excel formula generation, which is a core feature
- Separation of concerns is clean: 
    - React handles UI state and interaction
    - FastAPI handles data retrieval, validation, and computation

No server-side rendering, no hydration, no shared logic between frontend and backend.

**Deployment:** 
- Frontend: React -> Vercel
- Backend: FastAPI -> Render (free tier)



## Company Search Architecture

### Overview

Search, implemented as a debounced typeahead, is handled entirely via an in-memory company index.

Source:
https://www.sec.gov/files/company_tickers.json

### User Experience

- Results update dynamically (no submit step)
- The user selects a company from the dropdown list

### Design

- The company index is loaded from the SEC `company_tickers.json` dataset at application startup and stored in memory
- The index is refreshed lazily on the search path at most once every 24 hours (`CompanyIndex.maybe_refresh`), there is no background task
- All search queries operate exclusively on the in-memory dataset. No external network calls are made during search, ensuring deterministic latency

### Search Algorithm

1. Exact ticker match (highest priority)
2. Name match scoring:
   - exact match
   - prefix match
   - substring match
3. Results sorted by score
4. Deduplicated by CIK
5. Top 5 returned

### Constraints

- No external network calls during search
- Deterministic latency
- O(n) scan over ~10k entries

### Separation of Concerns

| Concern        | Endpoint                   |
|----------------|----------------------------|
| Identity       | `/api/search`              |
| Financial data | `/api/financials/{cik_10}` |

Search resolves identity only (CIK, name, ticker).

Company metadata (e.g., SIC, exchange) is retrieved as part of the financials request, alongside XBRL data and price inputs.

This separation ensures that search remains fast and deterministic while all external data fetching occurs in a single downstream request.




## Excel Export Design

The Excel workbook reproduces all financial calculations performed in the backend as formulas referencing the raw data, enabling full auditability without introducing new or independent logic.

Three sheets are generated:

### 1. Summary
- Final multiples table (12 TTM periods)
- Company metadata (name, ticker, CIK_10)
- Data retrieval timestamp
- Active warnings per period

### 2. Raw Financials
- All extracted XBRL values by period
- Fields:
  - Concept name
  - XBRL tag used
  - Fallback status (primary or fallback)
  - Unit (USD)
  - Entity context (consolidated or segment)
  - Period end date

### 3. Calculations
- Each multiple expressed as a live Excel formula
- References cells in the Raw Financials sheet
- No hardcoded values, all results recompute if inputs are edited

This structure ensures complete transparency: every output number can be traced to its raw input and calculation logic.



## Caching Strategy

EDGAR's `companyfacts` payload can be 5–10 MB per company. Re-fetching it on every request is inefficient.

**Design:** in-memory dictionary keyed by `CIK_10` with a 24-hour TTL.

```python
_cache: dict[str, CacheEntry] = {}
CACHE_TTL_HOURS = 24

def get(cik_10: str) -> FinancialData | None:
    entry = _cache.get(cik_10)
    if entry and (datetime.utcnow() - entry.cached_at).total_seconds() < CACHE_TTL_HOURS * 3600:
        return entry.data
    return None
```

**Why CIK, not ticker?** 

CIK is EDGAR's stable internal identifier. Tickers can change on renames or relistings, CIK never does.

**Why not Redis?** 

All managed Redis options introduce cost. For expected usage (likely low), in-process memory is sufficient.

**Known tradeoff:** 

Render's free tier spins down after 15 minutes of inactivity. Every cold start empties the cache. The first request for each company after a cold start pays the full EDGAR fetch cost (~1–3 seconds).  
This is disclosed in the README and the UI.



## Price Service

All price logic is isolated in `price.py`. 

Input: ticker & date  
Output: `Decimal` or `None`

**Why the next trading day's close?** 

EDGAR filings are typically submitted after 4:00 PM ET. The filing day's own close means the market had not yet seen the data. The next trading day is the first session where the disclosed information could be fully priced in. This aligns with standard academic and professional valuation practice.

Edge case: filings submitted before 9:30 AM ET could technically use the same trading day's close. This is not handled, the tool always uses the next trading day. This introduces a small systematic timing bias for filings submitted before market open or intra-day. Impact is minimal but consistent.

**Why adjusted close?** 

Stock splits distort raw prices over time. `yfinance`'s `history(auto_adjust=True)` returns split-adjusted close, making prices comparable across periods.

**Why a 14-day window?** 

The US market can be closed for 4–5 consecutive calendar days around Christmas–New Year. A 7-day window starting December 26 could return zero trading days. 14 days guarantees at least 4 trading days regardless of holiday placement. The cost is a marginally wider `yfinance` fetch, which is negligible.

**Shares outstanding:**

Drawn from the filing's XBRL data (`CommonStockSharesOutstanding`), not from the price service.  
This ensures the share count matches the exact period being valued.

**Shares consistency:** 

Market cap uses point-in-time shares outstanding from XBRL, while EPS reflects weighted-average diluted shares.  
This introduces minor inconsistencies relative to fully diluted valuation approaches used by institutional data providers.

**Treasury Stock Method (TSM):**

Institutional data providers compute fully diluted shares using the Treasury Stock Method. This requires:

- Detailed option and warrant disclosures
- Strike prices
- Assumed exercise timing
- Share repurchase assumptions

These inputs are not consistently available in EDGAR XBRL and often require manual interpretation of footnotes.

Implementing TSM would require:

- Parsing non-standard disclosures
- Introducing model assumptions
- Departing from strictly reported GAAP values

This conflicts with the project's principles of transparency and deterministic computation.

TSM is therefore excluded. The system uses reported diluted EPS and point-in-time shares only. The resulting inconsistency is disclosed rather than approximated.

**Ticker normalization:**

EDGAR stores `BRK.A`, yfinance expects `BRK-A`.  
The service normalizes `.` -> `-` before querying. If no price is returned after normalization, `price_unavailable` is surfaced rather than a silent wrong value.

**Timeouts:**

All outbound HTTP calls, both EDGAR and yfinance, are made with an explicit timeout.

EDGAR is not used during search. All EDGAR interactions occur only in:
- `/api/financials/{cik_10}`
- `/api/export/{cik_10}`

This prevents rate-limit exposure during user typing.

For EDGAR calls via `httpx`, a 15-second timeout is set at the client level:

```python
async with httpx.AsyncClient(timeout=15.0) as client:
    response = await client.get(url, headers=headers)
```

If EDGAR does not respond within 15 seconds, an `httpx.TimeoutException` is caught and re-raised as a structured `503` response with message: *"EDGAR is taking longer than usual. Please try again in a moment."* 

The same pattern applies to any synchronous HTTP calls made inside the yfinance wrapper.  
Silent hangs that eventually surface as platform-level 502s are worse than a clean, user-readable timeout message.



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

When a fallback fires, the `warnings` array includes a structured note (e.g., `{ code: "fallback_revenue", message: "Primary tag absent, using Revenues" }`).  
The Input Audit Panel shows which tag was used.

If no tag matches at all: value returns as `None`, UI displays `N/A`.

### Amended Filings

When both an original filing (e.g., `10-Q`, `10-K`) and an amended version (`10-Q/A`, `10-K/A`) exist for the same reporting period, the original filing is always used. If the original filing is unavailable, the amended filing is used as a fallback.

Rationale:
- Amended filings often restate only specific line items, not the full financial set
- Mixing amended and original data introduces internal inconsistency across concepts

A structured warning `amendment_exists` is attached to the affected period to indicate that an amended version is available but was not used.

Tradeoff: reproducibility and consistency are prioritized over post-hoc accuracy.

### Unit Validation

Only facts with `unitRef: USD` are accepted. Non-USD facts are rejected at extraction.  
USD-denominated facts reported with scaling (e.g., thousands or millions) are normalized when scale metadata is present. If scale cannot be reliably determined, the fact is rejected to prevent magnitude errors.



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

Each component maps to primary and fallback XBRL tags. Missing components are treated as zero. While this often reflects true zero balances, incomplete XBRL tagging can result in understated enterprise value.

If *all* financial debt tags are absent, is flagged as potentially understated with `ev_debt_missing` tag that highlights: *"All financial debt tags were absent. EV may be understated."*

### Long-Term Debt Deduplication

`LongTermDebtNoncurrent` is treated as the primary representation of non-current debt.

If `LongTermDebt` is present, the following logic is applied:

- If `LongTermDebt ≈ LongTermDebtNoncurrent + LongTermDebtCurrent` (within tolerance), it is treated as total debt and not added separately
- Otherwise, it is treated as non-current debt only

When this reconciliation occurs, a `debt_deduplicated` warning is attached.

This prevents double-counting current maturities in EV.

### Finance Lease Liabilities

Finance leases are economically identical to debt: the lessee controls the asset and owes fixed future payments. They are recognized under ASC 842 (effective for most public companies from fiscal years beginning after December 15, 2018).

Consistency with the denominators matters:
- Finance lease expense splits into a **depreciation component** (hits D&A, affecting EBITDA and EBIT) and an **interest component** (below operating income).
- EV includes `FinanceLeaseLiabilityNoncurrent` + `FinanceLeaseLiabilityCurrent` in the numerator.
- EBITDA adds back D&A, which includes the depreciation component of finance lease expense.
- EBIT includes the amortization of the lease asset but excludes the interest portion.

This maintains internal consistency across EV/EBITDA and EV/EBIT multiples.

If both finance lease tags are absent, they are treated as zero. A warning is set only for capital-intensive SIC sectors (manufacturing 2000–3999, transportation/utilities 4000–4999), where material finance leases are more likely. Absence is legitimate for most companies.

### Pre-ASC 842 Compatibility

For periods prior to ASC 842 adoption, the following fallback tags are used:

- `CapitalLeaseObligationsCurrent`
- `CapitalLeaseObligationsNoncurrent`

If these tags are used, a `lease_pre_asc842` warning is set to indicate differing accounting treatment across periods.

### Cash Fallback

Primary tag: `CashAndCashEquivalentsAtCarryingValue`. Fallback: `CashCashEquivalentsAndShortTermInvestments`, which includes short-term investments. 

When the fallback fires, `cash_fallback_includes_investments` warning is set, as the wider definition can overstate the cash deduction.

### Operating Lease Exclusion

Operating lease liabilities are excluded from EV. Under ASC 842, operating lease expense flows through operating expenses as a single line - it is not split into interest and depreciation the way finance lease expense is. 

Adding operating lease liabilities to EV without stripping lease expense from the denominators would overstate EV/EBIT and EV/EBITDA. Correcting for this (the EBITDAR methodology) requires non-GAAP adjustments not available from EDGAR XBRL. Excluded and disclosed.



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

- **EV/EBIT label:** EV/EBIT is proxied by `OperatingIncomeLoss`. For most companies these are equivalent. They can diverge when companies classify equity-method investment income or similar non-operating items above the operating line. The label in the UI and audit panel reads "Operating Income" to reflect the data source accurately.
- `N/A` means data is unavailable, not that the result is negative - these are categorically different.
- **Negative results** are displayed as negative numbers (negative P/E for loss-making companies, negative EV for net-cash companies, exception for FCF). 
- **Negative book value:** if `StockholdersEquity < 0`, P/B returns `N/A` with a `negative_book_value` warning. The ratio is not analytically interpretable when equity is negative, which is a distinct condition from a near-zero denominator and must be checked explicitly before the near-zero guard runs.
- **Negative FCF** returns `N/A` with an explanatory note. Negative FCF is ambiguous and not reported by professional databases as a negative multiple.
- **Near-zero denominators:** if `abs(denominator) < 0.01`, return `N/A` with `denominator_near_zero`. This avoids unstable or non-meaningful multiples from numerically insignificant denominators.
- **P/E fallback:** if diluted EPS is absent, falls back to basic EPS, labeled "P/E (basic)" with `fallback_eps_basic` warning.
- **CapEx Tag Selection:**
Only cash-based CapEx tags are used. Obligation-based tags (e.g., `CapitalExpendituresIncurringObligation`) are excluded because they do not reflect cash outflows and would distort free cash flow.
- **CapEx sign normalization:** CapEx is sometimes reported as a negative cash outflow. The tool takes the absolute value if necessary and sets `capex_sign_normalized`.
- **EV/EBIT vs EV/EBITDA:** both are shown so users can see the D&A impact. For capital-intensive companies they will diverge materially, for asset-light companies they will be close.

All computations are pure functions in `multiples.py`. Each returns `{ value: Decimal | None, warnings: [...] }`. All edge cases are covered in `test_multiples.py`.



### EBITDA Construction Limitations

```
EBITDA = Operating Income
       + Depreciation & Amortization
```

XBRL reporting of D&A is inconsistent:
- Some filers split depreciation and amortization into separate tags
- Some omit amortization entirely from standard tags

No reconstruction is attempted beyond the defined tag set. This can understate EBITDA and inflate EV/EBITDA.



## TTM (Trailing Twelve Months)

### Structure

Twelve TTM periods derived from 12 quarterly filings, stepping back one quarter at a time. Period 1 is the most recent quarter, Period 12 is 11 quarters prior.  
Each column is labeled `TTM [quarter end date]`. The filing date is shown separately so the price fetch date is verifiable.

12 periods provides enough history to see trend direction without requiring excessive historical EDGAR data.

### TTM Bridge

Income statement and cash flow items:

```
TTM = Most Recent Annual + Current YTD − Prior Year YTD
```

"YTD" is the single cumulative value reported in the 10-Q for that quarter - not a sum of individual quarters.

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
- Fallback status (primary or fallback)
- Unit
- Entity context (consolidated or segment)

Makes the data source traceable without requiring the user to inspect raw filings. Collapsible in the UI to reduce clutter.



## Error Handling

**Principle: a wrong answer is worse than no answer.**

| Situation | Behavior |
|---|---|
| XBRL tag missing | `None` returned, UI shows `N/A` with tooltip explaining the tag was not found |
| Negative result (valid) | Displayed as negative with note |
| Negative FCF | Displayed as `N/A` with explanatory note |
| Invalid price | Structured `price_unavailable` error applied |
| All debt tags absent | EV computed with debt=0, `ev_debt_missing` warning applied |
| Non-USD unit | Fact rejected at extraction |
| Ambiguous fact (multiple contexts) | Deterministic rule applied, if ambiguous `None` + `ambiguous_fact` applied |
| Period mismatch | Fact rejected, `period_mismatch` warning unused (kept for completeness) - validated with exact-key matching |
| EDGAR 429 | Retry once with exponential backoff, `503` to client if it fails |
| EDGAR timeout (>15s) | `503` with user-readable message |



## Testing Strategy

### Fixture-Based Extraction Tests

Real `companyfacts.json` responses (Apple, Microsoft, Cricut, Delta Air Lines, Target, Snowflake, Instacart, Berkshire Hathaway) are stored in `tests/fixtures/`. 

Extraction functions accept the parsed JSON dict directly, there is no HTTP mocking needed for extraction tests. This validates behavior against real API responses, not just that the correct URL was called.

Coverage includes: tag fallback behavior, finance lease handling, CapEx sign normalization, missing tag handling, non-USD rejection.

### TTM Tests

Constructed `ExtractedFinancials` objects covering: normal bridge, prior-year YTD absent (annualization fallback), fiscal year change mid-period (bridge degrades to fallback, not wrong result), 12-period rolling window correctness.

### Calculation Tests

Pure function tests on `multiples.py`: positive/negative/zero cases, negative EV (net-cash companies), negative FCF, near-zero denominators, EV composition with and without finance lease liabilities, EV/EBIT vs EV/EBITDA divergence on capital-intensive fixtures.

### Price Service Tests

`yfinance` library mocked directly. 

Coverage: output mapping, date validation (returned date >= requested), price > 0 check, ticker normalization.

### Frontend

Thin rendering layer. 

Unit tests on pure utility functions (formatters, display logic), manual verification otherwise. Deliberate tradeoff, not an oversight.



## CORS

```python
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",")
```

- **Local dev:** `http://localhost:5173`
- **Production:** Vercel deployment URL only



## URL State

Results are mapped to `/?cik={CIK_10}`. CIK-based URLs are stable and shareable. 

A "Copy Link" button copies the current URL to the clipboard via `navigator.clipboard.writeText(window.location.href)`. Button briefly shows "Copied!" on success. Degrades gracefully if the clipboard API is unavailable.



## Intentionally Excluded

| Feature | Reason |
|---|---|
| User accounts | Stateless by design, no persistence needed |
| Sector-normalized multiples | Requires a peer database, out of scope for v1 |
| Non-GAAP / adjusted figures | Company-specific, not available from EDGAR XBRL |
| Operating lease liabilities in EV | Requires EBITDAR adjustment, non-GAAP |
| Treasury Stock Method | Requires non-standard input and assumptions |
| IFRS support | Different taxonomy, EDGAR handles US filers only |
| EDGAR XBRL deep-linking | EDGAR does not support stable tag-level URLs |



## Tradeoffs Summary

| Area | Decision | Tradeoff |
|---|---|---|
| Hosting | Render free tier | Cache lost on cold start |
| Cache | In-process memory | Lost on restart, no horizontal scale |
| Price data | `yfinance` | Unofficial, can fail intermittently |
| Figures | GAAP only | No adjusted EBITDA or non-GAAP metrics |
| /A Filings | Originals preferred | Post-hoc accuracy may suffer |
| Scope | U.S. SEC filers | No IFRS / foreign private issuer support |
