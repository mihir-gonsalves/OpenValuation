# backend/app/services/edgar.py
"""
EDGAR HTTP client.

Responsibilities
----------------
- Fetch the XBRL companyfacts payload for a given CIK_10.
- Fetch company metadata (name, SIC, exchange) from the submissions endpoint.
- Enforce a 15-second timeout on all outbound EDGAR calls.
- Retry once with fixed backoff on HTTP 429 (rate limit).
- Translate network and HTTP errors into structured FastAPI HTTPExceptions.

EDGAR is NOT used during search. All EDGAR interactions occur exclusively in:
  - GET /api/financials/{cik_10}
  - GET /api/export/{cik_10}
This keeps the search path free from rate-limit exposure.

Rate limit: 10 requests/second. Single-tenant Render deployment is unlikely
to hit this, but the retry is a safety net.

URL format
----------
CIK_10 must be zero-padded to 10 digits (e.g. '0000320193').
EDGAR API calls prefix it with 'CIK': CIK0000320193.
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

    Callers that hold the lifespan-managed client from app.state pass it in to
    benefit from connection pooling. Callers without one (e.g. tests, one-off
    scripts) pass None and get a clean client scoped to the call.
    """
    if client is not None:
        yield client
    else:
        async with httpx.AsyncClient(timeout=EDGAR_TIMEOUT_SECONDS) as c:
            yield c


async def _get(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    """
    Perform a single GET request to an EDGAR endpoint.

    Timeout: 15 seconds (enforced at the httpx client level, not here).

    - On 429: waits EDGAR_RATE_LIMIT_BACKOFF_SECONDS, retries once.  
    - On timeout: raises HTTP 503 with a user-readable message.  
    - On 404: raises HTTP 404.  
    - On other 4xx/5xx: raises HTTP 502.
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
                logger.warning(
                    "EDGAR rate limit (429) on %s - waiting %.1fs before retry.",
                    url,
                    EDGAR_RATE_LIMIT_BACKOFF_SECONDS,
                )
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

        logger.error(
            "EDGAR returned unexpected HTTP %d for %s.", resp.status_code, url
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": "internal_error",
                "message": f"EDGAR returned an unexpected response (HTTP {resp.status_code}).",
            },
        )


def _check_taxonomy(cik_10: str, facts: dict[str, Any]) -> None:
    """
    Raise HTTP 422 if the companyfacts payload does not contain us-gaap facts.

    Two distinct cases are handled explicitly rather than silently returning a
    payload that Phase 2 would find empty:

      - IFRS filer:  'ifrs-full' present, 'us-gaap' absent.
      - Other filer: neither taxonomy present (e.g. DEI-only early filers).

    Both cases raise 422 so the user gets a clear explanation rather than
    seeing zero periods with no indication why.
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

    logger.warning(
        "No us-gaap facts for CIK %s - available taxonomies: %s.",
        cik_10,
        list(facts.keys()),
    )
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
    Fetch the full XBRL companyfacts payload for `cik_10` from EDGAR.

    Returns the raw JSON dict. Typically 5-10 MB.

    Parameters
    ----------
    cik_10  : 10-digit zero-padded CIK.
    client  : Optional shared httpx.AsyncClient (e.g. from app.state).
              If None, a short-lived client is created and closed internally.
              Pass the lifespan client in production to benefit from connection reuse.

    Raises HTTPException on timeout, 404, 429, unsupported taxonomy, or unexpected errors.
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
    Fetch company metadata from the EDGAR submissions endpoint for `cik_10`.

    Returns the raw JSON dict containing name, tickers, exchanges, SIC, etc.

    Parameters
    ----------
    cik_10  : 10-digit zero-padded CIK.
    client  : Optional shared httpx.AsyncClient (e.g. from app.state).
              If None, a short-lived client is created and closed internally.

    Raises HTTPException on timeout, 404, 429, or unexpected errors.
    """
    url = SUBMISSIONS_URL.format(cik_10=cik_10)
    logger.info("Fetching metadata for CIK %s.", cik_10)

    async with _resolve_client(client) as c:
        data = await _get(c, url)

    logger.info("metadata fetched for CIK %s (name=%s).", cik_10, data.get("name"))
    return data