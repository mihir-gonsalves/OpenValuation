# OpenValuation

`OpenValuation` is a free, no-account web tool for computing valuation multiples on any publicly traded U.S. company. Enter a name or ticker — the tool fetches XBRL filings from SEC EDGAR, computes trailing twelve-month (TTM) multiples, and displays them in a table.

An Excel workbook is available for download, exposing every formula and raw input value so the math is fully auditable and adjustable.

This is a portfolio project built on zero-cost infrastructure. Tradeoffs are documented below.



## How to Use

### Prerequisites

- Node.js 18+
- Python 3.11+

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

Frontend runs at `http://localhost:5173` and proxies API requests to `http://localhost:8000`.



## What It Does

### 1. Company Lookup

Accepts a company name or ticker and resolves it to an SEC Central Index Key (CIK) — EDGAR's stable internal identifier. If a name query matches multiple registrants, the frontend presents up to five candidates for confirmation. The company's SIC industry description is fetched and displayed alongside the name and ticker.

### 2. Filing Retrieval

Fetches the full XBRL fact history from:

```
data.sec.gov/api/xbrl/companyfacts/{CIK}.json
```

The 16 most recent quarterly filing periods (10-Q and 10-K) are selected from this response.

### 3. Price Data

For each filing period, the adjusted close price on the next trading day after the EDGAR filing timestamp is used — the first price at which the market had full visibility into the disclosed figures. Adjusted close is used so stock splits don't distort cross-period comparisons.

- **Shares outstanding** — extracted from XBRL (`CommonStockSharesOutstanding`), matched to the filing period
- **Market cap** — uses basic shares outstanding
- **P/E** — uses diluted EPS (`EarningsPerShareDiluted`); falls back to basic EPS if absent, labelled accordingly

### 4. Multiple Calculation

| Multiple | Formula |
|---|---|
| P/E | Price per Share ÷ Diluted EPS |
| EV/EBITDA | Enterprise Value ÷ EBITDA |
| EV/EBIT | Enterprise Value ÷ EBIT |
| EV/Revenue | Enterprise Value ÷ Revenue |
| P/S | Market Cap ÷ Revenue |
| P/B | Market Cap ÷ Stockholders' Equity |
| P/FCF | Market Cap ÷ Free Cash Flow |

All multiples use reported GAAP figures with no adjustment for one-time items, SBC, or non-recurring charges. Results are labelled "GAAP" in the UI.

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

Finance leases are included in EV on the same basis as other debt — they represent fixed future payment obligations controlled by the lessee. Missing debt components are treated as zero. If all debt tags are absent, the EV is flagged as potentially understated.

**Free Cash Flow** = Operating Cash Flow − Capital Expenditures. If FCF is negative or either tag is missing, P/FCF displays as `N/A` with an explanatory note.

### 5. TTM Presentation

12 TTM periods are derived from the 16 most recent filings, stepping back one quarter at a time. Each column is labelled `TTM [quarter end date]`.

- Income statement and cash flow items are TTM-annualized
- Balance sheet items are point-in-time as of the period end date

### 6. Results Display

All periods are shown in a single table. A "data as of" timestamp reflects when data was last fetched from EDGAR. Negative multiples (e.g., negative P/E for a loss-reporting company) are displayed as negative numbers. `N/A` is used only when a required XBRL tag was missing — not for zero or negative values.

Denominators with absolute value below 0.01 are treated as effectively zero. The multiple displays as `N/A` with a `denominator_near_zero` note.

Per-period warnings (e.g., `ev_debt_missing`, `fallback_eps_basic`) are shown inline rather than as blocking errors.

### 7. Input Audit Panel

Displayed below the results table, showing which XBRL tag was used for each concept and whether any fallback fired.

Displays for each extracted concept:
- XBRL tag name matched
- Fallback status (primary vs. fallback)
- Unit
- Entity context (consolidated vs. segment)

### 8. URL State

When results are displayed, the URL updates to `/?cik={CIK}`. Loading the app with a CIK parameter pre-populates the search and fetches results automatically. A "Copy Link" button copies this URL for sharing. CIK is used rather than ticker because it is EDGAR's stable, unambiguous identifier.

### 9. Excel Export

Generates a `.xlsx` workbook with three sheets:

- **Summary** — final multiples table, company info, data timestamp, and active warnings
- **Raw Financials** — every extracted XBRL value with tag name, fallback status, and filing period
- **Calculations** — each multiple as a live Excel formula referencing the Raw Financials sheet



## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                        User (Browser)                              │
│              Inputs company name or ticker                         │
└───────────────────────────┬────────────────────────────────────────┘
                            │ HTTP (JSON)
┌───────────────────────────▼────────────────────────────────────────┐
│               React + Vite  (hosted on Vercel)                     │
│                                                                    │
│  SearchBar -> DisambiguationList -> ResultsTable -> DownloadButton │
│                            AuditPanel                              │
│                                                                    │
│  All state lives in React. No local storage. No client-side        │
│  computation beyond rendering.                                     │
└───────────────────────────┬────────────────────────────────────────┘
                            │ HTTP (JSON / binary)
┌───────────────────────────▼────────────────────────────────────────┐
│               FastAPI  (hosted on Render free tier)                │
│                                                                    │
│  POST /api/search          EDGAR company lookup -> CIK + SIC       │
│  GET  /api/financials/{cik} EDGAR XBRL fetch -> multiples JSON     │
│  GET  /api/export/{cik}     openpyxl workbook -> binary response   │
│                                                                    │
│  In-memory cache: dict[CIK -> {data, cached_at}]                   │
│  TTL: 24 hours. Keyed by CIK (stable identifier), not ticker.      │
└──────────────────┬────────────────────────┬────────────────────────┘
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
| Frontend | React 18 + Vite | No SSR; FastAPI owns all computation |
| Frontend hosting | Vercel | Deploys from GitHub, global CDN |
| Backend | FastAPI + Pydantic | Async, strong typing |
| Backend hosting | Render (free tier) | See cold start limitation below |
| XBRL data | SEC EDGAR | Free, no key, authoritative |
| Price data | yfinance | No key; unofficial — see limitations |
| Excel generation | openpyxl | Writes live Excel formulas, not values |
| Cache | In-process Python dict | 24h TTL; cleared on restart |



## Limitations and Tradeoffs

### Cold Starts (Render Free Tier)

Render's free tier spins down the process after 15 minutes of inactivity. Cold starts take 30–50 seconds, during which the user sees a loading spinner. The in-memory cache is also cleared on restart, so the first request after a cold start pays the full EDGAR fetch cost on top of the startup delay. Subsequent requests for the same company use the cached response.

### Price Data (yfinance)

`yfinance` is an unofficial scraper of Yahoo Finance's internal API. Yahoo breaks it several times per year, the open-source community patches it. Where price data is unavailable or the ticker format can't be normalised (e.g., `BRK.A` vs `BRK-A`), a `price_unavailable` warning is surfaced rather than computing on a null value.

### XBRL Variability

Micro-cap companies and older filings sometimes use non-standard or legacy tags. The backend applies fallback logic for each concept. If all fallbacks fail, the relevant multiple is displayed as `N/A`. All monetary values are validated against their `unitRef` — non-USD values are rejected rather than silently used at the wrong scale.

Financial companies (SIC 6000–6999) are flagged in the UI. Conventional debt/equity definitions don't map cleanly onto bank balance sheets, so multiples may be less meaningful for these filers.

### Operating Leases

Operating lease liabilities are intentionally excluded from EV. Under ASC 842, operating lease expense flows through operating expenses as a single line — adding the liability to EV without a corresponding EBITDAR adjustment would overstate EV/EBIT and EV/EBITDA. That adjustment requires non-GAAP, company-specific inputs not available from EDGAR. Full rationale is in `DESIGN.md`.

### Amended Filings

When both a `10-Q` and `10-Q/A` exist for the same period, the original filing is used. A warning (`amendment_exists`) is set for the affected period.



## Project Structure (Planned)

```
OpenValuation/
├── backend/
│   ├── app/
│   │   ├── main.py                  FastAPI entry point, CORS, route registration
│   │   ├── routers/
│   │   │   ├── search.py            POST /api/search
│   │   │   ├── financials.py        GET  /api/financials/{cik}
│   │   │   └── export.py            GET  /api/export/{cik}
│   │   ├── services/
│   │   │   ├── edgar.py             EDGAR HTTP client + XBRL extraction + TTM logic
│   │   │   ├── price.py             yfinance wrapper
│   │   │   ├── multiples.py         Pure calculation functions
│   │   │   └── workbook.py          openpyxl Excel builder
│   │   ├── models/
│   │   │   ├── company.py           Pydantic models: CompanyResult, FilingPeriod
│   │   │   ├── financials.py        Pydantic models: ExtractedFinancials, MultipleSet
│   │   │   └── cache.py             CacheEntry model + TTL logic
│   │   └── cache.py                 In-memory store (module-level dict)
│   ├── tests/
│   │   ├── test_edgar.py
│   │   ├── test_multiples.py
│   │   ├── test_price.py
│   │   └── fixtures/
│   │       ├── aapl_companyfacts.json
│   │       └── msft_companyfacts.json
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── App.jsx
│   │   ├── components/
│   │   │   ├── SearchBar.jsx
│   │   │   ├── DisambiguationList.jsx
│   │   │   ├── ResultsTable.jsx
│   │   │   ├── AuditPanel.jsx
│   │   │   ├── LoadingState.jsx
│   │   │   └── ErrorMessage.jsx
│   │   └── api/
│   │       └── client.js            Typed fetch wrappers for all backend routes
│   ├── index.html
│   └── vite.config.js
├── README.md
└── DESIGN.md
```



## Data Sources

### SEC EDGAR

- **Endpoints:**
  - `https://efts.sec.gov/LATEST/search-index?q={query}` — company name/ticker search
  - `https://data.sec.gov/submissions/{CIK}.json` — company metadata (SIC code, etc.)
  - `https://data.sec.gov/api/xbrl/companyfacts/{CIK}.json` — full XBRL filing history
- **Rate limit:** 10 requests/second. EDGAR requires a `User-Agent` header with application name and contact email.
- **Authentication:** None required.

### Yahoo Finance (yfinance)

- **Provides:** Historical adjusted close prices for filing-date price matching.
- **Authentication:** None required.
- **Reliability:** Unofficial — see Limitations.



## XBRL Tags Used

All tags are from the `us-gaap` taxonomy. Tags are listed in fallback order. The Input Audit Panel shows which tag fired for each concept and period.

| Concept | Primary Tag | Fallback Tag(s) |
|---|---|---|
| Shares Outstanding | `CommonStockSharesOutstanding` | — |
| Revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` | `Revenues`, `SalesRevenueNet` |
| Net Income | `NetIncomeLoss` | — |
| EBIT | `OperatingIncomeLoss` | — |
| D&A | `DepreciationDepletionAndAmortization` | `DepreciationAndAmortization` |
| EPS (Diluted) | `EarningsPerShareDiluted` | `EarningsPerShareBasic` |
| Total Assets | `Assets` | — |
| Stockholders' Equity | `StockholdersEquity` | `StockholdersEquityAttributableToParent` |
| Long-Term Debt | `LongTermDebt` | `LongTermDebtNoncurrent` |
| Short-Term Borrowings | `ShortTermBorrowings` | `ShortTermDebt` |
| Current Portion of LT Debt | `LongTermDebtCurrent` | `LongTermNotesPayableCurrent` |
| Finance Lease (Non-Current) | `FinanceLeaseLiabilityNoncurrent` | — |
| Finance Lease (Current) | `FinanceLeaseLiabilityCurrent` | — |
| Cash | `CashAndCashEquivalentsAtCarryingValue` | `CashCashEquivalentsAndShortTermInvestments` |
| Minority Interest | `MinorityInterest` | `NonredeemableNoncontrollingInterest` |
| Preferred Stock | `PreferredStockValue` | `PreferredStockRedeemableValue` |
| Operating Cash Flow | `NetCashProvidedByUsedInOperatingActivities` | — |
| Capital Expenditures | `PaymentsToAcquirePropertyPlantAndEquipment` | `CapitalExpendituresIncurringObligation` |

**Notes:**
- Non-USD values are rejected and treated as missing.
- If all three debt tags and both finance lease tags are absent, EV is flagged as potentially understated (`ev_debt_missing`).
- Finance lease absence warning is triggered for capital-intensive SIC codes (manufacturing 2000–3999, transportation/utilities 4000–4999) where material finance leases are expected.
- CapEx sign is normalised if reported as a negative outflow; a `capex_sign_normalised` warning is set if the absolute value was taken.



## Non-Goals

This tool intentionally does not:

- Support non-U.S. companies or foreign private issuers
- Provide real-time or intraday price data
- Store user data or require an account
- Provide buy/sell recommendations or sector-normalised comparisons
- Guarantee meaningful results for financial companies (banks, insurance, REITs)
- Adjust GAAP figures for non-recurring items, restructuring, or SBC
- Include operating lease liabilities in EV

All output is informational. Nothing here is investment advice.



## License

MIT
