# OpenValuation

`OpenValuation` is a free, no-account-required web tool for computing valuation multiples on any publicly traded U.S. company. A user inputs a company name or stock ticker, and the tool fetches the last three annual (10-K) and last three quarterly (10-Q) filings directly from the SEC's EDGAR database, derives the standard valuation multiples, and presents them in a clean table.

Users can download an Excel workbook that exposes every formula and every raw input value — so the math is auditable, adjustable, and theirs to modify.

This is primarily a portfolio project. It is intentionally built on free-tier infrastructure with no vendor lock-in. The tradeoffs that come with that decision are documented explicitly below.



## How To Use

### Prerequisites

- Node.js 18+ (frontend)
- Python 3.11+ (backend)

### Running Locally

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

The frontend dev server runs at `http://localhost:5173` and proxies API requests to `http://localhost:8000`.



## What It Does

1. **Company lookup** — enter a company name ("Apple") or ticker ("AAPL"). The backend resolves this to an SEC Central Index Key (CIK), which is EDGAR's internal identifier for every registered filer. If a name query matches multiple registrants, the frontend presents up to five candidates for confirmation rather than silently resolving to the wrong entity. The company's SIC industry description is fetched at this stage and displayed in the UI alongside the name and ticker, giving users immediate sector context.

2. **Filing retrieval** — the backend fetches the company's full XBRL fact history from `data.sec.gov/api/xbrl/companyfacts/{CIK}.json`. From this single response, it extracts the three most recent 10-K periods and three most recent 10-Q periods.

3. **Price data** — for each filing period, the backend fetches the adjusted close price for the next trading day after the form's EDGAR-recorded filing timestamp, using `yfinance`'s historical OHLCV data. This is the first price at which the market had full visibility into the disclosed information. Adjusted close is used rather than plain close so that stock splits do not distort comparisons across periods. Shares outstanding are extracted directly from the filing's own XBRL data (`CommonStockSharesOutstanding`) rather than from a live price feed, ensuring the share count matches the exact period being valued.

4. **Multiple calculation** — the following multiples are computed for each available period:

   | Multiple | Formula | Notes |
   |-|-|-|
   | P/E | Price per Share ÷ Diluted EPS | Uses diluted EPS; falls back to basic EPS if diluted is absent, labelled accordingly |
   | EV/EBITDA | Enterprise Value ÷ GAAP EBITDA | EBITDA = EBIT + D&A; unadjusted for one-time items; D&A may include amortization of acquired intangibles |
   | EV/Revenue | Enterprise Value ÷ Revenue | — |
   | P/S | Market Cap ÷ Revenue | — |
   | P/B | Market Cap ÷ Book Value of Equity | — |

   Where:

   **Enterprise Value = Market Cap + Total Debt + Finance Lease Liabilities + Minority Interest + Preferred Stock − Cash**

   - **Total Debt** = Long-Term Debt + Short-Term Borrowings + Current Portion of Long-Term Debt
   - **Finance Lease Liabilities** = Finance Lease Liability (Noncurrent) + Finance Lease Liability (Current). These are contractual financial obligations analogous to debt and are included. Operating lease liabilities (capitalized under ASC 842) are excluded — see Limitations.
   - **Minority Interest** = noncontrolling interests in consolidated subsidiaries (zero for most companies)
   - **Preferred Stock** = carrying value of preferred equity, if any (zero for most companies)

   All multiples are computed on **GAAP figures** with no adjustment for one-time charges, restructuring costs, or non-recurring items. Results are labelled "GAAP" in the UI.

   **Annual vs. quarterly periods:** For annual (10-K) filings, income statement items (Revenue, Net Income, EBIT, D&A) are used directly. For quarterly (10-Q) filings, income statement items are computed as trailing twelve months (TTM) to make them comparable to annual figures. TTM = most recent annual value + year-to-date current year − year-to-date prior year (same stub period). Balance sheet items are always point-in-time.

5. **Results display** — multiples for all available periods are shown in a single table, grouped by filing type (annual vs. quarterly). Each column is labelled with the fiscal period end date and form type (10-K or 10-Q TTM). A "data as of" timestamp shows when the data was last fetched from EDGAR. Negative multiples (e.g., negative P/E for a company reporting a net loss) are displayed as negative numbers with an explanatory note — `N/A` is reserved exclusively for cases where a required XBRL tag was not found in the filing, it does not mean the result is negative or zero.

   The API response includes a top-level `warnings` array listing any data quality flags for the period (e.g., `ev_debt_missing`, `fallback_eps_basic`, `ttm_annualised`). These are displayed as inline notes in the UI rather than blocking errors, since the computation is still valid but the user should be aware of the specific uncertainty.

   An **Input Audit Panel** is displayed below the multiples table, showing exactly which XBRL tags were used for each concept and whether any fallbacks fired. Example:

   ```
   Revenue:    RevenueFromContractWithCustomerExcludingAssessedTax
   EPS:        EarningsPerShareDiluted
   Debt:       LongTermDebt + ShortTermBorrowings + LongTermDebtCurrent
   D&A:        DepreciationAndAmortization  (fallback — primary tag absent)
   ```

   This makes the data source fully traceable without requiring the user to download the Excel file.

6. **Excel export** — clicking Download generates a `.xlsx` workbook server-side with three sheets:
   - **Summary** — the final multiples table, company info, data timestamp, and any active warnings
   - **Raw Financials** — every extracted XBRL value, labelled by GAAP tag name, fallback status, and filing period
   - **Calculations** — each multiple computed as a live Excel formula referencing the Raw Financials sheet, so users can change any input and watch the multiples update



## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                        User (Browser)                              │
│              Inputs company name or ticker                         │
└───────────────────────────┬────────────────────────────────────────┘
                            │ HTTP (JSON)
                            ▼
┌────────────────────────────────────────────────────────────────────┐
│               React + Vite  (hosted on Vercel)                     │
│                                                                    │
│  SearchBar → DisambiguationList → ResultsTable → DownloadButton    │
│                            AuditPanel                              │
│                                                                    │
│  All state lives in React. No local storage. No client-side        │
│  computation beyond rendering.                                     │
└───────────────────────────┬────────────────────────────────────────┘
                            │ HTTP (JSON / binary)
                            ▼
┌────────────────────────────────────────────────────────────────────┐
│               FastAPI  (hosted on Render free tier)                │
│                                                                    │
│  POST /api/search          EDGAR company lookup → CIK + SIC        │
│  GET  /api/financials/{cik} EDGAR XBRL fetch → multiples JSON      │
│  GET  /api/export/{cik}     openpyxl workbook → binary response    │
│                                                                    │
│  In-memory cache: dict[CIK → {data, cached_at}]                    │
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

| Layer | Tool | Reason |
|-|-|-|
| Frontend framework | React 18 + Vite | Right-sized; no SSR needed since FastAPI owns the backend |
| Frontend hosting | Vercel (free tier) | Deploys from GitHub; global CDN; no configuration |
| Backend framework | FastAPI + Pydantic | Clean async, strong typing, great for data transformation |
| Backend hosting | Render (free tier) | No cold-start charges for personal projects; see limitations |
| XBRL data | SEC EDGAR API | Free, no key, authoritative |
| Price data | yfinance (Yahoo Finance) | No key; provides full historical OHLCV for filing-date price matching; unofficial — see Limitations |
| Excel generation | openpyxl | Python-native; writes real Excel formulas, not just values |
| Caching | In-process Python dict | Zero cost, sufficient at this scale; see limitations |



## Limitations

These are deliberate tradeoffs, not oversights. They are documented here so the project is honest about its constraints.

### Render Free Tier: Cold Starts
Render's free tier spins down services after 15 minutes of inactivity. The first request after inactivity triggers a cold start that can take 30–50 seconds. The UI displays a loading state with a note when this is expected. There is no fix for this without paying for a persistent server.

### In-Process Cache: Ephemeral
The in-memory cache resets on every cold start. A warm cache after a cold start requires the first user of each company to pay the EDGAR fetch cost again. For a personal portfolio project, this is acceptable. A persistent cache (Redis, Upstash) would solve this at low cost if the project grows.

### XBRL Tag Inconsistency
SEC EDGAR's XBRL data is mostly standardized via the `us-gaap` taxonomy, but companies have discretion in how they label certain line items. The parser includes fallback logic for common tag variations, but edge cases exist. Known categories of problematic companies:
- **Financial companies** (banks, insurance, REITs) — use different balance sheet structures; the tool detects these by SIC code and surfaces an informative warning rather than returning wrong data
- **Foreign private issuers** — file on IFRS rather than US GAAP; tags differ; the tool detects and surfaces a clear error
- **Very small caps** — sometimes file with non-standard or missing tags

When a required tag cannot be found, the corresponding multiple is displayed as `N/A` with a tooltip rather than silently returning a wrong value.

### Operating Lease Liabilities (ASC 842)
Since 2019, companies have been required to capitalize operating lease liabilities on the balance sheet under ASC 842 (`OperatingLeaseLiabilityNoncurrent`, `OperatingLeaseLiabilityCurrent`). This tool includes **finance lease liabilities** in EV (analogous to debt), but **excludes operating lease liabilities**. The traditional EV/EBITDA convention excludes operating leases because EBITDA is already a pre-rent metric — including lease liabilities in the numerator while using a pre-rent denominator would double-count the rent obligation.

For capital-intensive companies where operating leases are large (retailers, restaurant chains, airlines, logistics), EV as reported here will differ from "lease-adjusted" EV figures published by some financial data providers. This is disclosed in the UI when operating lease liabilities are detected and are material relative to reported EV.

### GAAP Figures Only
All multiples are computed on reported GAAP figures. No adjustments are made for one-time charges, restructuring costs, impairments, or other non-recurring items. For companies where "adjusted EBITDA" diverges significantly from GAAP EBITDA (common in growth companies, recent spin-offs, and companies undergoing restructuring), the multiples shown here will differ from those published by financial data providers that use adjusted figures. This is disclosed in the UI.

Note also that `DepreciationDepletionAndAmortization` (the D&A tag used in EBITDA) may include amortization of acquired intangibles and, for some filers in certain periods, goodwill impairment charges. This can make EBITDA comparability noisy for companies that have completed large acquisitions or recorded impairments.

### yfinance: Unofficial and Fragile
All price data is sourced through `yfinance`, which is an unofficial scraper of Yahoo Finance's internal API. Yahoo breaks it several times per year, the open-source community patches it. The price service layer includes explicit null checks and surfaces an error to the UI rather than computing a multiple on a `None` price. Finnhub is the identified fallback if Yahoo's reliability degrades during development.

Additionally, ticker formats can differ between EDGAR and Yahoo Finance (e.g., BRK.A in EDGAR vs BRK-A in yfinance). The price service normalizes common format mismatches. Where normalization fails, a `price_unavailable` error is surfaced rather than silently computing on a wrong price.



## Project Structure

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
│   │   │   ├── price.py             yfinance wrapper (swappable interface)
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
- **Endpoints used:**
  - `https://efts.sec.gov/LATEST/search-index?q={query}&dateRange=custom&...` — company name/ticker search
  - `https://data.sec.gov/submissions/{CIK}.json` — company metadata including SIC code
  - `https://data.sec.gov/api/xbrl/companyfacts/{CIK}.json` — full XBRL filing history
- **Rate limit:** 10 requests/second max; EDGAR requires a `User-Agent` header identifying the application and a contact email
- **Authentication:** None required
- **Reliability:** Official SEC data; production-grade uptime

### Yahoo Finance (yfinance)
- **What it provides:** Historical adjusted close prices for filing-date price matching
- **Authentication:** None required
- **Reliability:** Unofficial scraper; see Limitations above



## XBRL Tags Used

The backend extracts the following `us-gaap` taxonomy tags from the companyfacts response. Where multiple tag names are acceptable for the same concept, they are listed in fallback order. The Input Audit Panel in the UI shows which tag fired for each concept in each period.

All monetary facts are validated against their `unitRef` field. If a fact's units are not `USD` (e.g., a custom or non-standard unit), the value is rejected and treated as missing rather than silently used at the wrong scale.

| Concept | Primary Tag | Fallback Tag(s) |
|-|-|-|
| Shares Outstanding | `CommonStockSharesOutstanding` | — |
| Revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` | `Revenues`, `SalesRevenueNet` |
| Net Income | `NetIncomeLoss` | — |
| EBIT | `OperatingIncomeLoss` | — |
| D&A | `DepreciationDepletionAndAmortization` | `DepreciationAndAmortization` |
| EPS (Diluted) | `EarningsPerShareDiluted` | `EarningsPerShareBasic` |
| Total Assets | `Assets` | — |
| Total Liabilities | `Liabilities` | — |
| Stockholders' Equity | `StockholdersEquity` | `StockholdersEquityAttributableToParent` |
| Long-Term Debt | `LongTermDebt` | `LongTermDebtNoncurrent` |
| Short-Term Borrowings | `ShortTermBorrowings` | `ShortTermDebt` |
| Current Portion of LT Debt | `LongTermDebtCurrent` | `LongTermNotesPayableCurrent` |
| Finance Lease Liability (NC) | `FinanceLeaseLiabilityNoncurrent` | — |
| Finance Lease Liability (C) | `FinanceLeaseLiabilityCurrent` | — |
| Cash | `CashAndCashEquivalentsAtCarryingValue` | `CashCashEquivalentsAndShortTermInvestments` |
| Minority Interest | `MinorityInterest` | `NonredeemableNoncontrollingInterest` |
| Preferred Stock | `PreferredStockValue` | `PreferredStockRedeemableValue` |
| Operating Lease Liability (NC) | `OperatingLeaseLiabilityNoncurrent` | — |
| Operating Lease Liability (C) | `OperatingLeaseLiabilityCurrent` | — |

**Finance lease liabilities** are included in EV as financial debt obligations. **Operating lease liabilities** are fetched but excluded from EV, they are surfaced in the UI as a disclosure when material relative to reported EV. See Limitations for the full rationale.

**EPS:** Diluted EPS is used for P/E. If `EarningsPerShareDiluted` is absent, the tool falls back to `EarningsPerShareBasic` and labels the multiple "P/E (basic)".

**Total Debt:** The EV calculation sums all three debt components plus finance lease liabilities. Any individual component that is absent is treated as zero — a missing short-term debt tag almost always means the company has none, not that the tag is mis-labelled. If all financial debt tags (including finance lease) are absent, the EV is flagged in the UI as potentially understated.

**Minority Interest and Preferred Stock:** Included in EV for completeness. Zero for most companies. Material for conglomerates and companies with complex capital structures. Missing tags treated as zero.

**Amended filings:** When both a `10-K` and a `10-K/A` exist for the same period, the original is used unless no original is available. The UI labels the source form type so users can see when an amendment was used.



## Non-Goals

This tool intentionally does **not**:

- Support non-U.S. companies or foreign private issuers
- Provide real-time or intraday price data
- Store user data or require an account
- Provide buy/sell recommendations or sector-normalised comparisons
- Guarantee correctness for financial companies (banks, insurance, REITs)
- Adjust GAAP figures for one-time or non-recurring items
- Include operating lease liabilities in EV (excluded by convention; disclosed in UI when material)

All output is informational. Nothing here is investment advice.



## License

MIT
