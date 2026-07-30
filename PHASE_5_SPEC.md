# PHASE_5_SPEC.md

**Scope:** Deployment  
**Status:** Complete  
**Implemented in:** `backend/app/main.py`, `backend/app/services/price.py`, `frontend/src/api/client.ts`, `frontend/vite.config.ts`  
**Deployed on:** Render (backend) and Vercel (frontend)

## What this document is

The decision record for putting the app on Render and Vercel. It captures the handful of choices that came out of things breaking in production and that a reader cannot infer from the code alone.

It does not restate what is authoritative elsewhere:

| You want... | Read instead |
|---|---|
| The stack, the hosting split, why the cache is in-memory | `PROJECT_STATUS.md`, `DESIGN.md` |
| Cold-start handling in the UI, the retry predicate, the health poll | `PHASE_4_SPEC.md` §3 |
| Price semantics, the next-trading-day rule, ticker normalization | `README.md`, `app/services/price.py` |
| Required environment variables | `backend/.env.example`, `frontend/.env.example` |

There is no `render.yaml` and no CI workflow. Both services are configured through their dashboards and deploy on push to `main`.

## 1. The health endpoint lives under `/api`, and the reason is browser extensions

`/health` was blocked in production by ad blockers, which cancel the request inside the browser with `ERR_BLOCKED_BY_CLIENT` before it reaches the network. The backend was healthy the whole time and the endpoint returned 200 to curl and to direct navigation.

The consequence was disproportionate. `useServerReady` treats a never-resolving probe as a permanently cold server, so the landing page showed "Waking the server" forever and never showed the example chips.

The diagnosis rested on one observation: `POST /api/search` worked from the same page against the same origin. The filter was matching the `/health` path, not the domain, so moving the route to `/api/health` was enough.

## 2. Environment configuration is a two-pass cycle

`ALLOWED_ORIGINS` needs the Vercel URL and `VITE_API_BASE` needs the Render URL, so neither can be set correctly on a first pass. The order is backend first with a placeholder origin, then the frontend with the real backend URL, then back to the backend to set the real origin.

`VITE_API_BASE` is read at build time and compiled into the bundle. Changing it in the Vercel dashboard does nothing until a rebuild, which is the kind of thing that costs an hour when the value looks right in the UI.

`PYTHON_VERSION` is pinned to 3.10.12 to match local. Render's default has moved ahead to 3.13, where the pinned `pandas` and `yfinance` versions are not guaranteed to have wheels, and the fallback is a source build that fails or times out.

## 3. Price fetch failures are retried once

The first production deploy initially returned `price_unavailable` for every period of every company. 

The cause was Render's egress IP, which is shared across free-tier services, being throttled. `yfinance` throttles responses from unfamiliar (first time) IP addresses. Free-tier Render services do not support dedicated IPs, so `yfinance` throttles are unpredictable.

### 3.1 `None` meant two different things, which is what blocked retrying

`_download_window_sync` returned `None` both when `yfinance` raised and when it came back with an empty frame. Those need opposite handling. A call that failed or timed out is worth another attempt. An empty frame is read as the ticker having no data in the window, which another attempt will not change.

Splitting them is the substance of the change. `PriceFetchError` now signals a failed call and `None` is reserved for an empty frame. Only the former is retried.

The old handler was `except (asyncio.TimeoutError, Exception)`, which is redundant because `Exception` already covers `TimeoutError`, and which collapsed every cause into one log line. Attempts now log separately, so Render's logs distinguish a timeout from a rejection instead of reporting both as a generic failure.

### 3.2 One retry, not a budget

`PRICE_FETCH_ATTEMPTS = 2` with a flat 2s backoff. The observed failures clear within seconds, to the point that a manual refresh shortly after a blank table returns a full one. A second attempt covers this.

`PRICE_FETCH_TIMEOUT_SECONDS` went from 15 to 20. It is not what fixed the observed case, which failed fast rather than slowly, but it is the only thing covering a genuinely slow response.

## 4. A failed price fetch is never cached

Worth stating because it is what makes the refresh workaround work and why no cache invalidation is needed on a price failure.

The cache holds the raw companyfacts payload and company metadata, not computed periods. Prices are fetched on every response build, inside `extract_ttm_periods`. A blank table therefore leaves nothing bad behind, and a browser refresh re-runs the price fetch against a cache hit, without paying the EDGAR round trip again.

## 5. Known limitations (accepted)

1. **CORS is exact-match, so Vercel preview deployments fail.** Every preview gets a unique hostname that will not be in `ALLOWED_ORIGINS`. Previews render but every API call is blocked. Fixing this means `allow_origin_regex` in place of the origin list, which is a wider surface than production currently needs.
2. **The example chips are still gated behind the health probe.** Any probe failure, not only a blocked one, leaves the landing page showing the warming notice and never the chips, even though the chips are static links with no backend dependency. The `/api` rename removed the one observed cause.
3. **`warming` has no upper bound.** A probe that never resolves shows "Waking the server" indefinitely, which is wrong on a healthy backend rather than merely unhelpful.
