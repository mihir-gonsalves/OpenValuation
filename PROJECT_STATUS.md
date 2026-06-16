# OpenValuation: Project Status

**Last Updated:** 2026-06-14  
**Current Phase:** Phase 4 - Frontend and Excel (Phase 3 complete).

## Overview

A free, no-account web tool that computes valuation multiples for any publicly traded U.S. company using SEC EDGAR filings and period-specific historical prices.

Each filing uses the split-adjusted close on the next trading day after submission. This reflects the first price where the market had full visibility into the disclosed data.

**Stack**

* Frontend: React 18 + Vite -> Vercel
* Backend: FastAPI (Python 3.10+) -> Render
* Data: SEC EDGAR + `yfinance`
* Excel: `openpyxl`

All computation and data transformation run in Python. The frontend renders results only.

## Architecture Decisions

### Backend and Frontend Separation

React and FastAPI are used instead of a monolithic framework.

* Python is better suited for XBRL parsing, TTM computation, and Excel generation
* `openpyxl` enables live Excel formulas
* Clear separation of concerns between computation and UI

### CIK as Primary Identifier

* Tickers can change
* CIK is stable

Search resolves name or ticker to CIK once. All downstream endpoints use:

* `/financials/{cik_10}`
* `/export/{cik_10}`

### In-Memory Cache

* Keyed by CIK_10
* TTL: 24 hours
* Suitable for low request volume

Render cold starts clear the cache. The first request after restart triggers a full EDGAR fetch. This behavior is exposed in the UI.

### Price Service Isolation

All pricing logic is contained in `price.py`.

* Uses `yfinance`
* Normalizes tickers such as `BRK.B` to `BRK-B`
* Can be replaced with a single-file change if needed

### company_index.py

* Loads SEC ticker dataset
* Builds in-memory search index
* Provides synchronous lookup for `/api/search`

### edgar.py

Responsibilities:

* Fetch companyfacts (XBRL)
* Fetch company metadata (submissions endpoint)
* Handle EDGAR rate limiting and retries

Does not participate in search.

## Core Financial Logic

### TTM Presentation

* 12 rolling TTM periods from the 12 most recent quarterly filings
* Each period steps back one quarter
* Labels follow the format: `TTM [quarter end date]`

Rules:

* Income statement and cash flow use the TTM bridge
* Balance sheet values are point-in-time

TTM bridge:

* Most Recent Annual + Current YTD − Prior Year YTD

Fallback:

* If prior-year YTD is missing
* Use: Current YTD ÷ quarters elapsed × 4
* Apply `ttm_annualized` warning

### Enterprise Value

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

Key rules:

* Missing components default to zero
* If all debt and lease tags are missing, EV is flagged as potentially understated

Debt handling:

* `LongTermDebtNoncurrent` is primary
* `LongTermDebt` (total, current + non-current) is the fallback when the primary is absent
   * When used, current portion of LT debt is zeroed out to avoid double-counting
* Apply `debt_deduplicated` warning when the fallback fires

Leases:

* Finance leases are included
* Pre-ASC 842 uses capital lease tags with `lease_pre_asc842` warning
* Operating leases are excluded to avoid distortion

Cash fallback:

* If fallback tag includes short-term investments
* Apply `cash_fallback_includes_investments` warning

EBITDA limitation:

* EBITDA = Operating Income + D&A
* Incomplete D&A tagging can understate EBITDA
   * When D&A is absent, EV/EBITDA is N/A

### Multiples

| Multiple   | Formula               | Notes                    |
| ---------- | --------------------- | ------------------------ |
| P/E        | Price ÷ Diluted EPS   | Falls back to basic EPS  |
| EV/EBITDA  | EV ÷ EBITDA           | Near-zero guard          |
| EV/EBIT    | EV ÷ Operating Income | Negative allowed         |
| EV/Revenue | EV ÷ Revenue          | Zero guard               |
| P/S        | Market Cap ÷ Revenue  |                          |
| P/B        | Market Cap ÷ Equity   | Near-zero guard, negative equity returns N/A with `negative_book_value` |
| P/FCF      | Market Cap ÷ FCF      | Near-zero guard, negative FCF returns N/A with `negative_fcf` |

Rules:

* Missing data returns `N/A`
* Negative values are shown as negative except P/FCF
* Near-zero denominators return `N/A` with `denominator_near_zero`

Additional handling:

* CapEx uses absolute value if negative
* Only cash-based CapEx tags are used
* Finance lease absence in capital-intensive sectors triggers warning
* Market cap and EPS use different share definitions which introduces minor inconsistency

## Data Integrity Guarantees

Primary principle: no silent wrong values.

### Enforcement

* Only USD facts accepted
* Non-USD facts rejected
* Scale normalization is not required: the EDGAR companyfacts API reports values in full (unscaled) units

Duplicate handling:

* Priority order: original > amendment
* Remaining ambiguity returns `None` with `ambiguous_fact`

Period validation:

* Uses exact-key matching, not `period_mismatch` warning

Fallback logic:

* Ordered tag chains per concept
* First match used
* Exposed in UI and Excel

Warnings:

* Each period includes structured warnings
* Results are shown with flags, not suppressed

Interpretation:

* `N/A` means missing data
* Negative values mean valid negative results

### Transparency Surfaces

* Input Audit Panel in UI
* Raw Financials sheet in Excel
* Structured warnings in API response

### Known Limitations

* Financial companies may produce less meaningful results
* IFRS filers return errors
* Amended filings are ignored in favor of original filings

## Development Phases

### Phase 1: Core Backend Infrastructure

Status: Complete

* FastAPI setup, CORS, and routing
* EDGAR client with headers and timeout
* `/api/search` endpoint
* In-memory company index loaded at startup
* Initial XBRL extraction structure
* Cache implementation
* Price service wrapper
* Error and warning models

### Phase 2: XBRL Extraction and TTM Logic

Status: Complete

Highest risk area.

Extraction:

* Unit normalization
* Duplicate selection logic
* Period coherence enforcement
* Full tag fallback chains
* `ExtractedFinancials` schema

TTM:

* YTD vs single-quarter detection
* TTM bridge
* Rolling 12-period window
* Annualization fallback

Testing:

* Fixture-based tests using real companies
* Validate unit handling, duplicates, and periods

### Phase 3: Multiples Engine

Status: Complete

* Seven pure calculation functions
* EV computation including leases
* Denominator guards
* Negative handling
* Finance lease warning logic
* Full test coverage

See `PHASE_3_SPEC.md` for the decision record.

### Phase 4: Frontend and Excel

Status: Not started

Frontend:

* Debounced typeahead search with dropdown
* Results table with 12 periods
* Timestamp and URL state
* Audit panel and download
* Error and loading states

Excel:

* Summary sheet
* Raw Financials sheet
* Calculations sheet with formulas

### Phase 5: Deployment

Status: Not started

* Deploy backend on Render
* Deploy frontend on Vercel
* Configure CORS
* Set EDGAR headers
* CI and deployment pipeline

### Phase 6: QA and Finalization

Status: Not started

Manual testing across multiple company types.

Key scenarios:

* Finance lease handling
* Cash fallback behavior
* CapEx normalization
* Negative values
* Near-zero denominators
* URL state and deep linking

## Risk Areas

| Risk                      | Mitigation                                |
| ------------------------- | ----------------------------------------- |
| TTM bridge errors         | Write fixture tests before implementation |
| Unit normalization errors | Reject non-USD facts and test explicitly  |
| Duplicate fact ambiguity  | Deterministic rules with fallback to None |
| Period mismatch           | Enforce strict threshold and test         |
| Tag coverage gaps         | Expand fallback chains iteratively        |
| yfinance instability      | Isolate in `price.py`                     |
| Cash overstatement        | Explicit fallback warning                 |
| External timeouts         | Enforce timeout handling and tests        |
| Lease omission            | Sector-based warning                      |
| Cold start delays         | Communicate in UI                         |
| CORS errors               | Configure before deployment               |
| Debt double counting      | Deduplication logic                       |
| CapEx misclassification   | Restrict to cash-based tags               |
| EBITDA understatement     | Document limitation                       |
| Lease standard changes    | Use fallback tags with warnings           |

## How to Resume Development

1. Read `DESIGN.md`
2. Collect EDGAR fixtures for key companies
3. Write tests for unit handling, duplicates, and periods
4. Implement TTM logic in isolation
5. Build multiples engine after extraction passes tests
6. Validate ticker normalization
7. Validate finance lease behavior

## Success Criteria

### MVP

* Seven multiples across up to 12 TTM periods
* Correct TTM computation
* Missing data shown as `N/A`
* Negative values displayed correctly
* Finance leases included, operating leases excluded
* Excel export with formulas and traceability
* Warnings visible and accurate
* URL state functional
* Cold start behavior communicated
* Financial company warning and IFRS rejection

### Portfolio-Ready

* All MVP criteria met
* Manual QA across multiple companies
* No silent incorrect values
* Stable deployment
* Documentation complete and consistent
