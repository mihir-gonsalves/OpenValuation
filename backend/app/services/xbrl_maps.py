# backend/app/services/xbrl_maps.py
"""
XBRL fact-map machinery - pure data transformation, no extraction logic.

Deduplication, map building, the TTM bridge, and instant lookup. This module
has no knowledge of which XBRL tags exist, which is what makes it testable on
synthetic fact lists.

Glossary
--------
fact
    One XBRL measurement from one filing: a value, a unit, period information,
    and the accession number of the filing it came from.

flow fact
    Measured over a span (revenue in a quarter), so it carries `start` and
    `end`. Income statement and cash flow concepts.

instant fact
    Measured at a point (cash on hand), so it carries only `end`. Balance
    sheet concepts.

accession number (accn)
    Identifies one filing. The same figure appears in several filings, as the
    current period in one and a prior-year comparative in another, and the accn
    is what distinguishes them.

amendment
    A form ending in `/A`. Deprioritized in `_deduplicate_fact_group`.

anchor
    The 10-K or 10-Q a TTM period is built around. Discovery lives in xbrl.py.

TTM bridge
    `TTM = PriorFY_Annual + CurrentYTD - PriorYTD_SamePeriod`, implemented in
    `_get_ttm_value`. Flow concepts only, instants use direct lookup.

Performance
-----------
Each `_FlowEntry` carries two indexes built alongside its map: `annual_index`
({end: (fy_start, value)}, annual-duration facts only) makes the prior-FY
lookup O(1), and `prior_ytd_index` ({fy_start: [(duration, value)]}) makes the
prior-YTD lookup O(k) over the handful of YTDs sharing a fiscal-year start.
Without them the bridge would rescan the whole flow map on every call.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Duration and tolerance constants
# ---------------------------------------------------------------------------

# Annual facts span roughly a year, drifting with leap years and 52/53-week
# fiscal calendars. A fact is annual by length, not by form type.
_MIN_ANNUAL_DAYS = 350
_MAX_ANNUAL_DAYS = 380

# Absorbs the day or two of drift a leap day introduces between a YTD and its
# prior-year mirror.
_PRIOR_YTD_TOLERANCE_DAYS = 4

# Balance-sheet dates disagree by a few days across filings.
_INSTANT_DATE_TOLERANCE_DAYS = 7

_DAYS_PER_QUARTER = Decimal("91.25")

# Below half a quarter there is too little signal to extrapolate.
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
    Cached flow data for one (tag, unit) pair, built once and reused across all
    12 anchors.

    `amendment_keys` are the periods whose only source was an amendment, and
    `ambiguous_keys` are those with conflicting non-amendment values, whose
    value is dropped. The two indexes are described in the module docstring.
    """

    flow_map: FlowMap
    amendment_keys: set[tuple[date, date]]
    ambiguous_keys: set[tuple[date, date]]
    annual_index: AnnualIndex
    prior_ytd_index: PriorYTDIndex


class _InstantEntry(NamedTuple):
    """
    `_FlowEntry`'s counterpart, keyed by date alone since instants have no start.
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
    Resolve facts sharing a period into one canonical value, returning
    `(value, is_amendment_only, is_ambiguous)`.

    `companyfacts` repeats the same period across filings, as a current period in
    one and a comparative in another. Originals beat amendments, agreeing
    originals give their value, disagreeing ones give None and flag ambiguity,
    and an amendment-only group gives its value with the amendment flag set.
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
    return None, False, False  # empty group, not expected in practice


# ---------------------------------------------------------------------------
# Map builders
# ---------------------------------------------------------------------------


def _build_flow_map(facts: list[dict]) -> _FlowEntry:
    """
    Build a deduplicated flow map plus its two lookup indexes. `facts` must
    already be filtered to a single unit.

    The indexes are built from the resolved map rather than the raw facts, so
    they inherit deduplication and no ambiguous fact can slip into either one.
    The prior-YTD index is returned as a plain dict, since a defaultdict would
    materialise keys under callers that iterate it.
    """
    # --- Step 1: group raw facts by (start, end) ---
    grouped: dict[tuple[date, date], list[dict]] = defaultdict(list)
    for f in facts:
        if "start" not in f:
            continue  # instant fact
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
    `_build_flow_map` keyed by `end` alone. Instants are point-in-time lookups, so they need no bridge indexes.
    """
    grouped: dict[date, list[dict]] = defaultdict(list)
    for f in facts:
        if "start" in f:
            continue  # flow fact
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
    O(1) lookup of a full-year fact ending exactly on `target_end`, returning `(fiscal_year_start, value)`.
    """
    return annual_index.get(target_end)


def _find_prior_ytd(
    prior_ytd_index: PriorYTDIndex,
    prior_fy_start: date,
    ytd_duration_days: int,
) -> Decimal | None:
    """
    The YTD starting at `prior_fy_start` whose duration matches within tolerance.
    O(k) over the few YTDs sharing that fiscal-year start.
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
    Extrapolate a YTD to a full year when prior-year data is unavailable.

    Returns `(value, was_annualized)`, where the flag tells the caller to
    attach TTM_ANNUALIZED. Refuses YTDs shorter than the minimum.
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

    Returns `(ttm_value, was_annualized)`, where the flag means the bridge
    failed and the caller must attach TTM_ANNUALIZED.

    An annual anchor reads the full-year fact directly. A quarterly anchor
    applies `CurrentYTD + PriorFY_Annual - PriorYTD_SamePeriod`, falling back
    to `CurrentYTD / quarters_elapsed × 4` when either prior-year component is
    missing, which is common after an IPO or a fiscal-year change.
    """
    ytd_duration_days = (period_end - fiscal_year_start).days

    # For an annual anchor the full-year fact is the TTM value.
    if ytd_duration_days >= _MIN_ANNUAL_DAYS:
        return flow_map.get((fiscal_year_start, period_end)), False

    # A quarterly anchor needs a current YTD. Nothing to extrapolate without it.
    current_ytd = flow_map.get((fiscal_year_start, period_end))
    if current_ytd is None:
        return None, False

    prior_fy_end = fiscal_year_start - timedelta(days=1)
    prior_annual = _find_annual_fact(annual_index, prior_fy_end)
    if prior_annual is None:
        return _annualize(current_ytd, ytd_duration_days)
    prior_fy_start, prior_fy_value = prior_annual

    # The prior-year YTD mirroring the current YTD's duration.
    prior_ytd = _find_prior_ytd(prior_ytd_index, prior_fy_start, ytd_duration_days)
    if prior_ytd is None:
        return _annualize(current_ytd, ytd_duration_days)

    return current_ytd + prior_fy_value - prior_ytd, False


# ---------------------------------------------------------------------------
# Instant (balance-sheet) lookup helpers
# ---------------------------------------------------------------------------


def _get_instant_result(
    instant_map: InstantMap,
    period_end: date,
) -> tuple[Decimal | None, date | None]:
    """
    Locate an instant fact at or near `period_end`, exact date first and then
    outward to the tolerance limit.

    Returns `(value, matched_date)` so callers can check the matched date
    against `amendment_keys` and `ambiguous_keys`.
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
    The nearest ambiguous date within the tolerance window.

    Mirroring `_get_instant_result`'s window is what stops an ambiguous fact
    three days off period-end from silently reading as absent.
    """
    if period_end in ambiguous_keys:
        return period_end
    for delta in range(1, _INSTANT_DATE_TOLERANCE_DAYS + 1):
        for candidate in (period_end + timedelta(delta), period_end - timedelta(delta)):
            if candidate in ambiguous_keys:
                return candidate
    return None