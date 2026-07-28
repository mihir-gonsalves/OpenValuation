# backend/app/routers/financials.py
"""
GET /api/financials/{cik_10}

Cache lookup, then a parallel EDGAR fetch on a miss, then extraction (xbrl.py)
and multiples (multiples.py) over the payload.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Path, Request

import app.cache as cache_store
from app.models.company import CompanyMeta
from app.models.financials import EVComponents, FinancialsResponse, MultipleSet, TTMPeriod
from app.services import edgar, multiples, xbrl
from app.services.xbrl_warnings import dedup_warnings

logger = logging.getLogger(__name__)

router = APIRouter()

# Listed explicitly: vars() includes Pydantic internal keys and model_dump()
# returns plain dicts, so both break .warnings access.
_MULTIPLE_FIELDS = (
    "ev_revenue", "ev_ebitda", "ev_ebit", "pe", "pfcf", "ps", "pb",
)


@router.get(
    "/financials/{cik_10}",
    response_model=FinancialsResponse,
    summary="Get valuation multiples for a company.",
    description=(
        "Returns TTM valuation multiples across up to 12 periods for the given CIK. "
        "Data is cached for 24 hours. The first request for a company after a cold "
        "start pays the full EDGAR fetch cost (~1-3 seconds)."
    ),
)
async def get_financials(
    request: Request,
    cik_10: str = Path(
        ...,
        description="10-digit zero-padded EDGAR CIK, e.g. '0000320193'.",
        pattern=r"^\d{10}$",
        examples=["0000320193"],
    ),
) -> FinancialsResponse:
    """
    Main data endpoint.
    """
    return await resolve_financials(request, cik_10)


async def resolve_financials(request: Request, cik_10: str) -> FinancialsResponse:
    """
    Cache-or-fetch orchestration shared by the financials and export endpoints.
    """
    cached_entry = cache_store.get(cik_10)

    if cached_entry is not None:
        logger.info("Cache hit for CIK %s.", cik_10)
        payload = cached_entry.payload
        return await _build_response(
            company_meta=payload.company_meta,
            companyfacts=payload.companyfacts,
            cached_at=cached_entry.cached_at,
        )

    logger.info("Cache miss for CIK %s - fetching from EDGAR.", cik_10)

    # Two simultaneous misses for one CIK both fetch and both write identical payloads. 
    # Locking that away is not worth the complexity at this traffic level.
    http_client = request.app.state.edgar_client
    metadata, companyfacts = await asyncio.gather(
        edgar.fetch_metadata(cik_10, http_client),
        edgar.fetch_companyfacts(cik_10, http_client),
    )

    company_meta = CompanyMeta.from_submissions(cik_10, metadata)
    entry = cache_store.put(
        cik_10=cik_10,
        companyfacts=companyfacts,
        company_meta=company_meta,
    )

    return await _build_response(
        company_meta=company_meta,
        companyfacts=companyfacts,
        cached_at=entry.cached_at,
    )


async def _build_response(
    company_meta: CompanyMeta,
    companyfacts: dict,
    cached_at: datetime,
) -> FinancialsResponse:
    """
    Run extraction, then multiples, for one companyfacts payload.

    Async because extract_ttm_periods runs concurrent price fetches. It is
    awaited directly rather than dispatched to a thread, since the work is
    I/O-bound. An extraction failure propagates as a 500, while a multiples
    failure costs only that period's multiples and never its extracted data.
    """
    extracted_periods = await xbrl.extract_ttm_periods(
        companyfacts,
        ticker=company_meta.ticker,
        is_capital_intensive=company_meta.is_capital_intensive,
    )

    periods: list[TTMPeriod] = []
    for ef in extracted_periods:
        if ef.period_end is None:
            logger.warning("Skipping period with missing period_end.")
            continue

        multiples_set = MultipleSet()
        ev_components = EVComponents()
        try:
            multiples_set, ev_components = multiples.compute_all(ef)
        except Exception:
            logger.exception(
                "multiples.compute_all error for CIK %s period %s.",
                company_meta.cik_10,
                ef.period_end,
            )

        multiples_warnings = [
            w
            for field_name in _MULTIPLE_FIELDS
            for w in getattr(multiples_set, field_name).warnings
        ]

        periods.append(
            TTMPeriod(
                period_end=ef.period_end,
                filing_date=ef.filing_date,
                price=ef.price,
                multiples=multiples_set,
                ev_components=ev_components,
                extracted=ef,
                # Dedup here is what collapses ev_debt_missing, which multiples.py
                # attaches to every EV-based multiple, into one response warning.
                warnings=dedup_warnings(ef.warnings + multiples_warnings),
            )
        )

    return FinancialsResponse(
        company=company_meta,
        periods=periods,
        cached_at=cached_at,
        data_as_of=datetime.now(timezone.utc),
    )