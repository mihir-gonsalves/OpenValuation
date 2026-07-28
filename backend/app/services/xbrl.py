# backend/app/services/xbrl.py
"""
XBRL extraction service.

Converts a raw EDGAR companyfacts dict into a list of `ExtractedFinancials`,
one per TTM period (up to 12, most-recent-first).

Module structure
----------------
This file owns the tag chains, anchor discovery, the concept resolvers, and the
public entry point. It is the only file that knows which tags mean what.
`xbrl_maps.py` is the tag-agnostic data machinery (dedup, map builders, the
bridge, instant lookup) and `xbrl_warnings.py` owns warning wording and dedup.

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

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.models.errors import Warning, WarningCode, warn
from app.models.financials import AuditEntry, ExtractedFinancials
from app.services import price as price_svc
from app.services.xbrl_maps import (
    _FlowCache, _FlowEntry, _InstantCache, _InstantEntry, _MAX_ANNUAL_DAYS,
    _ambiguous_near, _build_flow_map, _build_instant_map, _get_instant_result, _get_ttm_value,
)
from app.services.xbrl_warnings import dedup_warnings, _make_flow_warnings

logger = logging.getLogger(__name__)

MAX_TTM_PERIODS = 12

_MIN_ANCHOR_DURATION_DAYS = 45
"""
Shortest fact duration accepted when deriving `fiscal_year_start`. Excludes
sub-quarterly stub data while still capturing Q1 YTD facts (≈ 85 days).
"""

# ---------------------------------------------------------------------------
# Tag chains: concept -> ordered fallback chain
# ---------------------------------------------------------------------------
# Resolvers walk each list in order and take the first tag that yields a value.
# The first tag is primary, and any later tag firing is a fallback that triggers that concept's FALLBACK_* warning.

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

# EPS tags (USD/shares), shared by _precompute_flow_maps and _extract_eps.
_EPS_TAGS = ("EarningsPerShareDiluted", "EarningsPerShareBasic")

# Instant concepts (balance-sheet items, point-in-time). Unit: USD.
_INSTANT_CHAINS: dict[str, list[str]] = {
    "total_assets": ["Assets"],
    "stockholders_equity": [
        "StockholdersEquity",
    ],
    # The LongTermDebt fallback triggers the dedup logic in _extract_debt.
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

# Pre-ASC 842 capital-lease fallback tags.
_CAPITAL_LEASE_CURRENT_TAG = "CapitalLeaseObligationsCurrent"
_CAPITAL_LEASE_NONCURRENT_TAG = "CapitalLeaseObligationsNoncurrent"

# Shares-outstanding tags.
_GAAP_SHARES_TAG = "CommonStockSharesOutstanding"  # instant, end = period_end
_DEI_SHARES_TAG = "EntityCommonStockSharesOutstanding"  # instant, end = report date

# Sentinel tags for discovering 10-K/10-Q anchors. Built from the revenue chain
# so that every tag accepted as revenue is an anchor source by construction.
_ANCHOR_DISCOVERY_TAGS: tuple[str, ...] = (
    *_FLOW_CHAINS["revenue"],
    "OperatingIncomeLoss",
    "NetIncomeLoss",
)


@dataclass
class _FilingAnchor:
    """
    One 10-K or 10-Q filing, the reference point for one TTM period.

    Identity is the accession number. `filed` drives the price lookup,
    `fiscal_year_start` drives the bridge, and the declared fiscal year and
    period drive the display label.
    """
    accn: str
    filed: date
    period_end: date
    fiscal_year_start: date
    fiscal_year: int | None = None
    fiscal_period: str | None = None


@dataclass
class _ConceptResult:
    """
    The result of resolving one concept from a tag chain.
    """
    value: Decimal | None
    unit: str | None = None
    tag_used: str | None = None
    is_fallback: bool = False
    warnings: list[Warning] = field(default_factory=list)


def _precompute_flow_maps(gaap: dict) -> _FlowCache:
    """
    Build deduplicated flow maps for every flow and EPS tag, once per payload.

    Reusing these across all 12 anchors takes the work from
    O(anchors × tags × facts) down to O(tags × facts). Each entry also carries
    the two bridge lookup indexes built in xbrl_maps.py.
    """
    cache: _FlowCache = {}

    for chain in _FLOW_CHAINS.values():
        for tag in chain:
            key = (tag, "USD")
            if key in cache or tag not in gaap:
                continue
            facts = gaap[tag].get("units", {}).get("USD", [])
            if facts:
                cache[key] = _build_flow_map(facts)

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
    Build deduplicated instant maps for every balance-sheet tag and GAAP shares.
    """
    cache: _InstantCache = {}

    for chain in _INSTANT_CHAINS.values():
        for tag in chain:
            key = (tag, "USD")
            if key in cache or tag not in gaap:
                continue
            facts = gaap[tag].get("units", {}).get("USD", [])
            if facts:
                cache[key] = _build_instant_map(facts)

    if _GAAP_SHARES_TAG in gaap:
        facts = gaap[_GAAP_SHARES_TAG].get("units", {}).get("shares", [])
        if facts:
            cache[(_GAAP_SHARES_TAG, "shares")] = _build_instant_map(facts)

    return cache


def _collect_filing_anchors(gaap: dict) -> list[_FilingAnchor]:
    """
    Discover every unique 10-K / 10-Q by unioning the accession numbers behind
    all the sentinel tags.

    No early exit after the first tag with facts: a company that changed its
    revenue tag mid-history has old filings discoverable only via the old tag
    and new ones only via the new tag, so a first-hit scan would silently drop
    half the window.

    Amendments are excluded here. Their values still flow through the dedup
    layer in `_build_*_map`.
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
        # All facts in one accession cover the same reporting period.
        period_end = max(date.fromisoformat(f["end"]) for f in facts)

        filed = date.fromisoformat(facts[0]["filed"])

        # The longest fact ending at period_end starts on the fiscal year's
        # first day, whether that fact is a 10-K's annual figure or a 10-Q's
        # YTD. The 45-day minimum filters out sub-quarterly stub data.
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

        # The issuer's own fiscal focus, carried on every fact. Authoritative
        # where date derivation is not: NRF-calendar retailers name the year
        # ending in Jan/Feb by the prior calendar year.
        focus = Counter(
            (f.get("fy"), f.get("fp"))
            for f in facts
            if date.fromisoformat(f["end"]) == period_end and f.get("fy") and f.get("fp")
        )
        fiscal_year, fiscal_period = focus.most_common(1)[0][0] if focus else (None, None)

        anchors.append(_FilingAnchor(
            accn=accn,
            filed=filed,
            period_end=period_end,
            fiscal_year_start=fiscal_year_start,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
        ))

    # --- Step 3: deduplicate anchors by period_end, keeping the later filing ---
    # Only fires when two non-amendment filings legitimately share a period_end.
    by_period: dict[date, _FilingAnchor] = {}
    for anchor in anchors:
        existing = by_period.get(anchor.period_end)
        if existing is None or anchor.filed > existing.filed:
            by_period[anchor.period_end] = anchor
    return list(by_period.values())


# ---------------------------------------------------------------------------
# Cache lookup helpers
# ---------------------------------------------------------------------------
# The `cache is None` branch keeps the resolvers usable in isolation, e.g. unit
# tests operating on a gaap dict with no precompute step. Production always passes a cache.


def _get_flow_entry(
    flow_cache: _FlowCache | None,
    gaap: dict,
    tag: str,
    unit: str,
) -> _FlowEntry | None:
    """
    Cached lookup, building on demand when there is no cache.
    """
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
    """
    Cached lookup, building on demand when there is no cache.
    """
    if instant_cache is not None:
        return instant_cache.get((tag, unit))
    facts = gaap.get(tag, {}).get("units", {}).get(unit, [])
    return _build_instant_map(facts) if facts else None


def _get_shares(
    gaap: dict,
    dei: dict,
    period_end: date,
    anchor_accn: str,
    *,
    instant_cache: _InstantCache | None = None,
) -> tuple[Decimal | None, str | None, list[Warning]]:
    """
    Resolve shares outstanding, returning `(value, tag_used, warnings)`.

    GAAP `CommonStockSharesOutstanding` comes first, matched to period_end
    through the deduplicated instant map so the usual rules apply (originals
    over amendments, conflicts detected). The DEI tag is the fallback and must
    be matched by accession, since its `end` is the report date rather than the
    period end. Two conflicting DEI facts in one accession return None rather
    than a silently chosen first value.
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
                        f"Amendment filing used for '{_GAAP_SHARES_TAG}', period: {matched_date}.",
                        concept=_GAAP_SHARES_TAG,
                    ))
                return value, _GAAP_SHARES_TAG, warnings

            # No value. Record any nearby ambiguity, then fall through to DEI.
            ambiguous_date = _ambiguous_near(entry.ambiguous_keys, period_end)
            if ambiguous_date is not None:
                warnings.append(warn(
                    WarningCode.AMBIGUOUS_FACT,
                    f"Conflicting GAAP share counts for '{_GAAP_SHARES_TAG}' "
                    f"at {ambiguous_date}, falling back to DEI shares.",
                ))

    # --- 2. DEI EntityCommonStockSharesOutstanding, matched by accession ---
    # No amendment fallback here: the DEI tag has no dedup map, so /A forms are
    # filtered out with no substitute.
    if _DEI_SHARES_TAG in dei:
        dei_candidates = [
            f for f in dei[_DEI_SHARES_TAG].get("units", {}).get("shares", [])
            if f.get("accn") == anchor_accn and not f.get("form", "").endswith("/A")
        ]
        if dei_candidates:
            distinct_values = {f["val"] for f in dei_candidates}
            if len(distinct_values) == 1:
                return Decimal(str(dei_candidates[0]["val"])), _DEI_SHARES_TAG, warnings
            # Conflicting values in one accession. None beats a guess.

    return None, None, warnings


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
    Walk a tag chain and return the first TTM value the bridge produces, with any annualization or amendment warnings attached.
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

        # No value. Record any ambiguity but keep walking, since a fallback tag may still produce a clean one.
        current_ytd_key = (fiscal_year_start, period_end)
        if current_ytd_key in entry.ambiguous_keys and pending_ambiguous is None:
            # First-wins, so the message names the tag the user expects.
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
    Balance-sheet counterpart to `_resolve_flow`, using the point-in-time lookup and its +/-7 day tolerance.
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
                    f"Amendment filing used for '{tag}', period: {matched_date}.",
                    concept=tag,
                ))
            return _ConceptResult(value=value, unit=unit, tag_used=tag, is_fallback=(chain_index > 0), warnings=result_warnings)

        # No value. Check the whole tolerance window for ambiguity, so a fact
        # a few days off period-end warns instead of reading as absent.
        ambiguous_date = _ambiguous_near(entry.ambiguous_keys, period_end)
        if ambiguous_date is not None and pending_ambiguous is None:
            pending_ambiguous = warn(
                WarningCode.AMBIGUOUS_FACT,
                f"Conflicting values for '{tag}' at {ambiguous_date}, value is N/A.",
            )

    # Exhausted the chain without finding a value.
    final_warnings = [pending_ambiguous] if pending_ambiguous is not None else []
    return _ConceptResult(value=None, warnings=final_warnings)


# ---------------------------------------------------------------------------
# Specialized extractors
# ---------------------------------------------------------------------------
# Wrappers that add concept-specific warning logic. Concepts needing only the
# resolver's standard behavior call it directly.


def _extract_revenue(
    gaap: dict,
    period_end: date,
    fiscal_year_start: date,
    *,
    flow_cache: _FlowCache | None = None,
) -> _ConceptResult:
    """
    Resolve revenue, attach FALLBACK_REVENUE when a non-primary tag fires.
    """
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
    Try diluted EPS first, then basic, attaching FALLBACK_EPS_BASIC on the
    basic path.

    The caller records this under the stable concept name "EPS", so
    `is_fallback` and the warning are what disambiguate which tag fired. The
    'P/E (basic)' label is multiples.py's job, read off `AuditEntry`.
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
            "Diluted EPS unavailable, using basic EPS. P/E labeled 'P/E (basic)'.",
        ))
    return result


def _extract_capex(
    gaap: dict,
    period_end: date,
    fiscal_year_start: date,
    *,
    flow_cache: _FlowCache | None = None,
) -> _ConceptResult:
    """
    Extract CapEx and normalize sign so the value is always a positive outflow.
    """
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
    """
    Extract cash, warn when the fallback tag (which includes short-term investments) fires.
    """
    result = _resolve_instant(
        gaap,
        _INSTANT_CHAINS["cash"],
        period_end,
        instant_cache=instant_cache,
    )
    if result.is_fallback and result.value is not None:
        result.warnings.append(warn(
            WarningCode.CASH_FALLBACK_INCLUDES_INVESTMENTS,
            "Cash fallback tag includes short-term investments, EV cash deduction may be overstated."
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
    Extract a finance lease liability, warning when the pre-ASC 842 fallback
    fires, since that accounting differs from post-adoption finance leases.
    """
    chain_key = "finance_lease_current" if current else "finance_lease_noncurrent"
    fallback_tag = (_CAPITAL_LEASE_CURRENT_TAG if current else _CAPITAL_LEASE_NONCURRENT_TAG)

    result = _resolve_instant(
        gaap,
        _INSTANT_CHAINS[chain_key],
        period_end,
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
    Extract long-term debt, returning `(result, used_total_lt_debt)`.

    A True flag means the `LongTermDebt` total fired instead of the noncurrent
    tag. Since that total already includes the current portion, the caller has
    to zero out current_portion_lt_debt to avoid double-counting.
    """
    result = _resolve_instant(
        gaap, 
        _INSTANT_CHAINS["long_term_debt"],
        period_end,
        instant_cache=instant_cache
    )
    # None when both tags failed, which correctly reads as False.
    used_total = result.tag_used == "LongTermDebt"
    if used_total and result.value is not None:
        result.warnings.append(warn(
            WarningCode.DEBT_DEDUPLICATED,
            "LongTermDebt (total) used instead of LongTermDebtNoncurrent, "
            "current portion excluded separately to avoid double-counting.",
        ))
    return result, used_total


def _build_audit_entry(concept: str, result: _ConceptResult) -> AuditEntry:
    """
    Build an `AuditEntry` from a `_ConceptResult`.
    """
    return AuditEntry(concept=concept, xbrl_tag=result.tag_used, is_fallback=result.is_fallback, unit=result.unit, value=result.value)

def _extract_for_anchor(
    anchor: _FilingAnchor,
    gaap: dict,
    dei: dict,
    is_capital_intensive: bool,
    flow_cache: _FlowCache,
    instant_cache: _InstantCache,
) -> ExtractedFinancials:
    """
    Extract every financial for one TTM anchor.

    Reads top to bottom: each concept produces a result, contributes its
    warnings, and appends an audit entry. Price is left None for
    `extract_ttm_periods` to fill in.
    """
    period_end = anchor.period_end
    fy_start = anchor.fiscal_year_start

    warnings: list[Warning] = []
    audit: list[AuditEntry] = []

    # --- Market data, first so the audit reads like a model header ---
    # _get_shares returns a tuple rather than a _ConceptResult, so its audit
    # entry is built inline.
    shares, shares_tag, shares_warnings = _get_shares(
        gaap,
        dei,
        period_end,
        anchor.accn,
        instant_cache=instant_cache,
    )
    warnings.extend(shares_warnings)
    audit.append(AuditEntry(concept="Shares Outstanding", xbrl_tag=shares_tag, unit="shares", value=shares))

    # The concept name stays "EPS" whichever tag fires. xbrl_tag and
    # is_fallback are what record the diluted-vs-basic distinction.
    eps_result = _extract_eps(
        gaap,
        period_end,
        fy_start,
        flow_cache=flow_cache,
    )
    warnings.extend(eps_result.warnings)
    audit.append(_build_audit_entry("EPS", eps_result))

    # --- Income statement (TTM bridge) ---
    revenue_result = _extract_revenue(
        gaap,
        period_end,
        fy_start,
        flow_cache=flow_cache,
    )
    warnings.extend(revenue_result.warnings)
    audit.append(_build_audit_entry("Revenue", revenue_result))

    operating_income_result = _resolve_flow(
        gaap,
        _FLOW_CHAINS["operating_income"],
        period_end,
        fy_start,
        concept_name="Operating Income",
        flow_cache=flow_cache,
    )
    warnings.extend(operating_income_result.warnings)
    audit.append(_build_audit_entry("Operating Income", operating_income_result))

    da_result = _resolve_flow(
        gaap,
        _FLOW_CHAINS["da"],
        period_end,
        fy_start,
        concept_name="Depreciation & Amortization",
        flow_cache=flow_cache,
    )
    warnings.extend(da_result.warnings)
    audit.append(_build_audit_entry("Depreciation & Amortization", da_result))

    net_income_result = _resolve_flow(
        gaap,
        _FLOW_CHAINS["net_income"],
        period_end,
        fy_start,
        concept_name="Net Income",
        flow_cache=flow_cache,
    )
    warnings.extend(net_income_result.warnings)
    audit.append(_build_audit_entry("Net Income", net_income_result))

    # --- Cash flow (TTM bridge) ---
    operating_cash_flow_result = _resolve_flow(
        gaap,
        _FLOW_CHAINS["operating_cash_flow"],
        period_end,
        fy_start,
        concept_name="Operating Cash Flow",
        flow_cache=flow_cache,
    )
    warnings.extend(operating_cash_flow_result.warnings)
    audit.append(_build_audit_entry("Operating Cash Flow", operating_cash_flow_result))

    capex_result = _extract_capex(
        gaap,
        period_end,
        fy_start,
        flow_cache=flow_cache,
    )
    warnings.extend(capex_result.warnings)
    audit.append(_build_audit_entry("CapEx", capex_result))

    # --- Balance sheet, book value ---
    total_assets_result = _resolve_instant(
        gaap,
        _INSTANT_CHAINS["total_assets"],
        period_end,
        instant_cache=instant_cache,
    )
    warnings.extend(total_assets_result.warnings)
    audit.append(_build_audit_entry("Total Assets", total_assets_result))

    stockholders_equity_result = _resolve_instant(
        gaap,
        _INSTANT_CHAINS["stockholders_equity"],
        period_end,
        instant_cache=instant_cache,
    )
    warnings.extend(stockholders_equity_result.warnings)
    audit.append(_build_audit_entry("Stockholders Equity", stockholders_equity_result))

    # --- Balance sheet, ordered to mirror the EV buildup ---
    long_term_debt_result, used_total_lt_debt = _extract_debt(
        gaap,
        period_end,
        instant_cache=instant_cache,
    )
    warnings.extend(long_term_debt_result.warnings)
    audit.append(_build_audit_entry("Long-Term Debt", long_term_debt_result))

    short_term_borrowings_result = _resolve_instant(
        gaap,
        _INSTANT_CHAINS["short_term_borrowings"],
        period_end,
        instant_cache=instant_cache,
    )
    warnings.extend(short_term_borrowings_result.warnings)
    audit.append(_build_audit_entry("Short-Term Borrowings", short_term_borrowings_result))

    # Zeroed out when the total-LTD fallback already covered it. Both branches
    # produce a _ConceptResult so the warning and audit pattern stays uniform.
    if used_total_lt_debt:
        current_portion_lt_debt_result = _ConceptResult(value=None)
    else:
        current_portion_lt_debt_result = _resolve_instant(
            gaap,
            _INSTANT_CHAINS["current_portion_lt_debt"],
            period_end,
            instant_cache=instant_cache,
        )
    warnings.extend(current_portion_lt_debt_result.warnings)
    audit.append(_build_audit_entry("Current Portion LT Debt", current_portion_lt_debt_result))

    finance_lease_current_result = _extract_finance_lease(
        gaap,
        period_end,
        current=True,
        instant_cache=instant_cache,
    )
    warnings.extend(finance_lease_current_result.warnings)
    audit.append(_build_audit_entry("Finance Lease (Current)", finance_lease_current_result))

    finance_lease_noncurrent_result = _extract_finance_lease(
        gaap,
        period_end,
        current=False,
        instant_cache=instant_cache,
    )
    warnings.extend(finance_lease_noncurrent_result.warnings)
    audit.append(_build_audit_entry("Finance Lease (Non-Current)", finance_lease_noncurrent_result))

    minority_interest_result = _resolve_instant(
        gaap,
        _INSTANT_CHAINS["minority_interest"],
        period_end,
        instant_cache=instant_cache,
    )
    warnings.extend(minority_interest_result.warnings)
    audit.append(_build_audit_entry("Minority Interest", minority_interest_result))

    preferred_stock_result = _resolve_instant(
        gaap,
        _INSTANT_CHAINS["preferred_stock"],
        period_end,
        instant_cache=instant_cache,
    )
    warnings.extend(preferred_stock_result.warnings)
    audit.append(_build_audit_entry("Preferred Stock", preferred_stock_result))

    cash_result = _extract_cash(
        gaap,
        period_end,
        instant_cache=instant_cache,
    )
    warnings.extend(cash_result.warnings)
    audit.append(_build_audit_entry("Cash", cash_result))

    # Reads the captured results, so it survives any reordering above.
    if (
        is_capital_intensive
        and finance_lease_current_result.value is None
        and finance_lease_noncurrent_result.value is None
    ):
        warnings.append(warn(
            WarningCode.FINANCE_LEASE_MISSING_CAPITAL_INTENSIVE,
            "Finance lease tags absent for a capital-intensive sector, EV may be understated.",
        ))

    # Dedup is deferred to extract_ttm_periods, which runs it once the price
    # warnings are attached and the union is complete.

    return ExtractedFinancials(
        period_end=period_end,
        fiscal_year_start=anchor.fiscal_year_start,
        fiscal_year=anchor.fiscal_year,
        fiscal_period=anchor.fiscal_period,
        filing_date=anchor.filed,
        price=None,  # filled in by extract_ttm_periods
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
        minority_interest=minority_interest_result.value,
        preferred_stock=preferred_stock_result.value,
        cash=cash_result.value,
        audit=audit,
        warnings=warnings,
    )


async def extract_ttm_periods(
    companyfacts: dict,
    *,
    ticker: str | None = None,
    is_capital_intensive: bool = False,
) -> list[ExtractedFinancials]:
    """
    Extract up to 12 TTM periods from a companyfacts dict, most-recent-first.

    Without a `ticker` every price is left None. `is_capital_intensive` gates
    the missing-finance-lease warning. Prices come from one batch download
    covering every anchor date.
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

    # --- Step 5: one batch price download spanning every anchor date ---
    # Replaces up to 12 simultaneous requests, which Yahoo rate-limits.
    if ticker:
        fetched = await price_svc.get_prices(ticker, [a.filed for a in anchors])
        for ef, anchor in zip(periods, anchors):
            ef.price = fetched.get(anchor.filed)
            if ef.price is None:
                ef.warnings.append(warn(
                    WarningCode.PRICE_UNAVAILABLE,
                    f"Adjusted close price unavailable for {ticker} after {ef.filing_date}, price-dependent multiples are N/A.",
                ))

    # --- Step 6: dedup, the only point that sees the full union ---
    # LEASE_PRE_ASC842 can fire from both the current and non-current leases.
    for ef in periods:
        ef.warnings = dedup_warnings(ef.warnings)

    return periods