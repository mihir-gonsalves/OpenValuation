# PHASE_1_SPEC.md - OpenValuation Core Backend Infrastructure

**Phase:** 1  
**Status:** Complete  
**Implemented in:** `backend/`

This document is the authoritative contract for the Phase 1 backend. It defines
exact API schemas, the error/warning model, timeout rules, and cache behaviour.
Phases 2-4 must not deviate from the contracts defined here.



## 1. API Contracts

### Base URL

| Environment | Base URL                             |
|-------------|--------------------------------------|
| Local       | `http://localhost:8000`              |
| Production  | `N/A`                                |

All endpoints are prefixed with `/api`.  
All request and response bodies are `application/json` unless noted.



### 1.1 `POST /api/search`

Resolves a company name or ticker to up to five CIK candidates.

**Design invariant:** Zero external network calls. Operates exclusively on the
in-memory company index. Deterministic, sub-millisecond latency.

#### Request

```json
{
  "query": "Apple"
}
```

| Field   | Type   | Required | Constraints           |
|---------|--------|----------|-----------------------|
| `query` | string | yes      | 1-200 chars, stripped |

#### Response `200 OK`

```json
{
  "results": [
    {
      "cik_10":  "0000320193",
      "name":    "Apple Inc.",
      "ticker":  "AAPL"
    }
  ]
}
```

| Field              | Type   | Notes                                         |
|--------------------|--------|-----------------------------------------------|
| `results`          | array  | 0-5 elements, sorted by match score (desc)    |
| `results[].cik_10` | string | 10-digit zero-padded CIK, e.g. `"0000320193"` |
| `results[].name`   | string | Registrant name as filed with the SEC         |
| `results[].ticker` | string | Upper-cased exchange ticker                   |

**SIC and exchange are not returned by search.** They are retrieved only after
the user selects a company, via `GET /api/financials/{cik_10}`.

#### Search algorithm (scored, deterministic)

| Priority | Condition                             | Score |
|----------|---------------------------------------|-------|
| 1        | Exact ticker match (case-insensitive) | 100   |
| 2        | Name exact match (normalised)         | 90    |
| 3        | Name prefix match                     | 70    |
| 4        | Name substring match                  | 50    |
| —        | No match                              | 0     |

Results are sorted by score (descending), then alphabetically by name for
stability. Deduplicated by CIK (same CIK appears at most once). Capped at 5.

#### Error responses

| Condition           | HTTP  | `error` code            |
|---------------------|-------|-------------------------|
| `query` empty       | `422` | _(Pydantic validation)_ |
| `query` > 200 chars | `422` | _(Pydantic validation)_ |



### 1.2 `GET /api/financials/{cik_10}`

Returns TTM valuation multiples for a company identified by CIK.

**Phase 1:** `periods` is always an empty array (extraction is Phase 2).  
**Phase 2+:** `periods` contains up to 12 TTM periods.

#### Path parameter

| Parameter | Type   | Format                             | Example      |
|-----------|--------|------------------------------------|--------------|
| `cik_10`  | string | 10 digits, zero-padded, `^\d{10}$` | `0000320193` |


#### Response `200 OK`

```json
{
  "company": {
    "cik_10":               "0000320193",
    "name":                 "Apple Inc.",
    "ticker":               "AAPL",
    "sic":                  "3571",
    "sic_description":      "Electronic Computers",
    "exchange":             "Nasdaq",
    "is_financial":         false,
    "is_capital_intensive": false
  },
  "periods":    [],
  "cached_at":  "2026-05-07T12:00:00Z",
  "data_as_of": "2026-05-07T12:01:34Z"
}
```

**`company` object**

| Field                  | Type           | Source           | Notes                                |
|------------------------|----------------|------------------|--------------------------------------|
| `cik_10`               | string         | Path parameter   | 10-digit zero-padded                 |
| `name`                 | string         | EDGAR submissions| Registrant name                      |
| `ticker`               | string \| null | EDGAR submissions| Primary ticker, null if none         |
| `sic`                  | string \| null | EDGAR submissions| 4-digit SIC code as string           |
| `sic_description`      | string \| null | EDGAR submissions| Human-readable SIC label             |
| `exchange`             | string \| null | EDGAR submissions| Primary listing exchange             |
| `is_financial`         | bool           | Derived from SIC | True if SIC 6000-6999                |
| `is_capital_intensive` | bool           | Derived from SIC | True if SIC 2000-3999 or 4000-4999   |

**`periods` array** (Phase 2+, empty in Phase 1)

Each element is a `TTMPeriod`:

```json
{
  "period_end":   "2024-09-28",
  "filing_date":  "2024-11-01",
  "price":        "222.9100",
  "multiples": {
    "pe":         { "value": "33.12", "label": "P/E",        "warnings": [] },
    "ev_ebitda":  { "value": "21.40", "label": "EV/EBITDA",  "warnings": [] },
    "ev_ebit":    { "value": "25.11", "label": "EV/EBIT",    "warnings": [] },
    "ev_revenue": { "value": "7.81",  "label": "EV/Revenue", "warnings": [] },
    "ps":         { "value": "8.54",  "label": "P/S",        "warnings": [] },
    "pb":         { "value": "45.22", "label": "P/B",        "warnings": [] },
    "pfcf":       { "value": "26.33", "label": "P/FCF",      "warnings": [] }
  },
  "ev_components": {
    "market_cap":              "3373000000000",
    "long_term_debt":          "85750000000",
    "short_term_borrowings":   "9958000000",
    "current_portion_lt_debt": "10912000000",
    "finance_lease_current":   "0",
    "finance_lease_noncurrent":"0",
    "minority_interest":       "0",
    "preferred_stock":         "0",
    "cash":                    "29965000000",
    "enterprise_value":        "3449655000000"
  },
  "extracted": { "...": "see ExtractedFinancials schema" },
  "warnings": []
}
```

**`multiple` value object**

| Field      | Type              | Notes                                                            |
|------------|-------------------|------------------------------------------------------------------|
| `value`    | string \| null    | Decimal string (e.g. `"33.12"`). `null` means N/A.               |
| `label`    | string            | Display label. Changes when fallback fires (e.g. `P/E (basic)`). |
| `warnings` | array of Warning  | Per-multiple warnings for this period.                           |

`null` means the required data was missing or a guard condition triggered.
It is categorically distinct from zero or a valid negative result.

**`TTMPeriod.warnings` contract**

`warnings` is the **union** of extraction warnings (from Phase 2) and multiples
warnings (from Phase 3). It must never be a subset of either source alone.

Concretely, after `compute_all` runs in Phase 3, all per-`MultipleValue.warnings`
lists must be collected and merged with `ef.warnings` before constructing the
`TTMPeriod`. See §9.2 for the required merge pattern.

**`data_as_of` vs `cached_at`**

| Field        | Meaning                                              |
|--------------|------------------------------------------------------|
| `cached_at`  | When EDGAR data was last fetched (cache write time)  |
| `data_as_of` | When this response was generated (computation time)  |

#### Error responses

| Condition               | HTTP  | `error` code       | `message`                                       |
|-------------------------|-------|--------------------|-------------------------------------------------|
| Invalid CIK format      | `422` | `invalid_cik`      | "'xxxxx' is not a valid CIK..."                 |
| CIK not on EDGAR        | `404` | `edgar_not_found`  | "Company not found on EDGAR. Verify the CIK."   |
| EDGAR timeout           | `503` | `edgar_timeout`    | "EDGAR is taking longer than usual..."          |
| EDGAR rate limit        | `503` | `edgar_rate_limit` | "EDGAR is currently rate-limiting..."           |
| IFRS filer              | `422` | `ifrs_filer`       | "This company files under IFRS taxonomy..."     |
| Unexpected server error | `500` | `internal_error`   | "Unexpected server error."                      |



### 1.3 `GET /api/export/{cik_10}`

Returns a `.xlsx` workbook as a binary stream.

**Phase 1:** returns `501 Not Implemented`.  
**Phase 4:** streams a three-sheet workbook (Summary, Raw Financials, Calculations).

#### Path parameter

Same format and validation as `GET /api/financials/{cik_10}` - regex `^\d{10}$` only.

#### Response `200 OK` (Phase 4)

```
Content-Type:        application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
Content-Disposition: attachment; filename="openvaluation_{cik_10}.xlsx"
Body:                binary .xlsx bytes
```

#### Error responses

| Condition           | HTTP  | `error` code      | Notes                                 |
|---------------------|-------|-------------------|---------------------------------------|
| Not yet implemented | `501` | `not_implemented` | Phase 1. Returns until Phase 4 ships. |
| Invalid CIK format  | `422` | `invalid_cik`     | Same validation as `/financials`      |



### 1.4 `GET /health`

Internal health check and cache statistics. Used by Render's health check
and for observability.

#### Response `200 OK`

```json
{
  "status":              "ok",
  "version":             "0.1.0",
  "cache": {
    "total_entries":     4,
    "live_entries":      3,
    "expired_entries":   1
  },
  "company_index_size":  10823
}
```



## 2. Error Model Schema

### 2.1 API error response body

Returned alongside the appropriate HTTP status code for all 4xx/5xx responses.

```json
{
  "error":   "edgar_timeout",
  "message": "EDGAR is taking longer than usual. Please try again in a moment."
}
```

| Field     | Type   | Values                                    |
|-----------|--------|-------------------------------------------|
| `error`   | string | One of the `ErrorCode` enum values below  |
| `message` | string | Human-readable, suitable for UI display   |

### 2.2 Error codes (`ErrorCode` enum)

| Code               | HTTP | Trigger                                               |
|--------------------|------|-------------------------------------------------------|
| `edgar_timeout`    | 503  | EDGAR did not respond within 15 seconds               |
| `edgar_rate_limit` | 503  | HTTP 429 on both initial request and single retry     |
| `edgar_not_found`  | 404  | EDGAR returned HTTP 404 for the given CIK             |
| `ifrs_filer`       | 422  | companyfacts contains `ifrs-full` but not `us-gaap`   |
| `invalid_cik`      | 422  | CIK does not match `^\d{10}$`                         |
| `internal_error`   | 500  | Unhandled exception (see server logs)                 |

### 2.3 Warning object (per-period, non-fatal)

Warnings are attached to individual TTM periods and never suppress a result.
They appear in the API response, in the UI inline with the multiples table,
and in the Excel Summary sheet.

```json
{
  "code":    "fallback_eps_basic",
  "message": "Diluted EPS unavailable. Using basic EPS. P/E labeled 'P/E (basic)'."
}
```

| Field     | Type   | Description                               |
|-----------|--------|-------------------------------------------|
| `code`    | string | Machine-readable `WarningCode` enum value |
| `message` | string | Human-readable explanation for UI display |

### 2.4 Warning codes (`WarningCode` enum)

#### Fallback tags

| Code                 | Trigger                                                        |
|----------------------|----------------------------------------------------------------|
| `fallback_revenue`   | Primary revenue tag absent, fallback used                      |
| `fallback_eps_basic` | Diluted EPS absent, basic EPS used. P/E label -> `P/E (basic)` |

#### EV construction

| Code                                  | Trigger                                                    |
|---------------------------------------|------------------------------------------------------------|
| `ev_debt_missing`                     | All financial debt and finance lease tags absent           |
| `debt_deduplicated`                   | `LongTermDebt` treated as total debt, not added separately |
| `cash_fallback_includes_investments`  | Fallback cash tag includes short-term investments          |
| `capex_sign_normalized`               | CapEx was negative, absolute value taken                   |

#### Finance leases

| Code                                      | Trigger                                               |
|-------------------------------------------|-------------------------------------------------------|
| `lease_pre_asc842`                        | Pre-ASC 842 capital lease fallback tags used          |
| `finance_lease_missing_capital_intensive` | Lease tags absent for SIC 2000-3999 or 4000-4999      |

#### Filing quality

| Code               | Trigger                                                    |
|--------------------|------------------------------------------------------------|
| `amendment_exists` | An amended filing exists but the original was used         |

#### TTM computation

| Code             | Trigger                                                              |
|------------------|----------------------------------------------------------------------|
| `ttm_annualized` | Prior-year YTD absent, TTM approximated via annualisation            |

#### Data integrity

| Code              | Trigger                                                              |
|-------------------|----------------------------------------------------------------------|
| `period_mismatch` | Income statement periods misaligned by >3 days, fact rejected        |
| `ambiguous_fact`  | Multiple XBRL contexts match after deduplication, value is None      |

#### Multiple computation

| Code                    | Trigger                                                       |
|-------------------------|---------------------------------------------------------------|
| `denominator_near_zero` | `abs(denominator) < 0.01`, multiple returned as N/A           |
| `negative_book_value`   | `StockholdersEquity < 0`, P/B returned as N/A                 |

#### Price data

| Code                | Trigger                                                       |
|---------------------|---------------------------------------------------------------|
| `price_unavailable` | yfinance returned no price for this period and ticker         |

### 2.5 `N/A` semantics

`null` in a multiple's `value` field means data was unavailable or a guard
condition triggered. It is **categorically distinct** from zero or a valid
negative result.

| Condition                      | Behaviour                         |
|--------------------------------|-----------------------------------|
| Required tag not found         | `null` + `N/A` in UI              |
| Valid negative result          | Negative value displayed          |
| Negative FCF                   | `null` + explanatory note         |
| Negative stockholders' equity  | `null` + `negative_book_value`    |
| `abs(denominator) < 0.01`      | `null` + `denominator_near_zero`  |



## 3. Timeout Rules

All outbound HTTP calls use **explicit timeouts**. Silent hangs that surface
as platform-level 502s are unacceptable - a clean user-readable 503 is preferred.

### 3.1 EDGAR endpoints

Applies to both `companyfacts` and `submissions` requests.

| Parameter      | Value               | Configured at                         |
|----------------|---------------------|---------------------------------------|
| Connect + read | **15 seconds**      | `httpx.AsyncClient(timeout=15.0)`     |
| Retry count    | **1** (on 429 only) | After `asyncio.sleep(2.0)`            |
| Retry backoff  | **2 seconds**       | Fixed, not exponential                |

The internal helper that resolves the HTTP client (either the lifespan-managed
shared client or a short-lived one for standalone callers) is named
`_resolve_client`. It is an `@asynccontextmanager` in `edgar.py`.

**On timeout:** raise `HTTPException(503)` with `error: "edgar_timeout"` and the
message: *"EDGAR is taking longer than usual. Please try again in a moment."*

**On 429 -> retry success:** transparent to the client.  
**On 429 -> retry 429:** raise `HTTPException(503)` with `error: "edgar_rate_limit"`.

### 3.2 yfinance (price service)

yfinance is synchronous. Its import is **lazy** (inside `_fetch_price_sync`) to
permit mocking in tests via `patch("yfinance.download", ...)`.

The async entry point `get_price` dispatches `_fetch_price_sync` to a thread pool
via `asyncio.to_thread`, then wraps the call with `asyncio.wait_for`.

| Parameter  | Value          | Configured at                             |
|------------|----------------|-------------------------------------------|
| Wall clock | **15 seconds** | `asyncio.wait_for(..., timeout=15.0)`     |
| On timeout | Return `None`  | `price_unavailable` warning set by caller |

Timeouts in the price service never propagate as HTTP 503. They produce a
`price_unavailable` warning on the affected period.

**Note on thread resources:** on `TimeoutError`, the underlying `_fetch_price_sync`
thread continues until yfinance's own network timeout fires. The event loop is
unblocked immediately, but the thread pool slot is held until yfinance finishes.
This is an accepted limitation given yfinance is synchronous with no cancellation
support.

### 3.3 Company index startup load

| Parameter | Value          | Configured at                                           |
|-----------|----------------|---------------------------------------------------------|
| Timeout   | **20 seconds** | `httpx.AsyncClient(timeout=20.0)` in `company_index.py` |

Failure is non-fatal: startup continues, search returns empty results until
the background refresh succeeds.

### 3.4 Background index refresh

Runs at most once every 24 hours. Does not block incoming requests. Errors
are logged as WARNING but do not propagate.



## 4. Cache Behaviour

### 4.1 Design

| Property       | Value                                                                         |
|----------------|-------------------------------------------------------------------------------|
| Implementation | Module-level Python `dict` (in-process memory)                                |
| Cache key      | `CIK_10` (10-digit zero-padded string)                                        |
| TTL            | **24 hours** (`CACHE_TTL_SECONDS = 86400`)                                    |
| Eviction       | **Lazy** - stale entries evicted on read, not by timer                        |
| Contents       | Raw EDGAR payloads (`companyfacts` + `metadata` dicts) + parsed `CompanyMeta` |
| Thread safety  | Safe for FastAPI's single-threaded async event loop                           |

### 4.2 Why CIK, not ticker?

CIK is EDGAR's stable internal identifier. Tickers change on renames and
exchange moves, CIK never does. Using CIK as the cache key ensures stability
across ticker changes.

### 4.3 What is cached?

The cache stores **raw EDGAR payloads**, not computed results.

- The expensive operation is the EDGAR fetch (5-10 MB companyfacts blob).
- Computation (XBRL extraction, TTM bridge, multiples) is fast and re-runs
  from the cached payload on every request.
- This ensures Phase 2 and Phase 3 logic is always applied without requiring
  cache invalidation when computation logic changes.

### 4.4 Cache hit flow

```
GET /api/financials/{cik_10}
  └─ cache.get(cik_10)             -> CacheEntry (not expired)
       └─ _build_response(...)     -> runs extraction + multiples from cached payload
            └─ FinancialsResponse  -> returned to client
```

`cached_at` in the response reflects when EDGAR data was last fetched.
`data_as_of` reflects when the computation ran (i.e., the current request time).

### 4.5 Cache miss flow

```
GET /api/financials/{cik_10}
  └─ cache.get(cik_10)             -> None (miss or expired)
       ├─ edgar.fetch_metadata(cik_10)       -> dict
       ├─ edgar.fetch_companyfacts(cik_10)   -> dict
       ├─ CompanyMeta.from_submissions(...)  -> CompanyMeta
       ├─ cache.put(cik_10, ...)             -> CacheEntry
       └─ _build_response(...)     -> runs extraction + multiples
            └─ FinancialsResponse  -> returned to client
```

### 4.6 Cache invalidation

No explicit invalidation endpoint is exposed. Entries expire via TTL.
The cache is cleared on every Render cold start (process restart). This
behaviour is disclosed in the README and communicated in the UI via a
loading indicator.

### 4.7 Known tradeoffs

| Tradeoff           | Impact                                                  |
|--------------------|---------------------------------------------------------|
| In-process memory  | Cache lost on cold start and process restart            |
| No shared cache    | No horizontal scaling, single Render instance only      |
| 24-hour TTL        | EDGAR data up to 24h stale, acceptable for valuation    |
| Lazy eviction      | Stale entries occupy memory until accessed again        |

### 4.8 Cache API (`app/cache.py`)

```python
cache.get(cik_10: str) -> CacheEntry | None
cache.put(cik_10, companyfacts, metadata, company_meta) -> CacheEntry
cache.invalidate(cik_10: str) -> None
cache.stats() -> dict  # {"total_entries", "live_entries", "expired_entries"}
```



## 5. Application Lifespan (`main.py`)

The lifespan handler manages two resources:

- `edgar_client` - a shared `httpx.AsyncClient` with connection pooling for all
  EDGAR requests. Attached to `app.state.edgar_client`.
- `refresh_task` - a background `asyncio.Task` that calls
  `company_index.maybe_refresh()` every hour.

The `yield` is wrapped in `try/finally` to guarantee cleanup during teardown,
regardless of how the application exits after entering the lifespan context:

```python
try:
    yield
finally:
    refresh_task.cancel()
    try:
        await refresh_task
    except asyncio.CancelledError:
        pass
    await edgar_client.aclose()
```

**Note:** exceptions raised *before* `yield` (during startup) prevent the
lifespan context from being entered at all, so `finally` does not run in that
case. The company index load failure is already non-fatal (wrapped in
`try/except`), so the only meaningful unhandled startup failure would be the
`httpx.AsyncClient` constructor, which cannot practically fail.



## 6. CIK Format Specification

All internal systems, cache keys, and API routes use **CIK_10** exclusively.

| Property       | Value                                                              | Example        |
|----------------|--------------------------------------------------------------------|----------------|
| Format         | Zero-padded 10-digit string                                        | `"0000320193"` |
| Regex          | `^\d{10}$`                                                         |                |
| Conversion     | `str(int(raw)).zfill(10)`                                          |                |
| EDGAR API call | Prefixed with `"CIK"`                                              | `CIK0000320193`|
| Source         | SEC `company_tickers.json` provides ints -> converted at ingestion |                |



## 7. CORS Configuration

```python
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",") if o.strip()]
```

| Environment | Origin                                         |
|-------------|------------------------------------------------|
| Local dev   | `http://localhost:5173`                        |
| Production  | Vercel deployment URL (set in Render env vars) |

Allowed methods: `GET`, `POST`.  
Allowed headers: `Content-Type`.  
`allow_credentials`: `false` (no cookies or auth headers).



## 8. Environment Variables

| Variable           | Default                                       | Description                              |
|--------------------|-----------------------------------------------|------------------------------------------|
| `ALLOWED_ORIGINS`  | `http://localhost:5173`                       | Comma-separated allowed CORS origins     |
| `EDGAR_USER_AGENT` | `OpenValuation/0.1 openvaluation@example.com` | User-Agent header for all EDGAR requests |



## 9. Phase Boundary Contracts

Phase 1 exposes these extension points for subsequent phases.

### 9.1 Phase 2 (XBRL Extraction) - Price Fetching Integration

**Architectural constraint:** `_build_response` is synchronous and dispatched via
`asyncio.to_thread`. It cannot `await` anything. `get_price` is async and must
not be called from within `_build_response` or from `xbrl.extract_ttm_periods`.

**Required pattern for Phase 2:** Price fetching must happen in the async route
handler (`get_financials`), after extraction and before multiples computation.
Run all period price fetches concurrently with `asyncio.gather`:

```python
# In get_financials, after cache hit/miss resolution:

extracted_periods = await asyncio.to_thread(xbrl.extract_ttm_periods, companyfacts)

if company_meta.ticker and extracted_periods:
    fetched_prices = await asyncio.gather(*[
        price.get_price(company_meta.ticker, ef.filing_date)
        if ef.filing_date is not None
        else asyncio.sleep(0, result=None)
        for ef in extracted_periods
    ])
    for ef, p in zip(extracted_periods, fetched_prices):
        ef.price = p

return await asyncio.to_thread(
    _build_response,
    company_meta,
    extracted_periods,   # pre-populated with prices
    cached_at,
)
```

This keeps price fetching in the event loop (where async I/O belongs), runs all
12 fetches concurrently (rather than sequentially in a thread), and keeps
`_build_response` purely CPU-bound as intended.

`_build_response` will need its signature updated to accept
`list[ExtractedFinancials]` directly rather than raw `companyfacts`. The
`xbrl.extract_ttm_periods` call moves out of `_build_response` and into the
route handler.

**Do not** call `_fetch_price_sync` directly from `xbrl.py`. That function exists
only for the `asyncio.to_thread` dispatch path and its direct use bypasses the
`asyncio.wait_for` timeout guard.

**Extension point:**

- Implement `app/services/xbrl.py` with:
  `extract_ttm_periods(companyfacts: dict) -> list[ExtractedFinancials]`
- Populate all fields of `ExtractedFinancials` except `price` (price is set by
  the route handler after extraction, as described above).
- The route handler in `app/routers/financials.py` will need updating to match
  the pattern above.

### 9.2 Phase 3 (Multiples Engine) - Warning Merge Requirement

- Implement `app/services/multiples.py::compute_all(f: ExtractedFinancials) -> tuple[MultipleSet, EVComponents]`
- Replace the `raise NotImplementedError` in each of the 7 pure calculator functions.
- `_build_response` calls `compute_all` after extraction.

**Required:** When constructing each `TTMPeriod`, `warnings` must be the union
of extraction warnings and all per-multiple warnings. The required merge pattern:

```python
multiples_set, ev_components = multiples.compute_all(ef)

multiples_warnings = [
    w
    for mv in vars(multiples_set).values()
    for w in mv.warnings
]

periods.append(
    TTMPeriod(
        ...
        warnings=ef.warnings + multiples_warnings,
    )
)
```

Omitting this merge causes all Phase 3 warnings (e.g. `denominator_near_zero`,
`negative_book_value`) to be silently dropped from the API response and Excel
export.

### 9.3 Phase 4 (Excel Export)

- Implement `app/services/workbook.py::build_workbook(response: FinancialsResponse) -> bytes`
- Update `app/routers/export.py` to call `build_workbook` and stream the result.
- Replace the `raise HTTPException(501, ...)` stub with the streaming response.

No contracts defined in this document need to change for Phase 4.



## 10. File Inventory (Phase 1)

```
backend/
├── app/
│   ├── main.py                    FastAPI entry point, CORS, lifespan, /health
│   ├── cache.py                   In-memory store: get / put / invalidate / stats
│   ├── user_agent.py              EDGAR User-Agent header builder
│   ├── models/
│   │   ├── errors.py              ErrorCode, WarningCode, Warning, APIError, pre-built instances
│   │   ├── company.py             SearchRequest, SearchResponse, CompanyCandidate, CompanyMeta
│   │   ├── cache.py               CacheEntry, EDGARPayload, CACHE_TTL_SECONDS
│   │   └── financials.py          ExtractedFinancials, MultipleSet, TTMPeriod, FinancialsResponse
│   ├── routers/
│   │   ├── search.py              POST /api/search
│   │   ├── financials.py          GET  /api/financials/{cik_10}
│   │   └── export.py              GET  /api/export/{cik_10} (501 stub)
│   └── services/
│       ├── company_index.py       CompanyIndex: load, search, background refresh
│       ├── edgar.py               fetch_companyfacts, fetch_metadata (_resolve_client, retry)
│       ├── price.py               get_price, _fetch_price_sync, _normalise_ticker
│       ├── multiples.py           compute_all + 7 pure functions (NotImplementedError stubs)
│       └── workbook.py            build_workbook (NotImplementedError stub)
├── tests/
│   ├── test_edgar.py              8 tests: fetch, timeout, 404, 429 retry, IFRS, User-Agent
│   ├── test_price.py              10 tests: normalisation, happy path, edge cases, timeout
│   └── test_search.py             19 tests: scoring, dedup, caps, CIK normalisation
└── requirements.txt
```

**Test results:** 37/37 passing.
