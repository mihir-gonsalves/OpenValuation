# OpenValuation: Architecture & Decision Record

This document explains the architectural choices made in `OpenValuation`, the tradeoffs that were consciously accepted, and the reasoning behind decisions that might otherwise look surprising. It is intended for contributors, code reviewers, and my own future reference.



## Guiding Principles

Every significant design decision in this project traces back to one of four principles:

1. **Free over scalable** — every infrastructure decision was filtered through a zero-cost requirement. Where paying would clearly improve the experience, that gap is documented rather than papered over.
2. **Transparent over polished** — when data is missing, say so with a labelled `N/A`. When a number is approximate, say so. Do not produce a confident-looking result backed by a silent assumption.
3. **Swappable at the seam** — any external data provider (price, filings) can be replaced by changing one module, not by refactoring the application.
4. **Computation stays in Python** — all financial logic, derivation, and formula construction lives in the backend. The frontend renders results, it does not compute them.



## Why FastAPI + React Instead of a Monolith

The most obvious alternative was a single Next.js app with API routes. That choice was rejected for the following reasons:

**Python's ecosystem is better for this problem.** `yfinance`, `pandas`, and `openpyxl` are the natural tools for financial data retrieval, tabular manipulation, and Excel generation. Their Node equivalents exist but are less mature, less documented, and less commonly used in the finance domain. Choosing Python for the backend means the hardest part of the project (XBRL extraction, TTM computation, formula-driven Excel generation) is handled by first-class tools.

**The Excel generation made the decision easy.** `openpyxl` can write real Excel formulas — `=B2*C2`, named ranges, formatted cells — directly into `.xlsx` files. The frontend-only alternative (SheetJS) can do this too, but Python's model for constructing a workbook programmatically is significantly cleaner for a structured three-sheet financial document. The moment Excel export became a core feature, the backend needed to be Python.

**The split is clean.** React handles what it is good at (stateful UI, loading states, error display). FastAPI handles what it is good at (async HTTP, data validation, computation). There is no SSR, no hydration, and no shared rendering context to reason about. Each side is independently deployable.



## Caching: Per-CIK In-Memory, Not Per-Request

### Why cache at all?

EDGAR's `companyfacts` endpoint returns a JSON payload that can be 5–10 MB for large companies (Apple, Microsoft). The payload contains every financial fact the company has ever filed across every form type. Fetching this on every request is wasteful and slow.

### Why per-CIK?

The cache is keyed by CIK, not ticker. CIK is EDGAR's stable internal identifier — tickers can change on company renames or relistings, but EDGAR never reassigns CIKs. Keying by CIK is consistent with the rest of the API, which uses CIK as the stable resource identifier after the initial search resolves it.

### Why not Redis?

Redis would be the correct production choice for a cache that persists across process restarts and scales horizontally. But Redis costs money on every managed hosting platform. For a personal project with likely low usage, in-process memory is sufficient.

The implementation is a module-level dict with a TTL check on read:

```python
_cache: dict[str, CacheEntry] = {}
CACHE_TTL_HOURS = 24

def get(cik: str) -> FinancialData | None:
    entry = _cache.get(cik)
    if entry and (datetime.utcnow() - entry.cached_at).total_seconds() < CACHE_TTL_HOURS * 3600:
        return entry.data
    return None

def set(cik: str, data: FinancialData) -> None:
    _cache[cik] = CacheEntry(data=data, cached_at=datetime.utcnow())
```

### The documented tradeoff: cold start cache loss

Render's free tier spins down the backend after 15 minutes of inactivity. Every cold start empties the cache. The first request for each company after a cold start pays the full EDGAR fetch cost (~1–3 seconds). This is a direct consequence of choosing free hosting over paid persistent infrastructure, and it is documented in both the README and the UI.



## Price Service: Swappable Interface Over Concrete Dependency

The price service is defined as an abstract interface with a single method for historical adjusted close:

```python
class PriceService(ABC):
    @abstractmethod
    def get_historical_price(self, ticker: str, after_date: date) -> Decimal | None:
        """Returns adjusted close price for the next trading day after after_date, or None on failure."""

class YahooPriceService(PriceService):
    def get_historical_price(self, ticker: str, after_date: date) -> Decimal | None:
        ticker_obj = yf.Ticker(ticker)
        hist = ticker_obj.history(
            start=after_date + timedelta(days=1),
            end=after_date + timedelta(days=15),  # 14-day window, see below
            auto_adjust=True,
        )
        if hist.empty:
            return None
        first_row = hist.iloc[0]
        # Validate that the returned row is on or after the requested date
        # and that the price is a positive, finite value.
        returned_date = first_row.name.date()
        price = Decimal(str(first_row["Close"]))
        if returned_date < after_date or price <= 0:
            return None
        return price

class FinnhubPriceService(PriceService):
    def get_historical_price(self, ticker: str, after_date: date) -> Decimal | None:
        ...  # fallback implementation
```

**Why the next trading day's adjusted close?**

EDGAR records filing timestamps to the second, typically after market close (4:00 PM ET). Using the filing day's own close price would mean the market had not yet seen the information. The next trading day's close is the first price where the market has had a full session to incorporate the disclosed data — which is the standard approach in academic and professional valuation practice.

Note: filings submitted before market open (before 9:30 AM ET) could technically be priced on the same trading day. This edge case is not handled, the tool always uses the next trading day. The impact is minimal and is documented here rather than implemented.

**Why adjusted close?**

Stock splits distort raw close prices across time. A raw P/E comparing a 2019 10-K price to a post-split 2024 price would be meaningless without adjustment. `yfinance`'s `history()` with `auto_adjust=True` (the default) returns split-adjusted close in the `Close` column.

**Why a 14-day window instead of 7?**

The US market can be closed for 4–5 consecutive calendar days around Christmas–New Year. A 7-day window starting December 26 could return zero trading days, triggering a confusing `price_unavailable` error for a filing submitted in late December. 14 days guarantees at least 4 trading days in the window regardless of holiday placement. The cost is a marginally wider `yfinance` fetch, this is negligible.

**Ticker format normalization.**

EDGAR tickers and Yahoo Finance tickers do not always share the same format. The most common divergence: EDGAR stores `BRK.A` and `BRK.B`, while yfinance expects `BRK-A` and `BRK-B`. The price service normalizes `.` to `-` in ticker strings before querying yfinance. Where normalization fails (no price returned after the attempt), a `price_unavailable` error is surfaced rather than a silent wrong value.

**Shares outstanding** are extracted from the filing's own XBRL data (`CommonStockSharesOutstanding`) rather than from the price service. This ensures the share count used in market cap calculations matches the exact period being valued.



## XBRL Extraction: Fallback Tag Logic

EDGAR's XBRL data uses the `us-gaap` taxonomy, which is mostly standardized. But companies have some latitude. Revenue, for example, is reported under at least three different tag names depending on the company's industry and the era in which it adopted XBRL:

- `RevenueFromContractWithCustomerExcludingAssessedTax` (post-ASC 606, most common)
- `Revenues` (older and/or simpler filers)
- `SalesRevenueNet` (legacy)

The extractor tries tags in priority order and uses the first match:

```python
REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
]

def extract_revenue(facts: dict) -> Decimal | None:
    for tag in REVENUE_TAGS:
        value = _extract_tag(facts, tag)
        if value is not None:
            return value
    return None
```

**EPS: diluted over basic.** P/E uses `EarningsPerShareDiluted`. Diluted EPS accounts for all potentially dilutive instruments (stock options, warrants, convertibles) and is the standard figure used by professional analysts and financial databases. Ignoring dilution produces an artificially low P/E for companies with heavy option programs — common in technology. If `EarningsPerShareDiluted` is absent, the tool falls back to `EarningsPerShareBasic` and labels the multiple "P/E (basic)" to avoid ambiguity. Using diluted EPS for P/E while using basic shares for market cap is standard practice — each is used in its conventional context.

**When no tag matches:** the corresponding field is returned as `None`. The API marks it as `null`, the UI renders it as `N/A` with a tooltip.

**Why not silently skip the multiple?** A blank could be mistaken for zero or an error. A labelled `N/A` is unambiguous — the data was not there, the tool did not guess.

**N/A vs. negative multiples — a critical distinction.** `N/A` means the required input data was not found in the filing. A *negative* multiple (e.g., negative P/E for a company reporting a net loss; negative EV for a cash-heavy company where Cash > Market Cap + Debt) means the data was found but the result is negative. These are mathematically valid results and must be displayed as negative numbers with an explanatory note — not suppressed as `N/A`. Conflating "data missing" with "result is negative" would be a silent wrong value of the worst kind.



## XBRL Extraction: Unit Normalization

EDGAR's companyfacts JSON includes a `unitRef` field on every monetary fact. All standard `us-gaap` monetary facts should carry `unitRef: "USD"`. In practice, the vast majority do. However, some older or non-standard filers have used custom units (e.g., `USD_thousands`). Silently treating a value reported in thousands as if it were in dollars produces a 1,000× error in every downstream calculation — the sort of error that passes all type checks and produces a plausible-looking but wrong number.

The extraction layer validates `unitRef` on every monetary fact before accepting it:

```python
def _extract_tag(facts: dict, tag: str, required_unit: str = "USD") -> Decimal | None:
    entries = facts.get("us-gaap", {}).get(tag, {}).get("units", {}).get(required_unit, [])
    if not entries:
        return None
    # Select the correct entry by period, unit is implicitly validated
    # by only looking in the required_unit bucket.
    ...
```

By looking only inside the `USD` bucket of the units dict, non-USD facts are implicitly rejected. If a tag has no `USD` entries at all, it is treated as missing. This is the correct behavior: it is better to surface `N/A` than to silently consume a value at the wrong scale.



## XBRL Extraction: Duplicate and Ambiguous Facts

EDGAR's XBRL data frequently includes multiple entries for the same tag and the same period, differing in their **context**: the entity scope (consolidated vs. segment vs. subsidiary), the scenario (restated vs. as-filed), or both. Picking arbitrarily among these can produce the wrong value without any exception.

The extraction layer applies a deterministic selection rule when multiple entries exist for the same tag and period:

1. **Prefer consolidated entity context** — entries with no segment dimension (i.e., the whole-company figure) are preferred over segment-level entries.
2. **Prefer original over restated** — when both an original and a restated figure are present for the same period, prefer the original. The UI labels when a restated figure was used (because no original was available).
3. **If ambiguity remains after these rules** — return `None` for that field and set `warning: "ambiguous_fact"` on the period. Do not guess.

This rule is written down, tested, and deterministic. It does not vary between runs or between companies.



## XBRL Extraction: Period Coherence

EDGAR XBRL facts are tagged with `start` and `end` dates (for duration facts like income statement items) or an `instant` date (for point-in-time facts like balance sheet items). For a given filing period, balance sheet facts and income statement facts will, in general, have different period representations — this is expected and not an error.

What can go wrong: income statement facts for nominally the same period tagged to slightly different date ranges (e.g., Revenue ends December 31 but D&A ends January 2). This happens. Using misaligned facts to compute EBITDA or EV introduces silent inconsistency.

**Coherence rule for income statement items (duration facts):** for a given period, all income statement facts must share the same `end` date. Facts whose `end` date differs from the primary income statement period end by more than 3 days are rejected and treated as missing for that field.

**Coherence rule for balance sheet items (instant facts):** all balance sheet facts must share an `instant` date matching the filing's `period_of_report`. Facts tagged to a different date are rejected.

When a coherence rejection occurs, the affected field is returned as `None` with `warning: "period_mismatch"` on the period object.



## Multiple Calculation: Pure Functions

All multiple computations live in `services/multiples.py` as pure functions that take `ExtractedFinancials` and a price value and return `MultipleSet`. They have no side effects, no network access, and no dependencies other than Python's `decimal` module.

```python
def compute_multiples(financials: ExtractedFinancials, price: Decimal) -> MultipleSet:
    if price is None:
        return MultipleSet.all_unavailable(reason="price_unavailable")
    market_cap = price * financials.shares_outstanding
    enterprise_value = (
        market_cap
        + (financials.long_term_debt or Decimal(0))
        + (financials.short_term_borrowings or Decimal(0))
        + (financials.long_term_debt_current or Decimal(0))
        + (financials.finance_lease_liability_noncurrent or Decimal(0))
        + (financials.finance_lease_liability_current or Decimal(0))
        + (financials.minority_interest or Decimal(0))
        + (financials.preferred_stock or Decimal(0))
        - (financials.cash or Decimal(0))
    )
    ...
```

**Why pure functions?** They are trivially testable. A test for P/E calculation does not need a running server, a real ticker, or a mocked HTTP client. It needs two objects and an assertion. This is also where the most financially sensitive logic lives, so it deserves the most test coverage.

**Decimal over float:** All financial arithmetic uses Python's `Decimal` type, not `float`. Floating-point representation errors in financial calculations are a well-documented class of production bug. `Decimal` arithmetic is exact for the precision levels this project requires.

**`or Decimal(0)` vs `if field is None`:** The EV formula uses explicit `None` checks rather than Python's `or` idiom for numeric fields. `financials.long_term_debt or Decimal(0)` would incorrectly coerce a legitimately reported `Decimal(0)` to `Decimal(0)` — which happens to produce the right result, but relies on coincidence. The safer form is `financials.long_term_debt if financials.long_term_debt is not None else Decimal(0)`. This is enforced throughout `multiples.py`.



## Enterprise Value: The Complete Formula

The EV formula used by this tool is:

**EV = Market Cap + Long-Term Debt + Short-Term Borrowings + Current Portion of Long-Term Debt + Finance Lease Liabilities (Noncurrent + Current) + Minority Interest + Preferred Stock − Cash**

### Why short-term debt?

The naive EV formula uses only long-term debt. This silently understates EV for any company carrying revolving credit, commercial paper, or near-term maturities on the short side of the balance sheet. Airlines, retailers, and leveraged companies routinely carry hundreds of millions in short-term borrowings that would be invisible in a long-term-only calculation.

Missing short-term debt tags are treated as zero (not `N/A`), because the absence of a short-term borrowings entry in a filing almost always means the company has none — unlike revenue or EPS, where a missing tag is a genuine labelling ambiguity. If all financial debt tags are absent, the EV is flagged in the UI as potentially understated.

### Why finance lease liabilities?

Post-ASC 842 (effective 2019), finance leases appear on the balance sheet as `FinanceLeaseLiabilityNoncurrent` and `FinanceLeaseLiabilityCurrent`. Finance leases are contractual obligations to make fixed future payments — economically indistinguishable from term debt. They are included in EV on the same basis as long-term debt.

### Why operating lease liabilities are excluded

Operating lease liabilities (`OperatingLeaseLiabilityNoncurrent`, `OperatingLeaseLiabilityCurrent`) also appear on the balance sheet post-ASC 842. They are excluded from EV by convention: the EBITDA denominator in EV/EBITDA is a pre-rent metric (operating income before depreciation, which does not include the rent expense embedded in operating lease cost). Including operating lease liabilities in the EV numerator while using a pre-rent EBITDA denominator would overstate the effective multiple by double-counting the rent obligation.

Operating lease liabilities are fetched from EDGAR so that a disclosure can be surfaced in the UI when they are large relative to reported EV — alerting users that "lease-adjusted" EV would be meaningfully higher for this company.

### Why minority interest and preferred stock?

EV is meant to represent the total cost to acquire the entire enterprise — all capital providers, not just common equity holders. Minority interest (noncontrolling interests in consolidated subsidiaries) and preferred stock are claims on the enterprise's cash flows that rank ahead of common equity. For most companies they are zero, for conglomerates and companies with complex capital structures they can be material. Including them when nonzero, and treating them as zero when absent, is the academically correct approach and requires no meaningful extra complexity.



## TTM Calculation for Quarterly Multiples

Annual (10-K) income statement figures represent a full twelve months and can be used directly. Quarterly (10-Q) figures require special handling: a single quarter's revenue divided into an enterprise value produces a multiple roughly four times higher than its annual equivalent, making cross-period comparison meaningless.

**Decision: trailing twelve months (TTM) for all income statement items in quarterly periods.**

TTM is computed using the LTM bridge:

```
TTM = Most Recent Annual (10-K) + YTD Current Year − YTD Prior Year (same stub period)
```

For example, if the most recent 10-Q covers the nine months ended September 30, 2024, and the most recent 10-K covers the year ended December 31, 2023:

```
TTM Revenue = FY2023 Revenue + 9M 2024 Revenue − 9M 2023 Revenue
```

This is the standard method used by financial databases (FactSet, Capital IQ) and valuation textbooks. The EDGAR `companyfacts` XBRL data includes `start` and `end` dates for every fact entry, which allows the extractor to identify YTD cumulative figures (e.g., start = January 1, duration ~273 days for nine months) vs. single-quarter figures (duration ~91 days). The TTM bridge uses YTD figures — it does not attempt to sum individual quarters.

**Fallback:** If the prior-year YTD figure is unavailable (recently public company, recent fiscal year change), TTM falls back to single-quarter 4x annualisation, labelled "Annualised" in the UI rather than "TTM" so users are not misled about the methodology.

**Balance sheet items** (Debt, Cash, Equity, Minority Interest, Preferred Stock) are point-in-time figures as of the filing period end date. No annualisation is applied.



## EBITDA: Definition and Limitations

EBITDA is computed as EBIT + D&A, where:
- **EBIT** = `OperatingIncomeLoss`
- **D&A** = `DepreciationDepletionAndAmortization` (primary) or `DepreciationAndAmortization` (fallback)

This is labelled "GAAP-derived EBITDA" in the UI. It is not an adjusted or normalized figure.

**Known limitations of the D&A tag:**

The `DepreciationDepletionAndAmortization` tag may include amortization of acquired intangible assets (customer lists, patents, trade names from acquisitions). For companies that have completed large acquisitions, this line can be significantly elevated by intangible amortization that is not present for organic peers — making EBITDA non-comparable across companies with different M&A histories.

In some periods, filers have also included goodwill impairment charges within this tag (though goodwill impairment more commonly appears in `GoodwillImpairmentLoss` separately). If a company records a large impairment, D&A and therefore EBITDA may be distorted for that period.

No adjustment is made for either of these items. The Raw Financials sheet of the Excel export exposes the raw D&A value alongside the XBRL tag name used, allowing users to investigate and adjust as they see fit.



## Warnings Array: Structured Non-Blocking Flags

Not every data quality issue warrants blocking a result. Some issues (fallback tag used, TTM annualised rather than LTM, finance lease liabilities absent) are worth disclosing but do not invalidate the computed multiple. Others (ambiguous fact, period mismatch) are more serious but still produce a computable result in most cases.

The API response includes a top-level `warnings` array on each period object:

```json
{
  "period": "Q3 2024 TTM",
  "multiples": { ... },
  "warnings": [
    { "code": "fallback_eps_basic", "message": "EarningsPerShareDiluted absent, P/E uses basic EPS." },
    { "code": "ttm_annualised", "message": "Prior-year YTD unavailable, income statement items annualised from single quarter." },
    { "code": "operating_lease_material", "message": "Operating lease liabilities ($4.2B) are material and excluded from EV by convention." }
  ]
}
```

Defined warning codes:

| Code | Meaning |
|-|-|
| `fallback_eps_basic` | `EarningsPerShareDiluted` absent; P/E uses basic EPS |
| `ttm_annualised` | Prior-year YTD absent; income statement annualised from single quarter |
| `ev_debt_missing` | All financial debt tags absent; EV may be understated |
| `ambiguous_fact` | Multiple contexts found for a tag; deterministic rule applied |
| `period_mismatch` | A fact's period dates diverge from the primary period end by >3 days; fact rejected |
| `operating_lease_material` | Operating lease liabilities exceed 20% of reported EV |
| `amended_filing_used` | No original form found; amendment used as source |

The frontend renders warnings as inline notes beneath the relevant period column, not as blocking error states.



## Input Audit Panel

The Input Audit Panel is a collapsible section rendered below the multiples table. It shows, for each filing period, exactly which XBRL tag was resolved for each financial concept and whether a fallback fired.

Example output:

```
FY2023 10-K
  Revenue:           RevenueFromContractWithCustomerExcludingAssessedTax
  EBIT:              OperatingIncomeLoss
  D&A:               DepreciationAndAmortization  !! fallback (primary tag absent)
  EPS:               EarningsPerShareDiluted
  LT Debt:           LongTermDebt
  ST Borrowings:     — (absent, treated as zero)
  Finance Leases:    FinanceLeaseLiabilityNoncurrent + FinanceLeaseLiabilityCurrent
  Cash:              CashAndCashEquivalentsAtCarryingValue
```

**Why this matters:** It makes every number in the table traceable to a specific EDGAR tag without requiring the user to download and inspect the Excel file. For a recruiter or analyst reviewing the tool, seeing the extraction logic surface transparently signals that the numbers were not produced by guesswork.

The same information is written to the Raw Financials sheet of the Excel export (tag name and fallback flag columns alongside each value).



## Search: Disambiguation Strategy

EDGAR's company search endpoint returns multiple registrants for ambiguous queries. Searching "Ford" returns Ford Motor Company, Ford Motor Credit, and several Ford-branded asset-backed trusts. Silent resolution to the wrong entity is the worst possible failure for a financial data tool — it produces confidently wrong numbers with no error surfaced.

Resolution strategy:

1. If the query matches the format of a ticker (1–5 characters, no spaces), attempt an exact ticker match. If exactly one result is returned, resolve automatically.
2. If the query is a company name, or a ticker query produces multiple matches, return up to five candidates with name, ticker, and SIC description. The frontend presents these as a short disambiguation list for the user to confirm before financials are fetched.
3. If any query type produces exactly one result, resolve automatically.

This is the minimum UI complexity required to prevent identity errors.



## SIC Code Display

EDGAR's `/submissions/{CIK}.json` endpoint returns the company's Standard Industrial Classification (SIC) code and description (e.g., "7372 — Prepackaged Software"). This is fetched once during the company lookup, stored on `CompanyResult`, and displayed in the UI alongside the company name and ticker.

This is worth the one extra EDGAR call for two reasons. First, it provides immediate sector context — a P/E of 25 reads differently for a utility than for a growth software company. The tool cannot reason about that context for the user, but it can show the information. Second, it makes the financial-company warning specific: instead of a generic "financials not supported," the UI can say "SIC 6022 (State commercial banks) — this sector uses different balance sheet structures that are not currently supported."



## Data Timestamp

The `cached_at` timestamp stored on each `CacheEntry` is returned in the API response and displayed in the UI as "Data as of [timestamp]." This tells users whether they are looking at a fresh fetch or a 23-hour-old cached response, which is relevant when a company has recently filed. The same timestamp is written to the Summary sheet of the Excel export.



## Excel Generation: Formulas Over Values

The most common approach for "export to Excel" features is to write computed values into cells — the same numbers already shown in the UI, just in a spreadsheet. This project takes a different approach: the Calculations sheet contains live Excel formulas that reference the Raw Financials sheet.

For example, the P/E formula cell is not `=15.32`. It is `=Price/EPS`, where `Price` and `EPS` are named references to cells in the Raw Financials sheet. A user who disagrees with the reported EPS can change it in Raw Financials and the P/E in the Calculations sheet updates automatically.

**Why this is worth the extra complexity:**

The goal of the download feature is to let users play around with different values and make adjustments they see fit. Static values do not serve that goal — they just reproduce the UI in a different format. Formulas make the workbook a tool, not a screenshot.

**How openpyxl supports this:**

```python
ws_calc["B2"] = "=RawFinancials!B5/RawFinancials!B12"  # P/E
ws_calc["B2"].number_format = "0.00x"
```

Named ranges are defined in the workbook to make formulas readable:

```python
wb.defined_names["Price"] = DefinedName("Price", attr_text="RawFinancials!$B$3")
wb.defined_names["EPS"] = DefinedName("EPS", attr_text="RawFinancials!$B$12")
```

The Raw Financials sheet includes a "Tag Used" column and a "Fallback?" flag column alongside each value, mirroring the Input Audit Panel in the UI.



## API Design: CIK as the Stable Identifier

The public-facing routes use CIK, not ticker, as the resource identifier after the initial lookup:

```
POST /api/search       { query: "Apple" }  -> { cik: "0000320193", name: "Apple Inc.", ticker: "AAPL", sic: "3571", sic_description: "Electronic Computers" }
GET  /api/financials/0000320193            -> { multiples: [...], filings: [...], cached_at: "...", warnings: [...] }
GET  /api/export/0000320193               -> .xlsx binary
```

**Why CIK and not ticker?**

EDGAR's canonical identifier is CIK. Tickers change (company renames, relistings), but EDGAR never reassigns CIKs. The lookup step resolves the user's human input to a CIK exactly once. Every subsequent operation uses the stable identifier. The cache is keyed by CIK for the same reason.



## Why No Database

A database would solve the cache persistence problem (survives cold starts). It was rejected because:

1. Managed PostgreSQL on any free tier (Supabase, Neon, Railway) introduces another service dependency that can break, change its free tier terms, or add latency.
2. The data being cached (EDGAR XBRL facts) is public and freely re-fetchable. Losing the cache costs a few seconds, not data integrity.
3. A database implies schema migrations, connection management, and ORM configuration — overhead not justified for a project at this scale.

If this project grows to the point where cold-start cache loss becomes a real user experience problem, the right solution is paying for a Render paid tier (persistent process, no spin-down), not adding a database.



## Testing Strategy

### Backend: Fixture-Based EDGAR Tests

The EDGAR response for any given company is deterministic at a point in time. Tests do not call the live EDGAR API. Instead:

1. The full `companyfacts.json` response for representative companies (Apple, Microsoft, a small-cap) is captured once and stored in `tests/fixtures/`.
2. The EDGAR service's extraction functions accept the parsed JSON dict directly. Tests call these functions with fixture data — no HTTP mocking required for extraction tests.
3. HTTP-level tests (that verify the client sets the correct `User-Agent`, handles 404, handles 429 with retry) use `httpx`'s built-in mock transport.

**Why fixture-based rather than mocked HTTP?** Mocking HTTP responses validates that the code calls the API correctly. Fixture-based tests validate that the code handles real API responses correctly. These are different things. The fixtures have caught real extraction bugs that HTTP mocking would have missed.

### Backend: TTM Calculation Tests

The TTM bridge logic is separately unit-tested with constructed `ExtractedFinancials` objects covering the following cases:
- Normal case: all three components (annual, current YTD, prior YTD) present
- Fallback case: prior YTD absent -> annualisation, correctly labelled
- Edge case: fiscal year change mid-period (YTD start date does not fall on January 1)

### Backend: Pure Calculation Tests

All multiple computations are pure functions. Tests construct `ExtractedFinancials` and a `Decimal` price directly and assert on the returned `MultipleSet`. No fixtures, no mocking, no server.

Edge cases covered:
- Negative EPS -> negative P/E (displayed, not suppressed)
- Negative EV (cash > market cap + debt) -> displayed with note
- Zero denominator (zero revenue) -> `N/A`
- Zero or near-zero EBITDA -> `N/A` with `denominator_near_zero` flag
- All debt tags absent -> EV flagged as potentially understated
- Missing minority interest / preferred -> treated as zero, no flag
- Finance lease liabilities present -> included in EV correctly
- Finance lease liabilities absent -> treated as zero, no separate flag (same as other debt components)

### Backend: Extraction Integrity Tests

Unit normalization, duplicate fact selection, and period coherence are tested separately from the multiples layer:
- Non-USD `unitRef` -> fact rejected, field returns `None`
- Multiple contexts for same tag/period -> deterministic rule applied; ambiguous case returns `None` with warning
- Period mismatch (>3 day divergence) -> fact rejected, warning set
- Legitimately reported `Decimal(0)` debt -> preserved as zero, not treated as missing

### Backend: Price Service Tests

The `PriceService` interface is the test boundary. Tests inject a `MockPriceService` that returns deterministic `Decimal` values. The `yfinance` wrapper is tested in isolation with a small set of tests that mock the `yfinance` library itself — these tests verify:
- Correct mapping from `yfinance`'s output schema to a `Decimal` price
- That returned date ≥ requested date; otherwise `None` returned
- That price > 0; otherwise `None` returned
- That ticker format normalization (`.` -> `-`) fires before the yfinance call

### Frontend: Not Prioritized

The frontend is thin: it renders API responses. For a personal portfolio project at this scale, unit tests on pure utilities (formatters, number display) and manual verification are sufficient. This is a deliberate tradeoff, not a gap left by accident.



## Error Handling Philosophy

**Surface the truth, don't hide it.**

- If an XBRL tag is missing: return `null` for that field, display `N/A` in the UI with a tooltip explaining the tag was not found.
- If a value is present but the multiple is negative (net loss -> negative P/E, net-cash company -> negative EV): display the negative number with a note. `N/A` means "data not found." Negative means "data found, result is negative." These are categorically different.
- If `yfinance` returns empty data: return a structured error (`{ error: "price_unavailable", message: "..." }`), display all market-cap-dependent multiples as unavailable rather than computing on a `None` price.
- If a company files on IFRS rather than US GAAP: return a structured error explaining that IFRS filers are not supported, rather than silently returning wrong or empty results.
- If EDGAR rate-limits the request (HTTP 429): retry once with exponential backoff, surface a `503` to the client if it fails again.
- If a search query is ambiguous: return multiple candidates, never silently resolve to the wrong entity.
- If all debt tags are absent from a filing: compute EV with debt = 0, but flag the result as potentially understated. This is different from a price failure — the computation is valid, it is the completeness of the input that is uncertain.
- If a fact's `unitRef` is not `USD`: reject the fact and treat the field as missing. A 1,000× scale error is worse than an `N/A`.
- If multiple facts exist for the same tag and period with different contexts: apply the deterministic selection rule. If ambiguity remains, return `None` with `warning: "ambiguous_fact"`.
- If a fact's period dates diverge from the primary period end by more than 3 days: reject the fact and return `None` with `warning: "period_mismatch"`.

The rule is: **a wrong answer is worse than no answer**. Every place where a silent bad value could propagate to the output is either a hard assertion, an explicit `None` check that produces a labelled `N/A`, or a structured warning that informs the user of the specific uncertainty.



## What Was Intentionally Left Out

### User Accounts and Saved Results
No authentication, no persistence of user sessions or saved comparisons. The tool is stateless by design.

### Sector-Normalised Multiples
A P/E of 30 means something different for a software company than for a utility. The tool does not compare a company's multiples to its sector average. That would require a reference dataset of peer companies and sector classifications — a meaningful additional data problem. It is a natural future feature but out of scope for v1.

### Adjusted / Non-GAAP Figures
All multiples are computed on reported GAAP figures. Adjusted EBITDA (stripping one-time charges, restructuring, stock-based compensation) is what many practitioners prefer, but the adjustments are company-specific, discretionary, and not available from EDGAR's XBRL data. GAAP figures are disclosed in the UI.

### Operating Lease Liabilities in EV
Operating lease liabilities are excluded from EV by convention (see Enterprise Value section). They are fetched and disclosed when material, but not included in the EV calculation. A future version could expose an optional "lease-adjusted EV" toggle.

### Support for Non-U.S. Companies
EDGAR only covers SEC-registered filers. Foreign private issuers file on different forms (20-F, 40-F) and often use IFRS rather than US GAAP. Supporting them would require IFRS tag mapping and different form-type filtering. Scoped out of v1.
