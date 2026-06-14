# backend/tests/test_xbrl_maps.py
"""
Unit tests for app/services/xbrl_maps.py.

All symbols imported directly from xbrl_maps - not re-exported through xbrl.py -
so this file clearly expresses which module is under test.

Coverage
--------
TestBuildFlowMap         _build_flow_map returns _FlowEntry (5-field NamedTuple)
TestBuildInstantMap      _build_instant_map returns _InstantEntry (3-field NamedTuple)
TestGetInstantResult     tolerance-aware point-in-time lookup (±7 days)
TestAmbiguousNear        find nearest ambiguous date within ±7 days
TestFindAnnualFact       O(1) lookup into annual_index
TestFindPriorYtd         O(k) lookup into prior_ytd_index with duration tolerance
TestAnnualize            extrapolate YTD to full-year estimate
TestGetTtmValue          TTM bridge - annual direct, quarterly bridge, fallbacks
TestDeduplicateFactGroup _deduplicate_fact_group - three return modes
TestAmbiguousFacts       conflicting flow facts excluded from map
TestNonUsdFacts          non-USD unit filtering via _precompute_* # USD-only filtering
TestUnitFiltering        end-to-end USD-only guarantee # USD-only filtering
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.xbrl_maps import (
    _ambiguous_near,
    _annualize,
    _build_flow_map,
    _build_instant_map,
    _deduplicate_fact_group,
    _find_annual_fact,
    _find_prior_ytd,
    _get_instant_result,
    _get_ttm_value,
)

# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def _make_flow_entry(pairs: list[tuple]):
    """Build a _FlowEntry via _build_flow_map from (start, end, value) tuples."""
    facts = [
        {"start": s, "end": e, "val": v,
         "form": "10-K", "accn": f"0000000001-{i:02d}-000001"}
        for i, (s, e, v) in enumerate(pairs)
    ]
    return _build_flow_map(facts)


# ===========================================================================
# TestBuildFlowMap
# ===========================================================================

class TestBuildFlowMap:
    """_build_flow_map returns _FlowEntry (5-field NamedTuple)."""

    def _fact(self, start, end, val, form="10-Q", accn="0000000001-00-000001"):
        return {"start": start, "end": end, "val": val, "form": form, "accn": accn}

    def test_returns_five_field_namedtuple(self):
        facts = [self._fact("2024-01-01", "2024-03-31", 100)]
        entry = _build_flow_map(facts)
        assert hasattr(entry, "flow_map")
        assert hasattr(entry, "amendment_keys")
        assert hasattr(entry, "ambiguous_keys")
        assert hasattr(entry, "annual_index")
        assert hasattr(entry, "prior_ytd_index")

    def test_single_fact_in_flow_map(self):
        facts = [self._fact("2024-01-01", "2024-03-31", 100)]
        entry = _build_flow_map(facts)
        key = (date(2024, 1, 1), date(2024, 3, 31))
        assert entry.flow_map[key] == Decimal("100")
        assert not entry.amendment_keys
        assert not entry.ambiguous_keys

    def test_annual_fact_goes_to_annual_index(self):
        """365-day facts must appear in annual_index keyed by end date."""
        facts = [self._fact("2024-01-01", "2024-12-31", 1_000)]
        entry = _build_flow_map(facts)
        assert date(2024, 12, 31) in entry.annual_index
        fy_start, val = entry.annual_index[date(2024, 12, 31)]
        assert fy_start == date(2024, 1, 1)
        assert val == Decimal("1000")

    def test_short_fact_goes_to_prior_ytd_index(self):
        """Sub-annual facts go into prior_ytd_index keyed by start date."""
        facts = [self._fact("2024-01-01", "2024-03-31", 250)]
        entry = _build_flow_map(facts)
        assert date(2024, 1, 1) in entry.prior_ytd_index
        assert date(2024, 3, 31) not in entry.annual_index

    def test_duplicate_same_value_deduped(self):
        facts = [
            self._fact("2024-01-01", "2024-03-31", 500, accn="0000000001-00-000001"),
            self._fact("2024-01-01", "2024-03-31", 500, accn="0000000002-00-000002"),
        ]
        entry = _build_flow_map(facts)
        key = (date(2024, 1, 1), date(2024, 3, 31))
        assert entry.flow_map[key] == Decimal("500")
        assert key not in entry.ambiguous_keys

    def test_conflicting_non_amendment_values_ambiguous(self):
        facts = [
            self._fact("2024-01-01", "2024-03-31", 100, accn="0000000001-00-000001"),
            self._fact("2024-01-01", "2024-03-31", 999, accn="0000000002-00-000002"),
        ]
        entry = _build_flow_map(facts)
        key = (date(2024, 1, 1), date(2024, 3, 31))
        assert key not in entry.flow_map
        assert key in entry.ambiguous_keys

    def test_amendment_only_key_in_amendment_keys(self):
        facts = [self._fact("2024-01-01", "2024-03-31", 200, form="10-Q/A")]
        entry = _build_flow_map(facts)
        key = (date(2024, 1, 1), date(2024, 3, 31))
        assert entry.flow_map[key] == Decimal("200")
        assert key in entry.amendment_keys

    def test_original_preferred_over_amendment(self):
        facts = [
            self._fact("2024-01-01", "2024-03-31", 300, form="10-Q"),
            self._fact("2024-01-01", "2024-03-31", 999, form="10-Q/A"),
        ]
        entry = _build_flow_map(facts)
        key = (date(2024, 1, 1), date(2024, 3, 31))
        assert entry.flow_map[key] == Decimal("300")
        assert key not in entry.amendment_keys

    def test_instant_facts_excluded(self):
        facts = [{"end": "2024-03-31", "val": 999, "form": "10-Q"}]
        entry = _build_flow_map(facts)
        assert len(entry.flow_map) == 0

    def test_multiple_distinct_periods(self):
        facts = [
            self._fact("2024-01-01", "2024-03-31", 100),
            self._fact("2024-01-01", "2024-06-30", 200),
            self._fact("2024-01-01", "2024-09-30", 300),
        ]
        entry = _build_flow_map(facts)
        assert len(entry.flow_map) == 3


# ===========================================================================
# TestBuildInstantMap
# ===========================================================================

class TestBuildInstantMap:
    """_build_instant_map returns _InstantEntry (3-field NamedTuple)."""

    def _instant(self, end, val, form="10-Q"):
        return {"end": end, "val": val, "form": form}

    def test_returns_three_field_namedtuple(self):
        facts = [self._instant("2024-03-31", 1000)]
        entry = _build_instant_map(facts)
        assert hasattr(entry, "instant_map")
        assert hasattr(entry, "amendment_keys")
        assert hasattr(entry, "ambiguous_keys")

    def test_single_instant_fact(self):
        facts = [self._instant("2024-03-31", 1000)]
        entry = _build_instant_map(facts)
        assert entry.instant_map[date(2024, 3, 31)] == Decimal("1000")

    def test_duplicate_same_value_deduped(self):
        facts = [self._instant("2024-03-31", 1000), self._instant("2024-03-31", 1000)]
        entry = _build_instant_map(facts)
        assert entry.instant_map[date(2024, 3, 31)] == Decimal("1000")

    def test_conflicting_values_ambiguous(self):
        facts = [self._instant("2024-03-31", 1000), self._instant("2024-03-31", 2000)]
        entry = _build_instant_map(facts)
        assert date(2024, 3, 31) not in entry.instant_map
        assert date(2024, 3, 31) in entry.ambiguous_keys

    def test_flow_facts_excluded(self):
        facts = [{"start": "2024-01-01", "end": "2024-03-31", "val": 999, "form": "10-Q"}]
        entry = _build_instant_map(facts)
        assert len(entry.instant_map) == 0


# ===========================================================================
# TestGetInstantResult
# ===========================================================================

class TestGetInstantResult:
    """_get_instant_result: tolerance-aware point-in-time lookup (±7 days)."""

    def test_exact_match(self):
        im = {date(2024, 3, 31): Decimal("1000")}
        value, matched = _get_instant_result(im, date(2024, 3, 31))
        assert value == Decimal("1000")
        assert matched == date(2024, 3, 31)

    def test_within_tolerance_positive(self):
        im = {date(2024, 4, 3): Decimal("999")}   # +3 days
        value, matched = _get_instant_result(im, date(2024, 3, 31))
        assert value == Decimal("999")

    def test_within_tolerance_negative(self):
        im = {date(2024, 3, 26): Decimal("888")}  # -5 days
        value, matched = _get_instant_result(im, date(2024, 3, 31))
        assert value == Decimal("888")

    def test_exactly_7_days_away_matches(self):
        im = {date(2024, 4, 7): Decimal("500")}
        value, matched = _get_instant_result(im, date(2024, 3, 31))
        assert value == Decimal("500")

    def test_8_days_away_not_matched(self):
        im = {date(2024, 4, 8): Decimal("777")}
        value, matched = _get_instant_result(im, date(2024, 3, 31))
        assert value is None
        assert matched is None

    def test_exact_preferred_over_nearby(self):
        im = {date(2024, 3, 31): Decimal("100"), date(2024, 4, 1): Decimal("999")}
        value, matched = _get_instant_result(im, date(2024, 3, 31))
        assert value == Decimal("100")
        assert matched == date(2024, 3, 31)

    def test_empty_map_returns_none(self):
        value, matched = _get_instant_result({}, date(2024, 3, 31))
        assert value is None
        assert matched is None


# ===========================================================================
# TestAmbiguousNear
# ===========================================================================

class TestAmbiguousNear:
    """_ambiguous_near: find nearest ambiguous date within ±7 days."""

    def test_exact_date_found(self):
        assert _ambiguous_near({date(2024, 3, 31)}, date(2024, 3, 31)) == date(2024, 3, 31)

    def test_within_tolerance(self):
        result = _ambiguous_near({date(2024, 4, 3)}, date(2024, 3, 31))
        assert result == date(2024, 4, 3)

    def test_beyond_tolerance_returns_none(self):
        assert _ambiguous_near({date(2024, 4, 10)}, date(2024, 3, 31)) is None

    def test_empty_set_returns_none(self):
        assert _ambiguous_near(set(), date(2024, 3, 31)) is None


# ===========================================================================
# TestFindAnnualFact
# ===========================================================================

class TestFindAnnualFact:
    """_find_annual_fact: O(1) lookup into annual_index."""

    def test_exact_365_day_fact_found(self):
        annual_index = {date(2024, 12, 31): (date(2024, 1, 1), Decimal("1000"))}
        result = _find_annual_fact(annual_index, date(2024, 12, 31))
        assert result is not None
        fy_start, val = result
        assert fy_start == date(2024, 1, 1)
        assert val == Decimal("1000")

    def test_wrong_end_date_returns_none(self):
        annual_index = {date(2024, 12, 31): (date(2024, 1, 1), Decimal("999"))}
        assert _find_annual_fact(annual_index, date(2025, 12, 31)) is None

    def test_snow_non_calendar_fiscal_year(self):
        annual_index = {date(2025, 1, 31): (date(2024, 2, 1), Decimal("3626396000"))}
        result = _find_annual_fact(annual_index, date(2025, 1, 31))
        assert result is not None
        fy_start, val = result
        assert fy_start == date(2024, 2, 1)

    def test_empty_index_returns_none(self):
        assert _find_annual_fact({}, date(2024, 12, 31)) is None


# ===========================================================================
# TestFindPriorYtd
# ===========================================================================

class TestFindPriorYtd:
    """_find_prior_ytd: O(k) lookup into prior_ytd_index with duration tolerance."""

    def test_exact_duration_match(self):
        prior_ytd_index = {date(2023, 1, 1): [(272, Decimal("503229000"))]}
        assert _find_prior_ytd(prior_ytd_index, date(2023, 1, 1), 272) == Decimal("503229000")

    def test_leap_year_tolerance(self):
        # delta = 1, within tolerance of 4
        prior_ytd_index = {date(2024, 1, 1): [(273, Decimal("999"))]}
        assert _find_prior_ytd(prior_ytd_index, date(2024, 1, 1), 272) == Decimal("999")

    def test_out_of_tolerance_not_matched(self):
        # delta = 28, exceeds tolerance
        prior_ytd_index = {date(2024, 1, 1): [(300, Decimal("888"))]}
        assert _find_prior_ytd(prior_ytd_index, date(2024, 1, 1), 272) is None

    def test_multiple_candidates_correct_one_selected(self):
        prior_ytd_index = {date(2023, 1, 1): [(91, Decimal("100")), (272, Decimal("500"))]}
        assert _find_prior_ytd(prior_ytd_index, date(2023, 1, 1), 272) == Decimal("500")

    def test_wrong_fy_start_returns_none(self):
        prior_ytd_index = {date(2024, 1, 1): [(272, Decimal("500"))]}
        assert _find_prior_ytd(prior_ytd_index, date(2023, 1, 1), 272) is None

    def test_empty_index_returns_none(self):
        assert _find_prior_ytd({}, date(2024, 1, 1), 272) is None


# ===========================================================================
# TestAnnualize
# ===========================================================================

class TestAnnualize:
    """_annualize: extrapolate YTD to full-year estimate."""

    def test_half_year_approximately_doubles(self):
        val, ann = _annualize(Decimal("500000"), 181)
        assert ann is True
        assert val is not None
        assert 950_000 < val < 1_050_000

    def test_nine_months_factor(self):
        val, ann = _annualize(Decimal("300000"), 273)
        assert ann is True
        assert val is not None
        assert 390_000 < val < 410_000

    def test_too_short_returns_none(self):
        val, ann = _annualize(Decimal("10000"), 20)
        assert val is None
        assert not ann

    def test_boundary_minimum_days_annualizes(self):
        val, ann = _annualize(Decimal("100000"), 40)
        assert ann is True
        assert val is not None


# ===========================================================================
# TestGetTtmValue
# ===========================================================================

class TestGetTtmValue:
    """_get_ttm_value: signature (flow_map, period_end, fy_start, annual_index, prior_ytd_index)."""

    def test_annual_filing_direct_lookup(self):
        entry = _make_flow_entry([("2024-01-01", "2024-12-31", 1_000_000)])
        val, ann = _get_ttm_value(
            entry.flow_map, date(2024, 12, 31), date(2024, 1, 1),
            entry.annual_index, entry.prior_ytd_index,
        )
        assert val == Decimal("1000000")
        assert not ann

    def test_quarterly_bridge_q1(self):
        # 1000 + 250 - 200 = 1050
        entry = _make_flow_entry([
            ("2023-01-01", "2023-12-31", 1_000),
            ("2023-01-01", "2023-03-31", 200),
            ("2024-01-01", "2024-03-31", 250),
        ])
        val, ann = _get_ttm_value(
            entry.flow_map, date(2024, 3, 31), date(2024, 1, 1),
            entry.annual_index, entry.prior_ytd_index,
        )
        assert val == Decimal("1050")
        assert not ann

    def test_quarterly_bridge_q3_crct(self):
        entry = _make_flow_entry([
            ("2024-01-01", "2024-12-31", 712_538_000),
            ("2024-01-01", "2024-09-30", 503_229_000),
            ("2025-01-01", "2025-09-30", 505_183_000),
        ])
        val, ann = _get_ttm_value(
            entry.flow_map, date(2025, 9, 30), date(2025, 1, 1),
            entry.annual_index, entry.prior_ytd_index,
        )
        assert val == Decimal("714492000")
        assert not ann

    def test_quarterly_bridge_snow_non_calendar_fy(self):
        entry = _make_flow_entry([
            ("2024-02-01", "2025-01-31", 3_626_396_000),
            ("2024-02-01", "2024-10-31", 2_639_626_000),
            ("2025-02-01", "2025-10-31", 3_399_952_000),
        ])
        val, ann = _get_ttm_value(
            entry.flow_map, date(2025, 10, 31), date(2025, 2, 1),
            entry.annual_index, entry.prior_ytd_index,
        )
        assert val == Decimal("4386722000")
        assert not ann

    def test_annualization_when_prior_fy_missing(self):
        entry = _make_flow_entry([("2024-01-01", "2024-06-30", 500_000)])
        val, ann = _get_ttm_value(
            entry.flow_map, date(2024, 6, 30), date(2024, 1, 1),
            entry.annual_index, entry.prior_ytd_index,
        )
        assert ann is True
        assert val is not None
        assert 950_000 < val < 1_050_000

    def test_current_ytd_missing_returns_none(self):
        entry = _make_flow_entry([("2023-01-01", "2023-12-31", 1_000)])
        val, ann = _get_ttm_value(
            entry.flow_map, date(2024, 3, 31), date(2024, 1, 1),
            entry.annual_index, entry.prior_ytd_index,
        )
        assert val is None
        assert not ann

    def test_annualization_when_prior_ytd_missing(self):
        entry = _make_flow_entry([
            ("2023-01-01", "2023-12-31", 800_000),
            ("2024-01-01", "2024-09-30", 700_000),
        ])
        val, ann = _get_ttm_value(
            entry.flow_map, date(2024, 9, 30), date(2024, 1, 1),
            entry.annual_index, entry.prior_ytd_index,
        )
        assert ann is True
        assert val is not None

    def test_negative_ttm_value_returned(self):
        """Negative TTM results must not be suppressed."""
        entry = _make_flow_entry([
            ("2023-01-01", "2023-12-31", -200_000),
            ("2023-01-01", "2023-09-30", -150_000),
            ("2024-01-01", "2024-09-30", -180_000),
        ])
        val, ann = _get_ttm_value(
            entry.flow_map, date(2024, 9, 30), date(2024, 1, 1),
            entry.annual_index, entry.prior_ytd_index,
        )
        # -200k + (-180k) - (-150k) = -230k
        assert val is not None
        assert val < 0
        assert not ann


# ===========================================================================
# TestDeduplicateFactGroup  # dedup: 3 return modes
# ===========================================================================

class TestDeduplicateFactGroup:
    """Direct tests for _deduplicate_fact_group - three return modes."""

    def test_unique_value_returns_decimal(self):
        facts = [
            {"val": 100, "form": "10-K"},
            {"val": 100, "form": "10-Q"},
        ]
        value, is_amendment_only, is_ambiguous = _deduplicate_fact_group(facts)
        assert value == Decimal("100")
        assert not is_amendment_only
        assert not is_ambiguous

    def test_conflicting_non_amendment_returns_ambiguous(self):
        facts = [
            {"val": 100, "form": "10-K"},
            {"val": 200, "form": "10-K"},
        ]
        value, is_amendment_only, is_ambiguous = _deduplicate_fact_group(facts)
        assert value is None
        assert not is_amendment_only
        assert is_ambiguous

    def test_amendment_only_returns_amendment_flag(self):
        facts = [{"val": 500, "form": "10-K/A"}]
        value, is_amendment_only, is_ambiguous = _deduplicate_fact_group(facts)
        assert value == Decimal("500")
        assert is_amendment_only
        assert not is_ambiguous

    def test_original_preferred_over_amendment(self):
        facts = [
            {"val": 100, "form": "10-K"},      # original wins
            {"val": 999, "form": "10-K/A"},
        ]
        value, is_amendment_only, _ = _deduplicate_fact_group(facts)
        assert value == Decimal("100")
        assert not is_amendment_only


# ===========================================================================
# TestAmbiguousFacts  (was Section D of test_xbrl.py - tests _build_flow_map directly)
# ===========================================================================

class TestAmbiguousFacts:

    def test_conflicting_flow_facts_excluded_from_map(self):
        entry = _build_flow_map([
            {"start": "2024-01-01", "end": "2024-12-31", "val": 100, "form": "10-K", "accn": "0001"},
            {"start": "2024-01-01", "end": "2024-12-31", "val": 200, "form": "10-K", "accn": "0002"},
        ])
        key = (date(2024, 1, 1), date(2024, 12, 31))
        assert key not in entry.flow_map
        assert key in entry.ambiguous_keys


# ===========================================================================
# TestNonUsdFacts / TestUnitFiltering  # USD-only filtering
# ===========================================================================

class TestUnitFiltering:
    """Non-USD facts must be excluded at the precompute step.

    README.md: 'Only facts with unitRef: USD are accepted.
    Non-USD facts are rejected at extraction.'
    """

    @pytest.mark.asyncio
    async def test_eur_facts_ignored_when_usd_present(self):
        """Both USD and EUR facts exist for the same tag - only USD is used."""
        from app.services.xbrl import extract_ttm_periods
        gaap = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {
                    "USD": [{"start": "2024-01-01", "end": "2024-12-31", "val": 1_000_000,
                             "form": "10-K", "accn": "0001", "filed": "2025-02-01"}],
                    "EUR": [{"start": "2024-01-01", "end": "2024-12-31", "val": 9_999_999,
                             "form": "10-K", "accn": "0001", "filed": "2025-02-01"}],
                }
            }
        }
        periods = await extract_ttm_periods(
            {"facts": {"us-gaap": gaap, "dei": {}}}, ticker=None,
        )
        assert len(periods) == 1
        assert periods[0].revenue == Decimal("1000000")

    @pytest.mark.asyncio
    async def test_eur_only_tag_treated_as_missing(self):
        """Only EUR facts present - revenue must be None, not the EUR value."""
        from app.services.xbrl import extract_ttm_periods
        gaap = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {
                    "EUR": [{"start": "2024-01-01", "end": "2024-12-31", "val": 9_999_999,
                             "form": "10-K", "accn": "0001", "filed": "2025-02-01"}],
                }
            },
            # Anchor discovery needs at least one USD anchor tag.
            "NetIncomeLoss": {
                "units": {
                    "USD": [{"start": "2024-01-01", "end": "2024-12-31", "val": 100,
                             "form": "10-K", "accn": "0001", "filed": "2025-02-01"}],
                }
            },
        }
        periods = await extract_ttm_periods(
            {"facts": {"us-gaap": gaap, "dei": {}}}, ticker=None,
        )
        assert len(periods) == 1
        assert periods[0].revenue is None  # not 9_999_999

    def test_non_usd_balance_sheet_tag_ignored(self):
        """Instant facts in non-USD units must also be excluded."""
        from app.services.xbrl import _resolve_instant, _INSTANT_CHAINS
        gaap = {
            "CashAndCashEquivalentsAtCarryingValue": {
                "units": {
                    "USD": [{"end": "2024-12-31", "val": 500_000, "form": "10-K"}],
                    "GBP": [{"end": "2024-12-31", "val": 9_999_999, "form": "10-K"}],
                }
            }
        }
        res = _resolve_instant(gaap, _INSTANT_CHAINS["cash"], date(2024, 12, 31))
        assert res.value == Decimal("500000")