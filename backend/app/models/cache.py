# backend/app/models/cache.py
"""
Cache models and TTL logic for the in-memory EDGAR payload cache.

Raw EDGAR payloads are cached, not computed results. The expensive step is the
fetch (a 5-10 MB companyfacts blob), while extraction and multiples are fast and
re-run on every request, so changing computation never needs an invalidation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.models.company import CompanyMeta

CACHE_TTL_SECONDS: int = 24 * 3600


class EDGARPayload(BaseModel):
    """Raw EDGAR data for one company, stored verbatim."""

    companyfacts: dict[str, Any]
    """Full companyfacts response: the complete XBRL fact history."""

    company_meta: CompanyMeta
    """Parsed company metadata derived from data.sec.gov/submissions/CIK{CIK_10}.json."""


class CacheEntry(BaseModel):
    """An EDGARPayload plus the UTC timestamp TTL is measured against."""

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