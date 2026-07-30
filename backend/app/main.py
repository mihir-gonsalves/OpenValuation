# backend/app/main.py
"""
OpenValuation's FastAPI application entry point.

Owns the lifespan (shared EDGAR client, company index), CORS, the structured
error handlers, the three /api routers, and /api/health.
"""

from __future__ import annotations

import logging
from dotenv import load_dotenv
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

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
    Create the shared EDGAR client and load the company index, then close the client on shutdown.

    The index load is non-fatal, and the index refreshes lazily on the search
    path (CompanyIndex.maybe_refresh) rather than from a background task here.
    """
    logger.info("Starting OpenValuation API.")

    # One client keeps a connection pool, so TCP connections to data.sec.gov are reused across requests.
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

    try:
        yield  # Application is running

    finally:
        # --- Shutdown ---
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
# Exception handlers - the {"error", "message"} body contract
# ---------------------------------------------------------------------------


@app.exception_handler(StarletteHTTPException)
async def structured_http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """
    Unwrap {"error", "message"} detail dicts to the top level.
    """
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": "internal_error",
            "message": str(exc.detail)
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """
    Map path-level CIK validation failures to invalid_cik.
    """
    if any(tuple(e.get("loc", ()))[:2] == ("path", "cik_10") for e in exc.errors()):
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_cik",
                "message": "CIK must be a 10-digit zero-padded string, e.g. '0000320193'.",
            },
        )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


# ---------------------------------------------------------------------------
# CORS middleware
# ---------------------------------------------------------------------------

_raw_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173") # will update later
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


@app.get("/api/health", tags=["Meta"], summary="Health and cache status.")
async def health() -> dict:
    """
    API status and cache statistics. Also the frontend's cold-start probe.
    """
    return {
        "status": "ok",
        "version": "0.1.0",
        "cache": cache_store.stats(),
        "company_index_size": len(app.state.company_index),
    }