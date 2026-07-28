# backend/app/models/company.py
"""
Company identity and metadata models.

Search returns identity only (CompanyCandidate). SIC and exchange come from the
EDGAR submissions endpoint after selection (CompanyMeta), which keeps search free
of external calls.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

# Financial filers (banks, insurance, REITs). Conventional debt/equity multiples
# are less meaningful for these.
_FINANCIAL_SIC_LOW = 6000
_FINANCIAL_SIC_HIGH = 6999

# Manufacturing and transportation/utilities. Absent finance leases warrant a
# warning for these filers.
CAPITAL_INTENSIVE_SIC_RANGES: list[tuple[int, int]] = [
    (2000, 3999),
    (4000, 4999),
]


def is_financial_company(sic: int | str | None) -> bool:
    """Return True if the SIC code falls within the financial sector (6000-6999)."""
    if sic is None:
        return False
    try:
        sic_int = int(sic)
    except (ValueError, TypeError):
        return False
    return _FINANCIAL_SIC_LOW <= sic_int <= _FINANCIAL_SIC_HIGH


def is_capital_intensive(sic: int | str | None) -> bool:
    """Return True if the SIC code falls in manufacturing or transportation/utilities."""
    if sic is None:
        return False
    try:
        sic_int = int(sic)
    except (ValueError, TypeError):
        return False
    return any(lo <= sic_int <= hi for lo, hi in CAPITAL_INTENSIVE_SIC_RANGES)


def normalize_cik(raw: int | str) -> str:
    """
    Convert any CIK representation to a zero-padded 10-digit string.

    company_tickers.json supplies CIKs as integers. Cache keys and API routes use
    CIK_10 exclusively.

    >>> normalize_cik(320193)
    '0000320193'
    """
    return str(int(raw)).zfill(10)


# ---------------------------------------------------------------------------
# Search models
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    """Request body for POST /api/search."""

    query: str = Field(..., max_length=200)
    """Company name or ticker. Case-insensitive."""

    @field_validator("query")
    @classmethod
    def strip_and_require_non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Query must not be empty.")
        return v


class CompanyCandidate(BaseModel):
    """One search result. Identity only, no SIC or exchange."""

    cik_10: str
    """10-digit zero-padded CIK, e.g. '0000320193'."""

    name: str
    """Registrant name as filed with the SEC."""

    ticker: str
    """Primary exchange ticker, upper-cased."""


class SearchResponse(BaseModel):
    """Response body for POST /api/search."""

    results: list[CompanyCandidate]
    """Up to 5 candidates, best match first."""


# ---------------------------------------------------------------------------
# Full company metadata (after selection)
# ---------------------------------------------------------------------------


class CompanyMeta(BaseModel):
    """Company metadata from the EDGAR submissions endpoint."""

    cik_10: str
    name: str
    ticker: str | None = None

    exchange: str | None = None
    """Primary listing exchange, e.g. 'Nasdaq', 'NYSE'."""
    
    sic: str | None = None
    """4-digit SIC code as a string, e.g. '7372'."""

    sic_description: str | None = None
    """Human-understandable SIC description, e.g. 'Prepackaged Software'."""

    is_financial: bool = False
    """SIC 6000-6999. Multiples may be less meaningful for these filers."""

    is_capital_intensive: bool = False
    """SIC 2000-3999 or 4000-4999. Absent finance leases trigger a warning."""

    @classmethod
    def from_submissions(cls, cik_10: str, data: dict) -> "CompanyMeta":
        """Build a CompanyMeta from a raw EDGAR submissions response."""
        tickers: list[str] = data.get("tickers") or []
        exchanges: list[str] = data.get("exchanges") or []
        
        sic_raw = data.get("sic")
        sic: str | None = str(sic_raw) if sic_raw else None

        return cls(
            cik_10=cik_10,
            name=data.get("name", ""),
            ticker=tickers[0].upper() if tickers else None,
            exchange=exchanges[0] if exchanges else None,
            sic=sic,
            sic_description=data.get("sicDescription"),
            is_financial=is_financial_company(sic),
            is_capital_intensive=is_capital_intensive(sic),
        )