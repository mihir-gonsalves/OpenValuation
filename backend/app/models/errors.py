# backend/app/models/errors.py
"""
Structured error and warning models for the OpenValuation API.

Warnings are per-period, non-fatal flags that surface data-quality issues
inline with the results (e.g., a fallback tag fired, EV may be understated).

Errors are request-level failures that prevent a response from being returned
(e.g., EDGAR timed out, CIK not found).

Design principle: a wrong answer is worse than no answer. Every ambiguity
that could silently produce an incorrect value is surfaced as a warning or
results in N/A, never silently swallowed.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Warning codes
# ---------------------------------------------------------------------------


class WarningCode(str, Enum):
    """
    Machine-readable codes attached to individual TTM periods.  
    Each code corresponds to a specific data-quality condition.
    """

    # --- Fallback tag fires ---
    FALLBACK_REVENUE = "fallback_revenue"
    """Primary revenue tag absent, a fallback tag was used."""

    FALLBACK_EPS_BASIC = "fallback_eps_basic"
    """Diluted EPS tag absent, basic EPS used. P/E label updated to 'P/E (basic)'."""

    # --- EV construction ---
    EV_DEBT_MISSING = "ev_debt_missing"
    """All financial debt and finance lease tags absent. EV may be understated.
    
    Set by Phase 3 (multiples.compute_all) when the union check finds every
    debt/lease field None on ExtractedFinancials. Phase 2 does not raise this."""

    DEBT_DEDUPLICATED = "debt_deduplicated"
    """LongTermDebt approximated total debt, not added separately to avoid double-counting."""

    CASH_FALLBACK_INCLUDES_INVESTMENTS = "cash_fallback_includes_investments"
    """Cash fallback tag (`CashCashEquivalentsAndShortTermInvestments`) includes
    short-term investments. Cash deduction in EV may be overstated."""

    CAPEX_SIGN_NORMALIZED = "capex_sign_normalized"
    """CapEx was reported as a negative outflow, absolute value was taken."""

    # --- Finance leases ---
    LEASE_PRE_ASC842 = "lease_pre_asc842"
    """Pre-ASC 842 capital lease tags used for this period. Lease accounting
    treatment differs from post-adoption periods."""

    FINANCE_LEASE_MISSING_CAPITAL_INTENSIVE = "finance_lease_missing_capital_intensive"
    """Both finance lease tags are absent for a capital-intensive SIC sector
    (manufacturing 2000-3999, transportation/utilities 4000-4999).
    Finance leases may be material and missing."""

    # --- Filing quality ---
    AMENDMENT_EXISTS = "amendment_exists"
    """An amended filing (10-Q/A or 10-K/A) exists for this period but the
    original filing was used for consistency."""

    # --- TTM computation ---
    TTM_ANNUALIZED = "ttm_annualized"
    """Prior-year YTD unavailable (e.g., recent IPO or fiscal year change).
    TTM approximated via annualization: Annual + (YTD / quarters) × 4.
    Result may be less precise than the standard bridge."""

    # --- Data integrity ---
    PERIOD_MISMATCH = "period_mismatch"
    """Income statement facts have misaligned period boundaries (>3 days).
    Affected facts were rejected, relevant multiples may show N/A.
    
    Unused, currently not planned for use. Retained for API completeness."""

    AMBIGUOUS_FACT = "ambiguous_fact"
    """Multiple XBRL contexts match the same tag and period after deduplication
    rules (consolidated > segment, original > restated). Value returned as None."""

    # --- Multiple computation ---
    DENOMINATOR_NEAR_ZERO = "denominator_near_zero"
    """abs(denominator) < 0.01. Multiple returned as N/A to avoid
    numerically unstable or meaningless results."""

    NEGATIVE_BOOK_VALUE = "negative_book_value"
    """Stockholders' equity is negative. P/B returned as N/A because a
    negative P/B ratio is not analytically interpretable."""

    # --- Price data ---
    PRICE_UNAVAILABLE = "price_unavailable"
    """Adjusted close price could not be retrieved from yfinance for this
    period. All price-dependent multiples (P/E, P/S, P/B, P/FCF, EV) are N/A."""


# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------


class ErrorCode(str, Enum):
    """
    Machine-readable codes for request-level failures.
    These prevent a successful response from being returned.
    """

    EDGAR_TIMEOUT = "edgar_timeout"
    """EDGAR did not respond within the 15-second timeout."""

    EDGAR_RATE_LIMIT = "edgar_rate_limit"
    """EDGAR returned HTTP 429. Retry was attempted once, still rate-limited."""

    EDGAR_NOT_FOUND = "edgar_not_found"
    """EDGAR returned HTTP 404. The CIK does not correspond to a known filer."""

    IFRS_FILER = "ifrs_filer"
    """Company files under IFRS taxonomy. Only US-GAAP XBRL is supported."""

    UNSUPPORTED_TAXONOMY = "unsupported_taxonomy"
    """companyfacts contains neither us-gaap nor ifrs-full facts
    (e.g. a DEI-only early filer). Treated as unsupported."""

    INVALID_CIK = "invalid_cik"
    """CIK format is invalid. Expected a 10-digit zero-padded string."""

    INTERNAL_ERROR = "internal_error"
    """Unexpected server error. Check server logs for details."""


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class Warning(BaseModel):
    """
    A single structured warning attached to a TTM period.  
    Warnings are surfaced in the API response and in the Excel export.
    They are never silently swallowed.
    """

    code: WarningCode
    message: str
    """Human-understandable explanation suitable for display in the UI."""

    model_config = {"use_enum_values": True}


class APIError(BaseModel):
    """
    Request-level error response body.
    Returned alongside the appropriate HTTP status code.
    """

    error: ErrorCode
    message: str
    """Human-understandable message suitable for display in the UI."""

    model_config = {"use_enum_values": True}


# ---------------------------------------------------------------------------
# warn() convenience constructor
# ---------------------------------------------------------------------------
# Pre-built WARN_* constants are intentionally absent. Phase 2 constructs
# warnings inline via warn(code, message) with dynamic per-period context
# (tag name, fiscal year, etc.). Phase 3 may reintroduce constants for
# period-static messages (e.g. NEGATIVE_BOOK_VALUE) once they are needed.
# ---------------------------------------------------------------------------


def warn(code: WarningCode, message: str) -> Warning:
    """Convenience constructor. Keeps call sites concise."""
    return Warning(code=code, message=message)