# backend/app/routers/financials.py
"""
GET /api/financials/{cik_10}

Fetches EDGAR XBRL data and computed valuation multiples for a company.

Flow
----
1. Validate CIK_10 format (422 on failure).
2. Check in-memory cache for {cik_10} (hit -> skip steps 3-4).
3. Fetch company metadata and XBRL companyfacts from EDGAR in parallel,
   using the lifespan-managed httpx client from app.state.
4. Write to cache.
5. Extract financials from companyfacts (Phase 2: app/services/xbrl.py).
6. Compute multiples (Phase 3: app/services/multiples.py).
7. Return FinancialsResponse.

Steps 5-6 degrade gracefully and independently:
  - xbrl.py absent or ImportError -> empty periods, valid company metadata returned.
  - xbrl.extract_ttm_periods raises NotImplementedError -> same as absent.
  - multiples.py absent or NotImplementedError (Phase 2) -> periods contain extracted
    data with empty MultipleSet/EVComponents rather than being silently dropped.

`_build_response` is a synchronous function dispatched via asyncio.to_thread so that
CPU-bound XBRL extraction (Phase 2+) does not block the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Path, Request

import app.cache as cache_store
from app.models.company import CompanyMeta
from app.models.financials import EVComponents, FinancialsResponse, MultipleSet, TTMPeriod
from app.services import edgar

logger = logging.getLogger(__name__)

router = APIRouter()


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
        example="0000320193",
    ),
) -> FinancialsResponse:
    """
    Main data endpoint. Returns valuation multiples for the given CIK.
    """
    # -------------------------------------------------------------------------
    # Step 2 - Cache lookup
    # -------------------------------------------------------------------------
    cached_entry = cache_store.get(cik_10)

    if cached_entry is not None:
        logger.info("Cache hit for CIK %s.", cik_10)
        payload = cached_entry.payload
        return await asyncio.to_thread(
            _build_response,
            payload.company_meta,
            payload.companyfacts,
            cached_entry.cached_at,
        )

    # -------------------------------------------------------------------------
    # Steps 3-4 - Fetch from EDGAR in parallel (cache miss)
    # -------------------------------------------------------------------------
    logger.info("Cache miss for CIK %s - fetching from EDGAR.", cik_10)

    http_client = request.app.state.edgar_client
    metadata, companyfacts = await asyncio.gather(
        edgar.fetch_metadata(cik_10, http_client),
        edgar.fetch_companyfacts(cik_10, http_client),
    )

    company_meta = CompanyMeta.from_submissions(cik_10, metadata)

    entry = cache_store.put(
        cik_10=cik_10,
        companyfacts=companyfacts,
        metadata=metadata,
        company_meta=company_meta,
    )

    return await asyncio.to_thread(
        _build_response,
        company_meta,
        companyfacts,
        entry.cached_at,
    )


# ---------------------------------------------------------------------------
# Internal: orchestrate extraction + multiples
# ---------------------------------------------------------------------------


def _build_response(
    company_meta: CompanyMeta,
    companyfacts: dict,
    cached_at: datetime,
) -> FinancialsResponse:
    """
    Synchronous. Orchestrate XBRL extraction (Phase 2) and multiples (Phase 3).

    Called via asyncio.to_thread so CPU-bound extraction does not block the
    event loop. Each phase is guarded independently:

    Extraction (Phase 2)
      - ImportError: xbrl.py not yet added -> empty periods.
      - NotImplementedError: stub not yet replaced -> empty periods.
      - Any other exception propagates as HTTP 500.

    Multiples (Phase 3)
      - ImportError or NotImplementedError on the first period -> all remaining
        periods are built with empty MultipleSet/EVComponents rather than
        being silently dropped. The flag short-circuits further attempts.
      - Any other exception propagates as HTTP 500.
    """

    # --- Phase 2: XBRL extraction ---
    try:
        from app.services import xbrl
        extracted_periods = xbrl.extract_ttm_periods(companyfacts)
    except (ImportError, NotImplementedError) as exc:
        logger.debug("xbrl not available: %s", exc)
        # Return an empty response stamped with the current computation time.
        return FinancialsResponse(
            company=company_meta,
            periods=[],
            cached_at=cached_at,
            data_as_of=datetime.now(timezone.utc),
        )

    # --- Phase 3: multiples computation ---
    multiples_ready = True
    try:
        from app.services import multiples  # noqa: PLC0415 - stubs replaced in Phase 3
    except ImportError:
        logger.debug("multiples service not yet available.")
        multiples_ready = False

    periods: list[TTMPeriod] = []
    for ef in extracted_periods:
        if ef.period_end is None:
            logger.warning("Skipping period with missing period_end.")
            continue

        multiples_set = MultipleSet()
        ev_components = EVComponents()

        if multiples_ready:
            try:
                multiples_set, ev_components = multiples.compute_all(ef)
            except NotImplementedError:
                logger.debug("multiples.compute_all not yet implemented.")
                multiples_ready = False  # stop attempting for remaining periods

        # Collect all warnings from multiples computation
        # - Period warnings aggregate extraction + multiples warnings
        multiples_warnings = [
            w
            for mv in vars(multiples_set).values()
            for w in mv.warnings
        ]

        periods.append(
            TTMPeriod(
                period_end=ef.period_end,
                filing_date=ef.filing_date,
                price=ef.price,
                multiples=multiples_set,
                ev_components=ev_components,
                extracted=ef,
                warnings=ef.warnings + multiples_warnings,
            )
        )

    return FinancialsResponse(
        company=company_meta,
        periods=periods,
        cached_at=cached_at,
        data_as_of=datetime.now(timezone.utc),
    )