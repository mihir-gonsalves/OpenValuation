# backend/app/routers/financials.py
"""
GET /api/financials/{cik_10}

Fetches EDGAR XBRL data and computed valuation multiples for a company.

Flow:
1. Validate CIK_10 format (422 on failure).
2. Check in-memory cache for {cik_10} (hit -> skip steps 3-4).
3. Fetch company metadata and XBRL companyfacts from EDGAR in parallel,
   using the lifespan-managed httpx client from app.state.
4. Write to cache.
5. Extract financials from companyfacts (Phase 2: app/services/xbrl.py).
6. Compute multiples (Phase 3: app/services/multiples.py).
7. Return FinancialsResponse.

Phase 2: _build_response is async, it awaits xbrl.extract_ttm_periods()
         (which internally runs price fetches concurrently).
         Phase 2 extraction data is never discarded.

Phase 3: multiples.compute_all() populates each TTMPeriod's MultipleSet /
         EVComponents. Any per-period exception is caught and logged, that
         period's multiples are empty while extraction data is preserved.
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

# MultipleSet field names for collecting per-multiple warnings.
# Listed explicitly: vars() includes Pydantic internal keys, model_dump()
# returns plain dicts that lose the .warnings attribute.
_MULTIPLE_FIELDS = (
    "pe", "ev_ebitda", "ev_ebit", "ev_revenue", "ps", "pb", "pfcf",
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
    Main data endpoint. Returns valuation multiples for the given CIK.
    """
    # -------------------------------------------------------------------------
    # Step 2 - Cache lookup
    # -------------------------------------------------------------------------
    cached_entry = cache_store.get(cik_10)

    if cached_entry is not None:
        logger.info("Cache hit for CIK %s.", cik_10)
        payload = cached_entry.payload
        return await _build_response(
            company_meta=payload.company_meta,
            companyfacts=payload.companyfacts,
            cached_at=cached_entry.cached_at,
        )

    # -------------------------------------------------------------------------
    # Steps 3-4 - Fetch from EDGAR in parallel (cache miss)
    # -------------------------------------------------------------------------
    logger.info("Cache miss for CIK %s - fetching from EDGAR.", cik_10)

    # Two simultaneous misses for the same CIK both fetch and both write.
    # The second write wins, both payloads are identical. The duplicate fetch
    # is accepted: locking here adds complexity for negligible benefit at
    # this project's traffic level.
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


# ---------------------------------------------------------------------------
# Internal: orchestrate extraction + multiples
# ---------------------------------------------------------------------------


async def _build_response(
    company_meta: CompanyMeta,
    companyfacts: dict,
    cached_at: datetime,
) -> FinancialsResponse:
    """
    Async. Orchestrate XBRL extraction (Phase 2) and multiples (Phase 3).

    _build_response is async because xbrl.extract_ttm_periods is async
    (it runs concurrent price fetches via asyncio.gather internally).
    It is awaited directly - not dispatched to a thread - because the
    extraction work is I/O-bound (price fetches), not CPU-bound.

    Extraction (Phase 2):
      - Any exception propagates as HTTP 500 internal_error (PHASE_1_SPEC §2.2).
        The legitimate-empty case (no anchors) returns periods=[] with 200
        from inside extract_ttm_periods itself.

    Multiples (Phase 3):
      - Any exception per period -> logged, that period's multiples are
        empty, extraction data is preserved.
    """
    # --- Phase 2: XBRL extraction + concurrent price fetches ---
    extracted_periods = await xbrl.extract_ttm_periods(
        companyfacts,
        ticker=company_meta.ticker,
        is_capital_intensive=company_meta.is_capital_intensive,
    )

    # --- Phase 3: multiples computation ---
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

        # Collect per-multiple warnings from Phase 3.
        # Iterate the seven known MultipleSet fields explicitly - using vars() or
        # model_dump() on a Pydantic v2 model includes internal state keys and
        # returns plain dicts respectively, both of which break .warnings access.
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
                # warnings = deduped union of all extraction-layer warnings (XBRL
                # extraction + price fetch, both attached inside extract_ttm_periods)
                # and all per-multiple warnings from Phase 3. Dedup is applied here
                # so that ev_debt_missing (which Phase 3 attaches to every EV-based
                # multiple) collapses to a single warning in the response.
                warnings=dedup_warnings(ef.warnings + multiples_warnings),
            )
        )

    return FinancialsResponse(
        company=company_meta,
        periods=periods,
        cached_at=cached_at,
        data_as_of=datetime.now(timezone.utc),
    )
