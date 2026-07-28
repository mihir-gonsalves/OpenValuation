# PHASE_1_SPEC.md

**Scope:** Core Backend Infrastructure  
**Status:** Complete  
**Implemented in:** `backend/`  

## What this document is

The decision record for Phase 1. It captures the choices behind the API surface, the cache, the failure policy, and the phase-boundary contracts that later phases had to honor.

It does not restate what is authoritative elsewhere:

| You want... | Read instead |
|---|---|
| Response shapes, field types, `WarningCode` / `ErrorCode` definitions | `app/models/*.py`, documented inline |
| Why CIK over ticker, why in-memory cache, why next-trading-day close | `DESIGN.md` |
| Financial semantics, tag chains, formulas | `README.md`, `DESIGN.md` |
| How extraction, multiples, and the export work | `PHASE_2_SPEC.md`, `PHASE_3_SPEC.md`, `PHASE_4_SPEC.md` |

## 1. Search scoring is a fixed integer ladder

`DESIGN.md` describes the ranking order. Ties break on lower-cased name ascending so the ordering is stable across requests. Results are deduplicated by CIK, keeping the highest-scoring occurrence, and capped at five.

| Condition (case-insensitive) | Score |
|---|---|
| Exact ticker | 100 |
| Normalized name exact | 90 |
| Name prefix | 70 |
| Name substring | 50 |
| No match | 0 |

The scores are deliberately far apart. Any future signal (exchange, market cap) can be added as a small adjustment without reordering the existing tiers.

A candidate carries identity only, no SIC and no exchange. Those come from the submissions fetch after selection, which is what keeps search off EDGAR entirely.

### 1.1 A failed index refresh starts a cooldown

The index refreshes lazily on the search path rather than from a background task, at most once per 24 hours, and the triggering request waits for the rebuild. The non-obvious part is `REFRESH_FAILURE_COOLDOWN_SECONDS = 300`. Every attempt records its time, so an SEC outage costs one request the 20s load timeout instead of stalling every subsequent search behind a doomed rebuild. The stale index keeps serving throughout, which is the right trade when the alternative is an empty result set. The staleness and cooldown checks are repeated inside the lock, so concurrent searches share one rebuild rather than queueing for their own.

## 2. `cached_at` and `data_as_of` mean different things

`cached_at` is when the EDGAR payload was last fetched. `data_as_of` is when this response was computed from it. They diverge on every cache hit, which is exactly what makes staleness visible to the client without exposing the cache itself.

## 3. The cache stores raw EDGAR payloads, not computed results

Extraction and multiples re-run on every cache hit. This costs a few milliseconds and buys the property that changing computation never requires invalidation. A cache of computed results would silently serve figures produced by old code after a deploy. Contract is `get` / `put` / `invalidate` / `stats`, keyed on `CIK_10`, 24h TTL, lazy eviction on read, oldest-first (not LRU) at `MAX_CACHE_ENTRIES`. `invalidate` has no caller today and is kept for symmetry, since a 24h TTL and a cold start on every deploy cover the cases an invalidation endpoint would.

Two simultaneous misses for one CIK both fetch and both write identical payloads. There is no per-key lock, since the duplicated fetch costs less than the coordination to avoid it.

## 4. Every outbound call has an explicit timeout

A clean 503 the user can read beats a silent hang that surfaces as a platform 502.

| Call | Timeout | Retry | On failure |
|---|---|---|---|
| EDGAR companyfacts / submissions | 15s | 1, on 429 only, 2s fixed backoff | `503 edgar_timeout`, still-429 becomes `503 edgar_rate_limit` |
| yfinance | 15s wall clock | none | returns `None`, becomes a `price_unavailable` warning, never an HTTP error |
| Company index startup load | 20s | none | non-fatal, search returns empty until the next lazy refresh |

Price failure is deliberately not an error. A company with no price still has seven periods of extractable financials and three multiples that do not need one.

Every EDGAR function takes an optional `client`. `_resolve_client` yields the shared lifespan client when one is passed and builds a short-lived one when it is not, so the same functions work in a request, in a test, and in a throwaway script without a second code path. yfinance is imported lazily inside the fetch helper rather than at module scope, which is what lets tests patch `yfinance.download` and keeps roughly a second of pandas import off startup.

**Accepted limitation.** yfinance is synchronous and dispatched via `asyncio.to_thread`. On timeout the event loop unblocks immediately but the thread-pool slot stays held until yfinance's own network timeout fires. A sync library with no cancellation offers no way around this.

## 5. Error bodies are `{error, message}` under FastAPI's `detail`

Codes and their triggers are documented in `app/models/errors.py`. New errors must match this shape.

| Error Code | Status | Trigger |
|---|---|---|
| `edgar_timeout` | 503 | EDGAR did not respond within 15s |
| `edgar_rate_limit` | 503 | HTTP 429 on the initial request and the single retry |
| `edgar_not_found` | 404 | EDGAR returned 404 for the CIK |
| `ifrs_filer` | 422 | companyfacts has `ifrs-full` but not `us-gaap` |
| `unsupported_taxonomy` | 422 | companyfacts has neither `us-gaap` nor `ifrs-full` facts |
| `invalid_cik` | 422 | CIK fails `^\d{10}$` |
| `internal_error` | 500 | unhandled exception |

An unexpected EDGAR status surfaces as 502 with `internal_error`, an unreachable host as `503 edgar_timeout`.

Two handlers in `main.py` enforce it. The first unwraps any `detail` dict carrying an `error` key to the top level and rewrites everything else as `internal_error` with the detail string as the message, so a bare `HTTPException` raised anywhere still reaches the client in contract shape. The second maps a validation failure to `invalid_cik` only when the failing location is `("path", "cik_10")`, and otherwise passes FastAPI's own `detail` list through untouched. That narrowing is what keeps an over-length search query from claiming to be a bad CIK.

The taxonomy check is a separate step after the fetch, and it distinguishes an IFRS filer from a filer with no recognized taxonomy at all. Both are 422 and both are dead ends, but naming which one lets the user understand why a real company returned nothing instead of seeing an unexplained empty table.

## 6. `/health` is a contract with the frontend, not just a probe

It returns `status`, `version`, `cache.stats()`, and `company_index_size`. The frontend's landing page polls it to decide whether the backend is cold, so it has to stay cheap and dependency-free: it reads process-local state and makes no outbound call. The cache and index numbers are there because they are the two pieces of state a restart silently resets.

CORS allows `GET` and `POST` with `Content-Type`, origins from `ALLOWED_ORIGINS`, and `allow_credentials=False`, since there are no accounts, no cookies, and nothing to authenticate. `ALLOWED_ORIGINS` and `EDGAR_USER_AGENT` both carry hardcoded dev fallbacks marked `# will update later`. The SEC requires the User-Agent on every request, so the fallback keeps a fresh clone working while being obviously wrong in production.

## 7. Lifespan owns exactly one resource

The shared `httpx.AsyncClient` on `app.state.edgar_client`, closed in a `finally` so shutdown cannot leak the connection pool. A startup exception before the `yield` would skip that `finally`, which is the usual argument against this pattern. It is safe here because the index load is wrapped in its own try/except and the only other startup step is the client constructor, which cannot practically fail.

## 8. Phase boundary contracts

**Phase 2 superseded the price plan.** Phase 1 assumed a sync `_build_response` with price fetching in the route handler. Phase 2 moved the fetch inside `extract_ttm_periods`, so `_build_response` is `async` and awaited directly.

**The warnings merge.** `TTMPeriod.warnings` is the union of extraction warnings and every per-multiple warning, then deduplicated. `financials.py` iterates the seven `MultipleSet` fields explicitly through `_MULTIPLE_FIELDS`:

```python
_MULTIPLE_FIELDS = ("ev_revenue", "ev_ebitda", "ev_ebit", "pe", "pfcf", "ps", "pb")
```

Not `vars(multiples_set).values()`, which on a Pydantic v2 model includes internal state keys, and not `model_dump()`, which returns plain dicts that have lost the `.warnings` attribute. Omitting the merge entirely drops every Phase 3 warning from both the API and the Excel export with no other symptom.

Dedup at the router boundary is what lets Phase 3 attach `ev_debt_missing` to all three EV multiples without producing three identical rows in the UI.

**Phase 4 reuses the orchestration, not the route.** `resolve_financials` was split out of the handler so `export.py` shares the cache lookup, the fetch, and the error contract, and adds none of its own.

## 9. Multiples failure is contained per period

`_build_response` calls `compute_all` inside a `try/except Exception`. A failure is logged and that period falls back to an empty `MultipleSet` and `EVComponents`, while the extraction data is never discarded or re-fetched. One bad period does not cost the user the other eleven.
