# PHASE_4_SPEC.md

**Scope:** Frontend and Excel Export  
**Status:** Complete  
**Implemented in:** `frontend/`, `backend/app/services/workbook.py`, `backend/app/routers/export.py`

## What this document is

The decision record for the React frontend and the Excel export. It captures the handful of choices a reader cannot infer from the component tree or the workbook code.

It does not restate what is authoritative elsewhere:

| You want... | Read instead |
|---|---|
| User-facing behavior, the formulas, warning semantics, the workbook's contents | `README.md`, `DESIGN.md` |
| Response shapes and enums | `app/models/*.py`, mirrored in `frontend/src/api/types.ts` |
| Component inventory, scripts, dev wiring | `README.md`, `frontend/package.json`, `vite.config.ts` |

The Pydantic models are authoritative. `types.ts` is a hand-maintained mirror and must be updated in lockstep.

## 1. Decimal fields arrive as JSON strings and are coerced at the boundary

This is the one correctness decision in the phase, and it corrected a wrong assumption in the original plan. Pydantic v2 serializes `Decimal` as a JSON **string** (`"8.26"`, `"451442000000"`), not a number. Verified against a live `GET /api/financials/0000320193`: every monetary and ratio field comes over as a string, dates are ISO strings as expected.

`client.ts:coerceDecimals` walks the parsed response and converts numeric strings back to `number`, so all downstream code keeps clean `number | null` types and the formatters and sparkline do real math.

**Why a denylist of string keys, not an allowlist.** Converting every numeric-looking string is unsafe: `cik_10` (`"0000320193"`) and `sic` (`"3571"`) are numeric-looking identifiers that must stay strings, and ISO dates must not be touched. An allowlist of numeric keys would silently miss any field added later. The denylist (`STRING_KEYS`, covering the identifiers, the labels, and the four date keys) is the smallest robust rule: every other numeric string is a Decimal. Any new numeric field is handled for free, and a new string field just needs adding to `STRING_KEYS`. `client.test.ts` pins both directions, conversion and preservation.

Large share counts (~1.5e10) become JS doubles. That is exact below 2^53 and fine for display. Full-precision values stay the backend's and the workbook's job, which is also why there is no `decimal.js` on the client.

## 2. The URL is the only place the selected company lives

`App.tsx` holds no selection state. Selecting a candidate calls `setSearchParams({ cik })` and `useFinancials(cik)` keys off the param. Shareable links and browser back/forward come free with nothing to keep in sync. The key is CIK rather than ticker, mirroring the backend's stable identifier.

## 3. Cold starts are a first-class state, and 4xx is never retried

The backend cold-starts in 30-50s on Render's free tier.

- `shouldRetry` retries server and network failures up to 10 times, with `coldStartDelay` backing off as `3^attempt` capped at 6s, so a query resolves on its own once the backend wakes and the user never retypes. Any `ApiError` with a 4xx status returns `false`, since a bad CIK or an IFRS filer will not become valid on retry. The same predicate gates the "Try again" button.
- The landing page polls `/health` on a 5s `refetchInterval` with `retry: false` rather than a retry budget, because an interval keeps probing through errors and flips the notice to the ready state on its own after an arbitrarily long wake. The interval turns itself off once data arrives, `staleTime: Infinity` means a warm server is never probed again, and `useServerReady` applies a 600ms grace period so a warm server resolves before the notice could flash.
- `LoadingState` adds a "server may be waking from idle" line after 6s, so a long wait reads as expected rather than broken.

## 4. Errors are normalized once, in the client

FastAPI wraps `{error, message}` inside `detail` and uses a different `detail: [...]` shape for 422 validation. `client.ts:toApiError` flattens all of these into one typed `ApiError`, and `lib/errors.ts` maps each code to friendly copy, including a synthetic `http_error` for network and cold-start failures. Components never see raw FastAPI shapes. Per-period warnings come through as-is from the backend.

## 5. Presentation choices worth recording

- **Identity lives in the token layer.** Radix owns behavior and accessibility, `src/index.css` owns every visual decision. The only editorial utilities are `.eyebrow`, `.rule-engraved`, and `.tnum` for tabular figures. Changing the whole look is a one-file edit.
- **cmdk does not filter.** `shouldFilter={false}`, because results are already ranked by `/api/search`. cmdk provides keyboard navigation only.
- **The sparkline breaks the line on `null`** rather than interpolating, so a missing TTM period reads as a gap instead of an invented trend. It is dependency-free inline SVG and takes values oldest to newest, the reverse of the table's order.
- **Warnings attach at two levels.** Per-multiple warnings render on the cell, period-level warnings (like `price_unavailable`, which nulls a whole column) render as one notes badge under the column header. Attaching a column-wide warning to every affected cell would be noise.
- **The audit panel leads with the EV buildup.** Picking a period shows market cap plus debt, leases, minority interest, and preferred stock less cash, above the per-concept table of which tag fired, whether it was a fallback, and the value used. That ordering is what makes every EV multiple traceable to reported figures.
- **`/learn` is a real route, not a modal.** The plain-language glossary deep-links and opens in a new tab, reached from the cold-start notice and the results footer. Its Back button pops history and falls back to `/` when the page was opened cold with nothing to return to.

## 6. The workbook ships formulas, never values

No multiple, market cap, or enterprise value is written as a number. Each is an Excel formula referencing the reported input cells, so editing any input recomputes that period on the spot. The workbook is an audit trail and a scratchpad at once, and this single decision drives the whole layout: Summary plus one sheet per period, each period sheet stacking Inputs, Calculations, Multiples, and Notes, with Summary cells as cross-sheet references into the period they summarize. Each Summary column header is a `HYPERLINK` into that period's sheet, so a reader drills from a figure to its buildup instead of facing a wall of numbers.

Consequence: openpyxl writes formula strings with no cached result, so a tool that reads the file without recalculating (`data_only=True`, some previewers) sees blanks. Excel, LibreOffice, and Sheets all recalculate on open, which is the intended path.

### 6.1 The formulas reproduce the backend guards

Each formula mirrors `multiples.py`, including the missing-input checks, the `ABS(...) < 0.01` denominator guard, and the negative book value and negative FCF guards. A fired guard yields `"-"`, the same N/A the backend returns, so an edited workbook never disagrees with the API it came from. A missing input is written as a **blank** cell, not `0` and not `"-"`, so it reads as editable-to-recompute, and the buildup formulas treat blank as zero.

### 6.2 Dollar figures are scaled to thousands, per-share figures are not

Price and EPS stay unscaled. Multiples need no adjustment because each is a ratio of two thousands-scaled figures and the scale cancels. This keeps balance-sheet lines readable without a trail of zeros.

### 6.3 Color encodes provenance

A reader can tell at a glance which cells are safe to edit.

| Color | Meaning |
|---|---|
| Blue | Editable input |
| Black | Formula |
| Green | Cross-sheet link |
| Amber | Fallback tag |

### 6.4 Sheet names are the period label, bounded by Excel's limit

A period sheet takes the period's display label (`Q1 2026`, or `Q3 FY23` for an off-calendar filer), truncated to Excel's 31-character cap and suffixed `(2)`, `(3)` on the rare collision. Price has no audit entry, so its Inputs row names its source as `yfinance (next trading day adj. close)` in `USD/share`.

### 6.5 Export reuses the financials cache-or-fetch path

`export.py` calls `resolve_financials`, the same orchestration behind `GET /api/financials`, so a downloaded workbook matches the results table exactly, shares the 24h cache, and surfaces failures as the identical structured `HTTPException`s.

## 7. Test layering

Everything runs offline, with no live backend.

- **Unit (Vitest):** `lib/format`, `lib/errors`, `Sparkline` gap segmentation, and `coerceDecimals` in both directions.
- **Component (Vitest, RTL, MSW):** MSW serves type-accurate fixtures so components run against real response shapes. The jsdom gotcha is that cmdk and Radix need `ResizeObserver` and `Element.prototype.scrollIntoView`, neither of which jsdom provides. Both are stubbed in `src/test/setup.ts`.
- **E2E (Playwright):** `vite preview` with the API mocked via `page.route`, covering search to select to table to audit-expand to copy-link, plus the `?cik=` deep-link path.
- **Workbook:** openpyxl does not evaluate formulas, so `test_workbook.py` cannot assert computed numbers. It pins the contract instead: Summary first then one sheet per period, every multiple an `IF`-guarded formula rather than a bare number, the Summary matrix holding live cross-sheet references, a missing input left blank, and a fallback flagged. That the formulas match the backend guards (§6.1) is verified by review, not by test.

## 8. Known limitations (accepted)

1. **Light theme only.** No dark tokens are defined. The token layer is the single point where a dark set would go.
2. **No per-cell tooltip for period-level N/As.** When a whole column is nulled, the explanation lives in the header's notes badge, not on each cell.
3. **Verification ran with yfinance blocked.** The sandbox returned `price_unavailable`, so shape and non-price numerics were validated end to end but a fully-populated live render was not observable there.
4. **Fixtures are hand-written, not generated.** Price-dependent values cannot be reproduced offline without yfinance, so the test fixtures are trimmed hand-authored responses kept structurally faithful to `types.ts`.
