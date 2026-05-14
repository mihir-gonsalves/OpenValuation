# backend/app/models/cache.py
"""
Cache models and TTL logic for the in-memory EDGAR payload cache.

Design:
  - Cache stores raw EDGAR payloads (companyfacts + metadata), not computed results.
  - The expensive operation is the EDGAR fetch (5-10 MB companyfacts blob).
  - Computation (XBRL extraction, TTM bridge, multiples) is fast and runs on every
    request from the cached payload. This ensures Phase 2 and Phase 3 logic is always
    applied without requiring cache invalidation.
  - TTL: 24 hours. Keyed by CIK_10 (stable, unambiguous EDGAR identifier).
  - Cache is cleared on Render cold starts. This behaviour is disclosed in the UI.

Cache key design:
  - Always CIK_10 (10-digit zero-padded string), never ticker.
  - Tickers can change on renames or exchange moves, CIK never does.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.models.company import CompanyMeta

# ---------------------------------------------------------------------------
# TTL constant (single source of truth)
# ---------------------------------------------------------------------------

CACHE_TTL_SECONDS: int = 24 * 3600  # 24 hours


# ---------------------------------------------------------------------------
# Payload stored inside each cache entry
# ---------------------------------------------------------------------------


class EDGARPayload(BaseModel):
    """
    Raw EDGAR data fetched for a single company.  
    Stored verbatim - no transformation applied at cache-write time.
    """

    companyfacts: dict[str, Any]
    """
    Full response from data.sec.gov/api/xbrl/companyfacts/CIK{CIK_10}.json.  
    Typically 5-10 MB. Contains the complete XBRL fact history for the company.
    """

    metadata: dict[str, Any]
    """
    Full response from data.sec.gov/submissions/CIK{CIK_10}.json.  
    Used to build CompanyMeta (name, ticker, SIC, exchange).
    """

    company_meta: CompanyMeta
    """
    Parsed company metadata derived from `metadata` at fetch time.  
    Stored here so we can cheaply return it on cache hits without re-parsing.
    """


# ---------------------------------------------------------------------------
# Cache entry (payload + timestamp)
# ---------------------------------------------------------------------------


class CacheEntry(BaseModel):
    """
    A single entry in the in-memory cache.  
    Wraps an EDGARPayload with a UTC timestamp for TTL enforcement.
    """

    payload: EDGARPayload
    cached_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    
    def is_expired(self, now: datetime | None = None) -> bool:
        """Return True if this entry is older than CACHE_TTL_SECONDS."""
        now = now or datetime.now(timezone.utc)
        return (now - self.cached_at).total_seconds() >= CACHE_TTL_SECONDS

    @property
    def age_seconds(self) -> float:
        """Seconds elapsed since the entry was cached."""
        return (datetime.now(timezone.utc) - self.cached_at).total_seconds()