# backend/app/services/xbrl_warnings.py
"""
Warning construction and per-period deduplication for the extraction layer.

Both functions are pure and touch only the warning model, which keeps message
wording out of the extraction logic.
"""

from __future__ import annotations

from datetime import date

from app.models.errors import Warning, WarningCode, warn

# Codes that collapse into one message listing every affected concept. The
# aggregation reads Warning.concept rather than parsing the message, so wording
# changes cannot break it.

_AGGREGATABLE_TEMPLATES: dict[str, str] = {
    WarningCode.TTM_ANNUALIZED.value: (
        "Prior-year data unavailable for {names}. TTM annualized from current YTD."
    ),
    WarningCode.AMENDMENT_EXISTS.value: (
        "Amendment filing used for {names}."
    ),
}


def _make_flow_warnings(
    tag: str,
    fiscal_year_start: date,
    period_end: date,
    amendment_keys: set[tuple[date, date]],
    was_annualized: bool,
    concept_name: str,
) -> list[Warning]:
    """
    The TTM_ANNUALIZED and AMENDMENT_EXISTS warnings for a flow concept.

    Known gap: only the current-YTD key is checked against `amendment_keys`, so
    the bridge's two prior-year components are accepted even when their source
    was an amendment. Closing it needs `_get_ttm_value` to return the keys it
    consulted. Deferred.
    """
    warnings: list[Warning] = []

    if was_annualized:
        warnings.append(warn(
            WarningCode.TTM_ANNUALIZED,
            f"Prior-year YTD unavailable for {concept_name}. "
            "TTM annualized from current YTD.",
            concept=concept_name,
        ))

    if (fiscal_year_start, period_end) in amendment_keys:
        warnings.append(warn(
            WarningCode.AMENDMENT_EXISTS,
            f"Amendment filing used for '{tag}'. "
            f"({fiscal_year_start}-{period_end}).",
            concept=tag,
        ))

    return warnings


def dedup_warnings(warnings: list[Warning]) -> list[Warning]:
    """
    Collapse a period's warnings to one per code, in first-occurrence order.

    The aggregatable codes merge their concept names into one message, so
    TTM_ANNUALIZED firing for Revenue, OCF, and EPS reads as a single line
    naming all three. Every other code keeps its first occurrence verbatim.

    `use_enum_values=True` means `w.code` is already the underlying string, so
    the keys here are strings and get re-wrapped for the aggregated output.
    """
    first_seen: dict[str, Warning] = {}
    aggregated_names: dict[str, list[str]] = {}

    for w in warnings:
        code = w.code
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