# backend/app/services/xbrl_warnings.py
"""
Warning helpers for the XBRL extraction layer.

Two responsibilities:

1. `_make_flow_warnings` - builds the standard warnings produced by a flow
   concept extraction (TTM annualization + amendment usage). Called from
   `_resolve_flow` and `_extract_eps` in xbrl.py.

2. `dedup_warnings` - collapses repeated warning codes within a single
   period into one aggregated message. Called once per period at the end of
   `extract_ttm_periods` and in the financials router.

Both functions are pure and depend only on the warning model - no XBRL data
structures, no I/O. They live here so xbrl.py can stay focused on extraction
logic.
"""

from __future__ import annotations

from datetime import date

from app.models.errors import Warning, WarningCode, warn

# ---------------------------------------------------------------------------
# Aggregation templates
# ---------------------------------------------------------------------------
# When the same warning code fires for several concepts in one period (e.g.
# TTM_ANNUALIZED for Revenue, OCF, and EPS), `dedup_warnings` collapses
# them into a single message that lists every affected concept.
#
# Aggregation uses Warning.concept (a structured field) rather than parsing
# the human-readable message, so wording changes never break deduplication.

_AGGREGATABLE_TEMPLATES: dict[str, str] = {
    WarningCode.TTM_ANNUALIZED.value: (
        "Prior-year data unavailable for {names}; TTM annualized from current YTD."
    ),
    WarningCode.AMENDMENT_EXISTS.value: (
        "Amendment filing used for {names}."
    ),
}


# ---------------------------------------------------------------------------
# Flow warning builder
# ---------------------------------------------------------------------------


def _make_flow_warnings(
    tag: str,
    fiscal_year_start: date,
    period_end: date,
    amendment_keys: set[tuple[date, date]],
    was_annualized: bool,
    concept_name: str,
) -> list[Warning]:
    """
    Build the TTM_ANNUALIZED and AMENDMENT_EXISTS warnings for a flow concept.

    Centralised here so future wording changes happen once. All flow extraction
    paths (revenue, EPS, operating income, ...) share the same message format
    by routing through this function.

    Known gap: only the current-YTD key `(fiscal_year_start, period_end)` is
    checked against `amendment_keys`. Prior-year components consumed by the
    TTM bridge (prior-FY annual, prior-YTD) are silently accepted even when
    their source is an amendment. Closing this requires `_get_ttm_value` to
    return the keys it consulted - deferred to a future phase.
    """
    warnings: list[Warning] = []

    if was_annualized:
        warnings.append(warn(
            WarningCode.TTM_ANNUALIZED,
            f"Prior-year YTD unavailable for {concept_name}; "
            "TTM annualized from current YTD.",
            concept=concept_name,
        ))

    if (fiscal_year_start, period_end) in amendment_keys:
        warnings.append(warn(
            WarningCode.AMENDMENT_EXISTS,
            f"Amendment filing used for '{tag}'; "
            f"({fiscal_year_start}-{period_end}).",
            concept=tag,
        ))

    return warnings


# ---------------------------------------------------------------------------
# Per-period warning deduplication
# ---------------------------------------------------------------------------


def dedup_warnings(warnings: list[Warning]) -> list[Warning]:
    """
    Collapse a period's warnings to one per code, preserving first-occurrence order.

    For aggregatable codes (TTM_ANNUALIZED, AMENDMENT_EXISTS), concept names
    from every occurrence are merged into a single message using Warning.concept.
    All other codes keep their first occurrence verbatim and discard later duplicates.

    Example. If TTM_ANNUALIZED fires for Revenue, OCF, and EPS in one period,
    the output reads:

        "Prior-year data unavailable for Revenue, Operating Cash Flow, EPS;
         TTM annualized from current YTD."

    rather than showing the same warning three times with different concept names.

    Note on the comparison key: `Warning.model_config` sets
    `use_enum_values=True`, so `w.code` is the underlying string (e.g.
    `"ttm_annualized"`), not the WarningCode enum member. The dict keys are
    therefore strings and is wrap with `WarningCode(code)` when reconstructing
    a Warning for the aggregated output.
    """
    first_seen: dict[str, Warning] = {}
    aggregated_names: dict[str, list[str]] = {}

    for w in warnings:
        code = w.code  # already a str - see docstring note
        if code not in first_seen:
            first_seen[code] = w
        if code in _AGGREGATABLE_TEMPLATES and w.concept is not None:
            aggregated_names.setdefault(code, []).append(w.concept)

    result: list[Warning] = []
    for code, original in first_seen.items():
        if code in _AGGREGATABLE_TEMPLATES and aggregated_names.get(code):
            unique_names = ", ".join(dict.fromkeys(aggregated_names[code]))
            result.append(Warning(
                code=WarningCode(code),
                message=_AGGREGATABLE_TEMPLATES[code].format(names=unique_names),
            ))
        else:
            result.append(original)
    return result