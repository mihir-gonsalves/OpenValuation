# PHASE_4_SPEC.md - Frontend (UX/UI) and Excel Export

**Phase:** 4  **Status:** Complete  **Implemented in:** `frontend/`, `backend/app/services/workbook.py`, `backend/app/routers/export.py`

## What this document is

This is the **decision record** for Phase 4 - the React + Vite +
TypeScript app in `frontend/` that turns the backend's JSON into the search,
results table, audit panel, and export affordance. It captures the non-obvious
choices: the design-system strategy, the API-boundary type coercion, the
URL-as-state model, cold-start awareness, and the test layering.

It deliberately does **not** restate what is authoritative elsewhere:

| You want... | Read instead |
|---|---|
| The user-facing behaviour (search, data presentation, URL state, Excel export contents), the seven formulas, warning semantics | `README.md` -> *What It Does*, *Multiple Calculation*, *Results Display* |
| The exact JSON shapes (`FinancialsResponse`, `TTMPeriod`, `MultipleSet`, `EVComponents`, `CompanyMeta`), `WarningCode`/`ErrorCode` enums, HTTP error mappings | `PHASE_1_SPEC.md` §1-2, `app/models/{financials,company,errors}.py` |
| What the backend computes and guarantees per period | `PHASE_2_SPEC.md`, `PHASE_3_SPEC.md` |
| The original frontend plan (component list, deployment) | `README.md` -> *Architecture*, *Tech Stack* |

The TypeScript contract in `frontend/src/api/types.ts` is a hand-maintained
mirror of the Pydantic models. **Those models are authoritative**, this doc
explains why the frontend reads them the way it does.



## 1. Scope and entry points

Phase 4 as originally framed (`README.md`) is "frontend + Excel export". This
phase delivers **both halves** - the React frontend documented in §2, and the
Excel export backend (`services/workbook.py` + `GET /api/export/{cik_10}`)
documented in §3.

The app is a single-page React SPA. Its surface:

- **Entry:** `src/main.tsx` mounts providers (TanStack Query, React Router,
  Radix `TooltipProvider`) and defines two routes: `/` (`App.tsx`) and `/learn`
  (`Learn.tsx`, the plain-language glossary in §2.11). `src/App.tsx` is the page
  shell and routes off the `?cik=` query param. With no `?cik`, it renders a
  centered masthead + hero search + a cold-start-aware status zone/ With a
  `?cik`, a top-bar layout of company header, results table, and audit panel.
- **Data:** `src/api/{types,client,queries}.ts` - the only code that talks to the
  backend. Everything else is presentational and consumes typed data.
- **Feature components:** `src/components/*` - see `README.md`.
- **Pure helpers / hooks:** `src/lib/{format,errors,utils,useDebounce,useServerReady}.ts`.

No client-side financial computation. The frontend formats, navigates, and
visualizes. The backend owns every number.



## 2. Design decisions and their rationale

These are the choices a reader cannot infer from the component tree alone.

### 2.1 The distinctiveness lives in a token layer, not in component edits

The goal was a flat, light, editorial look. The strategy: let Radix own behaviour 
and accessibility, and put all identity in **one place** - the CSS-variable token 
layer in `src/index.css`. A few editorial utility classes live in `@layer components`:
`.eyebrow` (small uppercase section labels), `.rule-engraved` (the masthead
rule), and `.tnum` (tabular numerals for aligned figures). Changing the entire
look is a one-file edit.

### 2.2 The URL (`?cik=`) is the single source of truth

`App.tsx` does not hold the selected company in React state. Selecting a
candidate calls `setSearchParams({ cik })` -> `useFinancials(cik)` keys off the URL
param. This gives shareable/deep-linkable results (see `CopyLinkButton`) and
browser back/forward for free, and matches the README's URL-state requirement
with no extra state to keep in sync. CIK (not ticker) is the key, mirroring the
backend's stable identifier.

### 2.3 Decimal fields arrive as JSON **strings** and are coerced at the boundary

This is the single most important correctness decision, and it corrected a wrong
assumption in the original plan.

Pydantic v2 serializes `Decimal` as a JSON **string** (`"8.26"`,
`"451442000000"`), not a number. Verified against a live
`GET /api/financials/0000320193`: every monetary/ratio field
(`price`, `revenue`, `value`, the multiple `value`s, all `ev_components`, etc.)
comes over as a string, `date`/`datetime` are ISO strings as expected.

`client.ts:coerceDecimals` walks the parsed `FinancialsResponse` and converts
numeric strings back to `number`, so all downstream code keeps clean
`number | null` types (`types.ts`) and the formatters/sparkline do real math.

**Why a key denylist, not an allowlist or a generic parse:**
- A generic "convert every numeric-looking string" is unsafe - `cik_10`
  (`"0000320193"`) and `sic` (`"3571"`) are numeric-looking identifiers that
  **must stay strings**, and ISO dates must not be touched.
- A denylist (`STRING_KEYS`) of the known string-valued keys - `cik_10`, `name`,
  `ticker`, `sic`, `sic_description`, `exchange`, `code`, `message`, `concept`,
  `label`, `xbrl_tag`, `unit`, `entity_context`, and the date keys
  `filing_date`/`period_end`/`cached_at`/`data_as_of` - is the smallest, most
  robust rule: every *other* numeric string is a Decimal and gets converted.

The denylist was **audited against the real response**: the financial numeric
fields convert, the numeric-looking IDs and all dates are preserved. A unit test
(`client.test.ts`) pins both directions.

A consequence worth stating: large share counts (~1.5e10) become JS doubles.
This is exact below 2^53 and fine for display. The auditable, full-precision
values remain the backend's job.

### 2.4 No `decimal.js` on the client

The values the UI shows are *already-computed* display ratios and magnitudes. The
frontend only formats them. Adding arbitrary-precision math would be dead weight,
exact arithmetic stays server-side (`PHASE_3_SPEC.md` §2.9).

### 2.5 Cold-start awareness is a first-class concern, and 4xx is never retried

The backend runs on Render's free tier and cold-starts in 30-50s
(`README.md` -> *Cold Starts*). This is handled in three places:

- **`shouldRetry` / `coldStartDelay`** (`queries.ts`) retry server/network
  failures up to 10 times with capped exponential backoff, so a typed query
  resolves on its own once the backend wakes - the user never retypes. Any
  `ApiError` with a 4xx status returns `false` (a bad CIK, unknown filer, or IFRS
  filer will not become valid on retry). The same predicate gates the "Try again"
  button in `ErrorMessage`.
- **The landing page** polls `GET /health` (`pingHealth` -> `useServerHealth`).
  Unlike the queries above, health uses a `refetchInterval` (5s, `retry: false`)
  rather than a bounded retry budget: the interval keeps probing through errors
  and returns `false` once `/health` succeeds, so the notice flips to the chips
  on its own even after a >30s cold start - no reload. `staleTime: Infinity` so a
  warm server, once it answers, is never probed again. `useServerReady` turns
  `warming` true only after a 600ms grace period, so a warm server resolves
  before the notice would ever flash. While warming, `WarmingNotice` sets the
  expectation. Once ready, `ExampleChips` offers one-click quick-start filers.
- **`LoadingState`** shows a spinner during a fetch and, after 6s, adds a
  "server may be waking from idle" line so the wait reads as expected.

### 2.6 Errors are normalized once, in the client

FastAPI wraps the `{error, message}` body inside `detail` (and emits a different
`detail: [...]` shape for 422 validation). `client.ts:toApiError` flattens all of
these into a single typed `ApiError {status, code, message}`. `lib/errors.ts`
then maps each `ErrorCode` (plus the synthetic `http_error` for network/cold-start
failures) to friendly title/body copy. Per-period *warning* messages come straight
from the backend and are rendered inline. Components never see raw FastAPI shapes.

### 2.7 Export is a mutation, and it degrades gracefully

`useExport` fetches the export URL as a blob, so the happy path is
blob -> object URL -> `<a download>`.

### 2.8 cmdk filters server-side, sparklines are hand-built

`SearchBar` sets `shouldFilter={false}` on the cmdk `Command` - results are
already ranked by the backend's `/api/search`, so cmdk only provides keyboard
navigation and selection, not filtering. It renders in two variants (`hero`,
`compact`), `SearchDropdown` is focus-gated and `preventDefault`s mousedown so a
click registers before the input blurs. `Sparkline` is a dependency-free inline
SVG (no charting library): it scales non-null points, **breaks the line on
`null`** so missing TTM periods read as gaps rather than interpolation, and is fed
values reversed to oldest->newest (the table itself is most-recent-first).

### 2.9 Warnings: period-level under the header, multiple-level on the cell

Per-multiple warnings (e.g. the `fallback_eps_basic` label flip to "P/E (basic)")
render on the cell with a dotted underline + tooltip. Period-level warnings (e.g.
`price_unavailable`, which nulls every price-dependent multiple for that column)
render as a single "N notes" badge under the column header - attaching them to
each affected cell would be noise. Both lists are deduplicated by code via
`dedupeWarnings` (`lib/format.ts`) before rendering.

### 2.10 The audit panel is per-period and shows the EV buildup

`AuditPanel` (a Radix `Collapsible`, "Input Audit") lets the user pick which TTM
period to inspect, then shows an **Enterprise Value Buildup** `dl`
(market cap + debt/lease/minority/preferred − cash = enterprise value, from
`ev_components`) above a per-concept table: which XBRL tag fired, whether it was a
`fallback`, its unit, entity context, and the value used. This is what makes
every EV/* multiple traceable to reported figures.

### 2.11 A plain-language `/learn` glossary is a second route

`main.tsx` mounts a `/learn` route (`Learn.tsx`) alongside `/`. It is a static,
plain-English glossary for readers new to the terms the tool surfaces: what
OpenValuation is, cold starts, TTM, what a valuation multiple is, enterprise
value, each of the seven specific multiples, the inline notes / dashed cells, and
where the data comes from. It is reached from two places - the cold-start
`WarmingNotice` ("Learn more") and the results-view footer ("Learn More") - and
deep-links directly (a shared link or new tab). Its "Back" button pops browser
history, falling back to `/` when `/learn` was opened cold with no history to
return to. It reuses the token layer and the vendored `Button`. It adds no
new styling primitives.



## 3. The Excel workbook (backend)

`services/workbook.py` (openpyxl) turns a computed `FinancialsResponse` into a
downloadable `.xlsx`, and `routers/export.py` streams it. The financial semantics
the workbook reproduces are authoritative in `README.md` / `services/multiples.py`
- this section records only the choices the code alone does not explain.

### 3.1 Every result is a live formula, never a value

The workbook is the audit trail **and** a scratchpad. No multiple, market cap, or
enterprise value is written as a number - each is an Excel formula referencing the
reported input cells. Edit any input and that period's figures recompute on the
spot. This is the single decision the whole layout serves.

### 3.2 One Summary sheet plus one sheet per TTM period

`Summary` carries company metadata, a color-coding key, and a seven-multiple
matrix with one column per period. Every matrix cell is a cross-sheet reference
into the period it summarizes, and each column header is a `HYPERLINK` into that
period's sheet, so a reader can drill from any figure straight to its buildup
rather than facing a wall of numbers.

Each period sheet stacks four blocks: **Inputs** (every reported XBRL value with
its tag, fallback flag, and unit), **Calculations** (Market Cap and the
Enterprise Value buildup as formulas over the Inputs), **Multiples** (the seven
ratios as formulas), and **Notes** (the period's data-quality warnings).

### 3.3 The formulas reproduce the backend guards

Each multiple formula mirrors `services/multiples.py`: the missing-input checks,
the near-zero denominator guard (`ABS(...) < 0.01`), and the negative-book-value /
negative-FCF guards. When a guard fires the formula yields `"-"`, the same N/A the
backend returns, so an edited workbook never disagrees with the API it came from.
A missing input is written as a **blank** cell (not `0`, not `"-"`) so it reads as
an editable-to-recompute input, and the buildup formulas treat a blank as zero.

### 3.4 Dollar figures are scaled to thousands, per-share figures are not

Every dollar figure is written "USD, in thousands (except per share)". Price and
EPS stay unscaled. Multiples need no adjustment - each is a ratio of two
thousands-scaled figures, so the scale cancels. Scaling keeps large balance-sheet
lines readable without a trail of zeros.

### 3.5 Color coding makes provenance visible

Blue is an editable input, black is a formula, green is a cross-sheet link, and a
fallback tag is amber - the same amber the frontend uses for data-quality
warnings (§2.1). A reader can tell at a glance which cells are safe to edit and
which are derived.

### 3.6 The export reuses the financials cache-or-fetch path

`export.py` calls `resolve_financials`, the same orchestration behind
`GET /api/financials`, so a downloaded workbook reflects exactly what the results
table shows, shares the 24h cache, and surfaces EDGAR / extraction failures as the
identical structured `HTTPException`s. The route streams the bytes with an
`attachment` disposition named `openvaluation_{cik_10}.xlsx`.

### 3.7 Sheet names are calendar quarters, de-duplicated to Excel's limit

A period sheet is named for the calendar quarter of its period end (e.g.
`Q3 2024`), truncated to Excel's 31-character cap and suffixed `(2)`, `(3)`... on
the rare collision. Price carries no audit entry, so its Inputs row shows the
source as `yfinance (next trading day adj. close)` with unit `USD/share`.

### 3.8 Tests pin the formula contract structurally

openpyxl does not evaluate formulas, so `tests/test_workbook.py` cannot assert
computed numbers. It pins the contract that matters instead - Summary first then
one sheet per period, every multiple an `IF`-guarded formula rather than a bare
number, the Summary matrix holding live cross-sheet references, a missing input
left blank, and a fallback flagged. That the formulas reproduce the backend guards
(§3.3) is verified in review. `tests/test_api_errors.py` covers the endpoint
end-to-end - a real fixture through `resolve_financials` to a loadable `.xlsx`.



## 4. API contract consumed

`types.ts` mirrors, field for field: `CompanyCandidate`/`SearchResponse`,
`CompanyMeta` (incl. `sic_description`, `is_financial`, `is_capital_intensive`),
`AuditEntry`, `ExtractedFinancials`, `MultipleValue`/`MultipleSet`
(keys `ev_revenue, ev_ebitda, ev_ebit, pe, pfcf, ps, pb`), `EVComponents`,
`TTMPeriod`, `FinancialsResponse`, and the `WarningCode`/`ErrorCode` string
unions. `MULTIPLE_KEYS` fixes the canonical row order.

Endpoints used: `POST /api/search`, `GET /api/financials/{cik_10}`,
`GET /api/export/{cik_10}` (returns the workbook from §3, still 501-tolerant on the
client), and `GET /health` (cold-start probe, §2.5).

Dev wiring: `vite.config.ts` proxies `/api` -> `http://localhost:8000`, so there
is no CORS in development and `VITE_API_BASE` stays empty. In production it is set
to the Render URL and the Vercel origin is added to the backend `ALLOWED_ORIGINS`.
`vercel.json` rewrites all paths to `index.html` for SPA routing.



## 5. Test strategy

The frontend has three layers, all runnable offline (no live backend). The Excel
backend's tests are covered in §3.8.

### 5.1 Unit (Vitest, pure functions)

`lib/format` (multiple/currency/share/date formatting, N/A handling, warning dedup),
`lib/errors` (error code -> friendly copy), `Sparkline` (single polyline, gap
segmentation around nulls, dashed baseline under two points), and
`client.coerceDecimals` (string->number conversion **and** the
identifier/date-preservation guarantee from §2.3).

### 5.2 Component (Vitest + React Testing Library + MSW)

MSW (`src/test/handlers.ts`) serves type-accurate fixtures
(`src/test/fixtures.ts`) so components run against real response shapes:
`ResultsTable` (seven rows, `x` formatting, N/A dash, per-period notes badge,
empty state), `SearchBar` (debounced query -> candidate list -> selection
callback), `ErrorMessage` (4xx hides retry, 5xx offers it).

**jsdom polyfills:** cmdk/Radix need `ResizeObserver` and
`Element.prototype.scrollIntoView`, which jsdom lacks. Both are stubbed in
`src/test/setup.ts`.

### 5.3 E2E (Playwright)

`e2e/app.spec.ts` runs against `vite preview` with the API mocked via
`page.route` (no backend needed): full search -> select -> table -> audit-expand
-> copy-link path, and the `?cik=` deep-link auto-load path. Browsers must be
installed once (`npx playwright install`).

### 5.4 Fixtures are hand-crafted, not generated

The fixtures are trimmed, hand-written responses tuned to the cases the UI must
handle (full data, an N/A multiple, the basic-EPS label flip, per-period
warnings). They are **not** generated from real companyfacts, because the
price-dependent values cannot be reproduced offline without yfinance. They are
kept structurally faithful to `types.ts`.



## 6. Known limitations (accepted in Phase 4)

1. **The workbook ships formulas, not cached values.** openpyxl writes formula
   strings with no cached result, so a tool that reads the file without
   recalculating (`load_workbook(..., data_only=True)`, some previewers) sees
   blanks. Excel, LibreOffice, and Google Sheets recalculate on open, which is the
   intended path and where §3.1's live-edit behaviour lives.
2. **No per-cell tooltip for period-level N/As.** When `price_unavailable` nulls
   a whole column, the cells show a plain N/A dash - the explanation lives in the
   column header's "notes" badge (§2.10), not on each cell.
3. **Light theme only.** No dark tokens are defined. The token layer is the
   single point where a dark set could later be added.
4. **Share counts as JS doubles** (§2.4) - exact for display, not a precision
   substitute for the backend/Excel values.
5. **Verification ran with yfinance blocked.** In the sandbox the live backend
   returned `price_unavailable` and null price-based multiples. The *shape* and the
   non-price numerics were validated end-to-end, but a fully-populated live render
   was not observable here.



## 7. What remains / what the next phase consumes

Phase 4 is complete - the frontend (§2) and the Excel export backend (§3) both
ship.

**Stable surfaces this phase establishes** for any follow-on work:

- `frontend/src/api/types.ts` as the single typed mirror of the Pydantic contract
  - update it in lockstep with `app/models/*`.
- `client.coerceDecimals` as the one place that knows Decimals are strings - any
  new numeric field is handled automatically unless its key is a string
  identifier, in which case add it to `STRING_KEYS`.
- The token layer in `src/index.css` as the single styling control point.

To run the frontend in isolation: `cd frontend && npm install && npm run dev`
(backend on `:8000`). Quality gates: `npm run typecheck`, `npm run lint`,
`npm run test`, `npm run test:e2e`.
