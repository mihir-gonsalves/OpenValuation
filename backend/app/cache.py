# backend/app/cache.py
"""
In-memory cache store for EDGAR payloads.

A module-level dict keyed by CIK_10, since tickers change and CIKs do not. There
is no expiry thread: stale entries are evicted lazily on read. Concurrent writes
to one key are not safe, but the event loop is single-threaded.
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

MAX_CACHE_ENTRIES = 8
"""
Parsed companyfacts dicts run ~5x their JSON size (3.6 MB of AAPL measures
~19 MB), so 8 entries bounds the cache near 150-250 MB. That leaves headroom
under Render's 512 MB after the Python baseline. Eviction is oldest-first, not LRU.
"""

_store: dict[str, CacheEntry] = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get(cik_10: str) -> CacheEntry | None:
    """
    Return the entry for `cik_10` if live, else None. Evicts on expiry.
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


def put(cik_10: str, companyfacts: dict[str, Any], company_meta: CompanyMeta) -> CacheEntry:
    """
    Store an entry for `cik_10`, overwriting any existing one.
    """
    # Only a new key grows the cache, so only a new key can force an eviction.
    if cik_10 not in _store and len(_store) >= MAX_CACHE_ENTRIES:
        oldest_key = min(_store, key=lambda k: _store[k].cached_at)
        logger.info(
            "Cache at capacity (%d entries) - evicting CIK %s to make room for %s.",
            MAX_CACHE_ENTRIES,
            oldest_key,
            cik_10,
        )
        del _store[oldest_key]

    payload = EDGARPayload(companyfacts=companyfacts, company_meta=company_meta)
    entry = CacheEntry(payload=payload)
    _store[cik_10] = entry
    logger.debug("Cached EDGAR payload for CIK %s.", cik_10)
    return entry


# Currently unused, kept for API completeness.
def invalidate(cik_10: str) -> None:
    """
    Evict the cache entry for `cik_10` if it exists.
    """
    removed = _store.pop(cik_10, None)
    if removed is not None:
        logger.debug("Invalidated cache entry for CIK %s.", cik_10)


def stats() -> dict[str, Any]:
    """
    Cache statistics for the health endpoint.
    """
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