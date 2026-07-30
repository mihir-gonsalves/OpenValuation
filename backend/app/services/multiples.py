# backend/app/services/multiples.py
"""
Multiples engine.

Every function here is pure: extracted values in, a (value, warnings) pair out,
no I/O and no mutation. Values are Decimal, never float.

The rules enforced across all seven multiples:
  - A None input gives a None output, so missing data is N/A and never a wrong number.
  - abs(denominator) < 0.01 gives N/A with denominator_near_zero.
  - Negative results are valid and shown, except for P/FCF and P/B, whose
    negative cases are distinct conditions checked before the near-zero guard.

The formulas live in README.md and DESIGN.md. The reasoning behind the guard
ordering and the warning routing lives in PHASE_3_SPEC.md.
"""

from __future__ import annotations

from decimal import Decimal

from app.models.errors import Warning, WarningCode, warn
from app.models.financials import ExtractedFinancials, EVComponents, MultipleSet, MultipleValue

DENOMINATOR_NEAR_ZERO_THRESHOLD = Decimal("0.01")

_ZERO = Decimal(0)


def _or_zero(x: Decimal | None) -> Decimal:
    """
    Treat a missing component as a zero balance (EV summation convention).
    """
    return x if x is not None else _ZERO


def _missing_inputs(*named: tuple[str, Decimal | None]) -> list[Warning]:
    """
    One input_missing warning per absent fundamental, by display name.
    """
    return [
        warn(
            WarningCode.INPUT_MISSING,
            f"{name} is unavailable for this period.",
            concept=name,
        )
        for name, value in named
        if value is None
    ]


def _safe_divide(
    numerator: Decimal | None,
    denominator: Decimal | None,
    denom_name: str,
) -> tuple[Decimal | None, list[Warning]]:
    """
    Divide with the two guards every multiple shares.

    A missing denominator (a fundamental) returns None with input_missing,
    since extraction raises nothing for an unmatched tag chain. 
    
    A missing numerator (price, market cap, or EV) stays silent - it has
    already been surfaced upstream by price_unavailable. 
    
    (Residual edge: shares outstanding missing while price is present is
    silent: rare, accepted.)
    
    A near-zero denominator returns None with denominator_near_zero.

    Callers with extra rules apply them before delegating here.
    """
    if denominator is None:
        return None, _missing_inputs((denom_name, None))
    if numerator is None:
        return None, []
    if abs(denominator) < DENOMINATOR_NEAR_ZERO_THRESHOLD:
        return None, [warn(
            WarningCode.DENOMINATOR_NEAR_ZERO,
            f"{denom_name} is near zero (|value| < {DENOMINATOR_NEAR_ZERO_THRESHOLD}), multiple reported as N/A.",
        )]
    return numerator / denominator, []


def _eps_is_basic(f: ExtractedFinancials) -> bool:
    """
    Whether the basic-EPS fallback fired, read off the 'EPS' audit entry.

    Extraction keeps that concept name stable and records the basic-vs-diluted
    distinction in is_fallback, so all this module owns is the label change.
    """
    for entry in f.audit:
        if entry.concept == "EPS":
            return entry.is_fallback
    return False


def compute_all(financials: ExtractedFinancials) -> tuple[MultipleSet, EVComponents]:
    """
    All seven multiples for one TTM period, plus the EV buildup for the audit
    panel and the Excel export.

    ev_debt_missing is attached to each of the three EV-based multiples, which
    keeps it next to what it explains. The router deduplicates per period, so
    the one underlying condition still surfaces once.
    """
    ev, ev_components, ev_warnings = compute_enterprise_value(financials)
    market_cap = ev_components.market_cap

    ev_revenue_value, ev_revenue_warnings = compute_ev_revenue(ev, financials.revenue)
    ev_ebitda_value, ev_ebitda_warnings = compute_ev_ebitda(ev, financials.operating_income, financials.depreciation_and_amortization)
    ev_ebit_value, ev_ebit_warnings = compute_ev_ebit(ev, financials.operating_income)
    pe_value, pe_label, pe_warnings = compute_pe(financials.price, financials.eps_diluted, eps_is_basic=_eps_is_basic(financials))
    pfcf_value, pfcf_warnings = compute_pfcf(market_cap, financials.operating_cash_flow, financials.capex)
    ps_value, ps_warnings = compute_ps(market_cap, financials.revenue)
    pb_value, pb_warnings = compute_pb(market_cap, financials.stockholders_equity)

    multiples = MultipleSet(
        ev_revenue=MultipleValue(value=ev_revenue_value, label="EV/Revenue", warnings=ev_revenue_warnings + ev_warnings),
        ev_ebitda=MultipleValue(value=ev_ebitda_value, label="EV/EBITDA", warnings=ev_ebitda_warnings + ev_warnings),
        ev_ebit=MultipleValue(value=ev_ebit_value, label="EV/EBIT", warnings=ev_ebit_warnings + ev_warnings),
        pe=MultipleValue(value=pe_value, label=pe_label, warnings=pe_warnings),
        pfcf=MultipleValue(value=pfcf_value, label="P/FCF", warnings=pfcf_warnings),
        ps=MultipleValue(value=ps_value, label="P/S", warnings=ps_warnings),
        pb=MultipleValue(value=pb_value, label="P/B", warnings=pb_warnings),
    )
    return multiples, ev_components


def compute_enterprise_value(f: ExtractedFinancials) -> tuple[Decimal | None, EVComponents, list[Warning]]:
    """
    Enterprise value and its itemised components.

    EV  = Market Cap
       + Long-Term Debt (non-current, or total after dedup)
       + Short-Term Borrowings
       + Current Portion of LT Debt
       + Finance Lease Liabilities (Current + Non-Current)
       + Minority Interest
       + Preferred Stock
       - Cash & Cash Equivalents

    Market Cap is price × basic shares. If either is missing, EV is None and the
    EV multiples are N/A, already explained upstream by price_unavailable or a missing tag.

    Missing balance-sheet components count as zero, since absence usually means a
    zero balance. The exception is a computable EV where every debt and lease tag
    is absent, which raises ev_debt_missing because EV may be understated. The
    returned components stay raw (None where absent) for the audit trail, so only
    the total applies the zero convention.
    """
    market_cap = (
        f.price * f.shares_outstanding
        if f.price is not None and f.shares_outstanding is not None
        else None
    )

    debt_lease_components = (
        f.long_term_debt,
        f.short_term_borrowings,
        f.current_portion_lt_debt,
        f.finance_lease_current,
        f.finance_lease_noncurrent,
    )

    warnings: list[Warning] = []
    enterprise_value: Decimal | None = None

    if market_cap is not None:
        enterprise_value = (
            market_cap
            + sum((_or_zero(c) for c in debt_lease_components), _ZERO)
            + _or_zero(f.minority_interest)
            + _or_zero(f.preferred_stock)
            - _or_zero(f.cash)
        )
        if all(c is None for c in debt_lease_components):
            warnings.append(warn(
                WarningCode.EV_DEBT_MISSING,
                "All financial debt and finance lease tags are absent, "
                "enterprise value may be understated.",
            ))

    components = EVComponents(
        market_cap=market_cap,
        long_term_debt=f.long_term_debt,
        short_term_borrowings=f.short_term_borrowings,
        current_portion_lt_debt=f.current_portion_lt_debt,
        finance_lease_current=f.finance_lease_current,
        finance_lease_noncurrent=f.finance_lease_noncurrent,
        minority_interest=f.minority_interest,
        preferred_stock=f.preferred_stock,
        cash=f.cash,
        enterprise_value=enterprise_value,
    )
    return enterprise_value, components, warnings


def compute_ev_revenue(
    ev: Decimal | None,
    revenue: Decimal | None,
) -> tuple[Decimal | None, list[Warning]]:
    """
    EV/Revenue = EV / Revenue.
    """
    return _safe_divide(ev, revenue, "Revenue")


def compute_ev_ebitda(
    ev: Decimal | None,
    operating_income: Decimal | None,
    da: Decimal | None,
) -> tuple[Decimal | None, list[Warning]]:
    """
    EV/EBITDA = EV / (Operating Income + D&A).

    Both operands are required. Absent D&A makes this N/A with input_missing
    rather than allow EV/EBIDTA to act as a proxy for EV/EBIT. Negative EBITDA
    gives a valid negative multiple.
    """
    missing = _missing_inputs(("Operating Income", operating_income), ("D&A", da))
    if missing:
        return None, missing
    return _safe_divide(ev, operating_income + da, "EBITDA")


def compute_ev_ebit(
    ev: Decimal | None,
    operating_income: Decimal | None,
) -> tuple[Decimal | None, list[Warning]]:
    """
    EV/EBIT = EV / Operating Income. Negative EBIT is valid (shown negative).
    """
    return _safe_divide(ev, operating_income, "Operating Income")


def compute_pe(
    price: Decimal | None,
    eps: Decimal | None,
    *,
    eps_is_basic: bool = False,
) -> tuple[Decimal | None, str, list[Warning]]:
    """
    P/E = Price / EPS (TTM).

    `eps` already holds basic EPS when the diluted tag was absent, so
    `eps_is_basic` changes nothing but the label. Negative EPS gives a valid negative P/E.
    """
    label = "P/E (basic)" if eps_is_basic else "P/E"
    value, warnings = _safe_divide(price, eps, "EPS")
    return value, label, warnings


def compute_pfcf(
    market_cap: Decimal | None,
    operating_cash_flow: Decimal | None,
    capex: Decimal | None,
) -> tuple[Decimal | None, list[Warning]]:
    """
    P/FCF = Market Cap / (Operating Cash Flow - CapEx).

    Negative FCF is N/A with negative_fcf, matching how professional databases
    suppress it rather than show a negative multiple. That check runs before the
    near-zero guard, since the more specific condition wins. CapEx arrives as a
    non-negative outflow.
    """
    if market_cap is None:
        return None, []
    missing = _missing_inputs(("Operating Cash Flow", operating_cash_flow), ("CapEx", capex))
    if missing:
        return None, missing
    fcf = operating_cash_flow - capex
    if fcf < _ZERO:
        return None, [warn(
            WarningCode.NEGATIVE_FCF,
            "Free cash flow is negative, which is not analytically interpretable",
        )]
    return _safe_divide(market_cap, fcf, "Free Cash Flow")


def compute_ps(
    market_cap: Decimal | None,
    revenue: Decimal | None,
) -> tuple[Decimal | None, list[Warning]]:
    """
    P/S = Market Cap / Revenue.
    """
    return _safe_divide(market_cap, revenue, "Revenue")


def compute_pb(
    market_cap: Decimal | None,
    stockholders_equity: Decimal | None,
) -> tuple[Decimal | None, list[Warning]]:
    """
    P/B = Market Cap / Stockholders' Equity.

    Negative equity is N/A with negative_book_value, unlike a negative P/E which
    is interpretable. Checked before the near-zero guard.
    """
    if market_cap is None:
        return None, []
    if stockholders_equity is None:
        return None, _missing_inputs(("Stockholders' Equity", None))
    if stockholders_equity < _ZERO:
        return None, [warn(
            WarningCode.NEGATIVE_BOOK_VALUE,
            "Stockholders' equity is negative, which is not analytically interpretable",
        )]
    return _safe_divide(market_cap, stockholders_equity, "Stockholders' Equity")