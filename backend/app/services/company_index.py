# backend/app/services/company_index.py
"""
In-memory company index for POST /api/search.

Design
------
Source:  https://www.sec.gov/files/company_tickers.json  
Format:  { "0": { "cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc." }, ... }

The dataset (~10 k entries) is loaded once at startup into an in-memory list.
A background refresh runs at most once every 24 hours without blocking requests.

All search queries operate exclusively on in-memory data.
Zero external network calls are made during search, ensuring deterministic latency.

Search algorithm
----------------
1. Exact ticker match (case-insensitive) -> score 100
2. Name exact match (normalised)         -> score  90
3. Name prefix match (normalised)        -> score  70
4. Name substring match (normalised)     -> score  50
Results are sorted by score (desc), deduplicated by CIK, and capped at 5.

CIK normalisation
-----------------
SEC provides CIKs as integers (e.g. 320193).
All entries are immediately converted to CIK_10 (zero-padded 10-digit string)
at ingestion time (e.g. '0000320193').
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.models.company import CompanyCandidate, normalise_cik
from app.user_agent import sec_headers

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
REFRESH_INTERVAL_SECONDS = 24 * 3600  # 24 hours
MAX_RESULTS = 5
LOAD_TIMEOUT_SECONDS = 20.0


# ---------------------------------------------------------------------------
# Internal entry (not exposed via API)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _IndexEntry:
    cik_10: str
    ticker: str          # upper-cased
    name: str            # raw registrant name
    name_lower: str      # lower-cased + stripped for matching


# ---------------------------------------------------------------------------
# CompanyIndex
# ---------------------------------------------------------------------------


class CompanyIndex:
    """
    Thread-safe (single-writer) in-memory company index.
    Loaded at application startup, refreshed in the background every 24 hours.
    """

    def __init__(self) -> None:
        self._entries: list[_IndexEntry] = []
        self._last_loaded_at: float = 0.0   # time.monotonic()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def load(self) -> None:
        """
        Fetch and build the index from SEC.

        Called once at startup (lifespan). Also called by the background refresher.
        Raises on network failure, startup caller should handle this gracefully.
        """
        async with self._lock:
            await self._fetch_and_build()

    async def maybe_refresh(self) -> None:
        """
        Refresh the index if it is older than REFRESH_INTERVAL_SECONDS.
        Errors during refresh are logged but do not propagate to the caller.
        """
        if time.monotonic() - self._last_loaded_at < REFRESH_INTERVAL_SECONDS:
            return
        try:
            async with self._lock:
                # Re-check after acquiring the lock: another coroutine may have
                # already refreshed while we were waiting.
                if time.monotonic() - self._last_loaded_at < REFRESH_INTERVAL_SECONDS:
                    return
                await self._fetch_and_build()
        except Exception as exc:
            logger.warning("Background index refresh failed: %s.", exc)

    def search(self, query: str) -> list[CompanyCandidate]:
        """
        Search the in-memory index for `query`.

        Returns up to MAX_RESULTS candidates, sorted by match score (descending).
        Deduplicates by CIK in case the same company appears under multiple tickers.
        """
        query = query.strip()
        if not query:
            return []

        q_upper = query.upper()
        q_lower = query.lower()

        scored: list[tuple[int, _IndexEntry]] = []

        for entry in self._entries:
            score = self._score(entry, q_upper, q_lower)
            if score > 0:
                scored.append((score, entry))

        # Sort: highest score first, then alphabetical by name for stability
        scored.sort(key=lambda t: (-t[0], t[1].name_lower))

        # Deduplicate by CIK (keep highest-score occurrence)
        seen_ciks: set[str] = set()
        results: list[CompanyCandidate] = []
        for _, entry in scored:
            if entry.cik_10 in seen_ciks:
                continue
            seen_ciks.add(entry.cik_10)
            results.append(
                CompanyCandidate(
                    cik_10=entry.cik_10,
                    name=entry.name,
                    ticker=entry.ticker,
                )
            )
            if len(results) == MAX_RESULTS:
                break

        return results

    def __len__(self) -> int:
        return len(self._entries)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _score(entry: _IndexEntry, q_upper: str, q_lower: str) -> int:
        """
        Compute a match score for `entry` against the query.
        Returns 0 if no match, positive otherwise (higher = better match).
        """
        # 1. Exact ticker match (highest priority)
        if entry.ticker == q_upper:
            return 100

        # 2. Name exact match
        if entry.name_lower == q_lower:
            return 90

        # 3. Name prefix match
        if entry.name_lower.startswith(q_lower):
            return 70

        # 4. Name substring match
        if q_lower in entry.name_lower:
            return 50

        return 0

    async def _fetch_and_build(self) -> None:
        """
        Internal: fetch company_tickers.json and rebuild _entries.
        Must be called with self._lock held.
        """
        logger.info("Fetching company index from %s.", TICKERS_URL)
        try:
            async with httpx.AsyncClient(timeout=LOAD_TIMEOUT_SECONDS) as client:
                resp = await client.get(
                    TICKERS_URL,
                    headers=sec_headers(),
                    follow_redirects=True,
                )
                resp.raise_for_status()
                raw: dict[str, Any] = resp.json()
        except httpx.TimeoutException:
            raise RuntimeError(
                f"Timed out fetching company index from {TICKERS_URL}. "
                f"(limit: {LOAD_TIMEOUT_SECONDS}s)."
            )
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"HTTP {exc.response.status_code} fetching company index."
            ) from exc

        entries: list[_IndexEntry] = []
        for item in raw.values():
            try:
                cik_10 = normalise_cik(item["cik_str"])
                ticker = str(item.get("ticker") or "").upper()
                name = str(item.get("title") or "").strip()
                entries.append(
                    _IndexEntry(
                        cik_10=cik_10,
                        ticker=ticker,
                        name=name,
                        name_lower=name.lower(),
                    )
                )
            except (KeyError, ValueError):
                continue  # malformed entry - skip silently

        self._entries = entries
        self._last_loaded_at = time.monotonic()
        logger.info("Company index loaded: %d entries.", len(entries))