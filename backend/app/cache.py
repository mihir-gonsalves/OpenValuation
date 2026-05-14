# backend/app/cache.py
"""
In-memory cache store for EDGAR payloads.

Design
------
- Module-level dict: simple, zero-dependency, zero-cost.
- Keyed by CIK_10 (10-digit zero-padded string). CIK is EDGAR's stable internal
  identifier. Tickers can change, CIK never does.
- TTL: 24 hours (CACHE_TTL_SECONDS from models/cache.py).
- No background expiry thread. Stale entries are evicted lazily on read.
- Not thread-safe for concurrent writes on the same key, but FastAPI's async
  event loop runs in a single thread, so this is safe for our use case.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from app.models.cache import CacheEntry, EDGARPayload
from app.models.company import CompanyMeta

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal store
# ---------------------------------------------------------------------------

_store: dict[str, CacheEntry] = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get(cik_10: str) -> CacheEntry | None:
    """
    Return the cache entry for `cik_10` if present and not expired, else None.  
    Expired entries are lazily evicted on read.
    """
    entry = _store.get(cik_10)
    if entry is None:
        return None
    if entry.is_expired():
        logger.debug("Cache miss (expired) for CIK %s (age %.0fs).", cik_10, entry.age_seconds)
        del _store[cik_10]
        return None
    logger.debug("Cache hit for CIK %s (age %.0fs).", cik_10, entry.age_seconds)
    return entry


def put(
    cik_10: str,
    companyfacts: dict[str, Any],
    metadata: dict[str, Any],
    company_meta: CompanyMeta,
) -> CacheEntry:
    """
    Store a new cache entry for `cik_10`.  
    Any existing entry (including non-expired ones) is overwritten.
    """
    payload = EDGARPayload(
        companyfacts=companyfacts,
        metadata=metadata,
        company_meta=company_meta,
    )
    entry = CacheEntry(payload=payload)
    _store[cik_10] = entry
    logger.debug("Cached EDGAR payload for CIK %s.", cik_10)
    return entry


def invalidate(cik_10: str) -> None:
    """Evict the cache entry for `cik_10` if it exists."""
    removed = _store.pop(cik_10, None)
    if removed is not None:
        logger.debug("Invalidated cache entry for CIK %s.", cik_10)


def stats() -> dict[str, Any]:
    """Return cache statistics for observability (e.g. health endpoint)."""
    now = datetime.now(timezone.utc)
    total = len(_store)
    live = sum(
        1 for e in _store.values()
        if not e.is_expired(now)
    )
    return {
        "total_entries": total,
        "live_entries": live,
        "expired_entries": total - live,
    }