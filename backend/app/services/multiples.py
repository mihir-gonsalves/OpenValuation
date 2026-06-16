# backend/app/services/multiples.py
"""
Multiples engine - Phase 3 implementation.

All functions in this module are pure: they take extracted financial values
as inputs and return a (value, warnings) pair (the EV builder also returns its
itemised components). No I/O, no side effects.

Design rules (enforced here):
  - None inputs propagate to None output (missing data -> N/A, never wrong value).
  - Near-zero denominators: abs(denominator) < 0.01 -> N/A with
    denominator_near_zero warning.
  - Negative values are shown as negative (valid economic result) except P/FCF.
  - Negative FCF -> N/A with negative_fcf warning (checked before the near-zero
    guard, because it is a distinct condition).
  - Negative book value -> N/A with negative_book_value warning (checked before
    the near-zero guard, because it is a distinct condition).
  - All functions return Decimal | None (never float).

Semantics that are NOT restated here (authoritative elsewhere):
  - The EV / EBITDA / FCF formulas and tag-level rules: README.md, DESIGN.md.
  - WarningCode definitions and HTTP/contract shapes: PHASE_1_SPEC.md.
  - Why each guard is the way it is, and the warning-routing contract with the
    router: PHASE_3_SPEC.md.
"""

from __future__ import annotations

from decimal import Decimal

from app.models.errors import Warning, WarningCode, warn
from app.models.financials import ExtractedFinancials, EVComponents, MultipleSet, MultipleValue

# ---------------------------------------------------------------------------
# Near-zero denominator threshold (absolute value)
# ---------------------------------------------------------------------------

DENOMINATOR_NEAR_ZERO_THRESHOLD = Decimal("0.01")

_ZERO = Decimal(0)


# ---------------------------------------------------------------------------
# Small internal helpers
# ---------------------------------------------------------------------------


def _or_zero(x: Decimal | None) -> Decimal:
    """Treat a missing component as a zero balance (EV summation convention)."""
    return x if x is not None else _ZERO


def _safe_divide(
    numerator: Decimal | None,
    denominator: Decimal | None,
    denom_name: str,
) -> tuple[Decimal | None, list[Warning]]:
    """
    Divide with the two universal guards shared by every multiple:

      - Missing data (either operand None) -> (None, []). No warning: the cause
        (price_unavailable, a missing tag, ...) was already surfaced upstream,
        re-flagging it here would only duplicate noise.
      - Near-zero denominator (abs < 0.01) -> (None, [denominator_near_zero]).

    A valid (possibly negative) result passes through unchanged. Callers that
    need extra rules (negative book value, negative FCF, an EBITDA bridge)
    apply them *before* delegating to this function.
    """
    if numerator is None or denominator is None:
        return None, []
    if abs(denominator) < DENOMINATOR_NEAR_ZERO_THRESHOLD:
        return None, [warn(
            WarningCode.DENOMINATOR_NEAR_ZERO,
            f"{denom_name} is near zero (|value| < {DENOMINATOR_NEAR_ZERO_THRESHOLD}), "
            "multiple reported as N/A.",
        )]
    return numerator / denominator, []


def _eps_is_basic(f: ExtractedFinancials) -> bool:
    """
    Whether the basic-EPS fallback fired, read off the 'EPS' audit entry.

    Phase 2 keeps the EPS audit concept name stable as "EPS" and records the
    basic-vs-diluted distinction in is_fallback (PHASE_2_SPEC §3.6). Phase 3
    owns only the label change: P/E -> 'P/E (basic)'. The fallback_eps_basic
    warning itself was already attached in Phase 2 and is not re-emitted here.
    """
    for entry in f.audit:
        if entry.concept == "EPS":
            return entry.is_fallback
    return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_all(financials: ExtractedFinancials) -> tuple[MultipleSet, EVComponents]:
    """
    Compute all seven valuation multiples for a single TTM period.

    Returns (MultipleSet, EVComponents). EVComponents is returned for the audit
    panel and the Excel export.

    Warning routing: the ev_debt_missing warning produced by the EV builder is
    attached to each of the three EV-based multiples (EV/EBITDA, EV/EBIT,
    EV/Revenue). The financials router deduplicates per period, so the single
    underlying condition surfaces once. See PHASE_1_SPEC §6.2 and PHASE_3_SPEC §4.
    """
    ev, ev_components, ev_warnings = compute_enterprise_value(financials)
    market_cap = ev_components.market_cap

    pe_value, pe_label, pe_warnings = compute_pe(
        financials.price,
        financials.eps_diluted,
        eps_is_basic=_eps_is_basic(financials),
    )
    ev_ebitda_value, ev_ebitda_warnings = compute_ev_ebitda(
        ev, financials.operating_income, financials.depreciation_and_amortization,
    )
    ev_ebit_value, ev_ebit_warnings = compute_ev_ebit(ev, financials.operating_income)
    ev_revenue_value, ev_revenue_warnings = compute_ev_revenue(ev, financials.revenue)
    ps_value, ps_warnings = compute_ps(market_cap, financials.revenue)
    pb_value, pb_warnings = compute_pb(market_cap, financials.stockholders_equity)
    pfcf_value, pfcf_warnings = compute_pfcf(
        market_cap, financials.operating_cash_flow, financials.capex,
    )

    multiples = MultipleSet(
        pe=MultipleValue(value=pe_value, label=pe_label, warnings=pe_warnings),
        ev_ebitda=MultipleValue(
            value=ev_ebitda_value, label="EV/EBITDA",
            warnings=ev_ebitda_warnings + ev_warnings,
        ),
        ev_ebit=MultipleValue(
            value=ev_ebit_value, label="EV/EBIT",
            warnings=ev_ebit_warnings + ev_warnings,
        ),
        ev_revenue=MultipleValue(
            value=ev_revenue_value, label="EV/Revenue",
            warnings=ev_revenue_warnings + ev_warnings,
        ),
        ps=MultipleValue(value=ps_value, label="P/S", warnings=ps_warnings),
        pb=MultipleValue(value=pb_value, label="P/B", warnings=pb_warnings),
        pfcf=MultipleValue(value=pfcf_value, label="P/FCF", warnings=pfcf_warnings),
    )
    return multiples, ev_components


# ---------------------------------------------------------------------------
# EV computation (Phase 3)
# ---------------------------------------------------------------------------


def compute_enterprise_value(f: ExtractedFinancials) -> tuple[Decimal | None, EVComponents, list[Warning]]:
    """
    Compute enterprise value and return itemised components.

    EV  = Market Cap
       + Long-Term Debt (non-current, or total after dedup)
       + Short-Term Borrowings
       + Current Portion of LT Debt
       + Finance Lease Liabilities (Current + Non-Current)
       + Minority Interest
       + Preferred Stock
       - Cash & Cash Equivalents

    Market Cap = price × basic shares outstanding. If either is missing, EV is
    None (the EV-based multiples become N/A, price_unavailable / a missing tag
    already explains why upstream).

    Missing balance-sheet components are treated as zero with no warning (absence
    usually reflects a true zero balance). Exception: when EV *is* computable
    (market cap present) and every financial-debt and finance-lease tag is
    absent, ev_debt_missing is raised - EV may be understated. The returned
    EVComponents preserve raw extracted values (None where absent) for the audit
    trail, the EV total applies the zero convention.
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


# ---------------------------------------------------------------------------
# Individual multiple calculators (Phase 3)
# ---------------------------------------------------------------------------


def compute_pe(
    price: Decimal | None,
    eps: Decimal | None,
    *,
    eps_is_basic: bool = False,
) -> tuple[Decimal | None, str, list[Warning]]:
    """
    P/E = Price / EPS (TTM).

    `eps` is ExtractedFinancials.eps_diluted, which already holds basic EPS when
    the diluted tag was absent (Phase 2 fallback). `eps_is_basic` only changes
    the label to 'P/E (basic)' - the fallback_eps_basic warning was already
    attached in Phase 2. Negative EPS yields a (valid) negative P/E.
    """
    label = "P/E (basic)" if eps_is_basic else "P/E"
    value, warnings = _safe_divide(price, eps, "EPS")
    return value, label, warnings


def compute_ev_ebitda(
    ev: Decimal | None,
    operating_income: Decimal | None,
    da: Decimal | None,
) -> tuple[Decimal | None, list[Warning]]:
    """
    EV/EBITDA = EV / (Operating Income + D&A).

    Both operating income and D&A are required. When D&A is absent, EBITDA is
    not reconstructed - EV/EBITDA is N/A rather than a silent proxy for EV/EBIT
    (README -> EBITDA Construction Limitation). No dedicated warning code exists
    for missing D&A. Negative EBITDA produces a (valid) negative multiple.
    """
    if operating_income is None or da is None:
        return None, []
    return _safe_divide(ev, operating_income + da, "EBITDA")


def compute_ev_ebit(
    ev: Decimal | None,
    operating_income: Decimal | None,
) -> tuple[Decimal | None, list[Warning]]:
    """EV/EBIT = EV / Operating Income. Negative EBIT is valid (shown negative)."""
    return _safe_divide(ev, operating_income, "Operating Income")


def compute_ev_revenue(
    ev: Decimal | None,
    revenue: Decimal | None,
) -> tuple[Decimal | None, list[Warning]]:
    """EV/Revenue = EV / Revenue."""
    return _safe_divide(ev, revenue, "Revenue")


def compute_ps(
    market_cap: Decimal | None,
    revenue: Decimal | None,
) -> tuple[Decimal | None, list[Warning]]:
    """P/S = Market Cap / Revenue."""
    return _safe_divide(market_cap, revenue, "Revenue")


def compute_pb(
    market_cap: Decimal | None,
    stockholders_equity: Decimal | None,
) -> tuple[Decimal | None, list[Warning]]:
    """
    P/B = Market Cap / Stockholders' Equity.

    Negative equity -> N/A with negative_book_value (a negative P/B is not
    analytically interpretable, unlike a negative P/E). This distinct condition
    is checked before the near-zero guard, per DESIGN.md -> Multiples.
    """
    if market_cap is None or stockholders_equity is None:
        return None, []
    if stockholders_equity < _ZERO:
        return None, [warn(
            WarningCode.NEGATIVE_BOOK_VALUE,
            "Stockholders' equity is negative, which is not analytically "
            "interpretable",
        )]
    return _safe_divide(market_cap, stockholders_equity, "Stockholders' Equity")


def compute_pfcf(
    market_cap: Decimal | None,
    operating_cash_flow: Decimal | None,
    capex: Decimal | None,
) -> tuple[Decimal | None, list[Warning]]:
    """
    P/FCF = Market Cap / (Operating Cash Flow - CapEx).

    Negative FCF -> N/A with negative_fcf. Unlike P/E, a negative P/FCF is not
    reported as a negative multiple (professional databases suppress it). Like
    P/B's negative-equity guard, it is a distinct condition that fires on present
    data and so carries a warning - see PHASE_3_SPEC §2.7. The negative check
    runs before the near-zero guard, mirroring P/B's "distinct condition first"
    rule. CapEx is already a non-negative outflow (Phase 2 normalises its sign).
    """
    if market_cap is None or operating_cash_flow is None or capex is None:
        return None, []
    fcf = operating_cash_flow - capex
    if fcf < _ZERO:
        return None, [warn(
            WarningCode.NEGATIVE_FCF,
            "Free cash flow is negative, which is not analytically interpretable",
        )]
    return _safe_divide(market_cap, fcf, "Free Cash Flow")