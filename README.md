# OpenValuation

`OpenValuation` is a free, no-account web tool that computes valuation multiples for any publicly traded U.S. company. Enter a company name or ticker to fetch XBRL filings from SEC EDGAR, compute trailing twelve-month (TTM) multiples, and display them in a tabular format.

From the tabular view, an Excel workbook is available for download. It exposes all raw inputs and formulas, making every calculation fully auditable and adjustable.

This project is built entirely on zero-cost infrastructure. Tradeoffs are documented below.



## How to Use

### Prerequisites

- Node.js 18+
- Python 3.10+

### Run Locally

**Backend**

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

**Frontend**

```bash
cd frontend
npm install
npm run dev
```

Other useful scripts can be found in `frontend/README.md`

Frontend runs at `http://localhost:5173` and proxies `/api` requests to `http://localhost:8000`
(see `frontend/vite.config.ts`), so no CORS configuration is needed in development.

In production, set `VITE_API_BASE` (in `frontend/.env` - see `.env.example`) to the deployed
backend URL, and add the Vercel frontend origin to the backend's `ALLOWED_ORIGINS`.



## What It Does

### 1. Company Lookup

Accepts a company name or ticker and resolves it to an SEC Central Index Key (CIK), EDGAR's stable internal identifier. 

If a name query matches multiple registrants, the UI presents up to five candidates for selection. 

Additional metadata (SIC, exchange) is retrieved only after selection.

**CIK Format**

- All CIKs are normalized at ingestion to a zero-padded 10-digit string (`CIK_10`), e.g. `0000320193`.
- The SEC `company_tickers.json` dataset provides CIKs as integers, these are converted to `CIK_10` immediately when building the in-memory index.
- All internal systems, cache keys, and API routes use `CIK_10` exclusively.
- When calling EDGAR APIs, the value is prefixed with `"CIK"` (e.g., `CIK0000320193`).

### 2. Filing Retrieval

Fetches the full XBRL fact history from:

```
data.sec.gov/api/xbrl/companyfacts/CIK{CIK_10}.json
```

The 12 most recent quarterly filing periods (10-Q and 10-K) are selected from this response.

### 3. Price Data

Price is defined as the split-adjusted close on the next trading day following the filing submission timestamp. This price reflects the first point at which the market had full visibility into the disclosed figures.

Split-adjusted close is used to ensure stock splits don't distort cross-period comparisons. Dividend reinvestment is not factored in.

Shares and pricing inputs are handled as follows:
- **Shares outstanding** are extracted from XBRL (`CommonStockSharesOutstanding`) and matched to the filing period
  - The DEI tag `EntityCommonStockSharesOutstanding` fallback is matched by accession number
- **Market capitalization** is calculated using basic shares outstanding
- **P/E** uses diluted EPS (`EarningsPerShareDiluted`), falling back to basic EPS when unavailable, with labeling applied

There is a structural timing mismatch in the underlying data:
- **Shares outstanding** are point-in-time values
- **EPS** is based on weighted-average shares over the reporting period

This can introduce minor inconsistencies in per-share comparisons.

A second inconsistency arises in equity value construction:
- **Market capitalization** uses basic shares outstanding
- **P/E** uses diluted EPS

Institutional data providers typically use diluted shares, derived using the Treasury Stock Method (TSM), for equity value. The difference is usually small but may be noticeable for companies with significant stock-based compensation. 

Implementing TSM is extremely complex and out of scope for this project. Full rationale is in `DESIGN.md`.

### 4. Multiple Calculation

| Multiple | Formula |
|---|---|
| P/E | Price per Share ÷ Diluted EPS |
| EV/EBITDA | Enterprise Value ÷ EBITDA |
| EV/EBIT | Enterprise Value ÷ Operating Income |
| EV/Revenue | Enterprise Value ÷ Revenue |
| P/S | Market Cap ÷ Revenue |
| P/B | Market Cap ÷ Stockholders' Equity |
| P/FCF | Market Cap ÷ Free Cash Flow |

All multiples are computed using reported GAAP values with no adjustments for one-time items, stock-based compensation, or other non-recurring charges. Results are labeled "GAAP" in the UI.

**Enterprise Value**

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

Finance leases are included on the same basis as other debt, as they represent fixed payment obligations. Missing components are treated as zero. If all debt-related tags are absent, EV is flagged as potentially understated.

**EBITDA Construction Limitation**

```
EBITDA = Operating Income
       + Depreciation & Amortization
```

Depreciation and amortization may be fragmented across tags or omitted. No reconstruction is attempted beyond the defined tag set, which can understate EBITDA and inflate EV/EBITDA. When D&A is absent, EV/EBITDA is reported as N/A rather than as a proxy for EV/EBIT.

**Free Cash Flow**

```
FCF = Operating Cash Flow
    − Capital Expenditures
```

If FCF is negative, P/FCF is shown as `N/A` with a `negative_fcf` warning. If required tags are missing, P/FCF is a silent `N/A` (the missing data is already surfaced upstream).

### 5. TTM Presentation

While derived from quarterly filings, TTM periods reflect annualized performance rather than single-quarter results.

Twelve TTM periods are constructed from the 12 most recent quarterly filings, stepping back one quarter at a time. Each TTM period represents a rolling 12-month window anchored to a specific quarter end date, columns are labeled `TTM [quarter end date]`.

- Income statement and cash flow items are TTM-annualized
- Balance sheet items are point-in-time as of the period end date

### 6. Results Display

All periods are displayed in a single table. A "data as of" timestamp indicates when data was last fetched.

- Negative multiples are shown as negative values, except for P/FCF
- `N/A` is used only when required data is missing
- Zero/near-zero denominators (|x| < 0.01) -> `N/A` with `denominator_near_zero`
- Negative denominators are allowed and produce negative multiples, with two
  exceptions: 
    - Negative FCF -> P/FCF is `N/A` with `negative_fcf`
    - Negative Stockholders' Equity -> P/B is `N/A` with `negative_book_value`
    (a negative P/B is not analytically interpretable the way a negative P/E is)

Warnings such as `ev_debt_missing` or `fallback_eps_basic` are displayed inline per period.

### 7. Input Audit Panel

Displayed below the results table. Shows how each value was derived.

For each concept:
- XBRL tag used
- Whether a fallback was applied
- Unit
- Entity context (consolidated or segment)

### 8. URL State

When results are displayed, the URL updates to `/?cik={CIK_10}`. 

Loading the app with a `CIK_10` parameter automatically triggers the lookup and fetch.

A "Copy Link" button copies the current URL. CIK is used instead of ticker because it is EDGAR's stable, unambiguous identifier.

### 9. Excel Export

Generates a `.xlsx` workbook with three sheets:

- **Summary:** Final multiples, company info, timestamp, and warnings
- **Raw Financials:** Extracted XBRL values with tags, fallback status, and filing period
- **Calculations:** Live Excel formulas referencing the Raw Financials sheet



## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                        User (Browser)                                │
│              Inputs company name or ticker                           │
└───────────────────────────┬──────────────────────────────────────────┘
                            │ HTTP (JSON)
┌───────────────────────────▼──────────────────────────────────────────┐
│            React 19 + Vite + TS  (hosted on Vercel)                  │
│                                                                      │
│  SearchBar -> SearchDropdown -> ResultsTable -> DownloadButton       │
│                            AuditPanel                                │
│                                                                      │
│  All state lives in React. No local storage. No client-side          │
│  financial computation, frontend is responsible for presentation,    │
│  formatting, and user interaction only.                              │
└───────────────────────────┬──────────────────────────────────────────┘
                            │ HTTP (JSON / binary)
┌───────────────────────────▼──────────────────────────────────────────┐
│               FastAPI  (hosted on Render free tier)                  │
│                                                                      │
│  POST /api/search               In-memory company index -> CIK       │
│  GET  /api/financials/{cik_10}  EDGAR XBRL fetch -> multiples JSON   │
│  GET  /api/export/{cik_10}      openpyxl workbook -> binary response │
│                                                                      │
│  In-memory cache: dict[CIK_10 -> {data, cached_at}]                  │
│  TTL: 24 hours. Keyed by CIK_10 (stable identifier), not ticker.     │
└──────────────────┬────────────────────────┬──────────────────────────┘
                   │                        │
         ┌─────────▼──────────┐  ┌──────────▼──────────┐
         │  SEC EDGAR API     │  │  Yahoo Finance      │
         │  data.sec.gov      │  │  (via yfinance)     │
         │  No key required   │  │  No key required    │
         │  companyfacts JSON │  │  Historical OHLCV   │
         └────────────────────┘  └─────────────────────┘
```



## Tech Stack

| Layer | Tool | Notes |
|---|---|---|
| Frontend | React 19 + Vite + TypeScript | No SSR, FastAPI owns all computation |
| Frontend hosting | Vercel | Deploys from GitHub, global CDN |
| Backend | FastAPI + Pydantic | Async, strong typing |
| Backend hosting | Render (free tier) | See cold start limitations |
| XBRL data | SEC EDGAR | Free, no key, authoritative |
| Price data | yfinance | No key, unofficial, see limitations |
| Excel generation | openpyxl | Writes live Excel formulas, not values |
| Cache | In-process Python dict | 24h TTL, cleared on restart |



## Limitations and Tradeoffs

### Cold Starts (Render Free Tier)

Render's free tier spins down the process after 15 minutes of inactivity. Cold starts take 30–50 seconds, during which the user sees a loading spinner. 

The in-memory cache is also cleared on restart, so the first request after a cold start pays the full EDGAR fetch cost on top of the startup delay. Subsequent requests for the same company use the cached response.

### Price Data (yfinance)

`yfinance` is an unofficial scraper of Yahoo Finance's internal API. Yahoo breaks it several times per year, the open-source community patches it. 

Where price data is unavailable or the ticker format can't be normalized (e.g., `BRK.A` vs `BRK-A`), a `price_unavailable` warning is surfaced rather than computing on a null value.

### XBRL Variability

Micro-cap companies and older filings sometimes use non-standard or legacy tags. The backend applies fallback logic for each concept. If all fallbacks fail, the relevant multiple is displayed as `N/A`. 

All monetary values are validated against their `unitRef`, non-USD facts are rejected. Scale normalization is not required: the EDGAR companyfacts API reports values in full (unscaled) units.

Financial companies (SIC 6000–6999) are flagged in the UI. Conventional debt/equity definitions don't map cleanly onto bank balance sheets, so multiples may be less meaningful for these filers.

### EPS TTM Bridge

P/E is computed as price ÷ diluted EPS, where EPS is extracted directly as reported (not reconstructed from net income ÷ diluted shares). The TTM bridge (`Annual + Current YTD - Prior YTD`) is applied to EPS just like any other flow item. This is an approximation: because share counts change each period, a correct TTM EPS would weight each quarter's per-share figure by its own share count, which requires summing four individual quarter values. The approximation is consistent with how many data providers compute TTM EPS, but may diverge from a precise calculation for companies undergoing significant share count changes.

### Transition Period Filings

Companies that change their fiscal year end file a "transition period" 10-K covering fewer than 12 months. The TTM bridge treats the transition-period filing as if it were a standard annual, which will produce an incorrect TTM for the periods anchored to it. A `ttm_annualized` warning fires in some of these cases (when prior-year YTD is missing), but not always.

### Operating Leases

Operating lease liabilities are intentionally excluded from EV. Under ASC 842, operating lease expense flows through operating expenses as a single line. 

Adding the liability to EV without a corresponding EBITDAR adjustment would overstate EV/EBIT and EV/EBITDA. That adjustment requires non-GAAP, company-specific inputs not available from EDGAR. 

Full rationale is in `DESIGN.md`.

### Amended Filings

When both an original filing (e.g., `10-Q`, `10-K`) and an amended filing (`10-Q/A`, `10-K/A`) exist for the same period, the original filing is used. A warning (`amendment_exists`) is attached to indicate that a restated version exists but was not used.

If the original filing is unavailable, the amended filing is used as a fallback.

The tool continues to use the original filing for consistency across concepts, which may result in using values that were later restated.

**Limitation:** the anchor selection (which quarterly filing anchors each TTM period) also excludes amendment-only filings. If a company's most recent quarter was submitted exclusively as a `10-Q/A` with no original `10-Q`, that quarter will be skipped and the most recent available period will be one quarter older than expected.


## Project Structure (Planned, Not Enforced)

```
OpenValuation/
├── backend/
│   ├── app/
│   │   ├── main.py                  FastAPI entry point, CORS, route registration
│   │   ├── routers/
│   │   │   ├── search.py            POST /api/search
│   │   │   ├── financials.py        GET  /api/financials/{cik_10}
│   │   │   └── export.py            GET  /api/export/{cik_10}
│   │   ├── services/
│   │   │   ├── company_index.py     In-memory company index + /api/search lookup
│   │   │   ├── edgar.py             EDGAR HTTP client + data fetching
│   │   │   ├── price.py             yfinance wrapper
│   │   │   ├── multiples.py         Pure calculation functions
│   │   │   ├── workbook.py          openpyxl Excel builder
│   │   │   ├── xbrl.py              XBRL extraction
│   │   │   ├── xbrl_maps.py         XBRL data transformation
│   │   │   └── xbrl_warnings.py     XBRL warning helpers
│   │   ├── models/
│   │   │   ├── company.py           Pydantic models: CompanyMeta, CompanyCandidate
│   │   │   ├── errors.py            Pydantic models: Warning, APIError + codes
│   │   │   ├── financials.py        Pydantic models: ExtractedFinancials, MultipleSet, TTMPeriod
│   │   │   └── cache.py             CacheEntry model + TTL logic
│   │   ├── cache.py                 In-memory store (module-level dict)
│   │   └── user_agent.py            EDGAR User Agent Setup     
│   ├── tests/
│   │   ├── test_edgar.py
│   │   ├── test_price.py
│   │   ├── test_search.py
│   │   ├── test_xbrl.py
│   │   ├── test_xbrl_maps.py
│   │   ├── test_xbrl_warnings.py
│   │   ├── test_xbrl_extended.py
│   │   ├── test_xbrl_aapl_msft.py
│   │   ├── test_api_errors.py
│   │   ├── test_multiples.py
│   │   ├── test_financials_route.py
│   │   └── fixtures/
│   │       ├── aapl_CIK0000320193.json
│   │       ├── msft_CIK0000789019.json
│   │       ├── crct_CIK0001828962.json
│   │       ├── dal_CIK0000027904.json
│   │       ├── tgt_CIK0000027419.json
│   │       ├── snow_CIK0001640147.json
│   │       ├── cart_CIK0001579091.json
│   │       └── brkb_CIK0001067983.json
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── App.tsx
│   │   ├── index.css                  Design tokens (single styling control point)
│   │   ├── main.tsx
│   │   ├── api/
│   │   │   ├── client.ts              Typed fetch wrappers + Decimal coercion
│   │   │   ├── queries.ts             TanStack Query hooks
│   │   │   └── types.ts               Typed mirror of the Pydantic contract
│   │   ├── components/
│   │   │   ├── AuditPanel.tsx
│   │   │   ├── CompanyHeader.tsx
│   │   │   ├── CopyLinkButton.tsx
│   │   │   ├── DownloadButton.tsx
│   │   │   ├── ErrorMessage.tsx
│   │   │   ├── ExampleChips.tsx
│   │   │   ├── Learn.tsx
│   │   │   ├── LoadingState.tsx
│   │   │   ├── ResultsTable.tsx
│   │   │   ├── SearchBar.tsx
│   │   │   ├── SearchDropdown.tsx
│   │   │   ├── Sparkline.tsx
│   │   │   ├── WarmingNotice.tsx
│   │   │   └── ui/                     Vendored, re-themed radix primitives
│   │   └── lib/
│   │       ├── errors.ts              Error code -> friendly copy
│   │       ├── format.ts              Display formatting helpers
│   │       ├── useDebounce.ts
│   │       ├── useServerReady.ts
│   │       └── utils.ts
│   ├── index.html
│   └── vite.config.ts
├── README.md
├── DESIGN.md
├── PROJECT_STATUS.md
└── PHASE_*_SPEC.md
```



## Data Sources

### SEC EDGAR

- **Endpoints:**
  - `https://www.sec.gov/files/company_tickers.json` - full ticker-to-CIK map, loaded at startup to build the in-memory search index
  - `https://data.sec.gov/submissions/CIK{CIK_10}.json` - company metadata (SIC code, etc.)
  - `https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK_10}.json` - full XBRL filing history

  `{CIK_10}` denotes the CIK zero-padded to 10 digits (e.g., `0000320193`).

  EDGAR endpoints require this value to be prefixed with `"CIK"`: `CIK0000320193`.

- **Rate limit:** 
  - 10 requests/second
  - EDGAR requires a `User-Agent` header with application name and contact email, no authentication required

### Yahoo Finance (yfinance)

- **Provides:** Historical split-adjusted close prices for filing-date price matching
- **Authentication:** None required
- **Reliability:** Unofficial - see Limitations



## XBRL Tags Used

All tags are from the `us-gaap` taxonomy. Tags are listed in fallback order. The Input Audit Panel shows which tag fired for each concept and period.

| Concept | Primary Tag | Fallback Tag(s) |
|---|---|---|
| Shares Outstanding | `CommonStockSharesOutstanding` | `EntityCommonStockSharesOutstanding` (DEI, matched by filing accession) |
| Revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` | `Revenues`, `SalesRevenueNet`, `RevenueFromContractWithCustomerIncludingAssessedTax` |
| Net Income | `NetIncomeLoss` | — |
| EBIT | `OperatingIncomeLoss` | — |
| D&A | `DepreciationDepletionAndAmortization` | `DepreciationAndAmortization` |
| EPS (Diluted) | `EarningsPerShareDiluted` | `EarningsPerShareBasic` |
| Total Assets | `Assets` | — |
| Stockholders' Equity | `StockholdersEquity` | — |
| Long-Term Debt (Non-Current) | `LongTermDebtNoncurrent` | `LongTermDebt` (see note below) |
| Short-Term Borrowings | `ShortTermBorrowings` | `ShortTermDebt` |
| Current Portion of LT Debt | `LongTermDebtCurrent` | — |
| Finance Lease (Non-Current) | `FinanceLeaseLiabilityNoncurrent` | `CapitalLeaseObligationsNoncurrent` |
| Finance Lease (Current) | `FinanceLeaseLiabilityCurrent` | `CapitalLeaseObligationsCurrent` |
| Cash | `CashAndCashEquivalentsAtCarryingValue` | `CashCashEquivalentsAndShortTermInvestments` |
| Minority Interest | `MinorityInterest` | — |
| Preferred Stock | `PreferredStockValue` | — |
| Operating Cash Flow | `NetCashProvidedByUsedInOperatingActivities` | — |
| Capital Expenditures | `PaymentsToAcquirePropertyPlantAndEquipment` | `PaymentsToAcquireProductiveAssets`, `PaymentsForCapitalImprovements` |

**Notes:**
- Non-USD values are rejected and treated as missing.
- EBIT is proxied by `OperatingIncomeLoss`. 
  - For most companies these are equivalent, but they can diverge when companies classify items such as equity-method investment income above the operating line. 
  - The UI and audit panel label this value as "Operating Income" to reflect the source accurately.
- `LongTermDebt` (total, current + non-current) is the fallback when `LongTermDebtNoncurrent` is absent.
  - When the fallback fires, current portion of LT debt is zeroed out so it is not added a second time to EV.
  - A `debt_deduplicated` warning is set when this adjustment is applied.
- If all three debt tags and both finance lease tags are absent, EV is flagged as potentially understated (`ev_debt_missing`).
- Missing finance lease data triggers a warning for capital-intensive SIC codes where material finance leases are expected.
  - Manufacturing (2000-3999).
  - Transportation/Utilities (4000-4999).
- CapEx sign is normalized if reported as a negative outflow. 
  - `capex_sign_normalized` warning is set if the absolute value was taken.



## Non-Goals

This tool intentionally does not:

- Support non-U.S. companies or foreign private issuers
- Provide real-time or intraday price data
- Store user data or require an account
- Provide buy/sell recommendations or sector-normalized comparisons
- Guarantee meaningful results for financial companies (banks, insurance, REITs)
- Adjust GAAP figures for non-recurring items, restructuring, or SBC
- Include operating lease liabilities in EV

All output is informational. Nothing here is investment advice.



## License

MIT
