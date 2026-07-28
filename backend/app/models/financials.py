# backend/app/models/financials.py
"""
Financial data models for OpenValuation.

  - Monetary values are Decimal, never float.
  - None means "data unavailable", distinct from zero or a valid negative.
  - FinancialsResponse is the exact shape serialized to JSON for the client.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, Field, computed_field

from app.models.company import CompanyMeta
from app.models.errors import Warning


# ---------------------------------------------------------------------------
# Audit entry - one row in the Input Audit Panel
# ---------------------------------------------------------------------------


class AuditEntry(BaseModel):
    """How a single financial concept was resolved for one period."""

    concept: str = Field(
        description="Concept name, e.g. 'Revenue', 'Operating Cash Flow'."
    )
    xbrl_tag: str | None = Field(
        default=None,
        description="The XBRL tag that matched."
    )
    is_fallback: bool = Field(
        default=False,
        description="A fallback tag was used."
    )
    unit: str | None = Field(
        default=None,
        description="e.g. 'USD', 'USD/shares', 'shares'."
    )
    entity_context: str | None = Field(
        default="consolidated",
        description=(
            "Hardcoded 'consolidated': the companyfacts endpoint only exposes facts "
            "tagged against the default entity context and omits dimensional segment "
            "breakdowns, so no segment-level value can reach the extractor. Populating "
            "this properly would require parsing raw instance documents. Kept for "
            "potential expansion."
        ),
    )
    value: Decimal | None = Field(
        default=None,
        description=(
            "The value used for this period: TTM-bridged for flow concepts, "
            "point-in-time for balance-sheet concepts, None when no tag matched."
        ),
    )


# ---------------------------------------------------------------------------
# Per-period extracted financials
# ---------------------------------------------------------------------------


class ExtractedFinancials(BaseModel):
    """
    Every raw XBRL value needed for one TTM period's multiples.

    Balance sheet fields are point-in-time (quarter end). Income statement and
    cash flow fields are TTM-bridged. None means no tag matched after exhausting
    the fallback chain, and the dependent multiple will be N/A.
    """

    filing_date: date | None = Field(
        default=None,
        description="Anchor filing's submission date. Drives the price fetch date.",
    )
    period_end: date | None = Field(
        default=None,
        description="Quarter end for this TTM window."
    )
    fiscal_year_start: date | None = Field(
        default=None,
        description="First day of the fiscal year containing period_end."
    )
    fiscal_year: int | None = Field(
        default=None,
        description="Issuer's declared fiscal year for the anchor filing."
    )
    fiscal_period: str | None = Field(
        default=None,
        description="Issuer's declared 'Q1', 'Q2', 'Q3', or 'FY'."
    )

    # --- Price and shares ---
    price: Decimal | None = Field(
        default=None,
        description="Adjusted close on the next trading day after filing_date."
    )
    shares_outstanding: Decimal | None = Field(
        default=None, 
        description="CommonStockSharesOutstanding at period_end (basic)."
    )
    eps_diluted: Decimal | None = Field(
        default=None,
        description="EarningsPerShareDiluted (TTM), or Basic on fallback."
    )

    # --- Income statement (TTM) ---
    revenue: Decimal | None = Field(
        default=None,
        description="Revenue, primary tag or fallback."
    )
    operating_income: Decimal | None = Field(
        default=None,
        description="OperatingIncomeLoss (proxy for EBIT)."
    )
    depreciation_and_amortization: Decimal | None = Field(
        default=None,
        description="DepreciationDepletionAndAmortization or DepreciationAndAmortization."
    )
    net_income: Decimal | None = Field(
        default=None,
        description="NetIncomeLoss."
    )

    # --- Cash flow (TTM) ---
    operating_cash_flow: Decimal | None = Field(
        default=None,
        description="NetCashProvidedByUsedInOperatingActivities."
    )
    capex: Decimal | None = Field(
        default=None,
        description="PaymentsToAcquirePropertyPlantAndEquipment. Always positive."
    )

    # --- Balance sheet (point-in-time) ---
    total_assets: Decimal | None = Field(
        default=None,
        description="Assets."
    )
    stockholders_equity: Decimal | None = Field(
        default=None,
        description="StockholdersEquity."
    )
    long_term_debt: Decimal | None = Field(
        default=None,
        description="LongTermDebtNoncurrent, or LongTermDebt after dedup."
    )
    short_term_borrowings: Decimal | None = Field(
        default=None,
        description="ShortTermBorrowings or ShortTermDebt."
    )
    current_portion_lt_debt: Decimal | None = Field(
        default=None,
        description="LongTermDebtCurrent."
    )
    finance_lease_current: Decimal | None = Field(
        default=None,
        description="FinanceLeaseLiabilityCurrent, or the pre-ASC 842 tag."
    )
    finance_lease_noncurrent: Decimal | None = Field(
        default=None,
        description="FinanceLeaseLiabilityNoncurrent, or the pre-ASC 842 tag."
    )
    minority_interest: Decimal | None = Field(
        default=None,
        description="MinorityInterest."
    )
    preferred_stock: Decimal | None = Field(
        default=None,
        description="PreferredStockValue."
    )
    cash: Decimal | None = Field(
        default=None,
        description="CashAndCashEquivalentsAtCarryingValue or CashCashEquivalentsAndShortTermInvestments.",
    )

    # --- Audit trail ---
    audit: list[AuditEntry] = Field(
        default_factory=list,
        description="One entry per concept."
    )
    warnings: list[Warning] = Field(
        default_factory=list,
        description="Data-quality warnings raised during extraction."
    )


# ---------------------------------------------------------------------------
# Computed multiples
# ---------------------------------------------------------------------------


class MultipleValue(BaseModel):
    """One computed valuation multiple. None means N/A."""

    value: Decimal | None = None
    label: str = Field(description="Display label, e.g. 'P/E', 'P/E (basic)', 'EV/EBITDA'.")
    warnings: list[Warning] = Field(default_factory=list, description="Warnings specific to this multiple.")


class MultipleSet(BaseModel):
    """All seven valuation multiples for a single TTM period."""

    ev_revenue: MultipleValue = Field(default_factory=lambda: MultipleValue(label="EV/Revenue"))
    ev_ebitda:  MultipleValue = Field(default_factory=lambda: MultipleValue(label="EV/EBITDA"))
    ev_ebit:    MultipleValue = Field(default_factory=lambda: MultipleValue(label="EV/EBIT"))
    pe:         MultipleValue = Field(default_factory=lambda: MultipleValue(label="P/E"))
    pfcf:       MultipleValue = Field(default_factory=lambda: MultipleValue(label="P/FCF"))
    ps:         MultipleValue = Field(default_factory=lambda: MultipleValue(label="P/S"))
    pb:         MultipleValue = Field(default_factory=lambda: MultipleValue(label="P/B"))


class EVComponents(BaseModel):
    """Itemised enterprise value buildup for one period, for auditability."""

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


_FISCAL_PERIOD_TO_QUARTER = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 4}


def period_quarter_label(
    period_end: date,
    fiscal_year_start: date | None,
    fiscal_year: int | None = None,
    fiscal_period: str | None = None,
) -> str:
    """
    Display label for a TTM period, e.g. 'Q1 2026' or 'Q3 FY23'.

    Calendar-year filers get a plain calendar quarter. Off-calendar filers get an
    'FY' marker so a quarter like Apple's January-March is not misread as calendar
    Q2. Both the quarter and the year prefer the issuer's own declaration, which is
    authoritative for NRF-calendar retailers like Target (whose year ending in
    January is named by the prior calendar year) and robust against 52/53-week
    rounding. Falls back to deriving from fiscal_year_start, then to the calendar
    quarter.
    """
    quarter = _FISCAL_PERIOD_TO_QUARTER.get(fiscal_period or "")
    if quarter is None:
        if fiscal_year_start is None:
            calendar_quarter = (period_end.month - 1) // 3 + 1
            return f"Q{calendar_quarter} {period_end.year}"
        quarter = min(4, max(1, round((period_end - fiscal_year_start).days / 91.3125)))

    off_calendar = (
        fiscal_year_start is not None
        and (fiscal_year_start + timedelta(days=364)).month != 12
    )
    if not off_calendar:
        return f"Q{quarter} {period_end.year}"
    if fiscal_year is None:
        fiscal_year = (fiscal_year_start + timedelta(days=364)).year
    return f"Q{quarter} FY{fiscal_year % 100:02d}"


class TTMPeriod(BaseModel):
    """One TTM column in the results table: inputs, multiples, and warnings."""

    filing_date: date | None = Field(
        default=None,
        description="Anchor filing's submission date."
    )
    period_end: date = Field(
        description="Quarter end for this TTM window."
    )
    price: Decimal | None = Field(
        default=None,
        description="Adjusted close used for price-dependent multiples."
    )

    extracted: ExtractedFinancials = Field(default_factory=ExtractedFinancials)
    ev_components: EVComponents = Field(default_factory=EVComponents)    
    multiples: MultipleSet = Field(default_factory=MultipleSet)

    warnings: list[Warning] = Field(
        default_factory=list,
        description="Union of extraction and multiples warnings, deduplicated.",
    )

    @computed_field(
        description="Column header, e.g. 'Q1 2026' (calendar filer) or 'Q3 FY23' (off-calendar)."
    )
    @property
    def label(self) -> str:
        return period_quarter_label(
            self.period_end,
            self.extracted.fiscal_year_start,
            self.extracted.fiscal_year,
            self.extracted.fiscal_period,
        )


# ---------------------------------------------------------------------------
# Top-level API response
# ---------------------------------------------------------------------------


class FinancialsResponse(BaseModel):
    """Full response body for GET /api/financials/{cik_10}."""

    company: CompanyMeta

    periods: list[TTMPeriod] = Field(
        default_factory=list,
        description="Up to 12 TTM periods, most recent first."
    )
    cached_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when EDGAR data was last fetched."
    )
    data_as_of: datetime = Field(
        description="UTC timestamp when this response was computed."
    )