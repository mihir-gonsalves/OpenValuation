# backend/app/services/company_index.py
"""
In-memory company index for POST /api/search.

Source: https://www.sec.gov/files/company_tickers.json, roughly 10k entries,
loaded once at startup and rebuilt lazily when a search arrives more than 24
hours later. The triggering request waits for that rebuild and concurrent
requests wait on the same one. There is no background task.

Matching is entirely in-memory, so no query ever touches an external service.
CIKs arrive as integers and are normalized to CIK_10 at ingestion.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.models.company import CompanyCandidate, normalize_cik
from app.user_agent import sec_headers

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
REFRESH_INTERVAL_SECONDS = 24 * 3600
REFRESH_FAILURE_COOLDOWN_SECONDS = 300
MAX_RESULTS = 5
LOAD_TIMEOUT_SECONDS = 20.0


@dataclass(slots=True)
class _IndexEntry:
    cik_10: str
    ticker: str          # upper-cased
    name: str            # raw registrant name
    name_lower: str      # lower-cased + stripped for matching


class CompanyIndex:
    """
    Single-writer in-memory index, loaded at startup and refreshed lazily.
    """

    def __init__(self) -> None:
        self._entries: list[_IndexEntry] = []
        self._last_loaded_at: float = 0.0   # time.monotonic()
        self._last_attempt_at: float = 0.0  # time.monotonic(), set on every refresh try
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        """
        Build the index from SEC. Raises on failure, which the startup caller
        is expected to swallow.
        """
        async with self._lock:
            await self._fetch_and_build()

    async def maybe_refresh(self) -> None:
        """
        Rebuild the index if it is stale, swallowing any failure.

        A failed attempt starts a cooldown, so an SEC outage does not stall
        every subsequent search for the full 20s load timeout.
        """
        now = time.monotonic()
        if now - self._last_loaded_at < REFRESH_INTERVAL_SECONDS:
            return
        if now - self._last_attempt_at < REFRESH_FAILURE_COOLDOWN_SECONDS:
            return  # a recent attempt failed, so serve the stale index
        try:
            async with self._lock:
                now = time.monotonic()
                if now - self._last_loaded_at < REFRESH_INTERVAL_SECONDS:
                    return
                if now - self._last_attempt_at < REFRESH_FAILURE_COOLDOWN_SECONDS:
                    return
                self._last_attempt_at = now
                await self._fetch_and_build()
        except Exception as exc:
            logger.warning("Lazy index refresh failed: %s.", exc)

    def search(self, query: str) -> list[CompanyCandidate]:
        """
        Up to MAX_RESULTS candidates, best score first, deduplicated by CIK in
        case one company appears under several tickers.
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

        # Name is the tiebreak, which keeps the ordering stable across requests.
        scored.sort(key=lambda t: (-t[0], t[1].name_lower))

        # Deduplicate by CIK (keep highest-score occurrence)
        seen_ciks: set[str] = set()
        results: list[CompanyCandidate] = []
        for _, entry in scored:
            if entry.cik_10 in seen_ciks:
                continue
            seen_ciks.add(entry.cik_10)
            results.append(
                CompanyCandidate(cik_10=entry.cik_10, name=entry.name, ticker=entry.ticker)
            )
            if len(results) == MAX_RESULTS:
                break

        return results

    def __len__(self) -> int:
        return len(self._entries)

    @staticmethod
    def _score(entry: _IndexEntry, q_upper: str, q_lower: str) -> int:
        """
        Match score for `entry`, 0 for no match.

        The tiers are spaced far apart so a future signal can adjust a score
        without reordering them.
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
        Fetch company_tickers.json and rebuild _entries. Requires the lock.
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
                cik_10 = normalize_cik(item["cik_str"])
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
                continue  # skip malformed entries

        self._entries = entries
        self._last_loaded_at = time.monotonic()
        logger.info("Company index loaded: %d entries.", len(entries))