# backend/app/services/xbrl.py
"""
XBRL extraction service.

Converts a raw EDGAR companyfacts dict into a list of `ExtractedFinancials`,
one per TTM period (up to 12, most-recent-first).

Module structure
----------------
The XBRL extraction layer is split across three files:

  xbrl.py            (this file) -- tag chains, anchor discovery, concept
                     resolvers, the public entry point. Knows which XBRL tags
                     map to which financial concepts.

  xbrl_maps.py       Pure data machinery -- duration constants, fact-map
                     types, deduplication, map builders, TTM bridge, instant
                     lookup. No knowledge of tags.

  xbrl_warnings.py   Warning helpers -- flow-concept warning construction
                     (TTM annualized, amendment used) and per-period
                     deduplication of repeated warning codes.

TTM Bridge Algorithm
--------------------
For a quarterly anchor (10-Q with period_end P and fiscal year start Y):

    TTM = PriorFY_Annual + CurrentYTD - PriorYTD_SamePeriod

where:
    CurrentYTD          = fact(start=Y,     end=P)
    PriorFY_Annual      = fact(start=Y-1yr, end=Y-1day),  duration ≈ 365 days
    PriorYTD_SamePeriod = fact(start=Y-1yr, end=P-1yr),   duration ≈ |P-Y| days

For an annual anchor (10-K, duration >= 350 days):
    TTM = the annual value directly

annualization fallback (when prior-year data is unavailable):
    TTM ≈ CurrentYTD / elapsed_quarters × 4
    Warning: ttm_annualized

Balance-sheet items use the point-in-time value at period_end (no bridge).
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.models.errors import Warning, WarningCode, warn
from app.models.financials import AuditEntry, ExtractedFinancials
from app.services import price as price_svc
from app.services.xbrl_maps import (
    _FlowCache,
    _FlowEntry,
    _InstantCache,
    _InstantEntry,
    _MAX_ANNUAL_DAYS,
    _ambiguous_near,
    _build_flow_map,
    _build_instant_map,
    _get_instant_result,
    _get_ttm_value,
)
from app.services.xbrl_warnings import dedup_warnings, _make_flow_warnings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

MAX_TTM_PERIODS = 12
"""Maximum number of TTM periods returned by `extract_ttm_periods`."""

_MIN_ANCHOR_DURATION_DAYS = 45
"""
Minimum fact duration (in days) accepted when computing `fiscal_year_start`
for an anchor. Excludes sub-quarterly stub data while still capturing every
Q1 YTD fact (which run ≈ 85 days in practice).
"""

# ---------------------------------------------------------------------------
# Tag chains: concept -> ordered list of XBRL tags
# ---------------------------------------------------------------------------
# Each list is the fallback chain: the resolver walks the list in order and
# returns the first tag that produces a value. The first tag is the "primary",
# any later tag firing is a fallback and triggers the FALLBACK_* warning
# specific to that concept (see the specialised extractors below).

# Flow concepts (have both start and end dates). Unit: USD.
_FLOW_CHAINS: dict[str, list[str]] = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ],
    "operating_income": [
        "OperatingIncomeLoss"
    ],
    "da": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
    ],
    "net_income": [
        "NetIncomeLoss"
    ],
    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities"
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsForCapitalImprovements",
    ],
}

# EPS tags. Unit: USD/shares.
# Single source of truth for `_precompute_flow_maps` and `_extract_eps`.
# Updating it is the only change needed if the EPS tag set ever changes.
_EPS_TAGS = ("EarningsPerShareDiluted", "EarningsPerShareBasic")

# Instant concepts (balance-sheet items, point-in-time). Unit: USD.
_INSTANT_CHAINS: dict[str, list[str]] = {
    "total_assets": ["Assets"],
    "stockholders_equity": [
        "StockholdersEquity",
    ],
    # LongTermDebtNoncurrent is primary, LongTermDebt is the fallback that
    # triggers the debt_deduplicated logic (see _extract_debt).
    "long_term_debt": [
        "LongTermDebtNoncurrent",
        "LongTermDebt"
    ],
    "short_term_borrowings": [
        "ShortTermBorrowings",
        "ShortTermDebt"
    ],
    "current_portion_lt_debt": [
        "LongTermDebtCurrent",
    ],
    "finance_lease_current": [
        "FinanceLeaseLiabilityCurrent",
        "CapitalLeaseObligationsCurrent",
    ],
    "finance_lease_noncurrent": [
        "FinanceLeaseLiabilityNoncurrent",
        "CapitalLeaseObligationsNoncurrent",
    ],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsAndShortTermInvestments",
    ],
    "minority_interest": [
        "MinorityInterest"
    ],
    "preferred_stock": [
        "PreferredStockValue",
    ],
}

# Named constants for the pre-ASC 842 capital-lease fallback tags.
_CAPITAL_LEASE_CURRENT_TAG = "CapitalLeaseObligationsCurrent"
_CAPITAL_LEASE_NONCURRENT_TAG = "CapitalLeaseObligationsNoncurrent"

# Shares-outstanding tags.
_GAAP_SHARES_TAG = "CommonStockSharesOutstanding"  # instant, end = period_end
_DEI_SHARES_TAG = "EntityCommonStockSharesOutstanding"  # instant, end = report date

# Tags used to discover 10-K/10-Q filing anchors. Derived from the revenue
# fallback chain plus OperatingIncomeLoss and NetIncomeLoss.
#
# Building this from `_FLOW_CHAINS["revenue"]` enforces an invariant
# syntactically: every tag accepted as revenue is also a valid anchor source
# by construction. Adding a revenue tag automatically makes it an anchor tag.
_ANCHOR_DISCOVERY_TAGS: tuple[str, ...] = (
    *_FLOW_CHAINS["revenue"],
    "OperatingIncomeLoss",
    "NetIncomeLoss",
)


# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------


@dataclass
class _FilingAnchor:
    """
    One 10-K or 10-Q filing used as the reference point for one TTM period.

    Identity is the accession number, the other fields drive extraction:
      filed              the date the filing was submitted (drives price lookup)
      period_end         the reporting period's last day (TTM column header)
      fiscal_year_start  the fiscal year's first day (drives the TTM bridge)
    """

    accn: str
    filed: date
    period_end: date
    fiscal_year_start: date


@dataclass
class _ConceptResult:
    """The result of resolving one concept from a tag chain."""

    value: Decimal | None
    unit: str | None = None
    tag_used: str | None = None
    is_fallback: bool = False
    warnings: list[Warning] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Map pre-computation (one call per companyfacts payload)
# ---------------------------------------------------------------------------


def _precompute_flow_maps(gaap: dict) -> _FlowCache:
    """
    Build deduplicated flow maps for every tag in `_FLOW_CHAINS` and `_EPS_TAGS`.

    Called once per companyfacts payload, before the per-anchor extraction
    loop. Reusing these maps across all 12 anchor periods avoids rebuilding
    the same map for every anchor × tag combination, reducing total work
    from O(anchors × tags × facts) to O(tags × facts).

    Each `_FlowEntry` includes `annual_index` and `prior_ytd_index` for
    O(1) and O(k) TTM bridge lookups, respectively (see xbrl_maps.py).
    """
    cache: _FlowCache = {}

    # USD monetary flow concepts.
    for chain in _FLOW_CHAINS.values():
        for tag in chain:
            key = (tag, "USD")
            if key in cache or tag not in gaap:
                continue
            facts = gaap[tag].get("units", {}).get("USD", [])
            if facts:
                cache[key] = _build_flow_map(facts)

    # EPS concepts (USD/shares).
    for tag in _EPS_TAGS:
        key = (tag, "USD/shares")
        if key in cache or tag not in gaap:
            continue
        facts = gaap[tag].get("units", {}).get("USD/shares", [])
        if facts:
            cache[key] = _build_flow_map(facts)

    return cache


def _precompute_instant_maps(gaap: dict) -> _InstantCache:
    """
    Build deduplicated instant maps for every tag in `_INSTANT_CHAINS` and
    for the GAAP shares tag.

    Called once per companyfacts payload. See `_precompute_flow_maps` for
    the reuse rationale.
    """
    cache: _InstantCache = {}

    # USD balance-sheet tags.
    for chain in _INSTANT_CHAINS.values():
        for tag in chain:
            key = (tag, "USD")
            if key in cache or tag not in gaap:
                continue
            facts = gaap[tag].get("units", {}).get("USD", [])
            if facts:
                cache[key] = _build_instant_map(facts)

    # GAAP shares tag (unit = shares).
    if _GAAP_SHARES_TAG in gaap:
        facts = gaap[_GAAP_SHARES_TAG].get("units", {}).get("shares", [])
        if facts:
            cache[(_GAAP_SHARES_TAG, "shares")] = _build_instant_map(facts)

    return cache


# ---------------------------------------------------------------------------
# Filing-anchor discovery
# ---------------------------------------------------------------------------


def _collect_filing_anchors(gaap: dict) -> list[_FilingAnchor]:
    """
    Scan every sentinel tag in `_ANCHOR_DISCOVERY_TAGS` and union their
    accession numbers to discover every unique 10-K / 10-Q filing in the
    companyfacts payload.

    All sentinel tags are always scanned (no early exit after the first hit).
    A company that changed its primary revenue tag mid-history will have older
    filings discoverable only via an earlier tag and newer filings discoverable
    only via a later one. Taking the union ensures the full filing history is
    captured for a complete 12-period window.

    Returns one `_FilingAnchor` per unique accession number that corresponds
    to an original (non-amendment) filing. Amendments are excluded here, their
    values still flow through the deduplication layer in `_build_*_map`.
    """
    # --- Step 1: collect facts grouped by accession number ---
    facts_by_accn: dict[str, list[dict]] = defaultdict(list)
    for tag in _ANCHOR_DISCOVERY_TAGS:
        if tag not in gaap:
            continue
        for f in gaap[tag].get("units", {}).get("USD", []):
            if f.get("form") in ("10-K", "10-Q"):
                facts_by_accn[f["accn"]].append(f)

    if not facts_by_accn:
        return []

    # --- Step 2: turn each accession into a _FilingAnchor ---
    anchors: list[_FilingAnchor] = []
    for accn, facts in facts_by_accn.items():
        # period_end = the maximum end date among this accession's facts.
        # All facts in the same accession cover the same reporting period, so
        # this is the canonical period_end for the filing.
        period_end = max(date.fromisoformat(f["end"]) for f in facts)

        # All facts in the same accession share the same filed date.
        filed = date.fromisoformat(facts[0]["filed"])

        # fiscal_year_start = start of the LONGEST fact ending at period_end.
        # The longest fact's start is the fiscal year's first day:
        #   Annual (10-K):  one fact, duration ≈ 365d -> fy_start = start of FY.
        #   Q3 (10-Q):      two facts (single-quarter ≈ 91d, YTD ≈ 272d)
        #                   -> max = 272d -> fy_start = start of FY.
        #   Q1 (10-Q):      one fact, duration ≈ 91d -> fy_start = start of FY.
        # The 45-day minimum filters out sub-quarterly stub data.
        candidates: list[tuple[int, date]] = []
        for f in facts:
            if date.fromisoformat(f["end"]) != period_end or "start" not in f:
                continue
            start = date.fromisoformat(f["start"])
            duration_days = (period_end - start).days
            if _MIN_ANCHOR_DURATION_DAYS <= duration_days <= _MAX_ANNUAL_DAYS:
                candidates.append((duration_days, start))

        if not candidates:
            continue

        fiscal_year_start = max(candidates, key=lambda x: x[0])[1]

        anchors.append(_FilingAnchor(
            accn=accn,
            filed=filed,
            period_end=period_end,
            fiscal_year_start=fiscal_year_start,
        ))

    # --- Step 3: deduplicate anchors by period_end ---
    # Amendments are already excluded above. This branch fires only when two
    # non-amendment filings legitimately share a period_end (e.g. a delayed
    # filer re-submitting on schedule). Keep the later filing date.
    by_period: dict[date, _FilingAnchor] = {}
    for anchor in anchors:
        existing = by_period.get(anchor.period_end)
        if existing is None or anchor.filed > existing.filed:
            by_period[anchor.period_end] = anchor
    return list(by_period.values())


# ---------------------------------------------------------------------------
# Cache lookup helpers
# ---------------------------------------------------------------------------
# Both `_resolve_flow`, `_resolve_instant`, and `_get_shares` need to ask
# the same question: "is there a cached entry for (tag, unit) or does it
# need to be built from gaap on demand?"
#
# The `cache is None` branch keeps the resolvers usable in isolation - for
# example, in unit tests that operate directly on a gaap dict without the
# precompute step. The production path always passes a cache.


def _get_flow_entry(
    flow_cache: _FlowCache | None,
    gaap: dict,
    tag: str,
    unit: str,
) -> _FlowEntry | None:
    """Cached lookup with on-demand build fallback. See module note above."""
    if flow_cache is not None:
        return flow_cache.get((tag, unit))
    facts = gaap.get(tag, {}).get("units", {}).get(unit, [])
    return _build_flow_map(facts) if facts else None


def _get_instant_entry(
    instant_cache: _InstantCache | None,
    gaap: dict,
    tag: str,
    unit: str,
) -> _InstantEntry | None:
    """Cached lookup with on-demand build fallback. See module note above."""
    if instant_cache is not None:
        return instant_cache.get((tag, unit))
    facts = gaap.get(tag, {}).get("units", {}).get(unit, [])
    return _build_instant_map(facts) if facts else None


# ---------------------------------------------------------------------------
# Shares outstanding lookup
# ---------------------------------------------------------------------------


def _get_shares(
    gaap: dict,
    dei: dict,
    period_end: date,
    anchor_accn: str,
    *,
    instant_cache: _InstantCache | None = None,
) -> tuple[Decimal | None, str | None, list[Warning]]:
    """
    Resolve shares outstanding for the anchor period.

    Priority:
      1. GAAP `CommonStockSharesOutstanding` - instant, matched to period_end
         with the standard +/- 7 day tolerance.
      2. DEI `EntityCommonStockSharesOutstanding` - matched by the anchor's
         accession number (this tag's `end` is the report date, not the
         period end, so it cannot be matched by date).

    On the GAAP path, the deduplicated instant map is consulted so that the
    standard dedup rules apply (originals over amendments, conflict detection).
    Iterating raw facts directly would silently return whichever fact appeared
    first when two non-amendment filings reported conflicting share counts at
    the same date.

    The DEI fallback applies basic conflict detection: if two non-amendment
    facts from the same accession report different share counts (very rare in
    practice - DEI shares are typically reported once per filing), the value
    is treated as ambiguous and None is returned.

    Returns
    -------
    `(value, tag_used, warnings)`. Both `value` and `tag_used` are None
    when no share count can be resolved. `warnings` carries any amendment
    or ambiguity flags encountered on the GAAP path (empty for the DEI path).
    """
    warnings: list[Warning] = []

    # --- 1. GAAP CommonStockSharesOutstanding ---
    if _GAAP_SHARES_TAG in gaap:
        entry = _get_instant_entry(instant_cache, gaap, _GAAP_SHARES_TAG, "shares")
        if entry is not None:
            value, matched_date = _get_instant_result(entry.instant_map, period_end)

            if value is not None and matched_date is not None:
                if matched_date in entry.amendment_keys:
                    warnings.append(warn(
                        WarningCode.AMENDMENT_EXISTS,
                        f"Amendment filing used for '{_GAAP_SHARES_TAG}'; period: {matched_date}.",
                        concept=_GAAP_SHARES_TAG,
                    ))
                return value, _GAAP_SHARES_TAG, warnings

            # Value not found - check for ambiguity nearby and record it.
            # Falls through to the DEI fallback regardless.
            ambiguous_date = _ambiguous_near(entry.ambiguous_keys, period_end)
            if ambiguous_date is not None:
                warnings.append(warn(
                    WarningCode.AMBIGUOUS_FACT,
                    f"Conflicting GAAP share counts for '{_GAAP_SHARES_TAG}' "
                    f"at {ambiguous_date}, falling back to DEI shares.",
                ))

    # --- 2. DEI EntityCommonStockSharesOutstanding (matched by accn) ---
    # Unlike the GAAP path, the DEI path has no amendment fallback: /A forms are
    # filtered out with no substitute, because the DEI tag has no dedup map.
    if _DEI_SHARES_TAG in dei:
        dei_candidates = [
            f for f in dei[_DEI_SHARES_TAG].get("units", {}).get("shares", [])
            if f.get("accn") == anchor_accn and not f.get("form", "").endswith("/A")
        ]
        if dei_candidates:
            distinct_values = {f["val"] for f in dei_candidates}
            if len(distinct_values) == 1:
                return Decimal(str(dei_candidates[0]["val"])), _DEI_SHARES_TAG, warnings
            # Multiple conflicting values from the same accession - ambiguous.
            # Return None rather than silently picking the first fact.

    return None, None, warnings


# ---------------------------------------------------------------------------
# Per-concept chain resolvers
# ---------------------------------------------------------------------------


def _resolve_flow(
    gaap: dict,
    chain: list[str],
    period_end: date,
    fiscal_year_start: date,
    *,
    unit: str = "USD",
    concept_name: str = "(unknown)",
    flow_cache: _FlowCache | None = None,
) -> _ConceptResult:
    """
    Walk a tag chain and return the first TTM value found.

    For each tag in order:
      - Look up the cached _FlowEntry (or build on demand).
      - Run the TTM bridge via `_get_ttm_value`.
      - If a value comes back, return immediately with TTM_ANNUALIZED and
        AMENDMENT_EXISTS warnings attached as appropriate.
    """
    pending_ambiguous: Warning | None = None

    for chain_index, tag in enumerate(chain):
        entry = _get_flow_entry(flow_cache, gaap, tag, unit)
        if entry is None:
            continue

        ttm_value, was_annualized = _get_ttm_value(
            entry.flow_map,
            period_end,
            fiscal_year_start,
            entry.annual_index,
            entry.prior_ytd_index,
        )

        if ttm_value is not None:
            return _ConceptResult(
                value=ttm_value,
                unit=unit,
                tag_used=tag,
                is_fallback=(chain_index > 0),
                warnings=_make_flow_warnings(
                    tag=tag,
                    fiscal_year_start=fiscal_year_start,
                    period_end=period_end,
                    amendment_keys=entry.amendment_keys,
                    was_annualized=was_annualized,
                    concept_name=concept_name,
                ),
            )

        # ttm_value is None - see if the current-YTD key was ambiguous.
        # Record the warning but keep trying subsequent tags, a fallback may
        # still produce an unambiguous value.
        current_ytd_key = (fiscal_year_start, period_end)
        if current_ytd_key in entry.ambiguous_keys and pending_ambiguous is None:
            # First-wins: only record the primary tag's ambiguity,
            # don't overwrite with a fallback's.
            pending_ambiguous = warn(
                WarningCode.AMBIGUOUS_FACT,
                f"Conflicting values for '{tag}' in period "
                f"({fiscal_year_start}-{period_end}), value is N/A.",
            )

    # Exhausted the chain without finding a value.
    final_warnings = [pending_ambiguous] if pending_ambiguous is not None else []
    return _ConceptResult(value=None, warnings=final_warnings)


def _resolve_instant(
    gaap: dict,
    chain: list[str],
    period_end: date,
    *,
    unit: str = "USD",
    instant_cache: _InstantCache | None = None,
) -> _ConceptResult:
    """
    Walk an instant (balance-sheet) tag chain and return the first value found.

    Mirrors `_resolve_flow` but uses point-in-time lookup with the +/- 7 day
    date tolerance from `_get_instant_result`.
    """
    pending_ambiguous: Warning | None = None

    for chain_index, tag in enumerate(chain):
        entry = _get_instant_entry(instant_cache, gaap, tag, unit)
        if entry is None:
            continue

        value, matched_date = _get_instant_result(entry.instant_map, period_end)

        if value is not None and matched_date is not None:
            result_warnings: list[Warning] = []
            if matched_date in entry.amendment_keys:
                result_warnings.append(warn(
                    WarningCode.AMENDMENT_EXISTS,
                    f"Amendment filing used for '{tag}'; period: {matched_date}.",
                    concept=tag,
                ))
            return _ConceptResult(
                value=value,
                unit=unit,
                tag_used=tag,
                is_fallback=(chain_index > 0),
                warnings=result_warnings,
            )

        # value is None - see if any date in the tolerance window is ambiguous.
        ambiguous_date = _ambiguous_near(entry.ambiguous_keys, period_end)
        if ambiguous_date is not None and pending_ambiguous is None:
            # First-wins: keep the primary tag's ambiguity warning, not a fallback's.
            pending_ambiguous = warn(
                WarningCode.AMBIGUOUS_FACT,
                f"Conflicting values for '{tag}' at {ambiguous_date}, value is N/A.",
            )

    # Exhausted the chain without finding a value.
    final_warnings = [pending_ambiguous] if pending_ambiguous is not None else []
    return _ConceptResult(value=None, warnings=final_warnings)


# ---------------------------------------------------------------------------
# Specialised extractors
# ---------------------------------------------------------------------------
# These wrap `_resolve_flow` / `_resolve_instant` with concept-specific
# warning logic (FALLBACK_REVENUE, FALLBACK_EPS_BASIC, LEASE_PRE_ASC842, etc.).
# Anything that just needs the resolver's standard behaviour bypasses these
# and calls the resolver directly.


def _extract_revenue(
    gaap: dict,
    period_end: date,
    fiscal_year_start: date,
    *,
    flow_cache: _FlowCache | None = None,
) -> _ConceptResult:
    """Resolve revenue, attach FALLBACK_REVENUE when a non-primary tag fires."""
    result = _resolve_flow(
        gaap,
        _FLOW_CHAINS["revenue"],
        period_end,
        fiscal_year_start,
        concept_name="Revenue",
        flow_cache=flow_cache,
    )
    if result.is_fallback and result.value is not None:
        result.warnings.append(warn(
            WarningCode.FALLBACK_REVENUE,
            f"Primary revenue tag absent, using fallback tag '{result.tag_used}'.",
        ))
    return result


def _extract_eps(
    gaap: dict,
    period_end: date,
    fiscal_year_start: date,
    *,
    flow_cache: _FlowCache | None = None,
) -> _ConceptResult:
    """
    Try diluted EPS first, then basic. Attach FALLBACK_EPS_BASIC when basic fires.

    The returned `_ConceptResult` always reports:
      unit        = "USD/shares"
      tag_used    = the XBRL tag that actually fired
      is_fallback = False for diluted, True for basic

    The caller records this under the stable concept name "EPS". The
    `is_fallback` flag together with the `fallback_eps_basic` warning make
    it unambiguous which tag was used.

    The label change ('P/E' -> 'P/E (basic)') is Phase 3's responsibility and
    is read from `AuditEntry.is_fallback`, this layer does not know labels.
    """
    result = _resolve_flow(
        gaap,
        list(_EPS_TAGS),
        period_end,
        fiscal_year_start,
        unit="USD/shares",
        concept_name="EPS",
        flow_cache=flow_cache,
    )
    if result.is_fallback and result.value is not None:
        result.warnings.append(warn(
            WarningCode.FALLBACK_EPS_BASIC,
            "Diluted EPS unavailable, using basic EPS. P/E labelled 'P/E (basic)'.",
        ))
    return result


def _extract_capex(
    gaap: dict,
    period_end: date,
    fiscal_year_start: date,
    *,
    flow_cache: _FlowCache | None = None,
) -> _ConceptResult:
    """Extract CapEx and normalise sign so the value is always a positive outflow."""
    result = _resolve_flow(
        gaap,
        _FLOW_CHAINS["capex"],
        period_end,
        fiscal_year_start,
        concept_name="CapEx",
        flow_cache=flow_cache,
    )
    if result.value is not None and result.value < 0:
        result.value = abs(result.value)
        result.warnings.append(warn(
            WarningCode.CAPEX_SIGN_NORMALIZED,
            "CapEx reported as negative outflow - absolute value taken.",
        ))
    return result


def _extract_cash(
    gaap: dict,
    period_end: date,
    *,
    instant_cache: _InstantCache | None = None,
) -> _ConceptResult:
    """Extract cash, warn when the fallback tag (which includes short-term investments) fires."""
    result = _resolve_instant(
        gaap, _INSTANT_CHAINS["cash"], period_end,
        instant_cache=instant_cache,
    )
    if result.is_fallback and result.value is not None:
        result.warnings.append(warn(
            WarningCode.CASH_FALLBACK_INCLUDES_INVESTMENTS,
            "Cash fallback tag includes short-term investments, "
            "EV cash deduction may be overstated.",
        ))
    return result


def _extract_finance_lease(
    gaap: dict,
    period_end: date,
    *,
    current: bool,
    instant_cache: _InstantCache | None = None,
) -> _ConceptResult:
    """
    Extract a finance lease liability (current or non-current).

    Attaches LEASE_PRE_ASC842 when the capital-lease fallback tag fires, since
    pre-ASC 842 capital-lease accounting differs from post-adoption finance
    leases.
    """
    chain_key = "finance_lease_current" if current else "finance_lease_noncurrent"
    fallback_tag = (
        _CAPITAL_LEASE_CURRENT_TAG if current else _CAPITAL_LEASE_NONCURRENT_TAG
    )

    result = _resolve_instant(
        gaap, _INSTANT_CHAINS[chain_key], period_end,
        instant_cache=instant_cache,
    )
    if result.is_fallback and result.value is not None:
        result.warnings.append(warn(
            WarningCode.LEASE_PRE_ASC842,
            f"Pre-ASC 842 capital lease tag used ({fallback_tag}), "
            "lease accounting differs from post-adoption periods.",
        ))
    return result


def _extract_debt(
    gaap: dict,
    period_end: date,
    *,
    instant_cache: _InstantCache | None = None,
) -> tuple[_ConceptResult, bool]:
    """
    Extract long-term debt.

    Returns `(result, used_total_lt_debt)`. `used_total_lt_debt=True`
    means `LongTermDebt` (which represents total long-term debt, current +
    non-current) was used instead of `LongTermDebtNoncurrent`. The caller
    must then zero out current_portion_lt_debt to avoid double-counting,
    since total LTD already includes the current portion.
    """
    result = _resolve_instant(
        gaap, _INSTANT_CHAINS["long_term_debt"], period_end,
        instant_cache=instant_cache,
    )
    # tag_used == "LongTermDebt" iff the total-debt fallback fired.
    # tag_used is None when both tags failed, None != "LongTermDebt" -> False.
    used_total = result.tag_used == "LongTermDebt"
    if used_total and result.value is not None:
        result.warnings.append(warn(
            WarningCode.DEBT_DEDUPLICATED,
            "LongTermDebt (total) used instead of LongTermDebtNoncurrent, "
            "current portion excluded separately to avoid double-counting.",
        ))
    return result, used_total


# ---------------------------------------------------------------------------
# Audit entry builder
# ---------------------------------------------------------------------------


def _build_audit_entry(concept: str, result: _ConceptResult) -> AuditEntry:
    """
    Build an `AuditEntry` from a `_ConceptResult`.

    `AuditEntry.value` stores the value used for this period's calculation -
    TTM-bridged for flow concepts, point-in-time for balance-sheet concepts.

    Note on `entity_context`: hardcoded to "consolidated" (the default in
    `AuditEntry`) because the extractor does not currently inspect XBRL
    context IDs to distinguish consolidated vs. segment-level facts. The
    deduplication rules favour the most authoritative filing, which in
    practice is always the consolidated report. A future phase could parse
    context IDs to label the rare segment-only case.
    """
    return AuditEntry(
        concept=concept,
        xbrl_tag=result.tag_used,
        is_fallback=result.is_fallback,
        unit=result.unit,
        value=result.value,
    )


# ---------------------------------------------------------------------------
# Per-anchor extraction
# ---------------------------------------------------------------------------


def _extract_for_anchor(
    anchor: _FilingAnchor,
    gaap: dict,
    dei: dict,
    is_capital_intensive: bool,
    flow_cache: _FlowCache,
    instant_cache: _InstantCache,
) -> ExtractedFinancials:
    """
    Extract all financials for one TTM anchor.

    Reads top-to-bottom: every concept produces a result, contributes its
    warnings, and appends an audit entry. Each block is three lines (or six
    for the specialised extractors that have extra inline warning logic).

    Price is left as None here. `extract_ttm_periods` fills it in
    asynchronously after this function returns.
    """
    period_end = anchor.period_end
    fy_start = anchor.fiscal_year_start

    warnings: list[Warning] = []
    audit: list[AuditEntry] = []

    # =======================================================================
    # Income statement (TTM bridge)
    # =======================================================================

    revenue_result = _extract_revenue(gaap, period_end, fy_start, flow_cache=flow_cache)
    warnings.extend(revenue_result.warnings)
    audit.append(_build_audit_entry("Revenue", revenue_result))

    operating_income_result = _resolve_flow(
        gaap, _FLOW_CHAINS["operating_income"], period_end, fy_start,
        concept_name="Operating Income", flow_cache=flow_cache,
    )
    warnings.extend(operating_income_result.warnings)
    audit.append(_build_audit_entry("Operating Income", operating_income_result))

    da_result = _resolve_flow(
        gaap, _FLOW_CHAINS["da"], period_end, fy_start,
        concept_name="Depreciation & Amortization", flow_cache=flow_cache,
    )
    warnings.extend(da_result.warnings)
    audit.append(_build_audit_entry("Depreciation & Amortization", da_result))

    net_income_result = _resolve_flow(
        gaap, _FLOW_CHAINS["net_income"], period_end, fy_start,
        concept_name="Net Income", flow_cache=flow_cache,
    )
    warnings.extend(net_income_result.warnings)
    audit.append(_build_audit_entry("Net Income", net_income_result))

    # EPS concept name is "EPS" regardless of which tag fires. The xbrl_tag
    # field records the exact tag used (diluted vs basic), is_fallback flags
    # the basic-EPS path, and FALLBACK_EPS_BASIC explains it in plain English.
    eps_result = _extract_eps(gaap, period_end, fy_start, flow_cache=flow_cache)
    warnings.extend(eps_result.warnings)
    audit.append(_build_audit_entry("EPS", eps_result))

    # =======================================================================
    # Cash flow (TTM bridge)
    # =======================================================================

    operating_cash_flow_result = _resolve_flow(
        gaap, _FLOW_CHAINS["operating_cash_flow"], period_end, fy_start,
        concept_name="Operating Cash Flow", flow_cache=flow_cache,
    )
    warnings.extend(operating_cash_flow_result.warnings)
    audit.append(_build_audit_entry("Operating Cash Flow", operating_cash_flow_result))

    capex_result = _extract_capex(gaap, period_end, fy_start, flow_cache=flow_cache)
    warnings.extend(capex_result.warnings)
    audit.append(_build_audit_entry("CapEx", capex_result))

    # =======================================================================
    # Balance sheet (point-in-time)
    # =======================================================================

    total_assets_result = _resolve_instant(
        gaap, _INSTANT_CHAINS["total_assets"], period_end,
        instant_cache=instant_cache,
    )
    warnings.extend(total_assets_result.warnings)
    audit.append(_build_audit_entry("Total Assets", total_assets_result))

    stockholders_equity_result = _resolve_instant(
        gaap, _INSTANT_CHAINS["stockholders_equity"], period_end,
        instant_cache=instant_cache,
    )
    warnings.extend(stockholders_equity_result.warnings)
    audit.append(_build_audit_entry("Stockholders Equity", stockholders_equity_result))

    # Long-term debt - may signal that total LTD was used, in which case the
    # current portion is zeroed out below to avoid double-counting.
    long_term_debt_result, used_total_lt_debt = _extract_debt(
        gaap, period_end, instant_cache=instant_cache,
    )
    warnings.extend(long_term_debt_result.warnings)
    audit.append(_build_audit_entry("Long-Term Debt", long_term_debt_result))

    # Current portion of LT debt - skipped (zeroed out) when total LTD was used.
    # Both branches produce a _ConceptResult so the warning/audit pattern is uniform.
    if used_total_lt_debt:
        current_portion_lt_debt_result = _ConceptResult(value=None)
    else:
        current_portion_lt_debt_result = _resolve_instant(
            gaap, _INSTANT_CHAINS["current_portion_lt_debt"], period_end,
            instant_cache=instant_cache,
        )
    warnings.extend(current_portion_lt_debt_result.warnings)
    audit.append(_build_audit_entry("Current Portion LT Debt", current_portion_lt_debt_result))

    short_term_borrowings_result = _resolve_instant(
        gaap, _INSTANT_CHAINS["short_term_borrowings"], period_end,
        instant_cache=instant_cache,
    )
    warnings.extend(short_term_borrowings_result.warnings)
    audit.append(_build_audit_entry("Short-Term Borrowings", short_term_borrowings_result))

    finance_lease_current_result = _extract_finance_lease(
        gaap, period_end, current=True, instant_cache=instant_cache,
    )
    warnings.extend(finance_lease_current_result.warnings)
    audit.append(_build_audit_entry("Finance Lease (Current)", finance_lease_current_result))

    finance_lease_noncurrent_result = _extract_finance_lease(
        gaap, period_end, current=False, instant_cache=instant_cache,
    )
    warnings.extend(finance_lease_noncurrent_result.warnings)
    audit.append(_build_audit_entry("Finance Lease (Non-Current)", finance_lease_noncurrent_result))

    cash_result = _extract_cash(gaap, period_end, instant_cache=instant_cache)
    warnings.extend(cash_result.warnings)
    audit.append(_build_audit_entry("Cash", cash_result))

    minority_interest_result = _resolve_instant(
        gaap, _INSTANT_CHAINS["minority_interest"], period_end,
        instant_cache=instant_cache,
    )
    warnings.extend(minority_interest_result.warnings)
    audit.append(_build_audit_entry("Minority Interest", minority_interest_result))

    preferred_stock_result = _resolve_instant(
        gaap, _INSTANT_CHAINS["preferred_stock"], period_end,
        instant_cache=instant_cache,
    )
    warnings.extend(preferred_stock_result.warnings)
    audit.append(_build_audit_entry("Preferred Stock", preferred_stock_result))

    # =======================================================================
    # Shares outstanding (point-in-time, no TTM bridge)
    # =======================================================================
    # `_get_shares` returns its own (value, tag, warnings) tuple rather than
    # a _ConceptResult, so the audit entry is built inline here.

    shares, shares_tag, shares_warnings = _get_shares(
        gaap, dei, period_end, anchor.accn, instant_cache=instant_cache,
    )
    warnings.extend(shares_warnings)
    audit.append(AuditEntry(
        concept="Shares Outstanding",
        xbrl_tag=shares_tag,
        unit="shares",
        value=shares,
    ))

    # =======================================================================
    # Sector-specific lease warning
    # =======================================================================
    # Robust to any reordering of the lease extraction above - reads from the
    # captured _ConceptResult values directly.

    if (is_capital_intensive
            and finance_lease_current_result.value is None
            and finance_lease_noncurrent_result.value is None):
        warnings.append(warn(
            WarningCode.FINANCE_LEASE_MISSING_CAPITAL_INTENSIVE,
            "Finance lease tags absent for a capital-intensive sector, "
            "EV may be understated.",
        ))

    # Warning deduplication is deferred to `extract_ttm_periods`, which runs
    # it after price warnings are attached. Running it here too would be safe
    # but redundant - duplicates from extraction (e.g. LEASE_PRE_ASC842 from
    # both the current and non-current leases) are caught in the final pass.

    return ExtractedFinancials(
        period_end=period_end,
        filing_date=anchor.filed,
        price=None,  # populated async by extract_ttm_periods
        shares_outstanding=shares,
        eps_diluted=eps_result.value,
        revenue=revenue_result.value,
        operating_income=operating_income_result.value,
        depreciation_and_amortization=da_result.value,
        net_income=net_income_result.value,
        operating_cash_flow=operating_cash_flow_result.value,
        capex=capex_result.value,
        total_assets=total_assets_result.value,
        stockholders_equity=stockholders_equity_result.value,
        long_term_debt=long_term_debt_result.value,
        short_term_borrowings=short_term_borrowings_result.value,
        current_portion_lt_debt=current_portion_lt_debt_result.value,
        finance_lease_current=finance_lease_current_result.value,
        finance_lease_noncurrent=finance_lease_noncurrent_result.value,
        cash=cash_result.value,
        minority_interest=minority_interest_result.value,
        preferred_stock=preferred_stock_result.value,
        audit=audit,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def extract_ttm_periods(
    companyfacts: dict,
    *,
    ticker: str | None = None,
    is_capital_intensive: bool = False,
) -> list[ExtractedFinancials]:
    """
    Extract up to 12 TTM periods from an EDGAR companyfacts dict.

    Parameters
    ----------
    companyfacts
        Raw JSON from EDGAR's /api/xbrl/companyfacts endpoint.
    ticker
        Exchange ticker for price lookup (e.g. 'AAPL'). If None, every
        period's `price` field is left as None.
    is_capital_intensive
        Whether the company is in a capital-intensive SIC range. Controls the
        FINANCE_LEASE_MISSING_CAPITAL_INTENSIVE warning.

    Returns
    -------
    Up to 12 `ExtractedFinancials`, most-recent-first. Prices are populated
    via a single batch yfinance download covering all anchor dates, `price`
    is None until the download completes.
    """
    gaap = companyfacts.get("facts", {}).get("us-gaap", {})
    dei = companyfacts.get("facts", {}).get("dei", {})

    # --- Step 1: discover filing anchors (union of all sentinel tags) ---
    anchors = _collect_filing_anchors(gaap)
    if not anchors:
        logger.warning("No 10-K/10-Q filing anchors found in companyfacts.")
        return []

    # --- Step 2: take the 12 most recent ---
    anchors.sort(key=lambda a: a.period_end, reverse=True)
    anchors = anchors[:MAX_TTM_PERIODS]

    # --- Step 3: pre-compute every fact map once, reuse across all anchors ---
    flow_cache = _precompute_flow_maps(gaap)
    instant_cache = _precompute_instant_maps(gaap)

    # --- Step 4: extract financials for each anchor (synchronous XBRL work) ---
    periods: list[ExtractedFinancials] = [
        _extract_for_anchor(a, gaap, dei, is_capital_intensive, flow_cache, instant_cache)
        for a in anchors
    ]

    # --- Step 5: fetch prices with a single batch download, apply sequentially ---
    # One yfinance request spans [min(filed)+1, max(filed)+14], covering all
    # anchor dates. This avoids Yahoo rate-limiting from up to 12 simultaneous
    # requests and removes ~11 network round-trips per API call.
    if ticker:
        fetched = await price_svc.get_prices(ticker, [a.filed for a in anchors])
        for ef, anchor in zip(periods, anchors):
            ef.price = fetched.get(anchor.filed)
            if ef.price is None:
                ef.warnings.append(warn(
                    WarningCode.PRICE_UNAVAILABLE,
                    f"Adjusted close price unavailable for {ticker} "
                    f"after {ef.filing_date}, price-dependent multiples are N/A.",
                ))

    # --- Step 6: deduplicate warnings once all warnings are attached ---
    # Running dedup here, after price warnings, is the only point that
    # covers the full union. LEASE_PRE_ASC842 can fire from both the current
    # and non-current lease extractors. PRICE_UNAVAILABLE can only fire once
    # per period, but running dedup makes that invariant robust to future
    # warning attachment points.
    for ef in periods:
        ef.warnings = dedup_warnings(ef.warnings)

    return periods