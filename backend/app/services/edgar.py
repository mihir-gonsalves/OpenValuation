# backend/app/services/edgar.py
"""
EDGAR HTTP client: companyfacts and submissions, with a 15s timeout, one retry
on 429, and network/HTTP errors translated into structured HTTPExceptions.

Search never touches EDGAR, which keeps the typing path free of rate-limit
exposure. EDGAR's own limit is 10 requests/second, comfortably above what a
single-tenant deployment reaches, so the retry is only a safety net.

URLs take CIK_10 prefixed with 'CIK', e.g. CIK0000320193.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from fastapi import HTTPException

from app.user_agent import sec_headers

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EDGAR_TIMEOUT_SECONDS = 15.0
EDGAR_RATE_LIMIT_BACKOFF_SECONDS = 2.0

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_10}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik_10}.json"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _resolve_client(client: httpx.AsyncClient | None) -> AsyncIterator[httpx.AsyncClient]:
    """
    Yield the provided client, or create and close a short-lived one.

    Production callers pass the lifespan client from app.state for connection
    pooling. Tests and scripts pass None and get a client scoped to the call.
    """
    if client is not None:
        yield client
    else:
        async with httpx.AsyncClient(timeout=EDGAR_TIMEOUT_SECONDS) as c:
            yield c


async def _get(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    """
    GET an EDGAR endpoint, retrying once on 429.

    Timeouts and network errors become 503, 404 stays 404, and any other status
    becomes 502. The timeout itself is enforced on the httpx client.
    """
    for attempt in (1, 2):
        try:
            resp = await client.get(url, headers=sec_headers())
        except httpx.TimeoutException:
            logger.warning("EDGAR timeout on %s (attempt %d).", url, attempt)
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "edgar_timeout",
                    "message": (
                        "EDGAR is taking longer than usual. "
                        "Please try again in a moment."
                    ),
                },
            )
        except httpx.RequestError as exc:
            logger.error("EDGAR request error on %s: %s.", url, exc)
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "edgar_timeout",
                    "message": "Could not reach EDGAR. Please try again.",
                },
            )

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "edgar_not_found",
                    "message": "Company not found on EDGAR. Verify the CIK.",
                },
            )

        if resp.status_code == 429:
            if attempt == 1:
                logger.warning("EDGAR rate limit (429) on %s - waiting %.1fs before retry.", url, EDGAR_RATE_LIMIT_BACKOFF_SECONDS)
                await asyncio.sleep(EDGAR_RATE_LIMIT_BACKOFF_SECONDS)
                continue
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "edgar_rate_limit",
                    "message": (
                        "EDGAR is currently rate-limiting requests. "
                        "Please wait a moment and try again."
                    ),
                },
            )

        logger.error("EDGAR returned unexpected HTTP %d for %s.", resp.status_code, url)
        raise HTTPException(
            status_code=502,
            detail={
                "error": "internal_error",
                "message": f"EDGAR returned an unexpected response (HTTP {resp.status_code}).",
            },
        )


def _check_taxonomy(cik_10: str, facts: dict[str, Any]) -> None:
    """
    Raise 422 if the payload carries no us-gaap facts.

    IFRS filers and filers with neither taxonomy (DEI-only early filers) are
    separated so the user gets an explanation rather than an unexplained zero periods.
    """
    if "us-gaap" in facts:
        return

    if "ifrs-full" in facts:
        logger.warning("IFRS filer detected for CIK %s.", cik_10)
        raise HTTPException(
            status_code=422,
            detail={
                "error": "ifrs_filer",
                "message": (
                    "This company files under IFRS taxonomy. "
                    "OpenValuation currently supports US-GAAP filers only."
                ),
            },
        )

    logger.warning("No us-gaap facts for CIK %s - available taxonomies: %s.", cik_10, list(facts.keys()),)
    raise HTTPException(
        status_code=422,
        detail={
            "error": "unsupported_taxonomy",
            "message": (
                "This company's XBRL filing does not include US-GAAP facts. "
                "OpenValuation currently supports US-GAAP filers only."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Public fetch functions
# ---------------------------------------------------------------------------


async def fetch_companyfacts(cik_10: str, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """
    Fetch the raw companyfacts payload for `cik_10`, typically 5-10 MB.

    Raises HTTPException on timeout, 404, 429, or an unsupported taxonomy.
    """
    url = COMPANYFACTS_URL.format(cik_10=cik_10)
    logger.info("Fetching companyfacts for CIK %s.", cik_10)

    async with _resolve_client(client) as c:
        data = await _get(c, url)

    _check_taxonomy(cik_10, data.get("facts", {}))

    logger.info("companyfacts fetched for CIK %s (%d bytes).", cik_10, len(str(data)))
    return data


async def fetch_metadata(cik_10: str, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """
    Fetch the raw submissions payload for `cik_10`: name, tickers, exchanges, SIC.
    """
    url = SUBMISSIONS_URL.format(cik_10=cik_10)
    logger.info("Fetching metadata for CIK %s.", cik_10)

    async with _resolve_client(client) as c:
        data = await _get(c, url)

    logger.info("metadata fetched for CIK %s (name=%s).", cik_10, data.get("name"))
    return data