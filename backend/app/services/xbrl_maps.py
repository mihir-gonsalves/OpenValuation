# backend/app/services/xbrl_maps.py
"""
XBRL fact-map machinery - pure data transformation, no extraction logic.

Glossary
--------
Most of the terms below are XBRL or SEC-specific.

fact
    A single XBRL measurement reported in one filing. Has a numeric value, a
    unit (USD, USD/shares, shares), period information, and the accession
    number identifying the filing it came from.

flow fact
    A measurement taken OVER a span of time - revenue earned in a quarter,
    cash spent over a year. Carries both `start` and `end` dates. Used
    for income-statement and cash-flow concepts.

instant fact
    A measurement taken AT a single point in time - cash on hand, debt
    outstanding. Carries only an `end` date (no `start`). Used for
    balance-sheet concepts.

tag
    The XBRL identifier for a concept, e.g. `"Revenues"` or `"Assets"`.
    Inside each tag, facts are grouped by unit.

accession number (accn)
    The unique identifier of a single filing. The same fact may appear in
    many filings (e.g. a Q3 figure shows up as "current period" in the Q3
    10-Q and again as "prior-year comparative" in the next Q3's 10-Q). The
    accn distinguishes them.

amendment
    A filing whose form ends in `/A` (10-K/A, 10-Q/A). Filed to correct
    or supplement an earlier filing. Deprioritised in deduplication - see
    `_deduplicate_fact_group`.

anchor
    A 10-K or 10-Q filing chosen as the reference point for one TTM period.
    Anchor discovery lives in xbrl.py, not here. This module just supplies
    the lookups that anchor-driven extraction needs.

TTM bridge
    The formula that constructs a trailing-twelve-month value from XBRL's
    year-to-date and annual facts:

        TTM = PriorFY_Annual + CurrentYTD - PriorYTD_SamePeriod

    Implemented in `_get_ttm_value`. The bridge applies to flow concepts,
    instant concepts use direct point-in-time lookup.

Responsibilities of this module
-------------------------------
- Shared duration / tolerance constants (annual range, prior-YTD fuzz, ...).
- Type definitions for cached flow and instant maps.
- Deduplication of facts that share the same period (`_deduplicate_fact_group`).
- Map builders that turn raw fact lists into `_FlowEntry` / `_InstantEntry`.
- TTM bridge implementation (`_get_ttm_value` plus its helpers).
- Tolerance-aware point-in-time lookup (`_get_instant_result`).

It has no knowledge of which XBRL tags exist - that lives in xbrl.py.

Performance design
------------------
Each `_FlowEntry` carries two pre-built indexes constructed at map-build time:

  annual_index     `{end_date: (fy_start, value)}` - annual-duration facts
                   only (350-380 days). Enables O(1) prior-FY annual lookup
                   in `_find_annual_fact`.

  prior_ytd_index  `{fy_start: [(duration_days, value)]}` - all non-annual
                   facts grouped by their start date (which is the fiscal-year
                   start). Enables O(k) prior-YTD lookup in `_find_prior_ytd`,
                   where k is the number of non-annual facts sharing one
                   fiscal-year start (typically 3 - Q1, Q2, Q3 YTD).

These indexes turn what would otherwise be linear scans of the full flow map
into constant-time / small-k lookups.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Duration and tolerance constants (single source of truth)
# ---------------------------------------------------------------------------

# Annual facts span roughly one year. Real filings occasionally drift to 364
# or 366 days (leap years, 52/53-week fiscal calendars).
_MIN_ANNUAL_DAYS = 350
_MAX_ANNUAL_DAYS = 380

# Prior-year YTDs can drift by a day or two from the current-year YTD because
# of leap days and weekend/weekday calendar shifts. Accept up to +/- 4 days 
# when matching the prior-YTD that mirrors the current YTD.
_PRIOR_YTD_TOLERANCE_DAYS = 4

# Balance-sheet dates from different sources (10-K vs 10-Q comparative) can
# disagree by a few days. Accept up to +/-7 days when locating an instant fact.
_INSTANT_DATE_TOLERANCE_DAYS = 7

# Used by the annualization fallback (when prior-year data is unavailable):
#   annualized_value = ytd_value / (ytd_days / DAYS_PER_QUARTER) * 4
_DAYS_PER_QUARTER = Decimal("91.25")  # 365.0 / 4

# Refuse to annualize YTDs shorter than this. Anything below half a quarter
# is too noisy to extrapolate (e.g. a partial stub month from a recent IPO).
_MIN_ANNUALIZATION_DAYS = 40

# ---------------------------------------------------------------------------
# Type aliases for cached maps
# ---------------------------------------------------------------------------

FlowMap = dict[tuple[date, date], Decimal]              # {(start, end): value}
InstantMap = dict[date, Decimal]                        # {end: value}
AnnualIndex = dict[date, tuple[date, Decimal]]          # end -> (fy_start, value)
PriorYTDIndex = dict[date, list[tuple[int, Decimal]]]   # fy_start -> [(duration, value)]

# Caches passed across the extraction layer. Keyed by (tag, unit) so the same
# tag in different units (rare but possible) stays distinct.
_FlowCache = dict[tuple[str, str], "_FlowEntry"]
_InstantCache = dict[tuple[str, str], "_InstantEntry"]


class _FlowEntry(NamedTuple):
    """
    Cached flow data for one (tag, unit) pair, reused across all anchor periods.

    Built once per companyfacts payload. Reuse avoids rebuilding the same
    `O(n_facts)` structure for every anchor × tag combination - a 12× speed-up
    in practice over a full 12-period extraction window.

    Fields
    ------
    flow_map         The deduplicated `{(start, end): value}` map of flow facts.
    amendment_keys   `(start, end)` pairs whose only source was an amendment.
                     Drives the AMENDMENT_EXISTS warning.
    ambiguous_keys   `(start, end)` pairs with conflicting non-amendment values.
                     Drives the AMBIGUOUS_FACT warning, the value is dropped.
    annual_index     End-date keyed index of full-year facts. See module docstring.
    prior_ytd_index  Fiscal-year-start keyed index of partial-year facts.
                     See module docstring.
    """

    flow_map: FlowMap
    amendment_keys: set[tuple[date, date]]
    ambiguous_keys: set[tuple[date, date]]
    annual_index: AnnualIndex
    prior_ytd_index: PriorYTDIndex


class _InstantEntry(NamedTuple):
    """
    Cached instant data for one (tag, unit) pair. Mirrors `_FlowEntry` but
    keyed by date rather than (start, end), since instant facts have no start.

    Fields
    ------
    instant_map     The deduplicated `{end_date: value}` map of instant facts.
    amendment_keys  Dates whose only source was an amendment filing.
    ambiguous_keys  Dates with conflicting non-amendment values.
    """

    instant_map: InstantMap
    amendment_keys: set[date]
    ambiguous_keys: set[date]


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def _deduplicate_fact_group(
    facts: list[dict],
) -> tuple[Decimal | None, bool, bool]:
    """
    The EDGAR companyfacts API returns the same (start, end) pair across multiple
    filings (current period + comparative). Resolve a group of facts that share the 
    same period (or same date, for instants) into a single canonical value.

    Priority rules:
      1. Non-amendment filings (10-K, 10-Q) preferred over amendments (10-K/A, 10-Q/A).
      2. If multiple non-amendment facts for the same (start, end) agree -> use value.
      3. If they disagree -> ambiguous_fact warning, value set to None.
      4. Amendment-only facts -> use value, attach amendment_exists warning per period.

    Returns
    -------
    `(value, is_amendment_only, is_ambiguous)`
      value              Decimal if unambiguous, else None.
      is_amendment_only  True when every fact in the group is an amendment.
      is_ambiguous       True when non-amendment facts carry conflicting values.
    """
    originals = [f for f in facts if not f.get("form", "").endswith("/A")]
    amendments = [f for f in facts if f.get("form", "").endswith("/A")]

    is_amendment_only = not originals
    candidates = amendments if is_amendment_only else originals

    distinct_values = {f["val"] for f in candidates}
    if len(distinct_values) == 1:
        return Decimal(str(candidates[0]["val"])), is_amendment_only, False
    if len(distinct_values) > 1:
        return None, False, True
    return None, False, False  # empty group - should not occur in practice


# ---------------------------------------------------------------------------
# Map builders
# ---------------------------------------------------------------------------


def _build_flow_map(facts: list[dict]) -> _FlowEntry:
    """
    Build a deduplicated flow map plus its two lookup indexes.

    The caller is responsible for unit filtering - `facts` must already be a
    single-unit list (e.g. `gaap[tag]["units"]["USD"]`).

    Build sequence:
      1. Group raw facts by `(start, end)` key.
      2. Resolve each group through `_deduplicate_fact_group`, populating
         the flow map plus the amendment/ambiguous key sets.
      3. From the resolved flow map (not raw facts), partition entries into
         the annual_index and prior_ytd_index. Using the resolved map means
         both indexes inherit deduplication automatically - no chance of an
         ambiguous fact slipping into one of the lookup indexes.

    Why prior_ytd_index uses `defaultdict` internally but returns a plain
    dict: the defaultdict's mutable default would surprise callers who
    iterate keys and see new ones materialise.
    """
    # --- Step 1: group raw facts by (start, end) ---
    grouped: dict[tuple[date, date], list[dict]] = defaultdict(list)
    for f in facts:
        if "start" not in f:
            continue  # instant fact - not our concern
        key = (date.fromisoformat(f["start"]), date.fromisoformat(f["end"]))
        grouped[key].append(f)

    # --- Step 2: deduplicate each group ---
    flow_map: FlowMap = {}
    amendment_keys: set[tuple[date, date]] = set()
    ambiguous_keys: set[tuple[date, date]] = set()

    for key, group in grouped.items():
        value, is_amendment_only, is_ambiguous = _deduplicate_fact_group(group)
        if is_ambiguous:
            ambiguous_keys.add(key)
        elif value is not None:
            flow_map[key] = value
            if is_amendment_only:
                amendment_keys.add(key)

    # --- Step 3: build indexes from the resolved flow map ---
    annual_index: AnnualIndex = {}
    prior_ytd_index_mut: dict[date, list[tuple[int, Decimal]]] = defaultdict(list)

    for (start, end), value in flow_map.items():
        duration_days = (end - start).days
        if _MIN_ANNUAL_DAYS <= duration_days <= _MAX_ANNUAL_DAYS:
            annual_index[end] = (start, value)
        else:
            prior_ytd_index_mut[start].append((duration_days, value))

    return _FlowEntry(
        flow_map=flow_map,
        amendment_keys=amendment_keys,
        ambiguous_keys=ambiguous_keys,
        annual_index=annual_index,
        prior_ytd_index=dict(prior_ytd_index_mut),
    )


def _build_instant_map(facts: list[dict]) -> _InstantEntry:
    """
    Build a deduplicated instant map for one (tag, unit) pair.

    Same shape as `_build_flow_map` but keyed by `end` only - instants have
    no `start`. No annual / prior-YTD indexes are needed because instants
    are point-in-time lookups, not bridges.
    """
    grouped: dict[date, list[dict]] = defaultdict(list)
    for f in facts:
        if "start" in f:
            continue  # flow fact - not our concern
        key = date.fromisoformat(f["end"])
        grouped[key].append(f)

    instant_map: InstantMap = {}
    amendment_keys: set[date] = set()
    ambiguous_keys: set[date] = set()

    for key, group in grouped.items():
        value, is_amendment_only, is_ambiguous = _deduplicate_fact_group(group)
        if is_ambiguous:
            ambiguous_keys.add(key)
        elif value is not None:
            instant_map[key] = value
            if is_amendment_only:
                amendment_keys.add(key)

    return _InstantEntry(
        instant_map=instant_map,
        amendment_keys=amendment_keys,
        ambiguous_keys=ambiguous_keys,
    )


# ---------------------------------------------------------------------------
# TTM bridge helpers
# ---------------------------------------------------------------------------


def _find_annual_fact(
    annual_index: AnnualIndex,
    target_end: date,
) -> tuple[date, Decimal] | None:
    """
    Look up a full-year (350-380 day) fact ending exactly on `target_end`.

    O(1) via the pre-built `annual_index`. Returns `(fiscal_year_start, value)`
    or None.
    """
    return annual_index.get(target_end)


def _find_prior_ytd(
    prior_ytd_index: PriorYTDIndex,
    prior_fy_start: date,
    ytd_duration_days: int,
) -> Decimal | None:
    """
    Look up a year-to-date fact whose `start` equals `prior_fy_start` and
    whose duration is within `_PRIOR_YTD_TOLERANCE_DAYS` of `ytd_duration_days`.

    O(k) over `prior_ytd_index[prior_fy_start]`, where k is the number of
    non-annual facts sharing that fiscal-year start - typically 3 (the Q1,
    Q2, and Q3 YTDs of that year).
    """
    for duration_days, value in prior_ytd_index.get(prior_fy_start, []):
        if abs(duration_days - ytd_duration_days) <= _PRIOR_YTD_TOLERANCE_DAYS:
            return value
    return None


def _annualize(
    ytd_value: Decimal,
    ytd_duration_days: int,
) -> tuple[Decimal | None, bool]:
    """
    Extrapolate a YTD value to a full-year estimate when no prior-year data
    is available.

    Returns `(annualized_value, was_annualized)`. The boolean tells the
    caller to attach a TTM_ANNUALIZED warning so the user knows the value
    came from extrapolation rather than the full bridge.

    Refuses to annualize YTDs shorter than `_MIN_ANNUALIZATION_DAYS` - too
    little data to extrapolate meaningfully.
    """
    if ytd_duration_days < _MIN_ANNUALIZATION_DAYS:
        return None, False
    quarters_elapsed = Decimal(str(ytd_duration_days)) / _DAYS_PER_QUARTER
    return ytd_value / quarters_elapsed * Decimal("4"), True


def _get_ttm_value(
    flow_map: FlowMap,
    period_end: date,
    fiscal_year_start: date,
    annual_index: AnnualIndex,
    prior_ytd_index: PriorYTDIndex,
) -> tuple[Decimal | None, bool]:
    """
    Compute the TTM (trailing-twelve-month) value for a flow concept.

    Returns `(ttm_value, was_annualized)`. `was_annualized=True` means the
    bridge failed and the value came from the annualization fallback -
    callers must attach the TTM_ANNUALIZED warning.

    Algorithm
    ---------
    For an annual anchor (the YTD already spans ~365 days):

        TTM = flow_map[(fiscal_year_start, period_end)]   # direct lookup

    For a quarterly anchor, apply the bridge:

        TTM = CurrentYTD + PriorFY_Annual - PriorYTD_SamePeriod

    When the prior-FY annual or prior-YTD fact is missing (common for recent
    IPOs or fiscal-year changes), fall back to annualization:

        TTM ≈ CurrentYTD / quarters_elapsed × 4
    """
    ytd_duration_days = (period_end - fiscal_year_start).days

    # Annual anchor - full-year fact IS the TTM value.
    if ytd_duration_days >= _MIN_ANNUAL_DAYS:
        return flow_map.get((fiscal_year_start, period_end)), False

    # Quarterly anchor - must have a current YTD to proceed.
    current_ytd = flow_map.get((fiscal_year_start, period_end))
    if current_ytd is None:
        return None, False

    # Look up the prior fiscal year's annual fact (O(1)).
    prior_fy_end = fiscal_year_start - timedelta(days=1)
    prior_annual = _find_annual_fact(annual_index, prior_fy_end)
    if prior_annual is None:
        return _annualize(current_ytd, ytd_duration_days)
    prior_fy_start, prior_fy_value = prior_annual

    # Look up the prior-year YTD that mirrors the current YTD's duration (O(k)).
    prior_ytd = _find_prior_ytd(prior_ytd_index, prior_fy_start, ytd_duration_days)
    if prior_ytd is None:
        return _annualize(current_ytd, ytd_duration_days)

    # Apply the bridge.
    return current_ytd + prior_fy_value - prior_ytd, False


# ---------------------------------------------------------------------------
# Instant (balance-sheet) lookup helpers
# ---------------------------------------------------------------------------


def _get_instant_result(
    instant_map: InstantMap,
    period_end: date,
) -> tuple[Decimal | None, date | None]:
    """
    Locate an instant fact at or near `period_end`, with a +/- 7 day tolerance.

    Search order: exact date first, then +/-1, +/-2, ... up to the tolerance limit.
    Returns `(value, matched_date)` so callers can verify whether the matched
    date belongs to `amendment_keys` or `ambiguous_keys`. `matched_date` is
    None when no value is found.
    """
    value = instant_map.get(period_end)
    if value is not None:
        return value, period_end
    for delta in range(1, _INSTANT_DATE_TOLERANCE_DAYS + 1):
        for candidate in (period_end + timedelta(delta), period_end - timedelta(delta)):
            value = instant_map.get(candidate)
            if value is not None:
                return value, candidate
    return None, None


def _ambiguous_near(ambiguous_keys: set[date], period_end: date) -> date | None:
    """
    Find the nearest date in `ambiguous_keys` within +/-7 days of `period_end`.
    Returns None if no match.

    The search mirrors `_get_instant_result`'s tolerance window so ambiguity
    checks cover the same dates as value lookups. Without this, an ambiguous
    value at, say, period_end + 3 days would silently pass as "no match"
    rather than producing an AMBIGUOUS_FACT warning.
    """
    if period_end in ambiguous_keys:
        return period_end
    for delta in range(1, _INSTANT_DATE_TOLERANCE_DAYS + 1):
        for candidate in (period_end + timedelta(delta), period_end - timedelta(delta)):
            if candidate in ambiguous_keys:
                return candidate
    return None