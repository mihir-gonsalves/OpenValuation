# PHASE_5_SPEC.md

**Scope:** Deployment  
**Status:** Complete  
**Implemented in:** `backend/app/main.py`, `frontend/src/api/client.ts`, `frontend/vite.config.ts`  
**Deployed on:** Render (backend) and Vercel (frontend)

## What this document is

The decision record for putting the app on Render and Vercel. It captures the handful of choices that came out of things breaking in production and that a reader cannot infer from the code alone.

It does not restate what is authoritative elsewhere:

| You want... | Read instead |
|---|---|
| The stack, the hosting split, why the cache is in-memory | `PROJECT_STATUS.md`, `DESIGN.md` |
| Cold-start handling in the UI, the retry predicate, the health poll | `PHASE_4_SPEC.md` §3 |
| Required environment variables | `backend/.env.example`, `frontend/.env.example` |
| Bugs found by testing the deployed app | `PHASE_6_SPEC.md` |

There is no `render.yaml` and no CI workflow. Both services are configured through their dashboards and deploy on push to `main`.

## 1. The health endpoint lives under `/api`, and the reason is browser extensions

`/health` was blocked in production by ad blockers, which cancel the request inside the browser with `ERR_BLOCKED_BY_CLIENT` before it reaches the network. The backend was healthy the whole time and the endpoint returned 200 to curl and to direct navigation.

The consequence was disproportionate. `useServerReady` treats a never-resolving probe as a permanently cold server, so the landing page showed "Waking the server" forever and never showed the example chips.

The diagnosis rested on one observation: `POST /api/search` worked from the same page against the same origin. The filter was matching the `/health` path, not the domain, so moving the route to `/api/health` was enough.

## 2. Environment configuration is a two-pass cycle

`ALLOWED_ORIGINS` needs the Vercel URL and `VITE_API_BASE` needs the Render URL, so neither can be set correctly on a first pass. The order is backend first with a placeholder origin, then the frontend with the real backend URL, then back to the backend to set the real origin.

`VITE_API_BASE` is read at build time and compiled into the bundle. Changing it in the Vercel dashboard does nothing until a rebuild, which is the kind of thing that costs an hour when the value looks right in the UI.

`PYTHON_VERSION` is pinned to 3.10.12 to match local. Render's default has moved ahead to 3.13, where the pinned `pandas` and `yfinance` versions are not guaranteed to have wheels, and the fallback is a source build that fails or times out.

## 3. Known limitations (accepted)

1. **CORS is exact-match, so Vercel preview deployments fail.** Every preview gets a unique hostname that will not be in `ALLOWED_ORIGINS`. Previews render but every API call is blocked. Fixing this means `allow_origin_regex` in place of the origin list, which is a wider surface than production currently needs.
2. **The example chips are still gated behind the health probe.** Any probe failure, not only a blocked one, leaves the landing page showing the warming notice and never the chips, even though the chips are static links with no backend dependency. The `/api` rename removed the one observed cause.
3. **`warming` has no upper bound.** A probe that never resolves shows "Waking the server" indefinitely, which is wrong on a healthy backend rather than merely unhelpful.
