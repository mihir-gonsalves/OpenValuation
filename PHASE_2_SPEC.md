# PHASE_2_SPEC.md - XBRL Extraction & TTM Bridge

**Phase:** 2  **Status:** Complete  **Implemented in:** `backend/`

## What this document is

This is the **decision record** for Phase 2 (XBRL extraction). It captures the
non-obvious choices made while building `services/xbrl.py`, `xbrl_maps.py`, and
`xbrl_warnings.py`, the limitations those choices accept, and the fixture
knowledge behind the test suite.

It deliberately does **not** restate what is already authoritative elsewhere:

| You want... | Read instead |
|---|---|
| Financial semantics (tag chains, EV/EBITDA/FCF formulas, dedup of `LongTermDebt`, fallback warnings) | `README.md` -> *XBRL Tags Used*, *Multiple Calculation*; `DESIGN.md` -> *XBRL Extraction*, *Enterprise Value* |
| The mechanics of the algorithms (bridge math, map building, instant lookup, anchor discovery) | Module + function docstrings in `services/xbrl.py` and `services/xbrl_maps.py` |
| Exact API shapes, the `WarningCode`/`ErrorCode` enums, cache/timeout contracts | `PHASE_1_SPEC.md` |
| Field-by-field types of `ExtractedFinancials` / `AuditEntry` | `app/models/financials.py` (every field is documented inline) |

The bridge formula itself - `TTM = PriorFY_Annual + CurrentYTD − PriorYTD_SamePeriod`
- Is documented at the top of `xbrl.py` and in `xbrl_maps.py`. 
- This doc explains *why the surrounding decisions are the way they are*, not what the formula is.



## 1. Scope and entry point

Phase 2 turns one EDGAR `companyfacts` dict into up to 12 `ExtractedFinancials`
(one per TTM period, most-recent-first) plus a per-period audit trail and
warnings. The public surface Phase 3 and the router depend on is exactly:

```python
async def extract_ttm_periods(
    companyfacts: dict,
    *,
    ticker: str | None = None,
    is_capital_intensive: bool = False,
) -> list[ExtractedFinancials]:
```

This signature and the `ExtractedFinancials` schema are the **stable contract**.
Everything else in the three files is private (`_`-prefixed) and may change
without affecting downstream phases. See §7.



## 2. Why three files

The extraction layer was split from one module into three along a single axis:
**knowledge of XBRL tags**.

- `xbrl.py` is the only file that knows a tag string means a financial concept.
- `xbrl_maps.py` is pure data machinery (dedup, map building, the bridge, instant
  lookup) and is completely tag-agnostic - it could process any `{period: value}`
  fact stream.
- `xbrl_warnings.py` isolates warning *wording and aggregation* so message changes
  happen in one place and don't churn the extraction logic.

The payoff is testability: the bulk of the tricky logic (the bridge, dedup,
tolerance windows) lives in `xbrl_maps.py` and is unit-testable on synthetic fact
lists with no tag knowledge and no fixtures. `xbrl.py`'s own tests can then focus
on tag selection and warning attachment.



## 3. Design decisions and their rationale

These are the choices a reader cannot infer from the formulas alone.

### 3.1 Anchor discovery unions *all* sentinel tags

`_collect_filing_anchors` scans every tag in `_ANCHOR_DISCOVERY_TAGS` (the revenue
chain plus `OperatingIncomeLoss` and `NetIncomeLoss`) and unions their accession
numbers, rather than stopping at the first tag that has facts.

**Why:** a company that switched its primary revenue tag mid-history (e.g. on ASC
606 adoption) has old filings discoverable only via the old tag and new filings
only via the new one. A first-hit scan would silently drop half the filing history
and return fewer than 12 periods. The union guarantees the complete window.

**Invariant worth preserving:** the discovery list is *built from*
`_FLOW_CHAINS["revenue"]`, so any tag accepted as revenue is automatically a valid
anchor source. Add a revenue tag and anchor discovery picks it up for free - don't
hardcode a parallel list.

### 3.2 Maps are pre-computed once, with two lookup indexes

`extract_ttm_periods` builds every fact map a single time (`_precompute_flow_maps`
/ `_precompute_instant_maps`) and reuses them across all 12 anchors. Each
`_FlowEntry` additionally carries an `annual_index` (`{end: (fy_start, value)}`)
and a `prior_ytd_index` (`{fy_start: [(duration, value)]}`).

**Why:** the naive shape is O(anchors × tags × facts) with a linear scan inside
the bridge on every call. A 30-year filer can exceed 1,000 facts per tag.
Pre-building collapses this to O(tags × facts) once, and the two indexes turn the
bridge's two prior-year lookups into an O(1) dict hit and an O(k<=4) scan. This is
the single most consequential performance decision in the phase.

### 3.3 Flow matching is exact, only durations get a tolerance

The bridge looks up its components by *exact* `(start, end)` keys. The only
tolerances anywhere are:

- Prior-YTD **duration** +/-4 days (`_PRIOR_YTD_TOLERANCE_DAYS`): absorbs the
  one-day drift a leap day or a Feb-1 fiscal start introduces between a YTD and
  its prior-year mirror.
- Annual-fact **duration** 350–380 days (`_MIN/_MAX_ANNUAL_DAYS`): a fact is
  "annual" by length, not by form type, which is what lets a 10-Q's embedded
  full-year comparative serve as `PriorFY_Annual`.
- Instant **date** +/-7 days (`_INSTANT_DATE_TOLERANCE_DAYS`): balance-sheet dates
  occasionally land a day or two off the expected quarter-end across filings.

**Why exact elsewhere:** the project's first principle is *no silent wrong value*.
If a required flow fact isn't at exactly the key the bridge needs, returning `None`
(-> `N/A`) is correct, combining a near-miss fact would fabricate a number. This is
why `WarningCode.PERIOD_MISMATCH` exists in the model but is **never raised** in
Phase 2 - the exact-match design makes the mismatch unrepresentable rather than
something to warn about. (The `>3 days` rejection language in `DESIGN.md`/`PROJECT_STATUS.md`
describes the intent, the implementation realizes it as exact-match + None.)

### 3.4 Annualization is the deliberate degradation path

When `PriorFY_Annual` or `PriorYTD` is missing (recent IPO, fiscal-year change),
the bridge degrades to `CurrentYTD / quarters_elapsed × 4` and attaches
`ttm_annualized` rather than returning `None`. A missing `CurrentYTD`, by contrast,
returns `None` - there is nothing to extrapolate from. YTDs shorter than
`_MIN_ANNUALIZATION_DAYS` (40) also return `None`: too little signal to annualize.

### 3.5 Ambiguity uses first-tag-wins, and is checked across the tolerance window

When dedup finds conflicting non-amendment values for a key, the value is dropped
(`None`) and the key is recorded as ambiguous. The resolvers surface a single
`ambiguous_fact` warning but keep walking the fallback chain - a later tag may
yield a clean value. Only the **primary** tag's ambiguity is reported if the whole
chain fails (`pending_ambiguous`, first-wins), so the message names the tag the
user expects. For instants, the ambiguity check (`_ambiguous_near`) mirrors the
same +/-7 day window as the value lookup, so an ambiguous fact three days off
period-end still produces a warning instead of silently reading as "absent."

### 3.6 EPS keeps a stable audit concept name

The audit entry for EPS is always `concept="EPS"`, never `"EPS (Diluted)"`, even
when diluted fired. The tag actually used is recorded in `xbrl_tag`, and
`is_fallback` + the `fallback_eps_basic` warning disambiguate the basic path.
Labeling the row "EPS (Diluted)" when basic was used would be an outright false
statement in the audit panel. A stable neutral label can't lie. The `P/E` ->
`P/E (basic)` label change is Phase 3's job and is read off `is_fallback`.

### 3.7 DEI shares are matched by accession, GAAP shares by date

GAAP `CommonStockSharesOutstanding` is an instant fact whose `end` is the true
period-end, so it's matched by date (+/-7 days) through the deduplicated instant map.
- Which means the normal dedup rules (originals over amendments, conflict -> None)
protect it. DEI `EntityCommonStockSharesOutstanding` uses the filing's *report
date* (weeks after period-end) as its `end`, so it cannot be date-matched - it's
matched by the anchor's accession number instead. GAAP is preferred, DEI is the
fallback for filers that stopped (or never started) tagging GAAP shares.

### 3.8 CapEx sign is normalized at extraction, not computation

`PaymentsToAcquirePropertyPlantAndEquipment` is occasionally reported as a negative
outflow. `_extract_capex` takes `abs()` and warns (`capex_sign_normalized`) so the
stored value is always a non-negative outflow and downstream FCF math (`OCF − CapEx`)
never has to second-guess the sign.



## 4. Known limitations (accepted in Phase 2)

1. **Amendment check covers only the current-YTD key.** `amendment_exists` is
   tested against `(fiscal_year_start, period_end)`. The two prior-year bridge
   components are accepted even if their source was an amendment. Closing this
   needs `_get_ttm_value` to return the keys it consulted - deferred. (Documented
   at the source in `xbrl_warnings._make_flow_warnings`.)
2. **`entity_context` is hardcoded `"consolidated"`.** No context-ID parsing.
   Dedup favors the authoritative (consolidated) filing in practice. See the note
   on `AuditEntry.entity_context` in `app/models/financials.py`.
3. **`PERIOD_MISMATCH` is structurally unreachable** by design - see §3.3.
4. **Amendment-only anchors are skipped.** `_collect_filing_anchors` filters out
   accession numbers whose form type ends in `/A`. If a company's most recent
   quarter was filed exclusively as a `10-Q/A` (no original `10-Q`), that quarter
   is excluded from the anchor list and the returned window will be one quarter
   older than expected.



## 5. Warning ownership (Phase 2 vs Phase 3)

The `WarningCode` enum is defined and documented in `PHASE_1_SPEC.md` §2.4 - not
restated here. What's Phase-2-specific is *which codes this phase actually emits*:

**Raised by Phase 2 extraction:**
`ttm_annualized`, `fallback_revenue`, `fallback_eps_basic`, `debt_deduplicated`,
`cash_fallback_includes_investments`, `capex_sign_normalized`, `lease_pre_asc842`,
`finance_lease_missing_capital_intensive`, `price_unavailable`, `amendment_exists`,
`ambiguous_fact`.

**Defined but reserved for Phase 3** (multiples engine):
`ev_debt_missing`, `denominator_near_zero`, `negative_book_value`.

**Defined but never raised** (see §3.3): `period_mismatch`.

Per-code dedup is applied once per period at the end of `extract_ttm_periods`
(`_dedup_warnings`). `ttm_annualized` and `amendment_exists` aggregate their
affected concept names into one message. Mechanics are in `xbrl_warnings.py`.



## 6. Test strategy and fixture knowledge

This is the part of Phase 2 that exists in no docstring: **why each real-data
fixture was chosen and what edge case it pins down.** Fixtures live in
`tests/fixtures/{ticker}_CIK{cik_10}.json` and are loaded directly (no HTTP mock)
so tests validate against real EDGAR payloads.

Unit-level logic (`_build_flow_map`, `_get_ttm_value`, `_find_annual_fact`,
`_find_prior_ytd`, `_annualize`, `_get_instant_result`) is covered by synthetic
fact lists - no fixture needed. The fixtures exist to prove the tag-selection and
bridge logic against messy reality.

### 6.1 Fixture rationale

| Fixture | What it uniquely exercises |
|---|---|
| **CRCT** (Cricut) | Plain Dec-31 FY, GAAP shares, positive EPS, no debt/CapEx - the clean baseline. |
| **SNOW** (Snowflake) | Non-calendar FY (Feb->Jan), **negative** EPS, **DEI-only** shares. |
| **CART** (Instacart) | Post-IPO (Sept 2023) - short history stresses the 12-period window, GAAP + DEI shares. |
| **TGT** (Target) | `LongTermDebtNoncurrent` absent -> forces the `LongTermDebt` total fallback + `debt_deduplicated`, with `current_portion` correctly zeroed, `cash` fallback (`cash_fallback_includes_investments`), leases tagged at **annual dates only**. |
| **DAL** (Delta) | Capital-intensive SIC (4512), `LongTermDebtNoncurrent` present at *every* period so the noncurrent-primary path fires and current portion is **kept**, proves the capital-intensive lease warning is **per-period** (fires at Q1 where leases are absent, suppressed at FY where they're present). |
| **BRKB** (Berkshire) | `MinorityInterest`, primary revenue tag deliberately narrower than the `Revenues` fallback (proves primary scope wins), **cross-filing** prior-YTD (Q3's prior-YTD comes from the *previous year's* Q3 filing, validating the global fact map), no cash/debt/shares in recent periods. |
| **AAPL** (Apple) | Sep FY with irregular quarter-ends, full **D&A bridge** (`DepreciationDepletionAndAmortization`), noncurrent-debt-primary with current portion kept, manufacturing-SIC capital-intensive lease warning (distinct from DAL's transport SIC). |
| **MSFT** (Microsoft) | Jun FY, **both D&A tags absent** (the only fixture where `da = None` everywhere), `ShortTermBorrowings` tag exists but has no recent fact (tests "stale tag -> None"), both cash tags present simultaneously so the **primary must win** over a fallback that's ~2.4× larger - the most consequential cash-correctness test. |

### 6.2 Hand-computed bridge oracles

These are the arithmetic checks the integration tests assert (values ×$1, unless a
unit is shown). They double as regression oracles - if extraction drifts, these
break first.

```
CRCT  Q3 2025   Rev  714,492,000 = 712,538,000 + 505,183,000 − 503,229,000
CRCT  Q1 2026   Rev  705,617,000 = 708,780,000 + 159,471,000 − 162,634,000
CRCT  Q1 2026   OCF  165,917,000 = 200,230,000 +  26,853,000 −  61,166,000
CRCT  Q1 2026   EPS         0.34 =        0.35 +        0.10 −        0.11

CART  Q1 2026   Rev  3,864,000,000 = 3,742,000,000 + 1,019,000,000 − 897,000,000
CART  Q1 2026   OCF    941,000,000 =   971,000,000 +   268,000,000 − 298,000,000

SNOW  Q3 FY26   Rev  4,386,722,000 = 3,626,396,000 + 3,399,952,000 − 2,639,626,000
SNOW  FY 2026   EPS  −3.95 (annual direct)

TGT   Q3 FY26   Rev  105,242 = 106,566 + 74,327 − 75,651            (×10⁶)
DAL   Q1 2026   Rev   65,178 =  63,364 + 15,854 − 14,040            (×10⁶)
BRKB  Q3 2025   Rev  247,366 = 249,714 + 184,510 − 186,858 (×10⁶, prior-YTD cross-filing)

AAPL  Q2 FY26   Rev  451,442 = 416,161 + 254,940 − 219,659         (×10⁶)
AAPL  Q2 FY26   OCF  140,222 = 111,482 +  82,627 −  53,887         (×10⁶)
AAPL  Q2 FY26   D&A   12,610 =  11,698 +   6,653 −   5,741         (×10⁶)
AAPL  Q2 FY26   EPS     8.26 =    7.46 +    4.85 −    4.05

MSFT  Q3 FY26   Rev  318,273 = 281,724 + 241,832 − 205,283         (×10⁶)
MSFT  Q3 FY26   OCF  170,141 = 136,162 + 127,494 −  93,515         (×10⁶)
MSFT  Q3 FY26   CapEx 97,225 =  64,551 +  80,146 −  47,472         (×10⁶)
MSFT  Q3 FY26   EPS    16.79 =   13.64 +   13.14 −    9.99
```

Anchor-discovery and balance-sheet point-in-time assertions (e.g. SNOW most-recent
anchor `2026-01-31` / fy-start `2025-02-01`; CRCT Q1 2026 GAAP shares 209,897,286)
are asserted in the integration tests directly, they are not reproduced here.

### 6.3 End-to-end (price mocked)

The async tests verify ordering (most-recent-first), `filing_date` population,
`price` set from the mock when `ticker` is given and `None` (with
`price_unavailable` on every period) when the mock returns nothing, that
`ticker=None` produces no price fetch and no warning, that
`is_capital_intensive=True` with no lease tags raises
`finance_lease_missing_capital_intensive` on every period, that the audit trail has
one entry per concept, and that empty companyfacts -> `[]`.



## 7. Completeness assessment

Every extractable concept and code path maps to at least one fixture (real-data)
or a synthetic unit test. The paths with **no** real-data fixture, and why that's
acceptable, are:

| Path | Coverage | Risk |
|---|---|---|
| `fallback_revenue` warning | synthetic | very low - one `if is_fallback` branch |
| `capex_sign_normalized` | synthetic | low - unconditional `abs()` once `val < 0` |
| Pre-ASC 842 `CapitalLeaseObligations*` | synthetic | low - identical lookup to the finance-lease tags |
| `PreferredStock` | `_INSTANT_CHAINS` only | negligible - structurally identical to `MinorityInterest` (proven by BRKB) |

All four are short, unconditional, and structurally analogous to already-proven
paths. Sourcing more real fixtures for them adds rate-limit exposure for near-zero
incremental confidence - **do not source additional fixtures** for these.

Coverage that *is* pinned by real data: all monetary `ExtractedFinancials` fields,
five distinct fiscal-year-end patterns (Dec, Jan-Feb, Sep, Jun, financial-sector
irregular), all three debt-resolution paths (noncurrent-primary / total-fallback /
absent), all cash-resolution paths (primary / primary-wins-over-fallback /
fallback-only / absent), and the bridge at Q1/Q2/Q3/FY with hand-computed oracles.



## 8. What Phase 3 consumes

Phase 3 (`services/multiples.py`) depends only on:

- The `extract_ttm_periods` signature (§1) and the `ExtractedFinancials` schema
  (`app/models/financials.py`), both stable.
- The router contract for merging multiples warnings into `TTMPeriod.warnings`,
  specified in `PHASE_1_SPEC.md` §6.2 and already wired in
  `app/routers/financials.py` (the `_MULTIPLE_FIELDS` loop).

Internal restructuring of the extraction layer (including the `xbrl_maps.py` split)
does not touch that contract. If Phase 3's multiples surface an extraction bug,
fix it behind the stable interface - the schema and entry-point signature should
not need to change.

To run Phase 3 in isolation, reshare all eight fixtures (CRCT, SNOW, CART, TGT,
DAL, BRKB, AAPL, MSFT) plus `xbrl.py`, `xbrl_maps.py`, `multiples.py`, and the
`financials`/`errors` models.
