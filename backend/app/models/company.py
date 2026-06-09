# backend/app/models/company.py
"""
Company-related Pydantic models.

CompanyCandidate:
- Lightweight result returned by POST /api/search.
- Identity only: CIK, name, ticker. No SIC or exchange.
Keeps search fast and deterministic (no external calls).

CompanyMeta:
- Full metadata returned as part of GET /api/financials/{cik_10}.
- Retrieved from the EDGAR submissions endpoint after the user
selects a company.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

# SIC codes 6000-6999 are financial companies (banks, insurance, REITs, etc.).
# Conventional debt/equity multiples may be less meaningful for these filers.
_FINANCIAL_SIC_LOW = 6000
_FINANCIAL_SIC_HIGH = 6999

# Capital-intensive SIC ranges where absent finance leases warrant a warning.
# Manufacturing: 2000-3999  |  Transportation / Utilities: 4000-4999
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


def normalise_cik(raw: int | str) -> str:
    """
    Convert any CIK representation to a zero-padded 10-digit string (CIK_10).

    The SEC company_tickers.json dataset provides CIKs as integers.
    All internal systems, cache keys, and API routes use CIK_10 exclusively.

    >>> normalise_cik(320193)
    '0000320193'
    >>> normalise_cik('320193')
    '0000320193'
    >>> normalise_cik('0000320193')
    '0000320193'
    """
    return str(int(raw)).zfill(10)


# ---------------------------------------------------------------------------
# Search models
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    """Request body for POST /api/search."""

    query: str = Field(..., max_length=200)
    """Company name or ticker symbol. Case-insensitive. 1-200 characters."""

    @field_validator("query")
    @classmethod
    def strip_and_require_non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Query must not be empty.")
        return v


class CompanyCandidate(BaseModel):
    """
    A single company result returned by POST /api/search.  
    Identity-only: CIK, name, ticker.

    SIC and exchange are NOT included - those are fetched only after selection
    to keep search fast and deterministic (zero external calls during typing).
    """

    cik_10: str
    """10-digit zero-padded CIK, e.g. '0000320193'."""

    name: str
    """Registrant name as filed with the SEC."""

    ticker: str
    """Primary exchange ticker symbol, upper-cased."""


class SearchResponse(BaseModel):
    """Response body for POST /api/search."""

    results: list[CompanyCandidate]
    """Up to 5 candidates, sorted by match quality (exact ticker > name prefix > substring)."""


# ---------------------------------------------------------------------------
# Full company metadata (after selection)
# ---------------------------------------------------------------------------


class CompanyMeta(BaseModel):
    """
    Full company metadata retrieved from the EDGAR submissions endpoint.  
    Returned as part of GET /api/financials/{cik_10} and GET /api/export/{cik_10}.
    """

    cik_10: str
    """10-digit zero-padded CIK."""

    name: str
    """Registrant name as filed with the SEC."""

    ticker: str | None = None
    """Primary exchange ticker. None if the company has no active ticker."""

    sic: str | None = None
    """4-digit SIC code as a string, e.g. '7372'."""

    sic_description: str | None = None
    """Human-understandable SIC description, e.g. 'Prepackaged Software'."""

    exchange: str | None = None
    """Primary listing exchange, e.g. 'Nasdaq', 'NYSE'."""

    is_financial: bool = False
    """True if SIC 6000-6999 (banks, insurance, REITs). Multiples may be less meaningful."""

    is_capital_intensive: bool = False
    """True if SIC 2000-3999 or 4000-4999. Absence of finance leases triggers a warning."""

    @classmethod
    def from_submissions(cls, cik_10: str, data: dict) -> "CompanyMeta":
        """
        Build a CompanyMeta from a raw EDGAR submissions endpoint response.

        The submissions endpoint returns:
        
            "name": "Apple Inc.",
            "tickers": ["AAPL"],
            "sic": "3571",
            "sicDescription": "Electronic Computers",
            "exchanges": ["Nasdaq"],
            ...
        """
        tickers: list[str] = data.get("tickers") or []
        exchanges: list[str] = data.get("exchanges") or []
        
        sic_raw = data.get("sic")
        sic: str | None = str(sic_raw) if sic_raw else None

        return cls(
            cik_10=cik_10,
            name=data.get("name", ""),
            ticker=tickers[0].upper() if tickers else None,
            sic=sic,
            sic_description=data.get("sicDescription"),
            exchange=exchanges[0] if exchanges else None,
            is_financial=is_financial_company(sic),
            is_capital_intensive=is_capital_intensive(sic),
        )