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
        description="Human-readable concept name, e.g. 'Revenue', 'Operating Cash Flow'."
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
        description="Unit of the matched fact, always 'USD' for monetary values.",
    )
    entity_context: str | None = Field(
        default=None,
        description="'consolidated' or 'segment', derived from the XBRL context.",
    )
    value: Decimal | None = Field(
        default=None,
        description="Raw extracted value before TTM annualisation (None if tag not found).",
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
    Income statement and cash flow fields are TTM-annualised values.

    Phase 1: schema defined with all fields set to None.  
    Phase 2: XBRL extraction logic populates every field.
    """

    period_end: date | None = Field(
        default=None,
        description="Quarter end date for this TTM window, e.g. 2024-09-28.",
    )
    filing_date: date | None = Field(
        default=None,
        description=(
            "Submission timestamp of the most recent quarterly filing in this window. "
            "Used to determine the price fetch date (next trading day after this date)."
        ),
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

    # --- Income statement (TTM-annualised) ---
    revenue: Decimal | None = Field(
        default=None,
        description="RevenueFromContractWithCustomerExcludingAssessedTax, Revenues, or SalesRevenueNet.",
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

    # --- Cash flow (TTM-annualised) ---
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
        description="StockholdersEquity or StockholdersEquityAttributableToParent.",
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
        description="LongTermDebtCurrent or LongTermNotesPayableCurrent.",
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
        description="NoncontrollingInterest or MinorityInterest.",
    )
    preferred_stock: Decimal | None = Field(
        default=None,
        description="PreferredStockValue or PreferredStockRedeemableValue.",
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

    pe: MultipleValue = MultipleValue(label="P/E")
    ev_ebitda: MultipleValue = MultipleValue(label="EV/EBITDA")
    ev_ebit: MultipleValue = MultipleValue(label="EV/EBIT")
    ev_revenue: MultipleValue = MultipleValue(label="EV/Revenue")
    ps: MultipleValue = MultipleValue(label="P/S")
    pb: MultipleValue = MultipleValue(label="P/B")
    pfcf: MultipleValue = MultipleValue(label="P/FCF")


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

    period_end: date = Field(
        description="Quarter end date for this TTM window."
    )
    filing_date: date | None = Field(
        default=None,
        description="Submission timestamp of the anchor quarterly filing.",
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

    `periods` is empty until Phase 2 (XBRL extraction) is implemented.  
    In Phase 2+, `periods` contains up to 12 TTM periods in reverse-chronological order.
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

    model_config = {"use_enum_values": True}