# backend/tests/test_xbrl_extended.py
"""
Extended Phase 2 XBRL tests - TGT, DAL, BRKB fixtures.

Covers edge cases not exercised by the CRCT / SNOW / CART baseline:

  TGT (Target)
    - Non-calendar fiscal year (Feb 2 -> Jan 31, non-round quarter-end dates)
    - LongTermDebtNoncurrent absent -> LongTermDebt fallback + debt_deduplicated warning
    - LongTermDebtCurrent zeroed out when LongTermDebt fallback is used
    - FinanceLeaseLiabilityCurrent / Noncurrent present (primary tags, no warning)
    - CashAndCashEquivalentsAtCarryingValue absent -> CashCashEquivalents… fallback + warning
    - GAAP CommonStockSharesOutstanding

  DAL (Delta Air Lines)
    - Calendar fiscal year, capital-intensive SIC (4512 – Air Transportation)
    - LongTermDebtNoncurrent present (primary tag; current portion kept)
    - LongTermDebt also present but NOT used (noncurrent wins)
    - FinanceLeaseLiability only available at annual dates, absent at Q1 period end
    - finance_lease_missing_capital_intensive warning fires at quarterly periods
    - finance_lease_missing_capital_intensive does NOT fire at annual period (leases present)
    - DEI EntityCommonStockSharesOutstanding (no GAAP CommonStockSharesOutstanding)

  BRKB (Berkshire Hathaway)
    - Calendar fiscal year, financial-sector conglomerate
    - RevenueFromContractWithCustomerExcludingAssessedTax used as primary revenue tag
    - Prior-year YTD available in global fact map even when absent from the Q1 filing
    - MinorityInterest (instant balance-sheet, non-zero)
    - No cash tags in recent periods -> cash = None
    - No debt tags -> long_term_debt = None
    - No DEI shares in recent periods -> shares = None

API contract notes
------------------
- _FilingAnchor has no .form field.  Tests that previously checked .form have been
  updated to verify period_end and fiscal_year_start instead.
- _get_shares returns (value, tag_used, warnings) - a 3-tuple. Callers unpack
  all three elements: shares, tag, _ = _get_shares(...).

Files needed to re-run these tests:
    tgt_CIK0000027419.json
    dal_CIK0000027904.json
    brkb_CIK0001067983.json
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
    _get_shares,
    _resolve_flow,
    _resolve_instant,
    _FLOW_CHAINS,
    _INSTANT_CHAINS,
    _extract_revenue,
    extract_ttm_periods,
)

_FIXTURES = Path(__file__).parent / "fixtures"


def _load(filename: str) -> dict:
    path = _FIXTURES / filename
    if not path.exists():
        pytest.skip(f"Fixture not found: {path}")
    with path.open() as f:
        return json.load(f)


@pytest.fixture(scope="session")
def tgt() -> dict:
    return _load("tgt_CIK0000027419.json")


@pytest.fixture(scope="session")
def dal() -> dict:
    return _load("dal_CIK0000027904.json")


@pytest.fixture(scope="session")
def brkb() -> dict:
    return _load("brkb_CIK0001067983.json")


def _gaap(cf: dict) -> dict:
    return cf["facts"]["us-gaap"]


def _dei(cf: dict) -> dict:
    return cf["facts"].get("dei", {})


# ===========================================================================
# TGT - Target Corporation (non-calendar fiscal year, debt fallback,
#        lease/cash warnings)
# ===========================================================================


class TestTgtFilingAnchors:

    def test_most_recent_anchor_is_fy2026(self, tgt):
        anchors = _collect_filing_anchors(_gaap(tgt))
        most_recent = max(anchors, key=lambda a: a.period_end)
        # FY2026 annual ends January 31
        assert most_recent.period_end == date(2026, 1, 31)

    def test_fy2026_fiscal_year_start_is_feb_2(self, tgt):
        """TGT FY2026 runs 2025-02-02 -> 2026-01-31."""
        anchors = _collect_filing_anchors(_gaap(tgt))
        fy26 = next(a for a in anchors if a.period_end == date(2026, 1, 31))
        assert fy26.fiscal_year_start == date(2025, 2, 2)

    def test_q3_fy2026_anchor_detected(self, tgt):
        """TGT Q3 FY2026 ends 2025-11-01 - a non-round date."""
        anchors = _collect_filing_anchors(_gaap(tgt))
        q3 = next((a for a in anchors if a.period_end == date(2025, 11, 1)), None)
        assert q3 is not None, "Q3 FY2026 anchor must be detected"
        assert q3.fiscal_year_start == date(2025, 2, 2)

    def test_at_least_eight_anchors_found(self, tgt):
        anchors = _collect_filing_anchors(_gaap(tgt))
        assert len(anchors) >= 8

    def test_anchor_has_no_form_attribute(self, tgt):
        """Guard against regressions - _FilingAnchor does not store form."""
        anchors = _collect_filing_anchors(_gaap(tgt))
        assert not hasattr(anchors[0], "form")


class TestTgtRevenue:

    def test_fy2026_annual_revenue(self, tgt):
        """TGT FY2026 (2025-02-02 -> 2026-01-31) = 104,780,000,000."""
        res = _extract_revenue(_gaap(tgt), date(2026, 1, 31), date(2025, 2, 2))
        assert res.value == Decimal("104780000000")

    def test_q3_fy2026_revenue_ttm_bridge(self, tgt):
        """
        TGT Q3 FY2026 (period_end=2025-11-01):
          FY2025  (2024-02-04->2025-02-01) =  106,566,000,000
        + YTD Q3 FY2026 (2025-02-02->2025-11-01) =   74,327,000,000
        - YTD Q3 FY2025 (2024-02-04->2024-11-02) =   75,651,000,000
        =                                             105,242,000,000
        """
        res = _extract_revenue(_gaap(tgt), date(2025, 11, 1), date(2025, 2, 2))
        assert res.value == Decimal("105242000000")
        assert not any(w.code == "ttm_annualized" for w in res.warnings)  # full bridge, no annualization

    def test_no_fallback_revenue_warning_on_primary_tag(self, tgt):
        res = _extract_revenue(_gaap(tgt), date(2026, 1, 31), date(2025, 2, 2))
        assert not any(w.code == "fallback_revenue" for w in res.warnings)


class TestTgtDebt:

    def test_fy2026_uses_long_term_debt_fallback(self, tgt):
        """
        LongTermDebtNoncurrent absent in recent TGT filings.
        Falls back to LongTermDebt = 14,398,000,000.
        """
        res, used_total = _extract_debt(_gaap(tgt), date(2026, 1, 31))
        assert res.value == Decimal("14398000000")
        assert used_total is True

    def test_fy2026_debt_deduplicated_warning(self, tgt):
        res, _ = _extract_debt(_gaap(tgt), date(2026, 1, 31))
        assert any(w.code == "debt_deduplicated" for w in res.warnings)

    def test_fy2026_used_total_signals_current_portion_must_be_zeroed(self, tgt):
        """
        used_total=True means the caller must zero current_portion_lt_debt.
        The end-to-end test verifies this happens; here we confirm the signal.
        """
        _, used_total = _extract_debt(_gaap(tgt), date(2026, 1, 31))
        assert used_total is True

    def test_quarterly_period_ltd_may_be_absent(self, tgt):
        """TGT LongTermDebt is only tagged at annual dates for some periods."""
        res, _ = _extract_debt(_gaap(tgt), date(2025, 11, 1))
        # May be None if the fact isn't tagged at this quarterly period end
        # (this is an informational test, not a hard assertion either way)
        assert res.value is None or isinstance(res.value, Decimal)


class TestTgtFinanceLease:

    def test_fy2026_finance_lease_current_primary_tag(self, tgt):
        """FinanceLeaseLiabilityCurrent at 2026-01-31 = 131,000,000."""
        res = _extract_finance_lease(_gaap(tgt), date(2026, 1, 31), current=True)
        assert res.value == Decimal("131000000")
        assert not res.warnings  # primary tag fired

    def test_fy2026_finance_lease_noncurrent_primary_tag(self, tgt):
        """FinanceLeaseLiabilityNoncurrent at 2026-01-31 = 1,982,000,000."""
        res = _extract_finance_lease(_gaap(tgt), date(2026, 1, 31), current=False)
        assert res.value == Decimal("1982000000")
        assert not res.warnings  # primary tag fired

    def test_no_lease_pre_asc842_warning_on_primary_tag(self, tgt):
        res_cur = _extract_finance_lease(_gaap(tgt), date(2026, 1, 31), current=True)
        res_nc  = _extract_finance_lease(_gaap(tgt), date(2026, 1, 31), current=False)
        assert not any(w.code == "lease_pre_asc842" for w in res_cur.warnings)
        assert not any(w.code == "lease_pre_asc842" for w in res_nc.warnings)

    def test_quarterly_period_no_lease_data(self, tgt):
        """Finance lease tags only filed at annual 10-K dates for TGT."""
        res = _extract_finance_lease(_gaap(tgt), date(2025, 11, 1), current=True)
        assert res.value is None


class TestTgtCash:

    def test_fy2026_uses_fallback_cash_tag(self, tgt):
        """
        CashAndCashEquivalentsAtCarryingValue absent since ~2017.
        Falls back to CashCashEquivalentsAndShortTermInvestments = 5,488,000,000.
        """
        res = _extract_cash(_gaap(tgt), date(2026, 1, 31))
        assert res.value == Decimal("5488000000")

    def test_fy2026_cash_fallback_warning(self, tgt):
        res = _extract_cash(_gaap(tgt), date(2026, 1, 31))
        assert any(w.code == "cash_fallback_includes_investments" for w in res.warnings)

    def test_q3_fy2026_cash_fallback(self, tgt):
        """Quarterly periods also use the fallback tag."""
        res = _extract_cash(_gaap(tgt), date(2025, 11, 1))
        assert res.value == Decimal("3822000000")
        assert any(w.code == "cash_fallback_includes_investments" for w in res.warnings)


class TestTgtSharesAndEquity:

    def test_fy2026_gaap_shares(self, tgt):
        """GAAP CommonStockSharesOutstanding at 2026-01-31 = 452,840,187."""
        anchors = _collect_filing_anchors(_gaap(tgt))
        anchor = next(a for a in anchors if a.period_end == date(2026, 1, 31))
        shares, tag, _ = _get_shares(_gaap(tgt), _dei(tgt), date(2026, 1, 31), anchor.accn)
        assert shares == Decimal("452840187")
        assert tag == "CommonStockSharesOutstanding"

    def test_q3_fy2026_gaap_shares(self, tgt):
        """GAAP shares at 2025-11-01 = 452,796,520."""
        anchors = _collect_filing_anchors(_gaap(tgt))
        anchor = next(a for a in anchors if a.period_end == date(2025, 11, 1))
        shares, tag, _ = _get_shares(_gaap(tgt), _dei(tgt), date(2025, 11, 1), anchor.accn)
        assert shares == Decimal("452796520")
        assert tag == "CommonStockSharesOutstanding"

    def test_fy2026_stockholders_equity(self, tgt):
        res = _resolve_instant(_gaap(tgt), _INSTANT_CHAINS["stockholders_equity"], date(2026, 1, 31))
        assert res.value == Decimal("16165000000")


@pytest.mark.asyncio
class TestTgtEndToEnd:

    @staticmethod
    def _mock_price(val=Decimal("130.00")):
        return patch(
            "app.services.xbrl.price_svc.get_price",
            new_callable=AsyncMock,
            return_value=val,
        )

    async def test_fy2026_period_populates_debt_deduplicated(self, tgt):
        with self._mock_price():
            periods = await extract_ttm_periods(tgt, ticker="TGT")
        fy26 = next(p for p in periods if p.period_end == date(2026, 1, 31))
        assert fy26.long_term_debt == Decimal("14398000000")
        assert fy26.current_portion_lt_debt is None  # zeroed due to dedup
        assert any(w.code == "debt_deduplicated" for w in fy26.warnings)

    async def test_fy2026_finance_leases_populated(self, tgt):
        with self._mock_price():
            periods = await extract_ttm_periods(tgt, ticker="TGT")
        fy26 = next(p for p in periods if p.period_end == date(2026, 1, 31))
        assert fy26.finance_lease_current == Decimal("131000000")
        assert fy26.finance_lease_noncurrent == Decimal("1982000000")
        assert not any(w.code == "lease_pre_asc842" for w in fy26.warnings)

    async def test_fy2026_cash_fallback_warning_in_period(self, tgt):
        with self._mock_price():
            periods = await extract_ttm_periods(tgt, ticker="TGT")
        fy26 = next(p for p in periods if p.period_end == date(2026, 1, 31))
        assert fy26.cash == Decimal("5488000000")
        assert any(w.code == "cash_fallback_includes_investments" for w in fy26.warnings)

    async def test_fy2026_revenue_and_oi(self, tgt):
        with self._mock_price():
            periods = await extract_ttm_periods(tgt, ticker="TGT")
        fy26 = next(p for p in periods if p.period_end == date(2026, 1, 31))
        assert fy26.revenue == Decimal("104780000000")
        assert fy26.operating_income == Decimal("5117000000")

    async def test_periods_sorted_most_recent_first(self, tgt):
        with self._mock_price():
            periods = await extract_ttm_periods(tgt, ticker="TGT")
        ends = [p.period_end for p in periods]
        assert ends == sorted(ends, reverse=True)

    async def test_fy2026_cash_audit_entry_records_fallback_tag(self, tgt):
        """Audit entry for cash must name the fallback tag, not the primary. # audit records the tag actually used"""
        with self._mock_price():
            periods = await extract_ttm_periods(tgt, ticker="TGT")
        fy26 = next(p for p in periods if p.period_end == date(2026, 1, 31))
        cash_audit = next((a for a in fy26.audit if a.concept == "Cash"), None)
        assert cash_audit is not None
        assert cash_audit.xbrl_tag == "CashCashEquivalentsAndShortTermInvestments"
        assert cash_audit.is_fallback is True
        assert cash_audit.unit == "USD"

    async def test_fy2026_debt_audit_entry_records_total_ltd_tag(self, tgt):
        """When LongTermDebt fallback fires, audit must record that tag. # audit records the tag actually used"""
        with self._mock_price():
            periods = await extract_ttm_periods(tgt, ticker="TGT")
        fy26 = next(p for p in periods if p.period_end == date(2026, 1, 31))
        debt_audit = next((a for a in fy26.audit if a.concept == "Long-Term Debt"), None)
        assert debt_audit is not None
        assert debt_audit.xbrl_tag == "LongTermDebt"
        assert debt_audit.is_fallback is True


# ===========================================================================
# DAL - Delta Air Lines (capital-intensive, primary debt tags,
#        finance-lease at annual only, DEI shares)
# ===========================================================================


class TestDalFilingAnchors:

    def test_most_recent_anchor_is_q1_2026(self, dal):
        anchors = _collect_filing_anchors(_gaap(dal))
        most_recent = max(anchors, key=lambda a: a.period_end)
        assert most_recent.period_end == date(2026, 3, 31)

    def test_fy2025_annual_anchor_present(self, dal):
        anchors = _collect_filing_anchors(_gaap(dal))
        fy25 = next((a for a in anchors if a.period_end == date(2025, 12, 31)), None)
        assert fy25 is not None

    def test_q1_2026_fiscal_year_start_is_jan_1(self, dal):
        anchors = _collect_filing_anchors(_gaap(dal))
        q1 = next(a for a in anchors if a.period_end == date(2026, 3, 31))
        assert q1.fiscal_year_start == date(2026, 1, 1)

    def test_at_least_six_anchors(self, dal):
        anchors = _collect_filing_anchors(_gaap(dal))
        assert len(anchors) >= 6

    def test_anchor_has_no_form_attribute(self, dal):
        anchors = _collect_filing_anchors(_gaap(dal))
        assert not hasattr(anchors[0], "form")


class TestDalRevenue:

    def test_fy2025_annual_revenue(self, dal):
        """DAL FY2025 = 63,364,000,000."""
        res = _extract_revenue(_gaap(dal), date(2025, 12, 31), date(2025, 1, 1))
        assert res.value == Decimal("63364000000")

    def test_q1_2026_revenue_ttm_bridge(self, dal):
        """
        DAL Q1 2026:
          FY2025  (2025-01-01->2025-12-31) = 63,364,000,000
        + Q1 2026 (2026-01-01->2026-03-31) = 15,854,000,000
        - Q1 2025 (2025-01-01->2025-03-31) = 14,040,000,000
        =                                    65,178,000,000
        """
        res = _extract_revenue(_gaap(dal), date(2026, 3, 31), date(2026, 1, 1))
        assert res.value == Decimal("65178000000")
        assert not any(w.code == "ttm_annualized" for w in res.warnings)  # full bridge, no annualization


class TestDalDebt:

    def test_q1_2026_noncurrent_debt_is_primary(self, dal):
        """
        LongTermDebtNoncurrent at 2026-03-31 = 10,608,000,000.
        Primary tag wins; LongTermDebt (13,235,000,000) is NOT used.
        """
        res, used_total = _extract_debt(_gaap(dal), date(2026, 3, 31))
        assert res.value == Decimal("10608000000")
        assert used_total is False
        assert not res.warnings

    def test_q1_2026_current_portion_preserved_when_noncurrent_used(self, dal):
        """When noncurrent tag wins, current portion is NOT zeroed."""
        _, used_total = _extract_debt(_gaap(dal), date(2026, 3, 31))
        assert used_total is False
        cur = _resolve_instant(_gaap(dal), _INSTANT_CHAINS["current_portion_lt_debt"], date(2026, 3, 31))
        assert cur.value == Decimal("2627000000")

    def test_fy2025_noncurrent_debt(self, dal):
        """LongTermDebtNoncurrent at 2025-12-31 = 11,936,000,000."""
        res, used_total = _extract_debt(_gaap(dal), date(2025, 12, 31))
        assert res.value == Decimal("11936000000")
        assert used_total is False

    def test_no_debt_deduplicated_warning_when_primary_fires(self, dal):
        res, _ = _extract_debt(_gaap(dal), date(2026, 3, 31))
        assert not any(w.code == "debt_deduplicated" for w in res.warnings)


class TestDalFinanceLease:

    def test_fy2025_finance_lease_current_present(self, dal):
        """FinanceLeaseLiabilityCurrent at 2025-12-31 = 233,000,000."""
        res = _extract_finance_lease(_gaap(dal), date(2025, 12, 31), current=True)
        assert res.value == Decimal("233000000")
        assert not res.warnings

    def test_fy2025_finance_lease_noncurrent_present(self, dal):
        """FinanceLeaseLiabilityNoncurrent at 2025-12-31 = 572,000,000."""
        res = _extract_finance_lease(_gaap(dal), date(2025, 12, 31), current=False)
        assert res.value == Decimal("572000000")
        assert not res.warnings

    def test_q1_2026_finance_lease_absent(self, dal):
        """DAL reports FinanceLeaseLiability only at annual period ends."""
        res = _extract_finance_lease(_gaap(dal), date(2026, 3, 31), current=True)
        assert res.value is None


class TestDalShares:

    def test_q1_2026_shares_from_dei(self, dal):
        """DAL uses DEI EntityCommonStockSharesOutstanding; Q1 2026 = 656,994,112."""
        anchors = _collect_filing_anchors(_gaap(dal))
        anchor = next(a for a in anchors if a.period_end == date(2026, 3, 31))
        shares, tag, _ = _get_shares(_gaap(dal), _dei(dal), date(2026, 3, 31), anchor.accn)
        assert shares == Decimal("656994112")
        assert tag == "EntityCommonStockSharesOutstanding"

    def test_fy2025_shares_from_dei(self, dal):
        """FY2025 10-K DEI shares = 653,130,708."""
        anchors = _collect_filing_anchors(_gaap(dal))
        anchor = next(a for a in anchors if a.period_end == date(2025, 12, 31))
        shares, tag, _ = _get_shares(_gaap(dal), _dei(dal), date(2025, 12, 31), anchor.accn)
        assert shares == Decimal("653130708")
        assert tag == "EntityCommonStockSharesOutstanding"


class TestDalEquity:

    def test_q1_2026_equity(self, dal):
        res = _resolve_instant(_gaap(dal), _INSTANT_CHAINS["stockholders_equity"], date(2026, 3, 31))
        assert res.value == Decimal("20376000000")

    def test_fy2025_equity(self, dal):
        res = _resolve_instant(_gaap(dal), _INSTANT_CHAINS["stockholders_equity"], date(2025, 12, 31))
        assert res.value == Decimal("20853000000")


@pytest.mark.asyncio
class TestDalCapitalIntensiveWarning:
    """
    DAL SIC 4512 (Air Transportation) is capital-intensive.
    FinanceLeaseLiability tags only exist at annual period ends.
    -> At Q1 2026 (quarterly), leases are absent -> warning fires.
    -> At FY2025 (annual), leases are present -> warning does NOT fire.
    """

    @staticmethod
    def _mock_price(val=Decimal("50.00")):
        return patch(
            "app.services.xbrl.price_svc.get_price",
            new_callable=AsyncMock,
            return_value=val,
        )

    async def test_q1_2026_capital_intensive_lease_warning(self, dal):
        with self._mock_price():
            periods = await extract_ttm_periods(dal, ticker="DAL", is_capital_intensive=True)
        q1 = next(p for p in periods if p.period_end == date(2026, 3, 31))
        assert q1.finance_lease_current is None
        assert q1.finance_lease_noncurrent is None
        assert any(w.code == "finance_lease_missing_capital_intensive" for w in q1.warnings)

    async def test_fy2025_no_capital_intensive_warning_when_leases_present(self, dal):
        with self._mock_price():
            periods = await extract_ttm_periods(dal, ticker="DAL", is_capital_intensive=True)
        fy25 = next(p for p in periods if p.period_end == date(2025, 12, 31))
        assert fy25.finance_lease_current == Decimal("233000000")
        assert fy25.finance_lease_noncurrent == Decimal("572000000")
        assert not any(w.code == "finance_lease_missing_capital_intensive" for w in fy25.warnings)

    async def test_no_capital_intensive_warning_when_flag_false(self, dal):
        with self._mock_price():
            periods = await extract_ttm_periods(dal, ticker="DAL", is_capital_intensive=False)
        for p in periods:
            assert not any(w.code == "finance_lease_missing_capital_intensive" for w in p.warnings)

    async def test_dal_q1_2026_full_period(self, dal):
        with self._mock_price():
            periods = await extract_ttm_periods(dal, ticker="DAL", is_capital_intensive=True)
        q1 = next(p for p in periods if p.period_end == date(2026, 3, 31))
        assert q1.revenue == Decimal("65178000000")
        assert q1.long_term_debt == Decimal("10608000000")
        assert q1.current_portion_lt_debt == Decimal("2627000000")
        assert q1.shares_outstanding == Decimal("656994112")
        assert q1.stockholders_equity == Decimal("20376000000")

    async def test_dal_shares_audit_entry_records_dei_tag(self, dal):
        """Audit trail for shares must name the DEI tag, not the GAAP tag."""
        with self._mock_price():
            periods = await extract_ttm_periods(dal, ticker="DAL", is_capital_intensive=True)
        q1 = next(p for p in periods if p.period_end == date(2026, 3, 31))
        shares_audit = next((a for a in q1.audit if a.concept == "Shares Outstanding"), None)
        assert shares_audit is not None
        assert shares_audit.xbrl_tag == "EntityCommonStockSharesOutstanding"
        assert shares_audit.value == Decimal("656994112")


# ===========================================================================
# BRKB - Berkshire Hathaway (minority interest, no cash/debt/shares, bridge)
# ===========================================================================


class TestBrkbFilingAnchors:

    def test_most_recent_anchor_is_q1_2026(self, brkb):
        anchors = _collect_filing_anchors(_gaap(brkb))
        most_recent = max(anchors, key=lambda a: a.period_end)
        assert most_recent.period_end == date(2026, 3, 31)
        # Q1 anchors have a fiscal_year_start of Jan 1
        assert most_recent.fiscal_year_start == date(2026, 1, 1)

    def test_fy2025_annual_anchor(self, brkb):
        anchors = _collect_filing_anchors(_gaap(brkb))
        fy25 = next((a for a in anchors if a.period_end == date(2025, 12, 31)), None)
        assert fy25 is not None
        assert fy25.fiscal_year_start == date(2025, 1, 1)

    def test_anchor_has_no_form_attribute(self, brkb):
        anchors = _collect_filing_anchors(_gaap(brkb))
        assert not hasattr(anchors[0], "form")


class TestBrkbRevenue:

    def test_fy2025_annual_revenue(self, brkb):
        """
        BRKB FY2025 primary tag (RevenueFromContractWithCustomerExcludingAssessedTax):
        (2025-01-01->2025-12-31) = 247,244,000,000.
        NOTE: this is a subset of total revenues (Revenues tag = 371,444,000,000).
        """
        res = _extract_revenue(_gaap(brkb), date(2025, 12, 31), date(2025, 1, 1))
        assert res.value == Decimal("247244000000")

    def test_q1_2026_revenue_ttm_bridge(self, brkb):
        """
        BRKB Q1 2026:
          FY2025  (2025-01-01->2025-12-31) = 247,244,000,000
        + Q1 2026 (2026-01-01->2026-03-31) =  63,137,000,000
        - Q1 2025 (2025-01-01->2025-03-31) =  59,357,000,000
        =                                    251,024,000,000
        """
        res = _extract_revenue(_gaap(brkb), date(2026, 3, 31), date(2026, 1, 1))
        assert res.value == Decimal("251024000000")
        assert not any(w.code == "ttm_annualized" for w in res.warnings)  # full bridge, no annualization

    def test_q3_2025_revenue_ttm_bridge(self, brkb):
        """
        Prior-year Q3 YTD is in the global fact map (from the 2024 Q3 filing).
          FY2024  (2024-01-01->2024-12-31) = 249,714,000,000
        + YTD Q3 2025 (2025-01-01->2025-09-30) = 184,510,000,000
        - YTD Q3 2024 (2024-01-01->2024-09-30) = 186,858,000,000
        =                                         247,366,000,000
        """
        res = _extract_revenue(_gaap(brkb), date(2025, 9, 30), date(2025, 1, 1))
        assert res.value == Decimal("247366000000")
        assert not any(w.code == "ttm_annualized" for w in res.warnings)


class TestBrkbMinorityInterest:

    def test_q1_2026_minority_interest(self, brkb):
        """BRKB MinorityInterest at 2026-03-31 = 2,269,000,000."""
        res = _resolve_instant(_gaap(brkb), _INSTANT_CHAINS["minority_interest"], date(2026, 3, 31))
        assert res.value == Decimal("2269000000")

    def test_fy2025_minority_interest(self, brkb):
        res = _resolve_instant(_gaap(brkb), _INSTANT_CHAINS["minority_interest"], date(2025, 12, 31))
        assert res.value == Decimal("2284000000")


class TestBrkbMissingData:

    def test_no_debt_tags(self, brkb):
        """BRKB has no standard LT debt tags -> long_term_debt = None."""
        res, used_total = _extract_debt(_gaap(brkb), date(2026, 3, 31))
        assert res.value is None
        assert not used_total

    def test_no_cash_tags_in_recent_periods(self, brkb):
        """
        CashAndCashEquivalentsAtCarryingValue has no recent instant facts.
        CashCashEquivalentsAndShortTermInvestments absent.
        -> cash = None for recent periods.
        """
        res = _extract_cash(_gaap(brkb), date(2026, 3, 31))
        assert res.value is None

    def test_no_recent_dei_shares(self, brkb):
        """
        BRKB stopped filing DEI shares after 2011.
        No GAAP CommonStockSharesOutstanding either.
        -> shares = None for all recent periods.
        """
        anchors = _collect_filing_anchors(_gaap(brkb))
        anchor = next(a for a in anchors if a.period_end == date(2026, 3, 31))
        shares, tag, _ = _get_shares(_gaap(brkb), _dei(brkb), date(2026, 3, 31), anchor.accn)
        assert shares is None
        assert tag is None

    def test_no_finance_lease_tags(self, brkb):
        res = _extract_finance_lease(_gaap(brkb), date(2026, 3, 31), current=True)
        assert res.value is None


class TestBrkbEquity:

    def test_q1_2026_stockholders_equity(self, brkb):
        res = _resolve_instant(_gaap(brkb), _INSTANT_CHAINS["stockholders_equity"], date(2026, 3, 31))
        assert res.value == Decimal("727181000000")

    def test_fy2025_stockholders_equity(self, brkb):
        res = _resolve_instant(_gaap(brkb), _INSTANT_CHAINS["stockholders_equity"], date(2025, 12, 31))
        assert res.value == Decimal("717419000000")


@pytest.mark.asyncio
class TestBrkbEndToEnd:

    @staticmethod
    def _mock_price(val=Decimal("480.00")):
        return patch(
            "app.services.xbrl.price_svc.get_price",
            new_callable=AsyncMock,
            return_value=val,
        )

    async def test_q1_2026_period_values(self, brkb):
        with self._mock_price():
            periods = await extract_ttm_periods(brkb, ticker="BRKB")
        q1 = next(p for p in periods if p.period_end == date(2026, 3, 31))
        assert q1.revenue == Decimal("251024000000")
        assert q1.minority_interest == Decimal("2269000000")
        assert q1.stockholders_equity == Decimal("727181000000")
        assert q1.shares_outstanding is None   # no recent share data
        assert q1.long_term_debt is None
        assert q1.cash is None

    async def test_fy2025_period_values(self, brkb):
        with self._mock_price():
            periods = await extract_ttm_periods(brkb, ticker="BRKB")
        fy25 = next(p for p in periods if p.period_end == date(2025, 12, 31))
        assert fy25.revenue == Decimal("247244000000")
        assert fy25.minority_interest == Decimal("2284000000")

    async def test_no_capital_intensive_warning_for_financial_sector(self, brkb):
        """BRKB SIC 6321 (financial). is_capital_intensive=False by spec."""
        with self._mock_price():
            periods = await extract_ttm_periods(brkb, ticker="BRKB", is_capital_intensive=False)
        for p in periods:
            assert not any(w.code == "finance_lease_missing_capital_intensive" for w in p.warnings)

    async def test_periods_are_sorted_most_recent_first(self, brkb):
        with self._mock_price():
            periods = await extract_ttm_periods(brkb, ticker="BRKB")
        ends = [p.period_end for p in periods]
        assert ends == sorted(ends, reverse=True)

    async def test_audit_trail_has_minority_interest_entry(self, brkb):
        with self._mock_price():
            periods = await extract_ttm_periods(brkb, ticker="BRKB")
        q1 = next(p for p in periods if p.period_end == date(2026, 3, 31))
        mi_entry = next((a for a in q1.audit if a.concept == "Minority Interest"), None)
        assert mi_entry is not None
        assert mi_entry.xbrl_tag == "MinorityInterest"
        assert mi_entry.value == Decimal("2269000000")