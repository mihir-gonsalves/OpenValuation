# backend/app/models/financials.py
"""
Financial data models for OpenValuation.

Design notes:
  - All monetary values use Python Decimal for exact arithmetic (no float rounding).
  - None means "data unavailable" - distinct from zero or a valid negative.
  - Each TTM period carries its own warnings list (non-fatal, per-period flags).
  - The top-level FinancialsResponse is the exact shape serialised to JSON for the client.

Phase 1 establishes the complete schema skeleton so that:
  - Phase 2 (XBRL extraction) fills in ExtractedFinancials.
  - Phase 3 (multiples engine) fills in MultipleSet.
  - The API response shape (FinancialsResponse) is stable from day one.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.company import CompanyMeta
from app.models.errors import Warning


# ---------------------------------------------------------------------------
# Audit entry - one row in the Input Audit Panel
# ---------------------------------------------------------------------------


class AuditEntry(BaseModel):
    """
    Records how a single financial concept was resolved for one period.  
    Displayed in the Input Audit Panel and written to the Raw Financials Excel sheet.
    """

    concept: str = Field(
        description="Human-understandable concept name, e.g. 'Revenue', 'Operating Cash Flow'."
    )
    xbrl_tag: str | None = Field(
        default=None,
        description="The XBRL tag that matched, e.g. 'RevenueFromContractWithCustomerExcludingAssessedTax'.",
    )
    is_fallback: bool = Field(
        default=False,
        description="True if a fallback tag was used instead of the primary tag.",
    )
    unit: str | None = Field(
        default=None,
        description="Unit of the matched fact, e.g. 'USD', 'USD/shares', or 'shares'.",
    )
    entity_context: str | None = Field(
        default="consolidated",
        description=(
            "'consolidated' or 'segment', derived from the XBRL context. "
            "Hardcoded 'consolidated': the SEC companyfacts endpoint (the sole "
            "data source) only exposes facts tagged against the default entity "
            "context and omits dimensional segment/member breakdowns, so no "
            "segment-level value ever reaches the extractor - the label is "
            "guaranteed by the source, not merely favoured by deduplication. "
            "A future phase parsing raw instance documents could populate this "
            "from context IDs. See PHASE_2_SPEC.md §4 (Known limitations)."
        ),
    )
    value: Decimal | None = Field(
        default=None,
        description=(
            "The value used for this period's calculation: TTM-bridged for flow "
            "concepts (revenue, operating income, EPS, ...), point-in-time for "
            "balance-sheet concepts (debt, cash, equity, ...). None when the tag "
            "was not found after exhausting all fallbacks."
        ),
    )


# ---------------------------------------------------------------------------
# Per-period extracted financials (Phase 2 output)
# ---------------------------------------------------------------------------


class ExtractedFinancials(BaseModel):
    """
    All raw XBRL values needed for multiple computation, for a single TTM period.

    Monetary fields are Decimal | None.  
    None means the tag was not found after exhausting all fallbacks - the corresponding 
    multiple will be N/A.

    Balance sheet fields are point-in-time (quarter-end date).  
    Income statement and cash flow fields are TTM-annualized values.

    Phase 1: schema defined with all fields set to None.  
    Phase 2: XBRL extraction logic populates every field.
    """

    filing_date: date | None = Field(
        default=None,
        description=(
            "Submission timestamp of the most recent quarterly filing in this window. "
            "Used to determine the price fetch date (next trading day after this date)."
        ),
    )
    period_end: date | None = Field(
        default=None,
        description="Quarter end date for this TTM window, e.g. 2024-09-28.",
    )

    # --- Price and shares ---
    price: Decimal | None = Field(
        default=None,
        description="Adjusted close on the next trading day after filing_date (from yfinance).",
    )
    shares_outstanding: Decimal | None = Field(
        default=None,
        description="CommonStockSharesOutstanding as of period_end (point-in-time, basic shares).",
    )
    eps_diluted: Decimal | None = Field(
        default=None,
        description="EarningsPerShareDiluted (TTM). Falls back to EarningsPerShareBasic.",
    )

    # --- Income statement (TTM-annualized) ---
    revenue: Decimal | None = Field(
        default=None,
        description="RevenueFromContractWithCustomerExcludingAssessedTax, Revenues, SalesRevenueNet, or RevenueFromContractWithCustomerIncludingAssessedTax.",
    )
    operating_income: Decimal | None = Field(
        default=None,
        description="OperatingIncomeLoss (proxy for EBIT).",
    )
    depreciation_and_amortization: Decimal | None = Field(
        default=None,
        description="DepreciationDepletionAndAmortization or DepreciationAndAmortization.",
    )
    net_income: Decimal | None = Field(
        default=None,
        description="NetIncomeLoss (TTM).",
    )

    # --- Cash flow (TTM-annualized) ---
    operating_cash_flow: Decimal | None = Field(
        default=None,
        description="NetCashProvidedByUsedInOperatingActivities.",
    )
    capex: Decimal | None = Field(
        default=None,
        description="PaymentsToAcquirePropertyPlantAndEquipment (and fallbacks). Always positive.",
    )

    # --- Balance sheet (point-in-time) ---
    total_assets: Decimal | None = Field(
        default=None,
        description="Assets.",
    )
    stockholders_equity: Decimal | None = Field(
        default=None,
        description="StockholdersEquity.",
    )
    long_term_debt: Decimal | None = Field(
        default=None,
        description="LongTermDebtNoncurrent (primary) or LongTermDebt (after deduplication logic).",
    )
    short_term_borrowings: Decimal | None = Field(
        default=None,
        description="ShortTermBorrowings or ShortTermDebt.",
    )
    current_portion_lt_debt: Decimal | None = Field(
        default=None,
        description="LongTermDebtCurrent.",
    )
    finance_lease_current: Decimal | None = Field(
        default=None,
        description="FinanceLeaseLiabilityCurrent or CapitalLeaseObligationsCurrent (pre-ASC 842).",
    )
    finance_lease_noncurrent: Decimal | None = Field(
        default=None,
        description="FinanceLeaseLiabilityNoncurrent or CapitalLeaseObligationsNoncurrent (pre-ASC 842).",
    )
    cash: Decimal | None = Field(
        default=None,
        description="CashAndCashEquivalentsAtCarryingValue or CashCashEquivalentsAndShortTermInvestments.",
    )
    minority_interest: Decimal | None = Field(
        default=None,
        description="MinorityInterest.",
    )
    preferred_stock: Decimal | None = Field(
        default=None,
        description="PreferredStockValue.",
    )

    # --- Audit trail ---
    audit: list[AuditEntry] = Field(
        default_factory=list,
        description="One entry per concept, recording which tag fired and whether a fallback was used.",
    )
    warnings: list[Warning] = Field(
        default_factory=list,
        description="Per-period data-quality warnings surfaced by the extraction step.",
    )


# ---------------------------------------------------------------------------
# Per-multiple result (Phase 3 output)
# ---------------------------------------------------------------------------


class MultipleValue(BaseModel):
    """
    The computed result for a single valuation multiple.  
    None means the multiple could not be computed (missing data, near-zero denominator, etc.).
    """

    value: Decimal | None = None
    label: str = Field(
        description="Display label, e.g. 'P/E', 'P/E (basic)', 'EV/EBITDA'."
    )
    warnings: list[Warning] = Field(
        default_factory=list,
        description="Warnings specific to this multiple for this period.",
    )


class MultipleSet(BaseModel):
    """
    All seven valuation multiples for a single TTM period.

    Phase 1: schema defined, all values None.  
    Phase 3: multiples engine populates each field.
    """

    ev_revenue: MultipleValue = Field(default_factory=lambda: MultipleValue(label="EV/Revenue"))
    ev_ebitda:  MultipleValue = Field(default_factory=lambda: MultipleValue(label="EV/EBITDA"))
    ev_ebit:    MultipleValue = Field(default_factory=lambda: MultipleValue(label="EV/EBIT"))
    pe:         MultipleValue = Field(default_factory=lambda: MultipleValue(label="P/E"))
    pfcf:       MultipleValue = Field(default_factory=lambda: MultipleValue(label="P/FCF"))
    ps:         MultipleValue = Field(default_factory=lambda: MultipleValue(label="P/S"))
    pb:         MultipleValue = Field(default_factory=lambda: MultipleValue(label="P/B"))


# ---------------------------------------------------------------------------
# Computed EV components (Phase 3 output, for transparency)
# ---------------------------------------------------------------------------


class EVComponents(BaseModel):
    """
    Itemised enterprise value components for one TTM period.
    Included in the response for auditability.
    """

    market_cap: Decimal | None = None
    long_term_debt: Decimal | None = None
    short_term_borrowings: Decimal | None = None
    current_portion_lt_debt: Decimal | None = None
    finance_lease_current: Decimal | None = None
    finance_lease_noncurrent: Decimal | None = None
    minority_interest: Decimal | None = None
    preferred_stock: Decimal | None = None
    cash: Decimal | None = None
    enterprise_value: Decimal | None = None


# ---------------------------------------------------------------------------
# Full TTM period (extraction + multiples combined)
# ---------------------------------------------------------------------------


class TTMPeriod(BaseModel):
    """
    A single TTM column in the results table.

    Combines raw extracted financials, computed multiples, and all warnings.  
    Column header: 'TTM {period_end}', e.g. 'TTM 2024-09-28'.
    """

    filing_date: date | None = Field(
        default=None,
        description="Submission timestamp of the anchor quarterly filing.",
    )
    period_end: date = Field(
        description="Quarter end date for this TTM window."
    )
    price: Decimal | None = Field(
        default=None,
        description="Adjusted close used for price-dependent multiples.",
    )
    multiples: MultipleSet = Field(default_factory=MultipleSet)
    ev_components: EVComponents = Field(default_factory=EVComponents)
    extracted: ExtractedFinancials = Field(default_factory=ExtractedFinancials)
    warnings: list[Warning] = Field(
        default_factory=list,
        description="All warnings for this period (union of extraction + multiples warnings).",
    )


# ---------------------------------------------------------------------------
# Top-level API response
# ---------------------------------------------------------------------------


class FinancialsResponse(BaseModel):
    """
    Full response body for GET /api/financials/{cik_10}.
    """

    company: CompanyMeta

    periods: list[TTMPeriod] = Field(
        default_factory=list,
        description="Up to 12 TTM periods, most recent first.",
    )
    cached_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when EDGAR data was last fetched for this company.",
    )
    data_as_of: datetime = Field(
        description="UTC timestamp of this response (when the computation ran)."
    )