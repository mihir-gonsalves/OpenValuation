# backend/tests/test_xbrl.py
"""
Tests for app/services/xbrl.py - Phase 2 XBRL extraction.

Structure
---------
Section B: concept-level integration tests using real EDGAR fixtures
           (CRCT, SNOW, CART JSON files).

Section C: extract_ttm_periods end-to-end tests (async, price mocked).

Section D: end-to-end warning behavior tests (not the helper - those live in
           test_xbrl_warnings.py).

Section E: structural invariants and anchor deduplication.

Section F: spec-compliance regression tests.

Note: Section A (xbrl_maps.py internals) has been moved to test_xbrl_maps.py.
      TestDedupWarningsHelper has been moved to test_xbrl_warnings.py.
      TestAmbiguousFacts has been moved to test_xbrl_maps.py.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.services.xbrl import (
    _FilingAnchor,
    _collect_filing_anchors,
    _extract_capex,
    _extract_cash,
    _extract_debt,
    _extract_eps,
    _extract_finance_lease,
    _get_shares,
    extract_ttm_periods,
)

# xbrl_maps symbols imported directly from their owning module
from app.services.xbrl_maps import (
    _ambiguous_near,
    _annualize,
    _build_flow_map,
    _build_instant_map,
    _find_annual_fact,
    _find_prior_ytd,
    _get_instant_result,
    _get_ttm_value,
)

# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(filename: str) -> dict:
    path = _FIXTURES / filename
    if not path.exists():
        pytest.skip(f"Fixture not found: {path}")
    with path.open() as f:
        return json.load(f)


@pytest.fixture(scope="session")
def crct() -> dict:
    return _load_fixture("crct_CIK0001828962.json")


@pytest.fixture(scope="session")
def snow() -> dict:
    return _load_fixture("snow_CIK0001640147.json")


@pytest.fixture(scope="session")
def cart() -> dict:
    return _load_fixture("cart_CIK0001579091.json")


def _gaap(cf: dict) -> dict:
    return cf["facts"]["us-gaap"]


def _dei(cf: dict) -> dict:
    return cf["facts"].get("dei", {})


# ---------------------------------------------------------------------------
# Helper: build _FlowEntry from (start, end, value) tuples
# ---------------------------------------------------------------------------

def _make_flow_entry(pairs: list[tuple]):
    """
    Build a _FlowEntry via _build_flow_map.
    Needed for _get_ttm_value which requires all three indexes
    (flow_map, annual_index, prior_ytd_index) from a single consistent source.
    """
    facts = [
        {"start": s, "end": e, "val": v,
         "form": "10-K", "accn": f"0000000001-{i:02d}-000001"}
        for i, (s, e, v) in enumerate(pairs)
    ]
    return _build_flow_map(facts)


# ===========================================================================
# SECTION B - Integration tests using real EDGAR fixtures
# ===========================================================================


class TestFilingAnchorDiscovery:
    """_FilingAnchor has: accn, filed, period_end, fiscal_year_start.  NO .form."""

    def test_crct_most_recent_anchor(self, crct):
        anchors = _collect_filing_anchors(_gaap(crct))
        most_recent = max(anchors, key=lambda a: a.period_end)
        assert most_recent.period_end == date(2026, 3, 31)
        assert most_recent.fiscal_year_start == date(2026, 1, 1)

    def test_crct_at_least_eight_anchors(self, crct):
        assert len(_collect_filing_anchors(_gaap(crct))) >= 8

    def test_snow_fy2026_annual_anchor(self, snow):
        anchors = _collect_filing_anchors(_gaap(snow))
        most_recent = max(anchors, key=lambda a: a.period_end)
        assert most_recent.period_end == date(2026, 1, 31)
        assert most_recent.fiscal_year_start == date(2025, 2, 1)
        # Annual anchor: duration >= 350 days
        assert (most_recent.period_end - most_recent.fiscal_year_start).days >= 350

    def test_snow_q3_fy2026_quarterly_anchor(self, snow):
        anchors = _collect_filing_anchors(_gaap(snow))
        q3 = next((a for a in anchors if a.period_end == date(2025, 10, 31)), None)
        assert q3 is not None
        assert q3.fiscal_year_start == date(2025, 2, 1)
        # Quarterly anchor: duration < 350 days
        assert (q3.period_end - q3.fiscal_year_start).days < 350

    def test_cart_at_least_five_anchors(self, cart):
        assert len(_collect_filing_anchors(_gaap(cart))) >= 5

    def test_all_anchors_have_fy_start_before_period_end(self, crct):
        for a in _collect_filing_anchors(_gaap(crct)):
            assert a.fiscal_year_start < a.period_end

    def test_anchor_has_no_form_attribute(self, crct):
        """_FilingAnchor must NOT expose a .form field."""
        anchors = _collect_filing_anchors(_gaap(crct))
        assert anchors
        assert not hasattr(anchors[0], "form")

    def test_non_calendar_fy_anchors_present_for_snow(self, snow):
        anchors = _collect_filing_anchors(_gaap(snow))
        feb_starts = [a for a in anchors if a.fiscal_year_start.month == 2]
        assert feb_starts


class TestGetShares:
    """_get_shares returns (value, tag_used, warnings) - 3-tuple."""

    def test_crct_gaap_shares_q1_2026(self, crct):
        anchors = _collect_filing_anchors(_gaap(crct))
        anchor = next(a for a in anchors if a.period_end == date(2026, 3, 31))
        shares, tag, warnings = _get_shares(_gaap(crct), _dei(crct), date(2026, 3, 31), anchor.accn)
        assert shares == Decimal("209897286")
        assert tag == "CommonStockSharesOutstanding"

    def test_crct_gaap_shares_fy2025(self, crct):
        anchors = _collect_filing_anchors(_gaap(crct))
        anchor = next(a for a in anchors if a.period_end == date(2025, 12, 31))
        shares, tag, _ = _get_shares(_gaap(crct), _dei(crct), date(2025, 12, 31), anchor.accn)
        assert shares == Decimal("211336284")
        assert tag == "CommonStockSharesOutstanding"

    def test_snow_dei_shares_fy2026(self, snow):
        anchors = _collect_filing_anchors(_gaap(snow))
        anchor = next(a for a in anchors if a.period_end == date(2026, 1, 31))
        shares, tag, _ = _get_shares(_gaap(snow), _dei(snow), date(2026, 1, 31), anchor.accn)
        assert shares == Decimal("345700000")
        assert tag == "EntityCommonStockSharesOutstanding"

    def test_cart_gaap_shares_q1_2026(self, cart):
        anchors = _collect_filing_anchors(_gaap(cart))
        anchor = next(a for a in anchors if a.period_end == date(2026, 3, 31))
        shares, tag, _ = _get_shares(_gaap(cart), _dei(cart), date(2026, 3, 31), anchor.accn)
        assert shares == Decimal("236710000")
        assert tag == "CommonStockSharesOutstanding"

    def test_missing_shares_returns_none(self):
        shares, tag, warnings = _get_shares({}, {}, date(2025, 3, 31), "fake-accn")
        assert shares is None
        assert tag is None
        assert isinstance(warnings, list)

    def test_returns_three_tuple(self, crct):
        anchors = _collect_filing_anchors(_gaap(crct))
        result = _get_shares(_gaap(crct), _dei(crct), anchors[0].period_end, anchors[0].accn)
        assert len(result) == 3


class TestBalanceSheetExtraction:

    def test_crct_equity_q1_2026(self, crct):
        from app.services.xbrl import _resolve_instant, _INSTANT_CHAINS
        res = _resolve_instant(_gaap(crct), _INSTANT_CHAINS["stockholders_equity"], date(2026, 3, 31))
        assert res.value == Decimal("357491000")

    def test_crct_equity_fy2025(self, crct):
        from app.services.xbrl import _resolve_instant, _INSTANT_CHAINS
        res = _resolve_instant(_gaap(crct), _INSTANT_CHAINS["stockholders_equity"], date(2025, 12, 31))
        assert res.value == Decimal("343561000")

    def test_snow_equity_fy2026(self, snow):
        from app.services.xbrl import _resolve_instant, _INSTANT_CHAINS
        res = _resolve_instant(_gaap(snow), _INSTANT_CHAINS["stockholders_equity"], date(2026, 1, 31))
        assert res.value == Decimal("1924102000")

    def test_cart_equity_q1_2026(self, cart):
        from app.services.xbrl import _resolve_instant, _INSTANT_CHAINS
        res = _resolve_instant(_gaap(cart), _INSTANT_CHAINS["stockholders_equity"], date(2026, 3, 31))
        assert res.value == Decimal("2395000000")


class TestCashExtraction:

    def test_crct_cash_q1_2026_primary_tag(self, crct):
        res = _extract_cash(_gaap(crct), date(2026, 3, 31))
        assert res.value == Decimal("236499000")
        assert not res.warnings

    def test_crct_cash_fy2025(self, crct):
        res = _extract_cash(_gaap(crct), date(2025, 12, 31))
        assert res.value == Decimal("256216000")

    def test_snow_cash_fy2026(self, snow):
        res = _extract_cash(_gaap(snow), date(2026, 1, 31))
        assert res.value == Decimal("2828163000")

    def test_fallback_cash_tag_warning(self):
        gaap = {
            "CashCashEquivalentsAndShortTermInvestments": {
                "units": {"USD": [{"end": "2024-03-31", "val": 5000, "form": "10-Q"}]}
            }
        }
        res = _extract_cash(gaap, date(2024, 3, 31))
        assert res.value == Decimal("5000")
        assert any(w.code == "cash_fallback_includes_investments" for w in res.warnings)

    def test_missing_cash_returns_none(self):
        assert _extract_cash({}, date(2024, 3, 31)).value is None


class TestCapexExtraction:

    def test_snow_capex_fy2026(self, snow):
        res = _extract_capex(_gaap(snow), date(2026, 1, 31), date(2025, 2, 1))
        assert res.value == Decimal("101628000")
        assert not any(w.code == "capex_sign_normalized" for w in res.warnings)

    def test_crct_capex_missing(self, crct):
        assert _extract_capex(_gaap(crct), date(2026, 3, 31), date(2026, 1, 1)).value is None

    def test_negative_capex_sign_normalised(self):
        gaap = {
            "PaymentsToAcquirePropertyPlantAndEquipment": {
                "units": {"USD": [
                    {"start": "2024-01-01", "end": "2024-12-31", "val": -50_000, "form": "10-K"}
                ]}
            }
        }
        res = _extract_capex(gaap, date(2024, 12, 31), date(2024, 1, 1))
        assert res.value == Decimal("50000")
        assert any(w.code == "capex_sign_normalized" for w in res.warnings)

    def test_cart_capex_fy2025(self, cart):
        res = _extract_capex(_gaap(cart), date(2025, 12, 31), date(2025, 1, 1))
        assert res.value == Decimal("61000000")


class TestEpsExtraction:

    def test_crct_eps_fy2025_diluted(self, crct):
        res = _extract_eps(_gaap(crct), date(2025, 12, 31), date(2025, 1, 1))
        assert res.value == Decimal("0.35")
        assert not res.is_fallback

    def test_snow_eps_fy2026_diluted(self, snow):
        res = _extract_eps(_gaap(snow), date(2026, 1, 31), date(2025, 2, 1))
        assert res.value == Decimal("-3.95")
        assert not res.is_fallback

    def test_crct_eps_q1_2026_bridge(self, crct):
        # FY2025(0.35) + Q1_2026(0.10) - Q1_2025(0.11) = 0.34
        res = _extract_eps(_gaap(crct), date(2026, 3, 31), date(2026, 1, 1))
        assert res.value == Decimal("0.34")
        assert not any(w.code == "ttm_annualized" for w in res.warnings)  # full bridge, no annualization

    def test_fallback_to_basic_eps(self):
        gaap = {
            "EarningsPerShareBasic": {
                "units": {"USD/shares": [
                    {"start": "2024-01-01", "end": "2024-12-31", "val": 2.5, "form": "10-K"}
                ]}
            }
        }
        res = _extract_eps(gaap, date(2024, 12, 31), date(2024, 1, 1))
        assert res.value == Decimal("2.5")
        assert res.is_fallback
        assert any(w.code == "fallback_eps_basic" for w in res.warnings)

    def test_eps_missing_returns_none(self):
        assert _extract_eps({}, date(2024, 12, 31), date(2024, 1, 1)).value is None

    @pytest.mark.asyncio
    async def test_basic_eps_fallback_records_correct_audit_entry(self):
        """The 'EPS' audit concept must record EarningsPerShareBasic when
        diluted is unavailable, and is_fallback must be True. # audit records the tag actually used"""
        gaap = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": [{"start": "2024-01-01", "end": "2024-12-31",
                                   "val": 1_000_000, "form": "10-K",
                                   "accn": "0001", "filed": "2025-02-01"}]}
            },
            "EarningsPerShareBasic": {
                "units": {"USD/shares": [{"start": "2024-01-01", "end": "2024-12-31",
                                          "val": 2.50, "form": "10-K",
                                          "accn": "0001", "filed": "2025-02-01"}]}
            },
        }
        periods = await extract_ttm_periods(
            {"facts": {"us-gaap": gaap, "dei": {}}}, ticker=None,
        )
        eps_audit = next((a for a in periods[0].audit if a.concept == "EPS"), None)
        assert eps_audit is not None
        assert eps_audit.xbrl_tag == "EarningsPerShareBasic"
        assert eps_audit.is_fallback is True
        assert eps_audit.unit == "USD/shares"


class TestDebtExtraction:

    def test_noncurrent_tag_preferred(self):
        gaap = {
            "LongTermDebtNoncurrent": {"units": {"USD": [{"end": "2024-12-31", "val": 1_000_000, "form": "10-K"}]}},
            "LongTermDebt": {"units": {"USD": [{"end": "2024-12-31", "val": 1_200_000, "form": "10-K"}]}},
        }
        res, used_total = _extract_debt(gaap, date(2024, 12, 31))
        assert res.value == Decimal("1000000")
        assert not used_total
        assert not res.warnings

    def test_fallback_to_total_ltd(self):
        gaap = {"LongTermDebt": {"units": {"USD": [{"end": "2024-12-31", "val": 1_200_000, "form": "10-K"}]}}}
        res, used_total = _extract_debt(gaap, date(2024, 12, 31))
        assert res.value == Decimal("1200000")
        assert used_total
        assert any(w.code == "debt_deduplicated" for w in res.warnings)

    def test_both_absent_returns_none(self, crct):
        res, used_total = _extract_debt(_gaap(crct), date(2026, 3, 31))
        assert res.value is None
        assert not used_total

    def test_snow_has_no_debt(self, snow):
        res, _ = _extract_debt(_gaap(snow), date(2026, 1, 31))
        assert res.value is None


class TestFinanceLeaseExtraction:

    def test_no_lease_tags_returns_none(self, crct):
        assert _extract_finance_lease(_gaap(crct), date(2026, 3, 31), current=True).value is None

    def test_primary_tag_no_warning(self):
        gaap = {"FinanceLeaseLiabilityCurrent": {"units": {"USD": [{"end": "2024-12-31", "val": 50_000, "form": "10-K"}]}}}
        res = _extract_finance_lease(gaap, date(2024, 12, 31), current=True)
        assert res.value == Decimal("50000")
        assert not res.warnings

    def test_capital_lease_fallback_warning(self):
        gaap = {"CapitalLeaseObligationsCurrent": {"units": {"USD": [{"end": "2018-12-31", "val": 30_000, "form": "10-K"}]}}}
        res = _extract_finance_lease(gaap, date(2018, 12, 31), current=True)
        assert res.value == Decimal("30000")
        assert any(w.code == "lease_pre_asc842" for w in res.warnings)


class TestRevenueExtraction:

    def _rev(self, cf, period_end, fy_start):
        from app.services.xbrl import _extract_revenue
        return _extract_revenue(_gaap(cf), period_end, fy_start)

    def test_crct_fy2025_annual(self, crct):
        assert self._rev(crct, date(2025, 12, 31), date(2025, 1, 1)).value == Decimal("708780000")

    def test_crct_q3_2025_bridge(self, crct):
        # 712_538k + 505_183k - 503_229k = 714_492k
        res = self._rev(crct, date(2025, 9, 30), date(2025, 1, 1))
        assert res.value == Decimal("714492000")
        assert not any(w.code == "ttm_annualized" for w in res.warnings)  # full bridge, no annualization

    def test_crct_q1_2026_bridge(self, crct):
        # 708_780k + 159_471k - 162_634k = 705_617k
        res = self._rev(crct, date(2026, 3, 31), date(2026, 1, 1))
        assert res.value == Decimal("705617000")
        assert not any(w.code == "ttm_annualized" for w in res.warnings)  # full bridge, no annualization

    def test_cart_q1_2026_bridge(self, cart):
        res = self._rev(cart, date(2026, 3, 31), date(2026, 1, 1))
        assert res.value == Decimal("3864000000")
        assert not any(w.code == "ttm_annualized" for w in res.warnings)  # full bridge, no annualization

    def test_snow_fy2026_annual(self, snow):
        assert self._rev(snow, date(2026, 1, 31), date(2025, 2, 1)).value == Decimal("4683946000")

    def test_snow_q3_fy2026_bridge(self, snow):
        res = self._rev(snow, date(2025, 10, 31), date(2025, 2, 1))
        assert res.value == Decimal("4386722000")
        assert not any(w.code == "ttm_annualized" for w in res.warnings)  # full bridge, no annualization

    def test_fallback_revenue_warning(self):
        from app.services.xbrl import _extract_revenue
        gaap = {"Revenues": {"units": {"USD": [{"start": "2024-01-01", "end": "2024-12-31", "val": 999_000, "form": "10-K"}]}}}
        res = _extract_revenue(gaap, date(2024, 12, 31), date(2024, 1, 1))
        assert res.value == Decimal("999000")
        assert any(w.code == "fallback_revenue" for w in res.warnings)


class TestOCFExtraction:

    def _ocf(self, cf, period_end, fy_start):
        from app.services.xbrl import _resolve_flow, _FLOW_CHAINS
        return _resolve_flow(_gaap(cf), _FLOW_CHAINS["operating_cash_flow"], period_end, fy_start, concept_name="OCF")

    def test_crct_fy2025_annual(self, crct):
        assert self._ocf(crct, date(2025, 12, 31), date(2025, 1, 1)).value == Decimal("200230000")

    def test_crct_q1_2026_bridge(self, crct):
        # 200_230k + 26_853k - 61_166k = 165_917k
        res = self._ocf(crct, date(2026, 3, 31), date(2026, 1, 1))
        assert res.value == Decimal("165917000")
        assert not any(w.code == "ttm_annualized" for w in res.warnings)  # full bridge, no annualization

    def test_cart_q1_2026_bridge(self, cart):
        res = self._ocf(cart, date(2026, 3, 31), date(2026, 1, 1))
        assert res.value == Decimal("941000000")
        assert not any(w.code == "ttm_annualized" for w in res.warnings)  # full bridge, no annualization

    def test_snow_fy2026_annual(self, snow):
        assert self._ocf(snow, date(2026, 1, 31), date(2025, 2, 1)).value == Decimal("1221942000")


# ===========================================================================
# SECTION C - extract_ttm_periods end-to-end (async, price mocked)
# ===========================================================================


@pytest.mark.asyncio
class TestExtractTtmPeriods:

    @staticmethod
    def _mock_price(value=Decimal("100.00")):
        return patch("app.services.xbrl.price_svc.get_price", new_callable=AsyncMock, return_value=value)

    async def test_crct_period_count_in_range(self, crct):
        with self._mock_price(Decimal("25.00")):
            periods = await extract_ttm_periods(crct, ticker="CRCT")
        assert 8 <= len(periods) <= 12

    async def test_max_twelve_periods_returned(self, crct):
        with self._mock_price():
            periods = await extract_ttm_periods(crct, ticker="CRCT")
        assert len(periods) <= 12

    async def test_crct_most_recent_period_is_q1_2026(self, crct):
        with self._mock_price(Decimal("25.00")):
            periods = await extract_ttm_periods(crct, ticker="CRCT")
        assert periods[0].period_end == date(2026, 3, 31)

    async def test_crct_most_recent_revenue_ttm(self, crct):
        with self._mock_price(Decimal("25.00")):
            periods = await extract_ttm_periods(crct, ticker="CRCT")
        assert periods[0].revenue == Decimal("705617000")

    async def test_crct_fy2025_period_values(self, crct):
        with self._mock_price(Decimal("22.00")):
            periods = await extract_ttm_periods(crct, ticker="CRCT")
        fy25 = next(p for p in periods if p.period_end == date(2025, 12, 31))
        assert fy25.revenue == Decimal("708780000")
        assert fy25.operating_cash_flow == Decimal("200230000")
        assert fy25.eps_diluted == Decimal("0.35")
        assert fy25.stockholders_equity == Decimal("343561000")
        assert fy25.shares_outstanding == Decimal("211336284")
        assert fy25.cash == Decimal("256216000")
        assert fy25.capex is None
        assert fy25.long_term_debt is None

    async def test_snow_fy2026_period_values(self, snow):
        with self._mock_price(Decimal("180.00")):
            periods = await extract_ttm_periods(snow, ticker="SNOW")
        fy26 = periods[0]
        assert fy26.period_end == date(2026, 1, 31)
        assert fy26.revenue == Decimal("4683946000")
        assert fy26.eps_diluted == Decimal("-3.95")
        assert fy26.shares_outstanding == Decimal("345700000")
        assert fy26.stockholders_equity == Decimal("1924102000")
        assert fy26.capex == Decimal("101628000")
        assert fy26.operating_cash_flow == Decimal("1221942000")
        assert fy26.cash == Decimal("2828163000")

    async def test_snow_q3_fy2026_revenue_bridge(self, snow):
        with self._mock_price(Decimal("150.00")):
            periods = await extract_ttm_periods(snow, ticker="SNOW")
        q3 = next(p for p in periods if p.period_end == date(2025, 10, 31))
        assert q3.revenue == Decimal("4386722000")
        assert q3.shares_outstanding == Decimal("342200000")

    async def test_cart_q1_2026_revenue_bridge(self, cart):
        with self._mock_price(Decimal("45.00")):
            periods = await extract_ttm_periods(cart, ticker="CART")
        q1 = next(p for p in periods if p.period_end == date(2026, 3, 31))
        assert q1.revenue == Decimal("3864000000")
        assert q1.shares_outstanding == Decimal("236710000")

    async def test_price_populated_when_ticker_provided(self, crct):
        with self._mock_price(Decimal("23.50")):
            periods = await extract_ttm_periods(crct, ticker="CRCT")
        assert all(p.price == Decimal("23.50") for p in periods)

    async def test_price_none_when_no_ticker(self, crct):
        periods = await extract_ttm_periods(crct, ticker=None)
        assert all(p.price is None for p in periods)

    async def test_periods_sorted_most_recent_first(self, crct):
        with self._mock_price():
            periods = await extract_ttm_periods(crct, ticker="CRCT")
        ends = [p.period_end for p in periods]
        assert ends == sorted(ends, reverse=True)

    async def test_audit_trail_populated(self, crct):
        with self._mock_price():
            periods = await extract_ttm_periods(crct, ticker="CRCT")
        p = periods[0]
        assert len(p.audit) > 0
        rev_audit = next((a for a in p.audit if a.concept == "Revenue"), None)
        assert rev_audit is not None
        assert rev_audit.xbrl_tag == "RevenueFromContractWithCustomerExcludingAssessedTax"
        assert rev_audit.is_fallback is False

    async def test_filing_date_populated(self, crct):
        with self._mock_price():
            periods = await extract_ttm_periods(crct, ticker="CRCT")
        assert all(p.filing_date is not None for p in periods)

    async def test_empty_companyfacts_returns_empty_list(self):
        periods = await extract_ttm_periods({"facts": {"us-gaap": {}, "dei": {}}}, ticker="TEST")
        assert periods == []

    async def test_price_unavailable_warning(self, crct):
        with self._mock_price(None):
            periods = await extract_ttm_periods(crct, ticker="CRCT")
        for p in periods:
            assert p.price is None
            assert any(w.code == "price_unavailable" for w in p.warnings)

    async def test_capital_intensive_false_no_lease_warning(self, crct):
        with self._mock_price():
            periods = await extract_ttm_periods(crct, ticker="CRCT", is_capital_intensive=False)
        for p in periods:
            assert not any(w.code == "finance_lease_missing_capital_intensive" for w in p.warnings)

    async def test_capital_intensive_true_fires_lease_warning(self, crct):
        with self._mock_price():
            periods = await extract_ttm_periods(crct, ticker="CRCT", is_capital_intensive=True)
        for p in periods:
            assert any(w.code == "finance_lease_missing_capital_intensive" for w in p.warnings)

    async def test_price_service_called_with_filing_dates(self, crct):
        with self._mock_price(Decimal("10.00")) as mock_get:
            periods = await extract_ttm_periods(crct, ticker="CRCT")
        called_dates = {call.args[1] for call in mock_get.call_args_list}
        expected_dates = {p.filing_date for p in periods if p.filing_date is not None}
        assert called_dates == expected_dates

    async def test_dei_shares_audit_entry_tag(self, snow):
        with self._mock_price(Decimal("180.00")):
            periods = await extract_ttm_periods(snow, ticker="SNOW")
        assert periods
        shares_audit = next((a for a in periods[0].audit if a.concept == "Shares Outstanding"), None)
        assert shares_audit is not None
        assert shares_audit.xbrl_tag == "EntityCommonStockSharesOutstanding"


# ===========================================================================
# SECTION D - End-to-end warning behavior
# ===========================================================================


class TestWarningDeduplication:

    @pytest.mark.asyncio
    async def test_lease_pre_asc842_appears_at_most_once(self):
        """Both current + noncurrent capital-lease fallbacks must not double the code."""
        gaap = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": [{
                    "start": "2018-01-01", "end": "2018-12-31", "val": 1_000_000,
                    "form": "10-K", "accn": "0000000001-19-000001", "filed": "2019-03-01",
                }]}
            },
            "CapitalLeaseObligationsCurrent": {"units": {"USD": [{"end": "2018-12-31", "val": 1_000, "form": "10-K"}]}},
            "CapitalLeaseObligationsNoncurrent": {"units": {"USD": [{"end": "2018-12-31", "val": 5_000, "form": "10-K"}]}},
        }
        periods = await extract_ttm_periods({"facts": {"us-gaap": gaap, "dei": {}}}, ticker=None)
        assert len(periods) == 1
        assert [w.code for w in periods[0].warnings].count("lease_pre_asc842") <= 1

    @pytest.mark.asyncio
    async def test_ttm_annualized_appears_at_most_once(self):
        """Multiple annualized concepts collapse to a single warning code per period."""
        gaap = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": [{"start": "2024-01-01", "end": "2024-09-30", "val": 300_000_000,
                                   "form": "10-Q", "accn": "0000000001-24-000099", "filed": "2024-11-01"}]}
            },
            "NetCashProvidedByUsedInOperatingActivities": {
                "units": {"USD": [{"start": "2024-01-01", "end": "2024-09-30", "val": 100_000_000,
                                   "form": "10-Q", "accn": "0000000001-24-000099", "filed": "2024-11-01"}]}
            },
        }
        periods = await extract_ttm_periods({"facts": {"us-gaap": gaap, "dei": {}}}, ticker=None)
        assert len(periods) == 1
        assert [w.code for w in periods[0].warnings].count("ttm_annualized") <= 1


class TestTtmAnnualizedWarning:

    @pytest.mark.asyncio
    async def test_annualization_warning_when_ipo_year(self):
        gaap = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": [{
                    "start": "2024-01-01", "end": "2024-09-30", "val": 300_000_000,
                    "form": "10-Q", "accn": "0000000001-24-000099", "filed": "2024-11-01",
                }]}
            }
        }
        periods = await extract_ttm_periods({"facts": {"us-gaap": gaap, "dei": {}}}, ticker=None)
        assert len(periods) == 1
        assert any(w.code == "ttm_annualized" for w in periods[0].warnings)
        assert periods[0].revenue is not None

    @pytest.mark.asyncio
    async def test_no_annualization_warning_when_bridge_complete(self):
        """Full TTM bridge must NOT set ttm_annualized."""
        gaap = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": [
                    {"start": "2023-01-01", "end": "2023-12-31", "val": 1_000_000,
                     "form": "10-K", "accn": "0001", "filed": "2024-02-01"},
                    {"start": "2023-01-01", "end": "2023-09-30", "val": 750_000,
                     "form": "10-Q", "accn": "0002", "filed": "2023-11-01"},
                    {"start": "2024-01-01", "end": "2024-09-30", "val": 800_000,
                     "form": "10-Q", "accn": "0003", "filed": "2024-11-01"},
                ]}
            }
        }
        periods = await extract_ttm_periods({"facts": {"us-gaap": gaap, "dei": {}}}, ticker=None)
        q3 = next((p for p in periods if p.period_end == date(2024, 9, 30)), None)
        assert q3 is not None
        assert not any(w.code == "ttm_annualized" for w in q3.warnings)


class TestDebtDeduplication:

    @pytest.mark.asyncio
    async def test_current_portion_zeroed_when_using_total_ltd(self):
        gaap = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": [{"start": "2024-01-01", "end": "2024-12-31", "val": 1_000_000,
                                   "form": "10-K", "accn": "0001", "filed": "2025-02-01"}]}
            },
            "LongTermDebt": {"units": {"USD": [{"end": "2024-12-31", "val": 500_000, "form": "10-K"}]}},
            "LongTermDebtCurrent": {"units": {"USD": [{"end": "2024-12-31", "val": 50_000, "form": "10-K"}]}},
        }
        periods = await extract_ttm_periods({"facts": {"us-gaap": gaap, "dei": {}}}, ticker=None)
        assert len(periods) == 1
        p = periods[0]
        assert p.long_term_debt == Decimal("500000")
        assert p.current_portion_lt_debt is None     # zeroed to avoid double-counting
        assert any(w.code == "debt_deduplicated" for w in p.warnings)


# ===========================================================================
# SECTION E - Structural invariants and anchor deduplication
# ===========================================================================


class TestFilingAnchorDuplicatePeriodDedup:

    def test_later_filed_wins_on_duplicate_period_end(self):
        gaap = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": [
                    {"start": "2024-01-01", "end": "2024-12-31", "val": 1_000_000,
                     "form": "10-K", "accn": "0000000001-25-000001", "filed": "2025-02-01"},
                    {"start": "2024-01-01", "end": "2024-12-31", "val": 1_100_000,
                     "form": "10-K", "accn": "0000000002-25-000002", "filed": "2025-03-15"},
                ]}
            }
        }
        anchors = _collect_filing_anchors(gaap)
        assert len(anchors) == 1
        assert anchors[0].filed == date(2025, 3, 15)
        assert anchors[0].accn == "0000000002-25-000002"


class TestAmbiguousFactWarningAtPeriodLevel:

    @pytest.mark.asyncio
    async def test_ambiguous_revenue_produces_warning(self):
        gaap = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": [
                    {"start": "2024-01-01", "end": "2024-12-31", "val": 1_000_000,
                     "form": "10-K", "accn": "0001", "filed": "2025-02-01"},
                    {"start": "2024-01-01", "end": "2024-12-31", "val": 2_000_000,
                     "form": "10-K", "accn": "0002", "filed": "2025-02-01"},
                ]}
            }
        }
        periods = await extract_ttm_periods({"facts": {"us-gaap": gaap, "dei": {}}}, ticker=None)
        assert len(periods) == 1
        assert periods[0].revenue is None
        assert any(w.code == "ambiguous_fact" for w in periods[0].warnings)

    @pytest.mark.asyncio
    async def test_ambiguous_instant_produces_warning(self):
        gaap = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": [{"start": "2024-01-01", "end": "2024-12-31", "val": 5_000_000,
                                   "form": "10-K", "accn": "0001", "filed": "2025-02-01"}]}
            },
            "CashAndCashEquivalentsAtCarryingValue": {
                "units": {"USD": [
                    {"end": "2024-12-31", "val": 100_000, "form": "10-K", "accn": "0001"},
                    {"end": "2024-12-31", "val": 999_000, "form": "10-K", "accn": "0002"},
                ]}
            },
        }
        periods = await extract_ttm_periods({"facts": {"us-gaap": gaap, "dei": {}}}, ticker=None)
        assert len(periods) == 1
        assert periods[0].cash is None
        assert any(w.code == "ambiguous_fact" for w in periods[0].warnings)

    @pytest.mark.asyncio
    async def test_fallback_used_when_primary_ambiguous(self):
        gaap = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": [
                    {"start": "2024-01-01", "end": "2024-12-31", "val": 1_000_000,
                     "form": "10-K", "accn": "0001", "filed": "2025-02-01"},
                    {"start": "2024-01-01", "end": "2024-12-31", "val": 9_000_000,
                     "form": "10-K", "accn": "0002", "filed": "2025-02-01"},
                ]}
            },
            "Revenues": {
                "units": {"USD": [{"start": "2024-01-01", "end": "2024-12-31", "val": 3_000_000,
                                   "form": "10-K", "accn": "0001", "filed": "2025-02-01"}]}
            },
        }
        periods = await extract_ttm_periods({"facts": {"us-gaap": gaap, "dei": {}}}, ticker=None)
        assert len(periods) == 1
        p = periods[0]
        assert p.revenue == Decimal("3000000")
        assert not any(w.code == "ambiguous_fact" for w in p.warnings)
        assert any(w.code == "fallback_revenue" for w in p.warnings)


class TestAmendmentExistsWarning:

    @pytest.mark.asyncio
    async def test_amendment_only_concept_produces_warning(self):
        gaap = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": [{"start": "2024-01-01", "end": "2024-12-31", "val": 5_000_000,
                                   "form": "10-K", "accn": "0001", "filed": "2025-02-01"}]}
            },
            "NetCashProvidedByUsedInOperatingActivities": {
                "units": {"USD": [{"start": "2024-01-01", "end": "2024-12-31", "val": 2_000_000,
                                   "form": "10-K/A", "accn": "0099", "filed": "2025-03-01"}]}
            },
        }
        periods = await extract_ttm_periods({"facts": {"us-gaap": gaap, "dei": {}}}, ticker=None)
        assert len(periods) == 1
        p = periods[0]
        assert p.operating_cash_flow == Decimal("2000000")
        assert any(w.code == "amendment_exists" for w in p.warnings)

    @pytest.mark.asyncio
    async def test_original_present_no_amendment_warning(self):
        gaap = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": [{"start": "2024-01-01", "end": "2024-12-31", "val": 5_000_000,
                                   "form": "10-K", "accn": "0001", "filed": "2025-02-01"}]}
            },
            "NetCashProvidedByUsedInOperatingActivities": {
                "units": {"USD": [
                    {"start": "2024-01-01", "end": "2024-12-31", "val": 2_000_000, "form": "10-K", "accn": "0001", "filed": "2025-02-01"},
                    {"start": "2024-01-01", "end": "2024-12-31", "val": 2_100_000, "form": "10-K/A", "accn": "0099", "filed": "2025-03-01"},
                ]}
            },
        }
        periods = await extract_ttm_periods({"facts": {"us-gaap": gaap, "dei": {}}}, ticker=None)
        assert len(periods) == 1
        assert periods[0].operating_cash_flow == Decimal("2000000")
        assert not any(w.code == "amendment_exists" for w in periods[0].warnings)


class TestTagChainOrder:
    """Chain order must match README.md specification."""

    def test_minority_interest_chain_order(self):
        from app.services.xbrl import _INSTANT_CHAINS
        chain = _INSTANT_CHAINS["minority_interest"]
        assert chain[0] == "NoncontrollingInterest"
        assert chain[1] == "MinorityInterest"

    def test_short_term_borrowings_chain_order(self):
        from app.services.xbrl import _INSTANT_CHAINS
        chain = _INSTANT_CHAINS["short_term_borrowings"]
        assert chain[0] == "ShortTermBorrowings"
        assert chain[1] == "ShortTermDebt"

    def test_noncontrolling_interest_wins_over_minority_interest(self):
        from app.services.xbrl import _resolve_instant, _INSTANT_CHAINS
        gaap = {
            "NoncontrollingInterest": {"units": {"USD": [{"end": "2024-12-31", "val": 50_000, "form": "10-K"}]}},
            "MinorityInterest": {"units": {"USD": [{"end": "2024-12-31", "val": 99_000, "form": "10-K"}]}},
        }
        res = _resolve_instant(gaap, _INSTANT_CHAINS["minority_interest"], date(2024, 12, 31))
        assert res.value == Decimal("50000")
        assert not res.is_fallback

    def test_minority_interest_fallback_fires(self):
        from app.services.xbrl import _resolve_instant, _INSTANT_CHAINS
        gaap = {"MinorityInterest": {"units": {"USD": [{"end": "2024-12-31", "val": 30_000, "form": "10-K"}]}}}
        res = _resolve_instant(gaap, _INSTANT_CHAINS["minority_interest"], date(2024, 12, 31))
        assert res.value == Decimal("30000")
        assert res.is_fallback


class TestSharesDeduplication:
    """_get_shares must use _build_instant_map deduplication rules."""

    def test_conflicting_gaap_shares_returns_none(self):
        gaap = {
            "CommonStockSharesOutstanding": {
                "units": {"shares": [
                    {"end": "2024-12-31", "val": 1_000_000_000, "form": "10-K", "accn": "0001"},
                    {"end": "2024-12-31", "val": 1_050_000_000, "form": "10-K", "accn": "0002"},
                ]}
            }
        }
        shares, tag, warnings = _get_shares(gaap, {}, date(2024, 12, 31), "any")
        assert shares is None

    def test_amendment_shares_not_preferred_over_original(self):
        gaap = {
            "CommonStockSharesOutstanding": {
                "units": {"shares": [
                    {"end": "2024-12-31", "val": 900_000_000, "form": "10-K", "accn": "0001"},
                    {"end": "2024-12-31", "val": 999_000_000, "form": "10-K/A", "accn": "0099"},
                ]}
            }
        }
        shares, tag, warnings = _get_shares(gaap, {}, date(2024, 12, 31), "any")
        assert shares == Decimal("900000000")


# ===========================================================================
# SECTION F - Spec-compliance regression tests
# ===========================================================================


class TestAnchorDurationFilter:
    """The 45-day minimum must filter out sub-quarterly stub data when
    computing fiscal_year_start. # 45-day min filters sub-quarter stubs"""

    def test_short_stub_fact_excluded_from_fy_start_candidates(self):
        """Even if a sub-45-day fact ends at period_end, fiscal_year_start
        should still come from a longer (proper-quarter) fact."""
        gaap = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": [
                    # 30-day stub - must be IGNORED for fy_start computation
                    {"start": "2024-12-01", "end": "2024-12-31", "val": 100,
                     "form": "10-K", "accn": "0001", "filed": "2025-02-01"},
                    # Proper annual fact - fy_start should derive from this
                    {"start": "2024-01-01", "end": "2024-12-31", "val": 1_000_000,
                     "form": "10-K", "accn": "0001", "filed": "2025-02-01"},
                ]}
            }
        }
        anchors = _collect_filing_anchors(gaap)
        assert len(anchors) == 1
        assert anchors[0].fiscal_year_start == date(2024, 1, 1)


class TestPrecomputeMaps:
    """Direct tests for the cache builders. These pin the (tag, unit) key
    scheme that downstream code depends on. # (tag, unit) cache key contract"""

    def test_flow_cache_keys_tag_unit_pairs(self):
        from app.services.xbrl import _precompute_flow_maps
        gaap = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": [{"start": "2024-01-01", "end": "2024-12-31",
                                   "val": 1_000, "form": "10-K"}]}
            },
            "EarningsPerShareDiluted": {
                "units": {"USD/shares": [{"start": "2024-01-01", "end": "2024-12-31",
                                          "val": 1.25, "form": "10-K"}]}
            },
        }
        cache = _precompute_flow_maps(gaap)
        assert ("RevenueFromContractWithCustomerExcludingAssessedTax", "USD") in cache
        assert ("EarningsPerShareDiluted", "USD/shares") in cache

    def test_flow_cache_omits_tags_with_no_usd_facts(self):
        """A tag present in gaap but with only non-USD units must not be cached."""
        from app.services.xbrl import _precompute_flow_maps
        gaap = {
            "Revenues": {
                "units": {"EUR": [{"start": "2024-01-01", "end": "2024-12-31",
                                   "val": 1_000, "form": "10-K"}]}
            },
        }
        cache = _precompute_flow_maps(gaap)
        assert ("Revenues", "USD") not in cache

    def test_instant_cache_includes_gaap_shares(self):
        from app.services.xbrl import _precompute_instant_maps, _GAAP_SHARES_TAG
        gaap = {
            _GAAP_SHARES_TAG: {
                "units": {"shares": [{"end": "2024-12-31", "val": 1_000_000_000,
                                      "form": "10-K"}]}
            },
        }
        cache = _precompute_instant_maps(gaap)
        assert (_GAAP_SHARES_TAG, "shares") in cache

    def test_flow_cache_handles_empty_gaap(self):
        from app.services.xbrl import _precompute_flow_maps
        assert _precompute_flow_maps({}) == {}