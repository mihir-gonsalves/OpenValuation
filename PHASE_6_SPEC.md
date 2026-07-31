# PHASE_6_SPEC.md

**Scope:** QA and Finalization  
**Status:** In progress  
**Implemented in:** `backend/app/services/price.py`

## What this document is

The decision record for fixes found by testing the deployed app rather than by building it. Everything here is a bug that only shows up against production, and the reasoning is kept because in each case the first explanation was wrong.

It does not restate what is authoritative elsewhere:

| You want... | Read instead |
|---|---|
| Hosting, environment configuration, the health endpoint | `PHASE_5_SPEC.md` |
| Price semantics, the next-trading-day rule, ticker normalization | `README.md`, `app/services/price.py` |
| Cache shape, key, and TTL | `PHASE_1_SPEC.md`, `app/cache.py` |
| Where `price_unavailable` is raised and what it nulls | `PHASE_2_SPEC.md`, `PHASE_3_SPEC.md` §1.2 |

## 1. Price fetch failures are retried once

The first production deploy returned `price_unavailable` for every period of every company.

Two different things can produce this symptom, and they were intially conflated:

1. Render's free-tier service only offers randomized egress IPs with no dedicated-IP option, `yfinance` throttles responses to unfamiliar IPs which makes price-fetching failures real and unpredictable.

2. The failure that dependably reproduced was the first request into a freshly started process. §1.1 covers what it actually was.

### 1.1 An empty frame is a failure, not an answer

`yf.download` never raises. It catches every internal error and substitutes an empty frame (`multi.py:_download_one`), recording the real cause only in a log line. Version 1.4.1 has no `raise_errors` option.

The failure that matters therefore arrives looking exactly like success with no rows, and `_download_window_sync` returned `None` for both. The first version of this fix retried only raised errors and read an empty frame as "this ticker has no data in the window". That reading is what made the retry dead code for the only failure it was written for, and it shipped that way. Anything short of usable rows is now retried.

The diagnosis came from the symptom rather than the logs. The first request against a freshly started process returned `price_unavailable` for every period while the XBRL half of the same response was complete, and an immediate reload returned full prices. Nothing that clears in under a second is a throttle. The failing call is what completes `yfinance`'s cookie handshake and leaves it in process memory, which is what the next call gets to use. The retry now absorbs that inside one request.

`PriceFetchError` stays, because `_download_window_sync` does work of its own around the download that can still raise. It is no longer the signal the retry depends on.

The cost of the new reading is one wasted attempt plus one backoff on a ticker that genuinely has no data in the window. That is the right trade against silently blanking all seven multiples for any company on the first request after most cold starts.

The old handler was `except (asyncio.TimeoutError, Exception)`, which is redundant because `Exception` already covers `TimeoutError`, and which collapsed every cause into one log line. Attempts now log separately, so Render's logs distinguish a timeout from an empty result instead of reporting both as a generic failure.

### 1.2 One retry, not a budget

`PRICE_FETCH_ATTEMPTS = 2` with a flat 2s backoff. The observed failures clear within milliseconds, to the point that a manual refresh immediately after a blank table returns a full one. A second attempt covers this.

`PRICE_FETCH_TIMEOUT_SECONDS` went from 15 to 20. It is not what fixed the observed case, which failed fast rather than slowly, but it is the only thing covering a genuinely slow response.

## 2. A failed price fetch is never cached

Worth stating because it is what makes the refresh workaround work and why no cache invalidation is needed on a price failure.

The cache holds the raw companyfacts payload and company metadata, not computed periods. Prices are fetched on every response build, inside `extract_ttm_periods`. A blank table therefore leaves nothing bad behind, and a browser refresh re-runs the price fetch against a cache hit, without paying the EDGAR round trip again.
