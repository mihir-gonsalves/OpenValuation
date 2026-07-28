# backend/app/routers/search.py
"""
POST /api/search

Resolves a company name or ticker to up to five CIK candidates.

Matching runs entirely against the in-memory index. The only network activity on
this path is the index refresh, at most once per 24 hours.
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
    Search the in-memory index, refreshing it first if it is over 24 hours old.
    """
    company_index = request.app.state.company_index
    await company_index.maybe_refresh()
    results = company_index.search(body.query)

    logger.info("search query=%r -> %d result(s).", body.query, len(results))

    return SearchResponse(results=results)