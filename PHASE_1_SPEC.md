# PHASE_1_SPEC.md - OpenValuation Core Backend Infrastructure

**Phase:** 1  **Status:** Complete  **Implemented in:** `backend/`

## What this document is

This is the **precise contract** for the Phase 1 backend: exact API request/
response shapes, the `ErrorCode`/`WarningCode` enums with their HTTP mappings, and
the operational invariants (timeouts, cache, phase boundaries) that later phases
must not break. These shapes live nowhere else at this precision - they are
normative here.

It does **not** repeat the *rationale* behind these decisions, which is owned by:

| You want... | Read instead |
|---|---|
| Why CIK over ticker, why next-trading-day close, why in-memory cache, TSM exclusion, CORS/price/cache reasoning | `DESIGN.md` |
| Financial semantics, EV/FCF formulas, tag chains | `README.md`, `DESIGN.md` |
| How extraction/multiples actually work | `PHASE_2_SPEC.md`, code docstrings |

All endpoints are prefixed `/api`, bodies are `application/json` unless noted.
Local base URL `http://localhost:8000`.



## 1. API Contracts

### 1.1 `POST /api/search`

Resolves a company name or ticker to up to five CIK candidates.
**Invariant:** no per-query external network calls - matching runs entirely
against the in-memory index. The only network activity on the search path is
the index refresh, which runs at most once per 24 hours (the triggering request
waits for it, concurrent requests wait on the same refresh). Rationale in
`DESIGN.md` -> *Company Search Architecture*.

**Request** - `{ "query": "Apple" }`, `query` required, 1–200 chars, stripped.

**Response `200`**

```json
{ "results": [ { "cik_10": "0000320193", "name": "Apple Inc.", "ticker": "AAPL" } ] }
```

`results` is 0–5 elements sorted by match score desc, then name asc for stability,
deduplicated by CIK. SIC and exchange are **not** returned by search - they come
from `/api/financials/{cik_10}` after selection.

**Scoring (deterministic):**

| Condition (case-insensitive) | Score |
|---|---|
| Exact ticker match | 100 |
| Name exact match (normalised) | 90 |
| Name prefix match | 70 |
| Name substring match | 50 |
| No match | 0 |

**Errors:** empty or >200-char `query` -> `422` (Pydantic validation).

### 1.2 `GET /api/financials/{cik_10}`

Returns TTM valuation multiples for a company. Path param `cik_10` is validated by
regex `^\d{10}$`. `periods` is empty in Phase 1, up to 12 entries from Phase 2 on.

**Response `200`**

```json
{
  "company": {
    "cik_10": "0000320193", "name": "Apple Inc.", "ticker": "AAPL",
    "sic": "3571", "sic_description": "Electronic Computers", "exchange": "Nasdaq",
    "is_financial": false, "is_capital_intensive": false
  },
  "periods": [],
  "cached_at": "2026-05-07T12:00:00Z",
  "data_as_of": "2026-05-07T12:01:34Z"
}
```

**`company`**

| Field | Type | Source | Notes |
|---|---|---|---|
| `cik_10` | string | path param | 10-digit zero-padded |
| `name` | string | EDGAR submissions | registrant name |
| `ticker` | string \| null | EDGAR submissions | primary ticker, null if none |
| `sic` | string \| null | EDGAR submissions | 4-digit SIC as string |
| `sic_description` | string \| null | EDGAR submissions | SIC label |
| `exchange` | string \| null | EDGAR submissions | primary listing exchange |
| `is_financial` | bool | derived | SIC 6000–6999 |
| `is_capital_intensive` | bool | derived | SIC 2000–3999 or 4000–4999 |

**`periods[]`** - each is a `TTMPeriod`:

```json
{
  "period_end": "2024-09-28", "filing_date": "2024-11-01", "price": "222.9100",
  "multiples": {
    "ev_revenue": { "value": "7.81",  "label": "EV/Revenue", "warnings": [] },
    "ev_ebitda":  { "value": "21.40", "label": "EV/EBITDA",  "warnings": [] },
    "ev_ebit":    { "value": "25.11", "label": "EV/EBIT",    "warnings": [] },
    "pe":         { "value": "33.12", "label": "P/E",        "warnings": [] },
    "pfcf":       { "value": "26.33", "label": "P/FCF",      "warnings": [] },
    "ps":         { "value": "8.54",  "label": "P/S",        "warnings": [] },
    "pb":         { "value": "45.22", "label": "P/B",        "warnings": [] }
  },
  "ev_components": { "market_cap": "...", "long_term_debt": "...", "cash": "...", "enterprise_value": "...", "...": "..." },
  "extracted": { "...": "ExtractedFinancials - see app/models/financials.py" },
  "warnings": []
}
```

**`multiple` value object**

| Field | Type | Notes |
|---|---|---|
| `value` | string \| null | Decimal string, `null` = N/A (categorically distinct from zero or a valid negative) |
| `label` | string | display label, changes on fallback (e.g. `P/E (basic)`) |
| `warnings` | Warning[] | per-multiple warnings for this period |

**`TTMPeriod.warnings`** is the **union** of extraction warnings (Phase 2) and
per-multiple warnings (Phase 3) - never a subset of either. The required merge is
in §6.2.

**`cached_at`** = when EDGAR data was last fetched. **`data_as_of`** = when this
response was computed.

**Errors**

| Condition | HTTP | `error` |
|---|---|---|
| Invalid CIK format | `422` | `invalid_cik` |
| CIK not on EDGAR | `404` | `edgar_not_found` |
| EDGAR timeout | `503` | `edgar_timeout` |
| EDGAR rate limit | `503` | `edgar_rate_limit` |
| IFRS filer | `422` | `ifrs_filer` |
| Non-GAAP/IFRS-less filer | `422` | `unsupported_taxonomy` |
| Unexpected server error | `500` | `internal_error` |

### 1.3 `GET /api/export/{cik_10}`

Phase 1: returns `501` `not_implemented`. Phase 4: streams a three-sheet `.xlsx`
(`Content-Type: ...spreadsheetml.sheet`, `Content-Disposition: attachment;
filename="openvaluation_{cik_10}.xlsx"`). Same `^\d{10}$` validation -> `422`
`invalid_cik` on bad CIK.

### 1.4 `GET /health`

```json
{
  "status": "ok", "version": "0.1.0",
  "cache": { "total_entries": 4, "live_entries": 3, "expired_entries": 1 },
  "company_index_size": 10823
}
```



## 2. Error & Warning Model

### 2.1 Error response body

Returned with the 4xx/5xx status: `{ "error": <ErrorCode>, "message": <human text> }`.

### 2.2 `ErrorCode` enum

| Code | HTTP | Trigger |
|---|---|---|
| `edgar_timeout` | 503 | EDGAR did not respond within 15s |
| `edgar_rate_limit` | 503 | HTTP 429 on the initial request and the single retry |
| `edgar_not_found` | 404 | EDGAR returned 404 for the CIK |
| `ifrs_filer` | 422 | companyfacts has `ifrs-full` but not `us-gaap` |
| `unsupported_taxonomy` | 422 | companyfacts has neither `us-gaap` nor `ifrs-full` facts |
| `invalid_cik` | 422 | CIK fails `^\d{10}$` |
| `internal_error` | 500 | unhandled exception |

> EDGAR-origin unexpected HTTP statuses surface as `502` with `error: "internal_error"`, an
> unreachable-host network error surfaces as `503 edgar_timeout`.

### 2.3 Warning object

Per-period, non-fatal, never suppresses a result:
`{ "code": <WarningCode>, "message": <human text> }`. Surfaced in the API
response, the UI table, and the Excel Summary sheet.

### 2.4 `WarningCode` enum (authoritative)

This is the single source of truth for warning codes. Phase 2 documents which of
these it actually emits vs. reserves, it does not redefine them.

| Group | Code | Trigger |
|---|---|---|
| Fallback tags | `fallback_revenue` | primary revenue tag absent, fallback used |
| | `fallback_eps_basic` | diluted EPS absent, basic used -> `P/E (basic)` |
| EV construction | `ev_debt_missing` | all financial-debt and finance-lease tags absent |
| | `debt_deduplicated` | `LongTermDebt` treated as total debt, current portion not added separately |
| | `cash_fallback_includes_investments` | fallback cash tag includes short-term investments |
| | `capex_sign_normalized` | CapEx was negative, `abs()` taken |
| Finance leases | `lease_pre_asc842` | pre-ASC 842 capital-lease fallback tags used |
| | `finance_lease_missing_capital_intensive` | lease tags absent for SIC 2000–3999 / 4000–4999 |
| Filing quality | `amendment_exists` | only source for a fact is an amendment (`/A`) filing |
| TTM | `ttm_annualized` | prior-year YTD absent, TTM approximated by annualization |
| Data integrity | `period_mismatch` | reserved - see `PHASE_2_SPEC.md` §3.3 (never raised) |
| | `ambiguous_fact` | conflicting non-amendment values after dedup, value is None |
| Multiples | `denominator_near_zero` | `abs(denominator) < 0.01`, multiple is N/A |
| | `negative_book_value` | `StockholdersEquity < 0`, P/B is N/A |
| | `negative_fcf` | `OperatingCashFlow − CapEx < 0`, P/FCF is N/A |
| Price | `price_unavailable` | yfinance returned no price for this period/ticker |

### 2.5 `N/A` semantics

`null` in a multiple's `value` means data was unavailable or a guard fired. It is
categorically distinct from zero or a valid negative. The precise guard rules
(negative book value, near-zero denominator, negative FCF) are owned by
`README.md` -> *Results Display* and `DESIGN.md` -> *Multiples*. The guards that
fire on present data (`negative_book_value`, `negative_fcf`, `denominator_near_zero`)
carry a warning, a `null` caused by a missing operand is silent (see
`PHASE_3_SPEC.md` §2.2).



## 3. Timeout Rules

Every outbound HTTP call uses an explicit timeout - a clean user-readable 503
beats a silent hang surfacing as a platform 502. Rationale in `DESIGN.md` ->
*Price Service*.

| Call | Timeout | Retry | On failure |
|---|---|---|---|
| EDGAR (`companyfacts`, `submissions`) | 15s connect+read | 1, on 429 only, after 2s fixed backoff | `503 edgar_timeout`, 429-after-retry -> `503 edgar_rate_limit` |
| yfinance (`price.py`) | 15s wall clock (`asyncio.wait_for`) | none | return `None` -> `price_unavailable` (never an HTTP error) |
| Company index startup load | 20s | - | non-fatal: search returns empty until background refresh succeeds |
| Background index refresh | - | ≤ once / 24h | logged WARNING, never propagates |

**Implementation notes.** The EDGAR client resolver is `_resolve_client`, an
`@asynccontextmanager` in `edgar.py` (shared lifespan client in prod, short-lived
client for standalone callers). yfinance is synchronous: its import is lazy inside
`_fetch_price_sync` (so tests can `patch("yfinance.download", ...)`), dispatched via
`asyncio.to_thread`. On yfinance timeout the event loop unblocks immediately but
the thread-pool slot is held until yfinance's own network timeout fires - an
accepted limitation of a sync library with no cancellation.



## 4. Cache Behaviour

Module-level dict in `app/cache.py`. Full design rationale (why CIK, why not Redis,
cold-start tradeoff) is in `DESIGN.md` -> *Caching Strategy*, the contract is:

- **Key:** `CIK_10`. **TTL:** 24h (`CACHE_TTL_SECONDS = 86400`). **Eviction:** lazy
  on read, oldest-first at `MAX_CACHE_ENTRIES = 8` (parsed dicts are ~5× their JSON size, see `app/cache.py`).
- **Stores raw EDGAR payloads**, not computed results - so Phase 2/3 logic always
  re-runs from cache and never needs invalidation when computation changes.
- Cleared on every restart/cold start. No explicit invalidation endpoint.

**API:** `get(cik_10) -> CacheEntry | None`, `put(cik_10, companyfacts,
company_meta) -> CacheEntry`, `invalidate(cik_10)`, `stats() -> dict`.

Hit path: `get` -> `_build_response` (re-runs extraction + multiples from the cached
payload). Miss path: parallel `fetch_metadata` + `fetch_companyfacts` ->
`CompanyMeta.from_submissions` -> `put` -> `_build_response`.



## 5. Operational invariants (reference)

These are realized in code, recorded here only as contracts.

- **Lifespan (`main.py`)** owns one resource: the shared `httpx.AsyncClient`
  (`app.state.edgar_client`, connection-pooled), closed in a `finally` block on shutdown.
  The company index is loaded once at startup (failure is non-fatal) and refreshed **lazily on
  the `/api/search` path** (`CompanyIndex.maybe_refresh`, <= once / 24h) - there is no background
  task. (A startup failure before `yield` skips `finally`. The only un-guarded startup step, the
  `AsyncClient` constructor, cannot practically fail, and the index load is already wrapped in
  try/except.)
- **CIK_10:** zero-padded 10-digit string, `^\d{10}$`, `str(int(raw)).zfill(10)`,
  EDGAR URLs prefix it with `CIK`. Used for all cache keys and routes. (Also in
  `README.md`.)
- **CORS:** `ALLOWED_ORIGINS` (comma-separated env, default
  `http://localhost:5173`). Methods `GET`, `POST`, header `Content-Type`,
  `allow_credentials=false`.
- **Env vars:** `ALLOWED_ORIGINS`, `EDGAR_USER_AGENT` (app name + contact email,
  required by SEC on every request - `app/user_agent.py`). See `.env.example`.



## 6. Phase Boundary Contracts

What Phase 1 owes later phases, and what they owe back.

### 6.1 Phase 2 - price fetching (settled)

Phase 2 placed price fetching inside `xbrl.extract_ttm_periods`, making it `async`,
`_build_response` is therefore `async` and awaited directly (no `asyncio.to_thread`
dispatch). The Phase 1 stub assumed the opposite (sync `_build_response` + price in
the route handler), that arrangement is superseded. See `PHASE_2_SPEC.md` §1 and
the `app/routers/financials.py` docstring. No extension point still owed here.

### 6.2 Phase 3 - multiples engine + warning merge (settled)

- `multiples.compute_all(f: ExtractedFinancials) -> tuple[MultipleSet, EVComponents]`
  is implemented with the seven pure calculators.
- `_build_response` calls `compute_all` per period inside a `try/except Exception`:
  any failure is logged via `logger.exception` and that period falls back to an
  empty `MultipleSet()` / `EVComponents()` while **extraction data is never discarded
  or re-fetched**.

**Required warning merge.** Each `TTMPeriod.warnings` must be `ef.warnings` plus
every per-multiple warning. The implemented pattern iterates the seven
`MultipleSet` fields **explicitly** (in `financials.py` as `_MULTIPLE_FIELDS`):

```python
_MULTIPLE_FIELDS = ("ev_revenue", "ev_ebitda", "ev_ebit", "pe", "pfcf", "ps", "pb")
multiples_warnings = [
    w
    for field_name in _MULTIPLE_FIELDS
    for w in getattr(multiples_set, field_name).warnings
]
warnings = ef.warnings + multiples_warnings
```

Do **not** use `vars(multiples_set).values()` or `model_dump()`: on a Pydantic v2
model `vars()` includes internal state keys, and `model_dump()` returns plain dicts
that lose the `.warnings` attribute. Listing the seven fields is explicit, fast,
and makes the schema contract visible. Omitting the merge silently drops all Phase 3
warnings (`denominator_near_zero`, `negative_book_value`, ...) from the API and Excel.

**Warning deduplication.** The router now applies `dedup_warnings` to the merged
union before building `TTMPeriod.warnings`. This collapses repeated codes - most
importantly `ev_debt_missing`, which Phase 3 should attach to each EV-based multiple
(`ev_revenue`, `ev_ebitda`, `ev_ebit`) so it reaches the response. Without dedup,
that produces three identical rows in the UI/Excel. The dedup at the router boundary
collapses them automatically, so Phase 3 implementers can attach the warning to each
affected multiple without worrying about triplication.

### 6.3 Phase 4 - Excel export (owed)

Implement `workbook.build_workbook(response: FinancialsResponse) -> bytes`, update
`routers/export.py` to stream it, replacing the `501` stub. No contract in this
document changes for Phase 4.



## 7. File Inventory (Phase 1)

```
backend/app/
├── main.py                FastAPI entry, CORS, lifespan, /health
├── cache.py               in-memory store: get / put / invalidate / stats
├── user_agent.py          EDGAR User-Agent builder
├── models/                errors, company, cache, financials (Pydantic schemas)
├── routers/               search.py, financials.py, export.py (501 stub)
└── services/
    ├── company_index.py   load, search, background refresh
    ├── edgar.py           fetch_companyfacts / fetch_metadata, _resolve_client, 429 retry
    ├── price.py           get_price, _fetch_price_sync, _normalise_ticker
    ├── multiples.py       compute_all + 7 pure fns (implemented in Phase 3)
    └── workbook.py        build_workbook (stub -> Phase 4)
```

Phase 1 tests: `test_edgar.py`, `test_price.py`, `test_search.py`. (XBRL extraction
tests arrived with Phase 2 - see `PHASE_2_SPEC.md` §6.)
