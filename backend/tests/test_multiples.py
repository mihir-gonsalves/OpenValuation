# backend/tests/test_multiples.py
"""
Phase 3 tests - the multiples engine (app/services/multiples.py).

Two layers, mirroring the Phase 2 strategy:

  1. Pure-function tests on synthetic inputs. Every guard and edge case
     (missing data, near-zero, negative, negative book value, negative FCF,
     EBITDA bridge, EV composition, ev_debt_missing) is pinned here with no
     fixtures and no I/O - these are the regression oracles.

  2. A thin real-data path: AAPL and SNOW companyfacts run through
     extract_ttm_periods (price mocked) into compute_all, proving the engine
     consumes real ExtractedFinancials correctly and that warnings reach the
     deduplicated per-period union the router builds.

Decimal is used throughout; the engine must never return float.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from app.models.errors import WarningCode
from app.models.financials import AuditEntry, ExtractedFinancials
from app.services.multiples import (
    compute_all,
    compute_enterprise_value,
    compute_ev_revenue,
    compute_ev_ebitda,
    compute_ev_ebit,
    compute_pe,
    compute_pfcf,
    compute_ps,
    compute_pb,
)
from app.services.xbrl import extract_ttm_periods
from app.services.xbrl_warnings import dedup_warnings

D = Decimal


def _codes(warnings) -> list[str]:
    return [w.code for w in warnings]


# ===========================================================================
# P/E
# ===========================================================================


class TestPE:
    def test_basic(self):
        value, label, warnings = compute_pe(D("100"), D("5"))
        assert value == D("20")
        assert label == "P/E"
        assert warnings == []

    def test_negative_eps_yields_negative_pe(self):
        value, label, warnings = compute_pe(D("100"), D("-4"))
        assert value == D("-25")
        assert warnings == []

    def test_basic_eps_fallback_changes_label_only(self):
        value, label, warnings = compute_pe(D("100"), D("5"), eps_is_basic=True)
        assert value == D("20")
        assert label == "P/E (basic)"
        # The fallback_eps_basic warning is owned by Phase 2, not re-emitted here.
        assert warnings == []

    def test_missing_price_is_na_without_warning(self):
        value, _, warnings = compute_pe(None, D("5"))
        assert value is None
        assert warnings == []

    def test_missing_eps_is_na_without_warning(self):
        value, _, warnings = compute_pe(D("100"), None)
        assert value is None
        assert warnings == []

    def test_near_zero_eps_is_na_with_warning(self):
        value, _, warnings = compute_pe(D("100"), D("0.009"))
        assert value is None
        assert _codes(warnings) == [WarningCode.DENOMINATOR_NEAR_ZERO]

    def test_eps_exactly_at_threshold_is_valid(self):
        # 0.01 is NOT < 0.01 - the guard is strict.
        value, _, warnings = compute_pe(D("1"), D("0.01"))
        assert value == D("100")
        assert warnings == []


# ===========================================================================
# EV/EBITDA  (the only multiple with a two-input denominator bridge)
# ===========================================================================


class TestEVEBITDA:
    def test_basic(self):
        value, warnings = compute_ev_ebitda(D("1000"), D("80"), D("20"))
        assert value == D("10")  # 1000 / (80 + 20)
        assert warnings == []

    def test_missing_da_is_na_no_proxy_for_ev_ebit(self):
        # D&A absent -> N/A, never a silent fallback to EV/EBIT.
        value, warnings = compute_ev_ebitda(D("1000"), D("80"), None)
        assert value is None
        assert warnings == []

    def test_missing_operating_income_is_na(self):
        value, warnings = compute_ev_ebitda(D("1000"), None, D("20"))
        assert value is None
        assert warnings == []

    def test_negative_ebitda_yields_negative_multiple(self):
        value, warnings = compute_ev_ebitda(D("1000"), D("-150"), D("50"))
        assert value == D("-10")  # 1000 / (-150 + 50)
        assert warnings == []

    def test_near_zero_ebitda_is_na_with_warning(self):
        value, warnings = compute_ev_ebitda(D("1000"), D("10"), D("-10"))
        assert value is None
        assert _codes(warnings) == [WarningCode.DENOMINATOR_NEAR_ZERO]

    def test_missing_ev_is_na(self):
        value, warnings = compute_ev_ebitda(None, D("80"), D("20"))
        assert value is None
        assert warnings == []


# ===========================================================================
# EV/EBIT, EV/Revenue, P/S  (plain safe-divide multiples)
# ===========================================================================


class TestPlainDivisionMultiples:
    def test_ev_ebit_basic(self):
        assert compute_ev_ebit(D("1000"), D("125")) == (D("8"), [])

    def test_ev_ebit_negative_allowed(self):
        value, warnings = compute_ev_ebit(D("1000"), D("-100"))
        assert value == D("-10")
        assert warnings == []

    def test_ev_ebit_near_zero(self):
        value, warnings = compute_ev_ebit(D("1000"), D("0.005"))
        assert value is None
        assert _codes(warnings) == [WarningCode.DENOMINATOR_NEAR_ZERO]

    def test_ev_revenue_basic(self):
        assert compute_ev_revenue(D("5000"), D("1000")) == (D("5"), [])

    def test_ev_revenue_missing(self):
        assert compute_ev_revenue(None, D("1000")) == (None, [])
        assert compute_ev_revenue(D("5000"), None) == (None, [])

    def test_ps_basic(self):
        assert compute_ps(D("8000"), D("1000")) == (D("8"), [])

    def test_ps_near_zero_revenue(self):
        value, warnings = compute_ps(D("8000"), D("0"))
        assert value is None
        assert _codes(warnings) == [WarningCode.DENOMINATOR_NEAR_ZERO]


# ===========================================================================
# P/B  (negative book value is a distinct condition, checked first)
# ===========================================================================


class TestPB:
    def test_basic(self):
        assert compute_pb(D("9000"), D("1000")) == (D("9"), [])

    def test_negative_equity_is_na_with_negative_book_value(self):
        value, warnings = compute_pb(D("9000"), D("-500"))
        assert value is None
        assert _codes(warnings) == [WarningCode.NEGATIVE_BOOK_VALUE]

    def test_negative_equity_takes_priority_over_near_zero(self):
        # -0.005 is both negative AND near-zero: the negative-book-value
        # diagnosis wins (it is the more meaningful one).
        value, warnings = compute_pb(D("9000"), D("-0.005"))
        assert value is None
        assert _codes(warnings) == [WarningCode.NEGATIVE_BOOK_VALUE]

    def test_near_zero_positive_equity_is_na(self):
        value, warnings = compute_pb(D("9000"), D("0.001"))
        assert value is None
        assert _codes(warnings) == [WarningCode.DENOMINATOR_NEAR_ZERO]

    def test_missing_inputs(self):
        assert compute_pb(None, D("1000")) == (None, [])
        assert compute_pb(D("9000"), None) == (None, [])


# ===========================================================================
# P/FCF  (negative FCF -> N/A with negative_fcf, no negative multiple)
# ===========================================================================


class TestPFCF:
    def test_basic(self):
        value, warnings = compute_pfcf(D("10000"), D("1200"), D("200"))
        assert value == D("10")  # 10000 / (1200 - 200)
        assert warnings == []

    def test_negative_fcf_is_na_with_warning(self):
        value, warnings = compute_pfcf(D("10000"), D("100"), D("400"))
        assert value is None  # not shown as a negative multiple
        assert _codes(warnings) == [WarningCode.NEGATIVE_FCF]

    def test_zero_fcf_is_near_zero(self):
        value, warnings = compute_pfcf(D("10000"), D("500"), D("500"))
        assert value is None
        assert _codes(warnings) == [WarningCode.DENOMINATOR_NEAR_ZERO]

    def test_near_zero_positive_fcf(self):
        value, warnings = compute_pfcf(D("10000"), D("500.005"), D("500"))
        assert value is None
        assert _codes(warnings) == [WarningCode.DENOMINATOR_NEAR_ZERO]

    def test_missing_capex_is_na(self):
        assert compute_pfcf(D("10000"), D("1200"), None) == (None, [])

    def test_missing_ocf_is_na(self):
        assert compute_pfcf(D("10000"), None, D("200")) == (None, [])


# ===========================================================================
# Enterprise value
# ===========================================================================


def _ef(**kwargs) -> ExtractedFinancials:
    """Build an ExtractedFinancials with only the fields a test cares about."""
    return ExtractedFinancials(**kwargs)


class TestEnterpriseValue:
    def test_full_composition(self):
        f = _ef(
            price=D("10"), shares_outstanding=D("100"),   # market cap 1000
            long_term_debt=D("300"),
            short_term_borrowings=D("50"),
            current_portion_lt_debt=D("40"),
            finance_lease_current=D("10"),
            finance_lease_noncurrent=D("20"),
            minority_interest=D("15"),
            preferred_stock=D("25"),
            cash=D("200"),
        )
        ev, components, warnings = compute_enterprise_value(f)
        # 1000 + 300 + 50 + 40 + 10 + 20 + 15 + 25 - 200
        assert ev == D("1260")
        assert components.market_cap == D("1000")
        assert components.enterprise_value == D("1260")
        assert warnings == []

    def test_missing_components_treated_as_zero(self):
        f = _ef(price=D("10"), shares_outstanding=D("100"), long_term_debt=D("300"))
        ev, components, warnings = compute_enterprise_value(f)
        assert ev == D("1300")  # 1000 + 300, everything else absent -> 0
        # raw component values are preserved (None where absent) for the audit panel
        assert components.cash is None
        assert components.short_term_borrowings is None
        assert warnings == []

    def test_net_cash_company_has_negative_ev(self):
        f = _ef(price=D("10"), shares_outstanding=D("100"), cash=D("5000"))
        ev, _, warnings = compute_enterprise_value(f)
        assert ev == D("-4000")  # 1000 - 5000
        # all debt/lease tags absent AND ev computable -> understatement flag
        assert _codes(warnings) == [WarningCode.EV_DEBT_MISSING]

    def test_ev_debt_missing_when_all_debt_tags_absent(self):
        f = _ef(price=D("10"), shares_outstanding=D("100"))
        _, _, warnings = compute_enterprise_value(f)
        assert _codes(warnings) == [WarningCode.EV_DEBT_MISSING]

    def test_no_ev_debt_missing_when_any_debt_tag_present(self):
        f = _ef(price=D("10"), shares_outstanding=D("100"), finance_lease_current=D("1"))
        _, _, warnings = compute_enterprise_value(f)
        assert warnings == []

    def test_missing_market_cap_makes_ev_none_and_suppresses_debt_warning(self):
        # No price -> no market cap -> EV None. ev_debt_missing would be noise
        # (there is no EV to understate), so it must NOT fire.
        f = _ef(shares_outstanding=D("100"))
        ev, components, warnings = compute_enterprise_value(f)
        assert ev is None
        assert components.market_cap is None
        assert components.enterprise_value is None
        assert warnings == []


# ===========================================================================
# compute_all - orchestration + warning routing
# ===========================================================================


class TestComputeAll:
    def _full_period(self) -> ExtractedFinancials:
        # market cap = 50 * 1000 = 50,000
        return _ef(
            price=D("50"), shares_outstanding=D("1000"),
            eps_diluted=D("2.5"),
            revenue=D("20000"),
            operating_income=D("4000"),
            depreciation_and_amortization=D("1000"),
            operating_cash_flow=D("5000"),
            capex=D("1000"),
            stockholders_equity=D("10000"),
            long_term_debt=D("8000"),
            cash=D("3000"),
        )

    def test_all_multiples_populated(self):
        f = self._full_period()
        multiples, components = compute_all(f)

        market_cap = D("50000")
        ev = market_cap + D("8000") - D("3000")  # 55,000
        assert components.enterprise_value == ev

        assert multiples.ev_revenue.value == ev / D("20000")
        assert multiples.ev_ebitda.value == ev / D("5000")        # 4000+1000
        assert multiples.ev_ebit.value == ev / D("4000")
        assert multiples.pe.value == D("50") / D("2.5")          # 20
        assert multiples.pe.label == "P/E"
        assert multiples.pfcf.value == market_cap / D("4000")     # 5000-1000
        assert multiples.ps.value == market_cap / D("20000")
        assert multiples.pb.value == market_cap / D("10000")      # 5

    def test_basic_eps_label_read_from_audit(self):
        f = self._full_period()
        f.audit.append(AuditEntry(concept="EPS", xbrl_tag="EarningsPerShareBasic", is_fallback=True))
        multiples, _ = compute_all(f)
        assert multiples.pe.label == "P/E (basic)"

    def test_diluted_eps_audit_keeps_plain_label(self):
        f = self._full_period()
        f.audit.append(AuditEntry(concept="EPS", xbrl_tag="EarningsPerShareDiluted", is_fallback=False))
        multiples, _ = compute_all(f)
        assert multiples.pe.label == "P/E"

    def test_ev_debt_missing_attached_to_each_ev_multiple_and_dedups_to_one(self):
        # No debt tags at all -> ev_debt_missing rides each EV multiple, and the
        # router's dedup_warnings collapses the union to a single warning.
        f = _ef(
            price=D("50"), shares_outstanding=D("1000"),
            revenue=D("20000"), operating_income=D("4000"),
            depreciation_and_amortization=D("1000"),
        )
        multiples, _ = compute_all(f)
        for field in ("ev_revenue", "ev_ebitda", "ev_ebit"):
            mult = getattr(multiples, field)
            assert WarningCode.EV_DEBT_MISSING in _codes(mult.warnings)
        # P/S and P/B do not depend on EV, so they never carry it.
        assert WarningCode.EV_DEBT_MISSING not in _codes(multiples.ps.warnings)

        union = [w for field in ("ev_revenue", "ev_ebitda", "ev_ebit", "pe", "pfcf", "ps", "pb")
                 for w in getattr(multiples, field).warnings]
        deduped = dedup_warnings(union)
        assert _codes(deduped).count(WarningCode.EV_DEBT_MISSING) == 1

    def test_price_unavailable_makes_every_multiple_na(self):
        f = self._full_period()
        f.price = None  # simulate price_unavailable
        multiples, components = compute_all(f)
        assert components.market_cap is None
        for field in ("ev_revenue", "ev_ebitda", "ev_ebit", "pe", "pfcf", "ps", "pb"):
            assert getattr(multiples, field).value is None

    def test_returns_decimal_never_float(self):
        multiples, _ = compute_all(self._full_period())
        for field in ("ev_revenue", "ev_ebitda", "ev_ebit", "pe", "pfcf", "ps", "pb"):
            value = getattr(multiples, field).value
            assert value is None or isinstance(value, Decimal)


# ===========================================================================
# Real-data path:  companyfacts -> extract_ttm_periods -> compute_all
# ===========================================================================

_FIXTURES = Path(__file__).parent / "fixtures"


def _load(fn: str) -> dict:
    p = _FIXTURES / fn
    if not p.exists():
        pytest.skip(f"Fixture not found: {p}")
    return json.loads(p.read_text())


def _mock_price(v: Decimal):
    """Patch the batch price fetch so every anchor resolves to a fixed close."""
    async def _fake_get_prices(ticker, filing_dates):
        return {d: v for d in filing_dates}

    return patch("app.services.xbrl.price_svc.get_prices", _fake_get_prices)


@pytest.mark.asyncio
class TestRealDataPath:
    async def test_aapl_fy2025_multiples(self):
        from datetime import date

        aapl = _load("aapl_CIK0000320193.json")
        price = D("210.00")
        with _mock_price(price):
            periods = await extract_ttm_periods(aapl, ticker="AAPL", is_capital_intensive=True)

        fy25 = next(p for p in periods if p.period_end == date(2025, 9, 27))
        multiples, components = compute_all(fy25)

        market_cap = price * D("14773260000")
        assert components.market_cap == market_cap

        # P/E from the hand-verified TTM EPS of 7.46 (diluted -> plain label).
        assert multiples.pe.value == price / D("7.46")
        assert multiples.pe.label == "P/E"
        # P/S and P/B from hand-verified TTM revenue / point-in-time equity.
        assert multiples.ps.value == market_cap / D("416161000000")
        assert multiples.pb.value == market_cap / D("73733000000")

        # AAPL carries real debt -> no understatement flag anywhere.
        all_warnings = [w for field in ("ev_revenue", "ev_ebitda", "ev_ebit", "pe", "pfcf", "ps", "pb")
                        for w in getattr(multiples, field).warnings]
        assert WarningCode.EV_DEBT_MISSING not in _codes(all_warnings)
        # EV-based multiples are computable.
        assert multiples.ev_ebitda.value is not None
        assert multiples.ev_ebit.value is not None

    async def test_snow_negative_eps_gives_negative_pe(self):
        from datetime import date

        snow = _load("snow_CIK0001640147.json")
        price = D("180.00")
        with _mock_price(price):
            periods = await extract_ttm_periods(snow, ticker="SNOW")

        # SNOW FY2026 (Jan year-end) has a hand-verified TTM EPS of -3.95.
        fy26 = next(p for p in periods if p.period_end == date(2026, 1, 31))
        assert fy26.eps_diluted == D("-3.95")
        multiples, _ = compute_all(fy26)
        assert multiples.pe.value == price / D("-3.95")
        assert multiples.pe.value < 0

    async def test_msft_missing_da_makes_ev_ebitda_na_but_ev_ebit_ok(self):
        from datetime import date

        msft = _load("msft_CIK0000789019.json")
        with _mock_price(D("400.00")):
            periods = await extract_ttm_periods(msft, ticker="MSFT")

        fy25 = next(p for p in periods if p.period_end == date(2025, 6, 30))
        assert fy25.depreciation_and_amortization is None  # MSFT lacks both D&A tags
        multiples, _ = compute_all(fy25)
        assert multiples.ev_ebitda.value is None      # no D&A -> N/A, no proxy
        assert multiples.ev_ebit.value is not None     # operating income present
