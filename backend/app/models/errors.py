# backend/app/models/errors.py
"""
Structured error and warning models.

Warnings are per-period, non-fatal flags surfaced inline with the results.
Errors are request-level failures that prevent a response entirely.

A wrong answer is worse than no answer, so every ambiguity that could silently
produce an incorrect value becomes a warning or an N/A.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Warning codes
# ---------------------------------------------------------------------------


class WarningCode(str, Enum):
    """Data-quality conditions attached to individual TTM periods."""

    # --- Fallback tag fires ---
    FALLBACK_TAG = "fallback_tag"
    """
    A concept's primary XBRL tag was absent, so a later tag in its chain was used.
    
    Raised for every chain except: `DEBT_DEDUPLICATED`, `CASH_FALLBACK_INCLUDES_INVESTMENTS`,
    and `LEASE_PRE_ASC842`.
    """

    # --- EV construction ---
    EV_DEBT_MISSING = "ev_debt_missing"
    """
    All debt and finance lease tags absent. EV may be understated.

    Raised by multiples.compute_all, not by extraction, and only when market cap is computable.
    """

    DEBT_DEDUPLICATED = "debt_deduplicated"
    """LongTermDebt approximated total debt, not added separately to avoid double-counting."""

    CASH_FALLBACK_INCLUDES_INVESTMENTS = "cash_fallback_includes_investments"
    """
    Cash fallback tag (`CashCashEquivalentsAndShortTermInvestments`) includes
    short-term investments. Cash deduction in EV may be overstated.
    """

    CAPEX_SIGN_NORMALIZED = "capex_sign_normalized"
    """CapEx was reported as a negative outflow, absolute value was taken."""

    # --- Finance leases ---
    LEASE_PRE_ASC842 = "lease_pre_asc842"
    """
    Pre-ASC 842 capital lease tags used for this period. Lease accounting
    treatment differs from post-adoption periods.
    """

    FINANCE_LEASE_MISSING_CAPITAL_INTENSIVE = "finance_lease_missing_capital_intensive"
    """
    Both finance lease tags are absent for a capital-intensive SIC sector
    (manufacturing 2000-3999, transportation/utilities 4000-4999).
    Finance leases may be material and missing.
    """

    # --- Filing quality ---
    AMENDMENT_EXISTS = "amendment_exists"
    """
    An amended filing (10-Q/A or 10-K/A) exists for this period but the
    original filing was used for consistency.
    """

    # --- TTM computation ---
    TTM_ANNUALIZED = "ttm_annualized"
    """
    Prior-year YTD unavailable (e.g., recent IPO or fiscal year change).
    TTM approximated via annualization: YTD / quarters elapsed × 4.
    Result may be less precise than the standard bridge.
    """

    # --- Data integrity ---
    PERIOD_MISMATCH = "period_mismatch"
    """
    Reserved, never raised. The bridge matches flow facts on exact (start, end)
    keys (PHASE_2_SPEC.md §2.3), so a mismatch returns None rather than a warning.
    Kept in the enum for API completeness.
    """

    AMBIGUOUS_FACT = "ambiguous_fact"
    """
    Multiple XBRL contexts match the same tag and period after deduplication
    rules (original > amendment). Value returned as None.
    """

    # --- Multiple computation ---
    DENOMINATOR_NEAR_ZERO = "denominator_near_zero"
    """
    abs(denominator) < 0.01. Multiple returned as N/A to avoid
    numerically unstable or meaningless results.
    """

    NEGATIVE_BOOK_VALUE = "negative_book_value"
    """
    Stockholders' equity is negative. P/B returned as N/A because a
    negative P/B ratio is not analytically interpretable.
    """

    NEGATIVE_FCF = "negative_fcf"
    """
    Free cash flow is negative. P/FCF returned as N/A, matching how
    professional databases suppress rather than display it.
    """

    INPUT_MISSING = "input_missing"
    """
    A required fundamental input (e.g. Revenue, D&A) was not found in the
    filing. The affected multiple is N/A. Price-side absences are not
    flagged with this code - those are covered by PRICE_UNAVAILABLE.
    """

    # --- Price data ---
    PRICE_UNAVAILABLE = "price_unavailable"
    """
    Adjusted close price could not be retrieved from yfinance for this
    period. All price-dependent results (P/E, P/S, P/B, P/FCF, and the
    EV-based multiples via market cap) are N/A.
    """


# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------


class ErrorCode(str, Enum):
    """Request-level failures. These prevent a successful response."""

    EDGAR_TIMEOUT = "edgar_timeout"
    """EDGAR did not respond within the 15-second timeout."""

    EDGAR_RATE_LIMIT = "edgar_rate_limit"
    """EDGAR returned HTTP 429. Retry was attempted once, still rate-limited."""

    EDGAR_NOT_FOUND = "edgar_not_found"
    """EDGAR returned HTTP 404. The CIK does not correspond to a known filer."""

    IFRS_FILER = "ifrs_filer"
    """Company files under IFRS taxonomy. Only US-GAAP XBRL is supported."""

    UNSUPPORTED_TAXONOMY = "unsupported_taxonomy"
    """
    companyfacts contains neither us-gaap nor ifrs-full facts
    (e.g. a DEI-only early filer). Treated as unsupported.
    """

    INVALID_CIK = "invalid_cik"
    """CIK format is invalid. Expected a 10-digit zero-padded string."""

    INTERNAL_ERROR = "internal_error"
    """Unexpected server error. Check server logs for details."""


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class Warning(BaseModel):
    """One warning attached to a TTM period."""

    code: WarningCode
    message: str
    """Human-understandable explanation suitable for display in the UI."""

    concept: str | None = None
    """
    Concept name for the aggregatable codes (FALLBACK_TAG, AMENDMENT_EXISTS, TTM_ANNUALIZED,
    INPUT_MISSING). Consumed by dedup_warnings, not displayed.
    """

    model_config = {"use_enum_values": True}


class APIError(BaseModel):
    """Error response body, returned with the matching HTTP status."""

    error: ErrorCode
    message: str
    """Human-understandable message suitable for display in the UI."""

    model_config = {"use_enum_values": True}


# Pre-built WARN_* constants are deliberately absent: extraction messages need
# per-period context (tag name, fiscal year), and the multiples messages read
# fine as single inline calls.


def warn(code: WarningCode, message: str, concept: str | None = None) -> Warning:
    """Convenience constructor."""
    return Warning(code=code, message=message, concept=concept)