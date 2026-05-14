# backend/app/routers/search.py
"""
POST /api/search

Resolves a company name or ticker to up to five CIK candidates.

Design constraints:
  - Zero external network calls during search.
  - All queries operate on the in-memory company index loaded at startup.
  - Deterministic, sub-millisecond latency.
  - SIC and exchange are NOT returned here, retrieved only after company selection
    via GET /api/financials/{cik_10}.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from app.models.company import SearchRequest, SearchResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/search",
    response_model=SearchResponse,
    summary="Search for a company by name or ticker.",
    description=(
        "Returns up to 5 company candidates matching the query. "
        "Results are sorted by match quality: exact ticker > name prefix > substring. "
        "No external network calls are made during search."
    ),
)
async def search(body: SearchRequest, request: Request) -> SearchResponse:
    """
    Search the in-memory company index for companies matching `body.query`.

    The index is loaded from SEC company_tickers.json at startup and refreshed
    in the background every 24 hours.
    """
    company_index = request.app.state.company_index
    results = company_index.search(body.query)

    logger.info(
        "search query=%r -> %d result(s).",
        body.query,
        len(results),
    )

    return SearchResponse(results=results)