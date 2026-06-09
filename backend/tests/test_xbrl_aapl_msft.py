# backend/tests/test_xbrl_aapl_msft.py
"""
Phase 2 XBRL tests - AAPL and MSFT fixtures.

AAPL (Apple Inc., CIK 0000320193)
  - Fiscal year ends late September (non-calendar, non-standard quarter dates,
    e.g. period_end = 2026-03-28, not 2026-03-31)
  - DepreciationDepletionAndAmortization present (primary tag) -> D&A TTM works
  - LongTermDebtNoncurrent present (primary wins); LongTermDebtCurrent kept
  - FinanceLeaseLiabilityCurrent/Noncurrent present at ANNUAL dates only
    -> SIC 3571 (manufacturing) is capital-intensive: warning fires at quarterly
    periods, suppressed at annual period (leases present)
  - CashAndCashEquivalentsAtCarryingValue present -> primary, no warning
  - GAAP CommonStockSharesOutstanding available at quarterly and annual dates

MSFT (Microsoft Corporation, CIK 0000789019)
  - Fiscal year ends June 30 (non-calendar, standard quarter-end dates)
  - DepreciationDepletionAndAmortization AND DepreciationAndAmortization BOTH
    missing -> da = None for all periods (real-data edge case)
  - ShortTermBorrowings present as a tag but only has data through 2018
    -> None for all current periods (tag exists, no recent facts)
  - CashAndCashEquivalentsAtCarryingValue (primary, ~32B) wins over
    CashCashEquivalentsAndShortTermInvestments (fallback, ~78B) - critical
    correctness check: wrong tag would overstate cash by ~2.4×
  - LongTermDebtNoncurrent present at every quarterly period end
  - GAAP CommonStockSharesOutstanding

API contract notes
------------------
- _FilingAnchor has NO .form field.  Tests that previously checked .form have been
  updated to verify period_end, fiscal_year_start, or count instead.
- _get_shares returns (value, tag_used, warnings) - unpack all three:
    shares, tag, _ = _get_shares(...)
  If the second element is passed to a standalone variable, a third must be present.

TTM verification values (all hand-computed from raw XBRL facts):

  AAPL FY2025  (2025-09-27):  Rev=416,161M  OCF=111,482M  EPS=7.46  DA=11,698M
  AAPL Q2FY26  (2026-03-28):  Rev=451,442M  OCF=140,222M  EPS=8.26  DA=12,610M
    Bridge: FY2025(416,161) + YTD_Q2FY26(254,940) - YTD_Q2FY25(219,659) = 451,442
    Bridge: FY2025(111,482) + YTD_Q2FY26(82,627)  - YTD_Q2FY25(53,887)  = 140,222
    Bridge: FY2025(7.46)    + YTD_Q2FY26(4.85)    - YTD_Q2FY25(4.05)    = 8.26
    Bridge: FY2025(11,698)  + YTD_Q2FY26(6,653)   - YTD_Q2FY25(5,741)   = 12,610

  MSFT FY2025  (2025-06-30):  Rev=281,724M  OCF=136,162M  EPS=13.64  CapEx=64,551M
  MSFT Q3FY26  (2026-03-31):  Rev=318,273M  OCF=170,141M  EPS=16.79  CapEx=97,225M
    Bridge: FY2025(281,724) + YTD_Q3FY26(241,832) - YTD_Q3FY25(205,283) = 318,273
    Bridge: FY2025(136,162) + YTD_Q3FY26(127,494) - YTD_Q3FY25(93,515)  = 170,141
    Bridge: FY2025(13.64)   + YTD_Q3FY26(13.14)   - YTD_Q3FY25(9.99)    = 16.79
    Bridge: FY2025(64,551)  + YTD_Q3FY26(80,146)  - YTD_Q3FY25(47,472)  = 97,225

Files needed to re-run these tests:
    aapl_CIK0000320193.json
    msft_CIK0000789019.json
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.services.xbrl import (
    _collect_filing_anchors,
    _extract_capex,
    _extract_cash,
    _extract_debt,
    _extract_eps,
    _extract_finance_lease,
    _extract_revenue,
    _get_shares,
    _resolve_flow,
    _resolve_instant,
    _FLOW_CHAINS,
    _INSTANT_CHAINS,
    extract_ttm_periods,
)

_FIXTURES = Path(__file__).parent / "fixtures"


def _load(fn: str) -> dict:
    p = _FIXTURES / fn
    if not p.exists():
        pytest.skip(f"Fixture not found: {p}")
    return json.loads(p.read_text())


@pytest.fixture(scope="session")
def aapl() -> dict:
    return _load("aapl_CIK0000320193.json")


@pytest.fixture(scope="session")
def msft() -> dict:
    return _load("msft_CIK0000789019.json")


def _g(cf): return cf["facts"]["us-gaap"]
def _d(cf): return cf["facts"].get("dei", {})


# ===========================================================================
# AAPL - Apple Inc.
# ===========================================================================


class TestAaplAnchors:

    def test_most_recent_anchor_is_q2_fy2026(self, aapl):
        anchors = _collect_filing_anchors(_g(aapl))
        latest = max(anchors, key=lambda a: a.period_end)
        assert latest.period_end == date(2026, 3, 28)   # non-round date

    def test_q2_fy2026_fiscal_year_starts_sep_28(self, aapl):
        anchors = _collect_filing_anchors(_g(aapl))
        q2 = next(a for a in anchors if a.period_end == date(2026, 3, 28))
        assert q2.fiscal_year_start == date(2025, 9, 28)

    def test_fy2025_annual_anchor_detected(self, aapl):
        anchors = _collect_filing_anchors(_g(aapl))
        fy25 = next((a for a in anchors if a.period_end == date(2025, 9, 27)), None)
        assert fy25 is not None
        assert fy25.fiscal_year_start == date(2024, 9, 29)

    def test_q1_fy2026_anchor_non_round_date(self, aapl):
        """Q1 FY2026 ends 2025-12-27 - not Dec 31."""
        anchors = _collect_filing_anchors(_g(aapl))
        q1 = next((a for a in anchors if a.period_end == date(2025, 12, 27)), None)
        assert q1 is not None

    def test_at_least_eight_anchors(self, aapl):
        assert len(_collect_filing_anchors(_g(aapl))) >= 8

    def test_anchor_has_no_form_attribute(self, aapl):
        """Guard: _FilingAnchor does not expose a .form field."""
        anchors = _collect_filing_anchors(_g(aapl))
        assert not hasattr(anchors[0], "form")


class TestAaplRevenue:

    def test_fy2025_annual(self, aapl):
        res = _extract_revenue(_g(aapl), date(2025, 9, 27), date(2024, 9, 29))
        assert res.value == Decimal("416161000000")

    def test_q2_fy2026_ttm_bridge(self, aapl):
        """
        FY2025(416,161) + YTD_Q2FY26(254,940) - YTD_Q2FY25(219,659) = 451,442.
        Prior-YTD duration: (2024-09-29->2025-03-29) = 181 days, matches
        current-YTD (2025-09-28->2026-03-28) = 181 days within ±4 tolerance.
        """
        res = _extract_revenue(_g(aapl), date(2026, 3, 28), date(2025, 9, 28))
        assert res.value == Decimal("451442000000")
        assert not any(w.code == "ttm_annualized" for w in res.warnings)


class TestAaplOCF:

    def _ocf(self, cf, period_end, fy_start):
        return _resolve_flow(
            _g(cf), _FLOW_CHAINS["operating_cash_flow"],
            period_end, fy_start, concept_name="OCF",
        ).value

    def test_fy2025_annual(self, aapl):
        assert self._ocf(aapl, date(2025, 9, 27), date(2024, 9, 29)) == Decimal("111482000000")

    def test_q2_fy2026_ttm_bridge(self, aapl):
        """FY2025(111,482) + YTD_Q2FY26(82,627) - YTD_Q2FY25(53,887) = 140,222."""
        res = _resolve_flow(
            _g(aapl), _FLOW_CHAINS["operating_cash_flow"],
            date(2026, 3, 28), date(2025, 9, 28), concept_name="OCF",
        )
        assert res.value == Decimal("140222000000")
        assert not any(w.code == "ttm_annualized" for w in res.warnings)  # full bridge, no annualization


class TestAaplDA:

    def _da(self, cf, period_end, fy_start):
        return _resolve_flow(
            _g(cf), _FLOW_CHAINS["da"],
            period_end, fy_start, concept_name="DA",
        ).value

    def test_fy2025_annual(self, aapl):
        assert self._da(aapl, date(2025, 9, 27), date(2024, 9, 29)) == Decimal("11698000000")

    def test_q2_fy2026_ttm_bridge(self, aapl):
        """FY2025(11,698) + YTD_Q2FY26(6,653) - YTD_Q2FY25(5,741) = 12,610."""
        res = _resolve_flow(
            _g(aapl), _FLOW_CHAINS["da"],
            date(2026, 3, 28), date(2025, 9, 28), concept_name="DA",
        )
        assert res.value == Decimal("12610000000")
        assert not any(w.code == "ttm_annualized" for w in res.warnings)  # full bridge, no annualization


class TestAaplEPS:

    def test_fy2025_annual(self, aapl):
        res = _extract_eps(_g(aapl), date(2025, 9, 27), date(2024, 9, 29))
        assert res.value == Decimal("7.46")
        assert not res.is_fallback

    def test_q2_fy2026_ttm_bridge(self, aapl):
        """FY2025(7.46) + YTD_Q2FY26(4.85) - YTD_Q2FY25(4.05) = 8.26."""
        res = _extract_eps(_g(aapl), date(2026, 3, 28), date(2025, 9, 28))
        assert res.value == Decimal("8.26")
        assert not any(w.code == "ttm_annualized" for w in res.warnings)  # full bridge, no annualization


class TestAaplCapex:

    def test_fy2025_annual(self, aapl):
        res = _extract_capex(_g(aapl), date(2025, 9, 27), date(2024, 9, 29))
        assert res.value == Decimal("12715000000")
        assert not any(w.code == "capex_sign_normalized" for w in res.warnings)


class TestAaplDebt:

    def test_fy2025_noncurrent_primary_wins(self, aapl):
        """LongTermDebtNoncurrent (primary) beats LongTermDebt (total)."""
        res, used_total = _extract_debt(_g(aapl), date(2025, 9, 27))
        assert res.value == Decimal("78328000000")
        assert not used_total
        assert not res.warnings

    def test_q2_fy2026_noncurrent(self, aapl):
        res, used_total = _extract_debt(_g(aapl), date(2026, 3, 28))
        assert res.value == Decimal("74404000000")
        assert not used_total

    def test_fy2025_current_portion_kept(self, aapl):
        """When noncurrent wins, current portion is NOT zeroed."""
        _, used_total = _extract_debt(_g(aapl), date(2025, 9, 27))
        assert not used_total
        cur = _resolve_instant(_g(aapl), _INSTANT_CHAINS["current_portion_lt_debt"],
                               date(2025, 9, 27))
        assert cur.value == Decimal("12350000000")

    def test_q2_fy2026_current_portion_kept(self, aapl):
        _, used_total = _extract_debt(_g(aapl), date(2026, 3, 28))
        assert not used_total
        cur = _resolve_instant(_g(aapl), _INSTANT_CHAINS["current_portion_lt_debt"],
                               date(2026, 3, 28))
        assert cur.value == Decimal("8310000000")


class TestAaplFinanceLease:

    def test_fy2025_annual_leases_present(self, aapl):
        """Leases reported at FY annual dates only."""
        cur = _extract_finance_lease(_g(aapl), date(2025, 9, 27), current=True)
        nc  = _extract_finance_lease(_g(aapl), date(2025, 9, 27), current=False)
        assert cur.value == Decimal("538000000")
        assert nc.value == Decimal("692000000")
        assert not cur.warnings and not nc.warnings  # primary tags

    def test_q2_fy2026_quarterly_leases_absent(self, aapl):
        """AAPL does not tag FinanceLeaseLiability at quarterly period ends."""
        cur = _extract_finance_lease(_g(aapl), date(2026, 3, 28), current=True)
        nc  = _extract_finance_lease(_g(aapl), date(2026, 3, 28), current=False)
        assert cur.value is None
        assert nc.value is None

    def test_q1_fy2026_quarterly_leases_absent(self, aapl):
        cur = _extract_finance_lease(_g(aapl), date(2025, 12, 27), current=True)
        assert cur.value is None


class TestAaplCashAndBalanceSheet:

    def test_fy2025_primary_cash_tag(self, aapl):
        res = _extract_cash(_g(aapl), date(2025, 9, 27))
        assert res.value == Decimal("35934000000")
        assert not res.warnings  # CashAndCashEquivalentsAtCarryingValue fired

    def test_q2_fy2026_cash(self, aapl):
        res = _extract_cash(_g(aapl), date(2026, 3, 28))
        assert res.value == Decimal("45572000000")
        assert not res.warnings

    def test_fy2025_equity(self, aapl):
        res = _resolve_instant(_g(aapl), _INSTANT_CHAINS["stockholders_equity"], date(2025, 9, 27))
        assert res.value == Decimal("73733000000")

    def test_q2_fy2026_equity(self, aapl):
        res = _resolve_instant(_g(aapl), _INSTANT_CHAINS["stockholders_equity"], date(2026, 3, 28))
        assert res.value == Decimal("106491000000")


class TestAaplShares:

    def test_fy2025_gaap_shares(self, aapl):
        anchors = _collect_filing_anchors(_g(aapl))
        anchor = next(a for a in anchors if a.period_end == date(2025, 9, 27))
        shares, tag, _ = _get_shares(_g(aapl), _d(aapl), date(2025, 9, 27), anchor.accn)
        assert shares == Decimal("14773260000")
        assert tag == "CommonStockSharesOutstanding"

    def test_q2_fy2026_gaap_shares(self, aapl):
        """Share count decreases over time due to buybacks."""
        anchors = _collect_filing_anchors(_g(aapl))
        anchor = next(a for a in anchors if a.period_end == date(2026, 3, 28))
        shares, tag, _ = _get_shares(_g(aapl), _d(aapl), date(2026, 3, 28), anchor.accn)
        assert shares == Decimal("14667688000")
        assert tag == "CommonStockSharesOutstanding"

        # Monotonically decreasing due to buybacks
        fy25_anchor = next(a for a in anchors if a.period_end == date(2025, 9, 27))
        fy25_shares, _, _ = _get_shares(_g(aapl), _d(aapl), date(2025, 9, 27), fy25_anchor.accn)
        assert shares < fy25_shares


@pytest.mark.asyncio
class TestAaplEndToEnd:

    @staticmethod
    def _mock_price(v=Decimal("210.00")):
        return patch(
            "app.services.xbrl.price_svc.get_price",
            new_callable=AsyncMock,
            return_value=v,
        )

    async def test_fy2025_period_comprehensive(self, aapl):
        with self._mock_price():
            periods = await extract_ttm_periods(aapl, ticker="AAPL", is_capital_intensive=True)
        fy25 = next(p for p in periods if p.period_end == date(2025, 9, 27))
        assert fy25.revenue             == Decimal("416161000000")
        assert fy25.operating_cash_flow == Decimal("111482000000")
        assert fy25.eps_diluted         == Decimal("7.46")
        assert fy25.depreciation_and_amortization == Decimal("11698000000")
        assert fy25.capex               == Decimal("12715000000")
        assert fy25.long_term_debt      == Decimal("78328000000")
        assert fy25.current_portion_lt_debt == Decimal("12350000000")
        assert fy25.finance_lease_current    == Decimal("538000000")
        assert fy25.finance_lease_noncurrent == Decimal("692000000")
        assert fy25.cash                == Decimal("35934000000")
        assert fy25.stockholders_equity == Decimal("73733000000")
        assert fy25.shares_outstanding  == Decimal("14773260000")

    async def test_fy2025_no_lease_warning_annual_period(self, aapl):
        """Leases present at annual -> capital_intensive warning must NOT fire."""
        with self._mock_price():
            periods = await extract_ttm_periods(aapl, ticker="AAPL", is_capital_intensive=True)
        fy25 = next(p for p in periods if p.period_end == date(2025, 9, 27))
        assert not any(w.code == "finance_lease_missing_capital_intensive" for w in fy25.warnings)

    async def test_q2_fy2026_lease_warning_quarterly(self, aapl):
        """
        AAPL SIC 3571 = manufacturing -> capital_intensive=True.
        Leases absent at quarterly period end -> warning fires.
        """
        with self._mock_price():
            periods = await extract_ttm_periods(aapl, ticker="AAPL", is_capital_intensive=True)
        q2 = next(p for p in periods if p.period_end == date(2026, 3, 28))
        assert q2.finance_lease_current is None
        assert q2.finance_lease_noncurrent is None
        assert any(w.code == "finance_lease_missing_capital_intensive" for w in q2.warnings)

    async def test_q1_fy2026_lease_warning_quarterly(self, aapl):
        with self._mock_price():
            periods = await extract_ttm_periods(aapl, ticker="AAPL", is_capital_intensive=True)
        q1 = next(p for p in periods if p.period_end == date(2025, 12, 27))
        assert any(w.code == "finance_lease_missing_capital_intensive" for w in q1.warnings)

    async def test_q2_fy2026_period_values(self, aapl):
        with self._mock_price():
            periods = await extract_ttm_periods(aapl, ticker="AAPL")
        q2 = next(p for p in periods if p.period_end == date(2026, 3, 28))
        assert q2.revenue             == Decimal("451442000000")
        assert q2.operating_cash_flow == Decimal("140222000000")
        assert q2.eps_diluted         == Decimal("8.26")
        assert q2.long_term_debt      == Decimal("74404000000")
        assert q2.current_portion_lt_debt == Decimal("8310000000")
        assert q2.cash                == Decimal("45572000000")
        assert q2.stockholders_equity == Decimal("106491000000")
        assert q2.shares_outstanding  == Decimal("14667688000")

    async def test_no_debt_deduplicated_warning(self, aapl):
        """Primary LongTermDebtNoncurrent tag fires -> no dedup warning."""
        with self._mock_price():
            periods = await extract_ttm_periods(aapl, ticker="AAPL")
        for p in periods:
            assert not any(w.code == "debt_deduplicated" for w in p.warnings)

    async def test_periods_sorted_most_recent_first(self, aapl):
        with self._mock_price():
            periods = await extract_ttm_periods(aapl, ticker="AAPL")
        ends = [p.period_end for p in periods]
        assert ends == sorted(ends, reverse=True)


# ===========================================================================
# MSFT - Microsoft Corporation (June fiscal year, no D&A, cash primary wins)
# ===========================================================================


class TestMsftAnchors:

    def test_most_recent_anchor_is_q3_fy2026(self, msft):
        anchors = _collect_filing_anchors(_g(msft))
        latest = max(anchors, key=lambda a: a.period_end)
        assert latest.period_end == date(2026, 3, 31)

    def test_q3_fy2026_fiscal_year_starts_july_1(self, msft):
        anchors = _collect_filing_anchors(_g(msft))
        q3 = next(a for a in anchors if a.period_end == date(2026, 3, 31))
        assert q3.fiscal_year_start == date(2025, 7, 1)

    def test_fy2025_annual_anchor_ends_june_30(self, msft):
        anchors = _collect_filing_anchors(_g(msft))
        fy25 = next((a for a in anchors if a.period_end == date(2025, 6, 30)), None)
        assert fy25 is not None
        assert fy25.fiscal_year_start == date(2024, 7, 1)

    def test_at_least_eight_anchors(self, msft):
        assert len(_collect_filing_anchors(_g(msft))) >= 8

    def test_anchor_has_no_form_attribute(self, msft):
        anchors = _collect_filing_anchors(_g(msft))
        assert not hasattr(anchors[0], "form")


class TestMsftRevenue:

    def test_fy2025_annual(self, msft):
        res = _extract_revenue(_g(msft), date(2025, 6, 30), date(2024, 7, 1))
        assert res.value == Decimal("281724000000")

    def test_q3_fy2026_ttm_bridge(self, msft):
        """
        FY2025(281,724) + YTD_Q3FY26(241,832) - YTD_Q3FY25(205,283) = 318,273.
        Prior-YTD: (2024-07-01->2025-03-31) = 273 days, matches current 273 days.
        """
        res = _extract_revenue(_g(msft), date(2026, 3, 31), date(2025, 7, 1))
        assert res.value == Decimal("318273000000")
        assert not any(w.code == "ttm_annualized" for w in res.warnings)


class TestMsftOCF:

    def _ocf(self, cf, period_end, fy_start):
        return _resolve_flow(
            _g(cf), _FLOW_CHAINS["operating_cash_flow"],
            period_end, fy_start, concept_name="OCF",
        ).value

    def test_fy2025_annual(self, msft):
        assert self._ocf(msft, date(2025, 6, 30), date(2024, 7, 1)) == Decimal("136162000000")

    def test_q3_fy2026_ttm_bridge(self, msft):
        """FY2025(136,162) + YTD_Q3FY26(127,494) - YTD_Q3FY25(93,515) = 170,141."""
        res = _resolve_flow(
            _g(msft), _FLOW_CHAINS["operating_cash_flow"],
            date(2026, 3, 31), date(2025, 7, 1), concept_name="OCF",
        )
        assert res.value == Decimal("170141000000")
        assert not any(w.code == "ttm_annualized" for w in res.warnings)  # full bridge, no annualization


class TestMsftCapex:

    def test_fy2025_annual(self, msft):
        res = _extract_capex(_g(msft), date(2025, 6, 30), date(2024, 7, 1))
        assert res.value == Decimal("64551000000")

    def test_q3_fy2026_ttm_bridge(self, msft):
        """FY2025(64,551) + YTD_Q3FY26(80,146) - YTD_Q3FY25(47,472) = 97,225."""
        res = _extract_capex(_g(msft), date(2026, 3, 31), date(2025, 7, 1))
        assert res.value == Decimal("97225000000")
        assert not any(w.code == "ttm_annualized" for w in res.warnings)  # full bridge, no annualization


class TestMsftEPS:

    def test_fy2025_annual(self, msft):
        res = _extract_eps(_g(msft), date(2025, 6, 30), date(2024, 7, 1))
        assert res.value == Decimal("13.64")
        assert not res.is_fallback

    def test_q3_fy2026_ttm_bridge(self, msft):
        """FY2025(13.64) + YTD_Q3FY26(13.14) - YTD_Q3FY25(9.99) = 16.79."""
        res = _extract_eps(_g(msft), date(2026, 3, 31), date(2025, 7, 1))
        assert res.value == Decimal("16.79")
        assert not any(w.code == "ttm_annualized" for w in res.warnings)  # full bridge, no annualization


class TestMsftDA:

    def test_da_none_both_tags_missing(self, msft):
        """
        DepreciationDepletionAndAmortization AND DepreciationAndAmortization are
        both absent from MSFT's XBRL filing. da must be None for all periods.
        """
        res = _resolve_flow(
            _g(msft), _FLOW_CHAINS["da"],
            date(2025, 6, 30), date(2024, 7, 1), concept_name="DA",
        )
        assert res.value is None
        assert res.tag_used is None

    def test_da_none_quarterly_period(self, msft):
        res = _resolve_flow(
            _g(msft), _FLOW_CHAINS["da"],
            date(2026, 3, 31), date(2025, 7, 1), concept_name="DA",
        )
        assert res.value is None


class TestMsftDebt:

    def test_fy2025_noncurrent_primary(self, msft):
        res, used_total = _extract_debt(_g(msft), date(2025, 6, 30))
        assert res.value == Decimal("40152000000")
        assert not used_total
        assert not res.warnings

    def test_q3_fy2026_noncurrent_at_quarterly_period(self, msft):
        """MSFT reports LongTermDebtNoncurrent at every quarterly period end."""
        res, used_total = _extract_debt(_g(msft), date(2026, 3, 31))
        assert res.value == Decimal("31423000000")
        assert not used_total

    def test_fy2025_current_portion_kept(self, msft):
        cur = _resolve_instant(
            _g(msft), _INSTANT_CHAINS["current_portion_lt_debt"], date(2025, 6, 30)
        )
        assert cur.value == Decimal("2999000000")

    def test_q3_fy2026_current_portion(self, msft):
        cur = _resolve_instant(
            _g(msft), _INSTANT_CHAINS["current_portion_lt_debt"], date(2026, 3, 31)
        )
        assert cur.value == Decimal("8839000000")


class TestMsftShortTermBorrowings:

    def test_no_recent_short_term_borrowings(self, msft):
        """
        ShortTermBorrowings tag exists (56 facts) but last fact is 2018-06-30.
        For current period ends (2025+) the tag returns None.
        """
        res = _resolve_instant(
            _g(msft), _INSTANT_CHAINS["short_term_borrowings"], date(2026, 3, 31)
        )
        assert res.value is None

    def test_no_recent_short_term_borrowings_annual(self, msft):
        res = _resolve_instant(
            _g(msft), _INSTANT_CHAINS["short_term_borrowings"], date(2025, 6, 30)
        )
        assert res.value is None


class TestMsftCash:

    def test_primary_cash_tag_wins(self, msft):
        """
        CashAndCashEquivalentsAtCarryingValue = 32,105M (primary).
        CashCashEquivalentsAndShortTermInvestments = 78,272M (fallback).
        Primary must win - fallback would overstate cash by ~2.4×.
        """
        res = _extract_cash(_g(msft), date(2026, 3, 31))
        assert res.value == Decimal("32105000000")
        assert not res.warnings

    def test_primary_cash_tag_wins_fy2025(self, msft):
        res = _extract_cash(_g(msft), date(2025, 6, 30))
        assert res.value == Decimal("30242000000")
        assert not res.warnings


class TestMsftBalanceSheet:

    def test_fy2025_equity(self, msft):
        res = _resolve_instant(
            _g(msft), _INSTANT_CHAINS["stockholders_equity"], date(2025, 6, 30)
        )
        assert res.value == Decimal("343479000000")

    def test_q3_fy2026_equity(self, msft):
        res = _resolve_instant(
            _g(msft), _INSTANT_CHAINS["stockholders_equity"], date(2026, 3, 31)
        )
        assert res.value == Decimal("414367000000")


class TestMsftShares:

    def test_fy2025_gaap_shares(self, msft):
        anchors = _collect_filing_anchors(_g(msft))
        anchor = next(a for a in anchors if a.period_end == date(2025, 6, 30))
        shares, tag, _ = _get_shares(_g(msft), _d(msft), date(2025, 6, 30), anchor.accn)
        assert shares == Decimal("7434000000")
        assert tag == "CommonStockSharesOutstanding"

    def test_q3_fy2026_gaap_shares(self, msft):
        anchors = _collect_filing_anchors(_g(msft))
        anchor = next(a for a in anchors if a.period_end == date(2026, 3, 31))
        shares, tag, _ = _get_shares(_g(msft), _d(msft), date(2026, 3, 31), anchor.accn)
        assert shares == Decimal("7429000000")
        assert tag == "CommonStockSharesOutstanding"


@pytest.mark.asyncio
class TestMsftEndToEnd:

    @staticmethod
    def _mock_price(v=Decimal("420.00")):
        return patch(
            "app.services.xbrl.price_svc.get_price",
            new_callable=AsyncMock,
            return_value=v,
        )

    async def test_fy2025_period_comprehensive(self, msft):
        with self._mock_price():
            periods = await extract_ttm_periods(msft, ticker="MSFT")
        fy25 = next(p for p in periods if p.period_end == date(2025, 6, 30))
        assert fy25.revenue             == Decimal("281724000000")
        assert fy25.operating_cash_flow == Decimal("136162000000")
        assert fy25.eps_diluted         == Decimal("13.64")
        assert fy25.capex               == Decimal("64551000000")
        assert fy25.depreciation_and_amortization is None   # both tags missing
        assert fy25.long_term_debt      == Decimal("40152000000")
        assert fy25.current_portion_lt_debt == Decimal("2999000000")
        assert fy25.short_term_borrowings is None           # tag exists, no recent data
        assert fy25.cash                == Decimal("30242000000")
        assert fy25.stockholders_equity == Decimal("343479000000")
        assert fy25.shares_outstanding  == Decimal("7434000000")

    async def test_q3_fy2026_period_values(self, msft):
        with self._mock_price():
            periods = await extract_ttm_periods(msft, ticker="MSFT")
        q3 = next(p for p in periods if p.period_end == date(2026, 3, 31))
        assert q3.revenue             == Decimal("318273000000")
        assert q3.operating_cash_flow == Decimal("170141000000")
        assert q3.eps_diluted         == Decimal("16.79")
        assert q3.capex               == Decimal("97225000000")
        assert q3.depreciation_and_amortization is None
        assert q3.long_term_debt      == Decimal("31423000000")
        assert q3.current_portion_lt_debt == Decimal("8839000000")
        assert q3.cash                == Decimal("32105000000")
        assert q3.stockholders_equity == Decimal("414367000000")
        assert q3.shares_outstanding  == Decimal("7429000000")

    async def test_no_capital_intensive_warning_for_software_sic(self, msft):
        """MSFT SIC 7372 - not capital-intensive. No lease warning."""
        with self._mock_price():
            periods = await extract_ttm_periods(msft, ticker="MSFT", is_capital_intensive=False)
        for p in periods:
            assert not any(w.code == "finance_lease_missing_capital_intensive" for w in p.warnings)

    async def test_no_debt_deduplicated_warning(self, msft):
        """LongTermDebtNoncurrent (primary) fires -> no dedup warning."""
        with self._mock_price():
            periods = await extract_ttm_periods(msft, ticker="MSFT")
        for p in periods:
            assert not any(w.code == "debt_deduplicated" for w in p.warnings)

    async def test_periods_sorted_most_recent_first(self, msft):
        with self._mock_price():
            periods = await extract_ttm_periods(msft, ticker="MSFT")
        ends = [p.period_end for p in periods]
        assert ends == sorted(ends, reverse=True)

    async def test_cash_primary_wins_in_full_period(self, msft):
        """
        Regression guard: the cash deduction used in EV must be
        CashAndCashEquivalentsAtCarryingValue (~30-32B), not
        CashCashEquivalentsAndShortTermInvestments (~78B).
        """
        with self._mock_price():
            periods = await extract_ttm_periods(msft, ticker="MSFT")
        fy25 = next(p for p in periods if p.period_end == date(2025, 6, 30))
        # Primary tag = ~30B. Fallback tag = ~78B. A 2× difference is
        # detectable without knowing the exact current value.
        assert fy25.cash is not None
        assert fy25.cash < Decimal("50000000000"), (
            f"Cash ({fy25.cash}) looks like the fallback tag "
            "(CashCashEquivalentsAndShortTermInvestments) was used instead of primary"
        )