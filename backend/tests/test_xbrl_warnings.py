# backend/tests/test_xbrl_warnings.py
"""
Tests for app/services/xbrl_warnings.py.

Coverage
--------
TestDedupWarningsHelper         dedup_warnings - basic properties
TestDedupWarningsAggregation    AMENDMENT_EXISTS aggregation # aggregation regression guard
TestMakeFlowWarnings            _make_flow_warnings in isolation
test_no_orphan_warning_codes    every WarningCode is raised or documented # orphan-code guard
"""

from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal

import pytest

from app.models.errors import Warning, WarningCode, warn
from app.services.xbrl_warnings import dedup_warnings, _make_flow_warnings


# ===========================================================================
# TestDedupWarningsHelper
# (moved from Section D of test_xbrl.py)
# ===========================================================================

class TestDedupWarningsHelper:
    """Direct tests for dedup_warnings."""

    def test_distinct_codes_unchanged(self):
        result = dedup_warnings([
            warn(WarningCode.PRICE_UNAVAILABLE, "no price"),
            warn(WarningCode.EV_DEBT_MISSING, "no debt"),
        ])
        assert len(result) == 2

    def test_non_aggregatable_duplicates_collapsed_to_first(self):
        result = dedup_warnings([
            warn(WarningCode.PRICE_UNAVAILABLE, "first"),
            warn(WarningCode.PRICE_UNAVAILABLE, "second"),
        ])
        assert len(result) == 1
        assert result[0].message == "first"

    def test_aggregatable_codes_merged_into_one(self):
        result = dedup_warnings([
            warn(WarningCode.TTM_ANNUALIZED, "...", concept="Revenue"),
            warn(WarningCode.TTM_ANNUALIZED, "...", concept="Operating Cash Flow"),
        ])
        assert len(result) == 1
        assert "Revenue" in result[0].message
        assert "Operating Cash Flow" in result[0].message

    def test_order_preserved(self):
        result = dedup_warnings([
            warn(WarningCode.PRICE_UNAVAILABLE, "price"),
            warn(WarningCode.EV_DEBT_MISSING, "debt"),
            warn(WarningCode.CAPEX_SIGN_NORMALIZED, "capex"),
        ])
        assert [w.code for w in result] == [
            "price_unavailable", "ev_debt_missing", "capex_sign_normalized",
        ]


# ===========================================================================
# TestDedupWarningsAggregation  # aggregation regression guard
# ===========================================================================

class TestDedupWarningsAggregation:
    """Aggregatable codes must merge concept names into a single message.

    Option B normalized AMENDMENT_EXISTS messages to use '; period <date>.'
    suffix so the existing regex can extract the concept name from the
    quoted tag between 'for' and ';'.
    """

    def test_amendment_exists_aggregates_two_flow_tags(self):
        """Two AMENDMENT_EXISTS warnings with concept field collapse to one."""
        result = dedup_warnings([
            warn(WarningCode.AMENDMENT_EXISTS, "...", concept="LongTermDebt"),
            warn(WarningCode.AMENDMENT_EXISTS, "...", concept="Assets"),
        ])
        assert len(result) == 1
        msg = result[0].message
        assert "LongTermDebt" in msg
        assert "Assets" in msg

    def test_amendment_exists_aggregates_shares_and_flow(self):
        """Shares + flow amendment warnings must both appear in the merged message."""
        result = dedup_warnings([
            warn(WarningCode.AMENDMENT_EXISTS, "...", concept="CommonStockSharesOutstanding"),
            warn(WarningCode.AMENDMENT_EXISTS, "...", concept="LongTermDebt"),
        ])
        assert len(result) == 1
        msg = result[0].message
        assert "CommonStockSharesOutstanding" in msg
        assert "LongTermDebt" in msg

    def test_amendment_exists_dedupes_repeated_tag(self):
        """Same tag named twice - should appear exactly once in the merged message."""
        result = dedup_warnings([
            warn(WarningCode.AMENDMENT_EXISTS, "...", concept="LongTermDebt"),
            warn(WarningCode.AMENDMENT_EXISTS, "...", concept="LongTermDebt"),
        ])
        assert len(result) == 1
        assert result[0].message.count("LongTermDebt") == 1

    def test_ttm_annualized_aggregates_three_concepts(self):
        """TTM_ANNUALIZED aggregation uses concept field, not message format."""
        result = dedup_warnings([
            warn(WarningCode.TTM_ANNUALIZED, "...", concept="Revenue"),
            warn(WarningCode.TTM_ANNUALIZED, "...", concept="Operating Cash Flow"),
            warn(WarningCode.TTM_ANNUALIZED, "...", concept="CapEx"),
        ])
        assert len(result) == 1
        assert "Revenue" in result[0].message
        assert "Operating Cash Flow" in result[0].message
        assert "CapEx" in result[0].message

    @pytest.mark.asyncio
    async def test_multiple_amendment_concepts_aggregate_into_single_warning(self):
        """End-to-end: OCF and EPS from amendment-only sources produce one merged warning."""
        from app.services.xbrl import extract_ttm_periods
        gaap = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": [
                    {"start": "2024-01-01", "end": "2024-12-31", "val": 5_000_000,
                     "form": "10-K", "accn": "0001", "filed": "2025-02-01"},
                ]}
            },
            "NetCashProvidedByUsedInOperatingActivities": {
                "units": {"USD": [
                    {"start": "2024-01-01", "end": "2024-12-31", "val": 2_000_000,
                     "form": "10-K/A", "accn": "0099", "filed": "2025-03-01"},
                ]}
            },
            "EarningsPerShareDiluted": {
                "units": {"USD/shares": [
                    {"start": "2024-01-01", "end": "2024-12-31", "val": 1.50,
                     "form": "10-K/A", "accn": "0099", "filed": "2025-03-01"},
                ]}
            },
        }
        periods = await extract_ttm_periods(
            {"facts": {"us-gaap": gaap, "dei": {}}}, ticker=None,
        )
        p = periods[0]
        amendment_warnings = [w for w in p.warnings if w.code == "amendment_exists"]
        assert len(amendment_warnings) == 1, (
            f"Expected exactly one amendment_exists warning after dedup, "
            f"got {len(amendment_warnings)}"
        )
        msg = amendment_warnings[0].message
        assert "NetCashProvidedByUsedInOperatingActivities" in msg
        assert "EarningsPerShareDiluted" in msg


# ===========================================================================
# TestMakeFlowWarnings  (new - _make_flow_warnings was previously untested in isolation)
# ===========================================================================

class TestMakeFlowWarnings:
    """_make_flow_warnings produces correct warnings for TTM_ANNUALIZED and AMENDMENT_EXISTS."""

    def test_no_warnings_when_neither_condition_met(self):
        result = _make_flow_warnings(
            tag="Revenues",
            fiscal_year_start=date(2024, 1, 1),
            period_end=date(2024, 12, 31),
            amendment_keys=set(),
            was_annualized=False,
            concept_name="Revenue",
        )
        assert result == []

    def test_ttm_annualized_warning_when_was_annualized(self):
        result = _make_flow_warnings(
            tag="Revenues",
            fiscal_year_start=date(2024, 1, 1),
            period_end=date(2024, 12, 31),
            amendment_keys=set(),
            was_annualized=True,
            concept_name="Revenue",
        )
        codes = [w.code for w in result]
        assert "ttm_annualized" in codes
        assert "Revenue" in result[0].message

    def test_amendment_warning_when_key_in_amendment_keys(self):
        key = (date(2024, 1, 1), date(2024, 12, 31))
        result = _make_flow_warnings(
            tag="NetCashProvidedByUsedInOperatingActivities",
            fiscal_year_start=date(2024, 1, 1),
            period_end=date(2024, 12, 31),
            amendment_keys={key},
            was_annualized=False,
            concept_name="Operating Cash Flow",
        )
        codes = [w.code for w in result]
        assert "amendment_exists" in codes
        msg = next(w.message for w in result if w.code == "amendment_exists")
        assert "NetCashProvidedByUsedInOperatingActivities" in msg
        # Message uses the '<tag>. (<period>).' format
        assert ". (" in msg

    def test_both_warnings_when_both_conditions_met(self):
        key = (date(2024, 1, 1), date(2024, 9, 30))
        result = _make_flow_warnings(
            tag="Revenues",
            fiscal_year_start=date(2024, 1, 1),
            period_end=date(2024, 9, 30),
            amendment_keys={key},
            was_annualized=True,
            concept_name="Revenue",
        )
        codes = [w.code for w in result]
        assert "ttm_annualized" in codes
        assert "amendment_exists" in codes

    def test_amendment_key_mismatch_no_warning(self):
        """Only the current-YTD key (fy_start, period_end) triggers AMENDMENT_EXISTS."""
        wrong_key = (date(2023, 1, 1), date(2023, 12, 31))
        result = _make_flow_warnings(
            tag="Revenues",
            fiscal_year_start=date(2024, 1, 1),
            period_end=date(2024, 12, 31),
            amendment_keys={wrong_key},
            was_annualized=False,
            concept_name="Revenue",
        )
        assert result == []


# ===========================================================================
# test_no_orphan_warning_codes  # orphan-code guard
# ===========================================================================

def test_no_orphan_warning_codes():
    """Every WarningCode must be raised somewhere in the services layer OR
    explicitly marked deprecated/deferred in its docstring.

    Prevents a future developer from adding a new WarningCode to errors.py
    without wiring it up anywhere.
    """
    import app.services.xbrl as xbrl_mod
    import app.services.xbrl_maps as xbrl_maps_mod
    import app.services.xbrl_warnings as xbrl_warnings_mod

    sources = "\n".join(
        inspect.getsource(mod)
        for mod in (xbrl_mod, xbrl_maps_mod, xbrl_warnings_mod)
    )

    # Codes raised in Phase 3 (multiples.py) or deprecated by the anchor model.
    deferred = {
        "ev_debt_missing",        # raised in Phase 3 (multiples.py)
        "denominator_near_zero",  # raised in Phase 3 (multiples.py)
        "negative_book_value",    # raised in Phase 3 (multiples.py)
        "negative_fcf",           # raised in Phase 3 (multiples.py)
        "period_mismatch",        # deprecated by anchor model (see errors.py docstring)
    }

    for code in WarningCode:
        if code.value in deferred:
            continue
        assert f"WarningCode.{code.name}" in sources, (
            f"{code.name!r} declared in errors.py but never raised in the services "
            "layer. Either implement it or add it to the 'deferred' set above with "
            "a docstring explanation."
        )