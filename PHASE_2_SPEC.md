# PHASE_2_SPEC.md

**Scope:** XBRL Extraction and the TTM Bridge  
**Status:** Complete  
**Implemented in:** `backend/app/services/xbrl*.py`

## What this document is

The decision record for XBRL extraction. It captures the non-obvious choices in `xbrl.py`, `xbrl_maps.py`, and `xbrl_warnings.py`, the limitations they accept, and the fixture knowledge behind the test suite.

It does not restate what is authoritative elsewhere:

| You want... | Read instead |
|---|---|
| Tag chains, formulas, debt dedup, fallback semantics | `README.md`, `DESIGN.md` |
| Bridge math, map building, instant lookup, anchor discovery | Module and function docstrings in `xbrl.py`, `xbrl_maps.py` |
| Field types of `ExtractedFinancials` / `AuditEntry` | `app/models/financials.py` |

The stable contract downstream depends on is `extract_ttm_periods(companyfacts, *, ticker, is_capital_intensive) -> list[ExtractedFinancials]` and the `ExtractedFinancials` schema. Everything else in the three files is private.

## 1. The three-file split is along one axis: knowledge of XBRL tags

`xbrl.py` is the only file that knows a tag string means a financial concept. `xbrl_maps.py` is tag-agnostic data machinery and could process any `{period: value}` fact stream. `xbrl_warnings.py` isolates wording and aggregation so message changes do not churn extraction logic.

The payoff is testability. The tricky logic (bridge, dedup, tolerance windows) lives in `xbrl_maps.py` and is unit-testable on synthetic fact lists with no fixtures, which lets `xbrl.py`'s own tests focus on tag selection.

## 2. Design decisions

### 2.1 Anchor discovery unions all sentinel tags

`_collect_filing_anchors` scans every tag in `_ANCHOR_DISCOVERY_TAGS` (the revenue chain plus `OperatingIncomeLoss` and `NetIncomeLoss`) and unions their accession numbers rather than stopping at the first tag with facts. 

**Why:** a company that switched its primary revenue tag mid-history (ASC 606 adoption, for example) has old filings discoverable only via the old tag and new filings only via the new one. A first-hit scan would silently drop half the history and return fewer than 12 periods.

**Invariant worth preserving:** the discovery list is built from `_FLOW_CHAINS["revenue"]`, so any tag accepted as revenue is automatically a valid anchor source. Do not hardcode a parallel list.

### 2.2 An anchor's fiscal year start is derived, its fiscal year label is not

`fiscal_year_start` is the start of the longest fact ending at `period_end`, bounded by `_MIN_ANCHOR_DURATION_DAYS` (45) below and `_MAX_ANNUAL_DAYS` above. The lower bound drops sub-quarterly stub facts while still admitting a Q1 YTD at roughly 85 days, and the longest-fact rule works uniformly for a 10-K's annual figure and a 10-Q's YTD.

`fiscal_year` and `fiscal_period` are not derived from those dates. They are the modal `fy` and `fp` the issuer stamped on its own facts, because an NRF-calendar retailer names a year ending in January or February after the prior calendar year and a date-derived label would contradict the filing. Anchors are then deduplicated by `period_end`, keeping the later filing, which only fires when two non-amendment filings legitimately share a period end.

### 2.3 Maps are pre-computed once with two lookup indexes

Every fact map is built a single time and reused across all 12 anchors. Each `_FlowEntry` carries an `annual_index` (`{end: (fy_start, value)}`) and a `prior_ytd_index` (`{fy_start: [(duration, value)]}`).

The naive shape is O(anchors x tags x facts) with a linear scan inside the bridge on every call, and a 30-year filer can exceed 1,000 facts per tag. Pre-building collapses this to O(tags x facts) once and turns the bridge's two prior-year lookups into a dict hit and a scan of at most four candidates. This is the most consequential performance decision in the phase.

### 2.4 Flow matching is exact, only durations get a tolerance

The bridge looks up components by exact `(start, end)` keys. The only tolerances are:

| Tolerance | Window | Constant | Why |
|---|---|---|---|
| Prior-YTD duration | +/-4 days | `_PRIOR_YTD_TOLERANCE_DAYS` | Absorbs leap-day drift |
| Annual duration | 350-380 days | `_MIN_ANNUAL_DAYS` / `_MAX_ANNUAL_DAYS` | A fact is annual by length rather than form type, which is what lets a 10-Q's embedded full-year comparative serve as `PriorFY_Annual` |
| Instant dates | +/-7 days | `_INSTANT_DATE_TOLERANCE_DAYS` | — |

Exactness elsewhere follows from "no silent wrong value". If a required flow fact is not at the key the bridge needs, `None` is correct. Combining a near-miss fact would fabricate a number. This is why `period_mismatch` exists in the enum but is never raised: exact matching makes the mismatch unrepresentable rather than something to warn about. Where `DESIGN.md` and `PROJECT_STATUS.md` describe rejecting periods off by more than three days, exact matching plus `None` is how that intent is realized.

### 2.5 Annualization is the deliberate degradation path

When `PriorFY_Annual` or `PriorYTD` is missing (recent IPO, fiscal year change), the bridge degrades to `CurrentYTD / quarters_elapsed x 4` and attaches `ttm_annualized`. A missing `CurrentYTD` returns `None` instead, since there is nothing to extrapolate from, as do YTDs shorter than 40 days. `quarters_elapsed` is the YTD duration over `_DAYS_PER_QUARTER` (91.25) and stays fractional rather than rounding to a whole quarter, so an off-cycle YTD scales proportionally.

### 2.6 Ambiguity is first-tag-wins and is checked across the tolerance window

Conflicting non-amendment values drop the value and record the key as ambiguous. The resolvers surface one `ambiguous_fact` warning but keep walking the fallback chain, since a later tag may be clean. Only the primary tag's ambiguity is reported if the whole chain fails, held in `pending_ambiguous` on a first-wins basis so the message names the tag the user expects. For instants the ambiguity check (`_ambiguous_near`) mirrors the same +/-7 day window as the value lookup, so an ambiguous fact three days off period-end warns instead of silently reading as absent.

### 2.7 EPS keeps a stable audit concept name

The audit entry is always `concept="EPS"`, never `"EPS (Diluted)"`. The tag used is in `xbrl_tag`, and `is_fallback` plus `fallback_tag` disambiguate the basic path. Labeling the row "EPS (Diluted)" when basic fired would be an outright false statement in the audit panel. A neutral label cannot lie.

### 2.8 GAAP shares match by date, DEI shares match by accession

GAAP `CommonStockSharesOutstanding` is an instant whose `end` is the true period-end, so it goes through the deduplicated instant map and gets the normal protections. DEI `EntityCommonStockSharesOutstanding` uses the filing's report date, weeks after period-end, so it cannot be date-matched and is matched on the anchor's accession number instead. GAAP is preferred, DEI is the fallback for filers that never tagged GAAP shares.

### 2.9 CapEx sign is normalized at extraction, not at computation

`PaymentsToAcquirePropertyPlantAndEquipment` is occasionally reported negative. `_extract_capex` takes `abs()` and warns, so the stored value is always a non-negative outflow and downstream FCF math never second-guesses the sign.

## 3. Known limitations (accepted)

1. **The amendment check covers only the current-YTD key.** It is tested against `(fiscal_year_start, period_end)`, so the two prior-year bridge components are accepted even if sourced from an amendment. Closing this needs `_get_ttm_value` to return the keys it consulted. Deferred.
2. **`entity_context` is hardcoded `"consolidated"`.** No context-ID parsing. Dedup favors the consolidated filing in practice.
3. **`period_mismatch` is structurally unreachable** by design (§2.4).
4. **Amendment-only anchors are skipped.** Discovery accepts only form `10-K` and `10-Q` exactly, so a quarter filed solely as a `10-Q/A` is excluded and the window is one quarter older than expected. Amended values still reach the dedup layer inside the maps.

## 4. Which warnings this phase emits

Codes are defined in `app/models/errors.py`.

**Raised by Phase 2 extraction:** `ttm_annualized`, `fallback_tag`, `debt_deduplicated`, `cash_fallback_includes_investments`, `capex_sign_normalized`, `lease_pre_asc842`, `finance_lease_missing_capital_intensive`, `price_unavailable`, `amendment_exists`, and `ambiguous_fact`. 

**Defined but reserved for Phase 3** (multiples engine): `ev_debt_missing`, `denominator_near_zero`, `negative_book_value`, `negative_fcf`, `input_missing`. 

**Defined but never raised** (see §2.4): `period_mismatch`.

Per-code dedup runs once per period at the end of `extract_ttm_periods`. `ttm_annualized` and `amendment_exists` aggregate their affected concept names into a single message.

## 5. Fixture knowledge

Unit logic is covered by synthetic fact lists. The real-data fixtures are loaded straight from `tests/fixtures/{ticker}_CIK{cik_10}.json` with no HTTP mock, and exist to prove tag selection and the bridge against messy reality. Each was chosen for a specific edge case.

| Fixture | What it uniquely exercises |
|---|---|
| **CRCT** (Cricut) | Plain Dec-31 FY, GAAP shares, positive EPS, no debt or CapEx. The clean baseline. |
| **SNOW** (Snowflake) | Non-calendar FY (Feb to Jan), negative EPS, DEI-only shares. |
| **CART** (Instacart) | Sept 2023 IPO, so the short history stresses the 12-period window, GAAP and DEI shares. |
| **TGT** (Target) | `LongTermDebtNoncurrent` absent, forcing the total fallback and `debt_deduplicated` with `current_portion` zeroed, cash fallback, leases tagged at annual dates only. |
| **DAL** (Delta) | Capital-intensive SIC 4512, noncurrent-primary debt path with current portion kept, and the lease warning proven per-period (fires at Q1, suppressed at FY). |
| **BRKB** (Berkshire) | `MinorityInterest`, primary revenue tag narrower than the `Revenues` fallback (primary scope wins), prior-YTD sourced from the previous year's own filing, no cash or debt or shares in recent periods. |
| **AAPL** (Apple) | Sep FY with irregular quarter-ends, full D&A bridge, manufacturing-SIC lease warning distinct from DAL's transport SIC. |
| **MSFT** (Microsoft) | Jun FY, both D&A tags absent (the only fixture where `da = None` everywhere), a stale `ShortTermBorrowings` tag with no recent fact, and both cash tags present so the primary must win over a fallback ~2.4x larger. |

### 5.1 Hand-computed bridge oracles

The arithmetic the integration tests assert. If extraction drifts, these break first.

```
CRCT   Q3 2025   Rev   714,492,000 = 712,538,000 + 505,183,000 − 503,229,000
CRCT   Q1 2026   Rev   705,617,000 = 708,780,000 + 159,471,000 − 162,634,000
CRCT   Q1 2026   OCF   165,917,000 = 200,230,000 +  26,853,000 −  61,166,000
CRCT   Q1 2026   EPS          0.34 =        0.35 +        0.10 −        0.11

CART   Q1 2026   Rev   3,864,000,000 = 3,742,000,000 + 1,019,000,000 − 897,000,000
CART   Q1 2026   OCF     941,000,000 =   971,000,000 +   268,000,000 − 298,000,000

SNOW   Q3 FY26   Rev   4,386,722,000 = 3,626,396,000 + 3,399,952,000 − 2,639,626,000
SNOW   FY 2026   EPS   −3.95 (annual direct)

TGT    Q3 FY26   Rev   105,242 = 106,566 +  74,327 −  75,651         (×10⁶)
DAL    Q1 2026   Rev    65,178 =  63,364 +  15,854 −  14,040         (×10⁶)
BRKB   Q3 2025   Rev   247,366 = 249,714 + 184,510 − 186,858         (×10⁶, prior-YTD cross-filing)

AAPL   Q2 FY26   Rev   451,442 = 416,161 + 254,940 − 219,659         (×10⁶)
AAPL   Q2 FY26   OCF   140,222 = 111,482 +  82,627 −  53,887         (×10⁶)
AAPL   Q2 FY26   D&A    12,610 =  11,698 +   6,653 −   5,741         (×10⁶)
AAPL   Q2 FY26   EPS      8.26 =    7.46 +    4.85 −    4.05

MSFT   Q3 FY26   Rev   318,273 = 281,724 + 241,832 − 205,283         (×10⁶)
MSFT   Q3 FY26   OCF   170,141 = 136,162 + 127,494 −  93,515         (×10⁶)
MSFT   Q3 FY26   CapEx  97,225 =  64,551 +  80,146 −  47,472         (×10⁶)
MSFT   Q3 FY26   EPS     16.79 =   13.64 +   13.14 −    9.99
```

### 5.2 What the end-to-end tests pin, with price mocked

Ordering is most-recent-first, `filing_date` is populated, `price` follows the mock and becomes `None` with `price_unavailable` on every period when the mock returns nothing, `ticker=None` fetches no price and raises no warning, `is_capital_intensive=True` with no lease tags warns on every period, the audit trail holds one entry per concept, and empty companyfacts returns `[]`.

### 5.3 Paths with no real-data fixture

`fallback_tag`, `capex_sign_normalized`, pre-ASC 842 capital-lease tags, and `PreferredStock` are covered synthetically only. All four are short, unconditional, and structurally identical to paths already proven by fixtures. **Do not source additional fixtures for these.** The rate-limit exposure buys near-zero incremental confidence.

What the fixtures do pin: every monetary `ExtractedFinancials` field, five fiscal-year-end patterns, all three debt-resolution paths, all four cash-resolution paths, and the bridge at Q1, Q2, Q3, and FY against the oracles above.
