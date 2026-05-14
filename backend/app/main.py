# backend/app/main.py
"""
OpenValuation API - FastAPI application entry point.

Startup sequence
----------------
1. Load company index from SEC company_tickers.json into memory.  
   - This is a required step. If it fails, the application starts anyway
   but search will return empty results until the index refreshes.

2. Register CORS middleware (origins from ALLOWED_ORIGINS env var).

3. Mount routers:
   POST /api/search
   GET  /api/financials/{cik_10}
   GET  /api/export/{cik_10}
"""

from __future__ import annotations

import asyncio
import logging
from dotenv import load_dotenv
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import export, financials, search
from app.services.company_index import CompanyIndex
from app.services.edgar import EDGAR_TIMEOUT_SECONDS
import app.cache as cache_store

# ---------------------------------------------------------------------------
# Load local environment variables
# ---------------------------------------------------------------------------

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lifespan: startup and shutdown
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Application lifespan handler.

    Startup:
      - Initialise and load the in-memory company index.
      - Attach the index to app.state so routers can access it.

    Shutdown:
      - No cleanup required (in-memory state is discarded).
    """
    # --- Startup ---
    logger.info("Starting OpenValuation API.")

    # Shared HTTP client for all EDGAR requests.
    # A single client maintains a connection pool, so TCP connections to
    # data.sec.gov are reused across requests rather than re-opened each time.
    edgar_client = httpx.AsyncClient(timeout=EDGAR_TIMEOUT_SECONDS)
    app.state.edgar_client = edgar_client

    company_index = CompanyIndex()
    try:
        await company_index.load()
        logger.info("Company index loaded (%d entries).", len(company_index))
    except Exception as exc:
        # Non-fatal: search will return empty results until the next refresh.
        # The financials and export endpoints are unaffected.
        logger.error(
            "Failed to load company index at startup: %s. "
            "Search will return empty results until the index refreshes.",
            exc,
        )

    app.state.company_index = company_index

    async def _refresh_loop() -> None:
        """Check every hour whether the index is stale, refresh if so."""
        while True:
            try:
                await asyncio.sleep(3600)
                await company_index.maybe_refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Background refresh failed.")

    refresh_task = asyncio.create_task(_refresh_loop())

    try:
        yield  # Application is running

    finally:
        # --- Shutdown ---
        refresh_task.cancel()
        try:
            await refresh_task
        except asyncio.CancelledError:
            pass

        await edgar_client.aclose()
        logger.info("Shutting down OpenValuation API.")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="OpenValuation API",
    summary="Compute valuation multiples from SEC EDGAR XBRL filings.",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ---------------------------------------------------------------------------
# CORS middleware
# ---------------------------------------------------------------------------

_raw_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173")
ALLOWED_ORIGINS: list[str] = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

logger.info("CORS allowed origins: %s.", ALLOWED_ORIGINS)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(search.router, prefix="/api", tags=["Search"])
app.include_router(financials.router, prefix="/api", tags=["Financials"])
app.include_router(export.router, prefix="/api", tags=["Export"])

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/health", tags=["Meta"], summary="Health and cache status")
async def health() -> dict:
    """
    Returns API status and cache statistics.
    Used by Render health checks and monitoring.
    """
    return {
        "status": "ok",
        "version": "0.1.0",
        "cache": cache_store.stats(),
        "company_index_size": len(app.state.company_index),
    }