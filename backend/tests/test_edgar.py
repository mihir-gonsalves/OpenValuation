# backend/tests/test_edgar.py
"""
Tests for app/services/edgar.py

All EDGAR HTTP calls are intercepted by respx (httpx mock library).
No real network requests are made.

Coverage:
  - Successful companyfacts fetch (200)
  - Successful metadata fetch (200)
  - Timeout -> HTTP 503 with edgar_timeout error code
  - HTTP 429 -> one retry -> still 429 -> HTTP 503 with edgar_rate_limit
  - HTTP 429 -> one retry -> 200 -> success
  - HTTP 404 -> HTTP 404 with edgar_not_found
  - IFRS filer detected -> HTTP 422 with ifrs_filer
  - User-Agent header is always sent
"""

from __future__ import annotations

import os
from unittest.mock import patch

import httpx
import pytest
import respx
from fastapi import HTTPException

from app.services.edgar import (
    COMPANYFACTS_URL,
    SUBMISSIONS_URL,
    fetch_companyfacts,
    fetch_metadata,
)

CIK = "0000320193"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _companyfacts_fixture(taxonomy: str = "us-gaap") -> dict:
    """Minimal companyfacts response structure."""
    return {
        "cik": 320193,
        "entityName": "Apple Inc.",
        "facts": {
            taxonomy: {
                "Assets": {
                    "label": "Assets",
                    "description": "Sum of the carrying amounts...",
                    "units": {"USD": []},
                }
            }
        },
    }


def _metadata_fixture() -> dict:
    """Minimal submissions endpoint response."""
    return {
        "cik": "0000320193",
        "name": "Apple Inc.",
        "tickers": ["AAPL"],
        "exchanges": ["Nasdaq"],
        "sic": "3571",
        "sicDescription": "Electronic Computers",
    }


# ---------------------------------------------------------------------------
# companyfacts - happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_companyfacts_success():
    url = COMPANYFACTS_URL.format(cik_10=CIK)
    respx.get(url).mock(
        return_value=httpx.Response(200, json=_companyfacts_fixture())
    )

    data = await fetch_companyfacts(CIK)

    assert data["entityName"] == "Apple Inc."
    assert "us-gaap" in data["facts"]


# ---------------------------------------------------------------------------
# companyfacts - timeout -> 503
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_companyfacts_timeout_raises_503():
    url = COMPANYFACTS_URL.format(cik_10=CIK)
    respx.get(url).mock(side_effect=httpx.TimeoutException("timed out"))

    with pytest.raises(HTTPException) as exc_info:
        await fetch_companyfacts(CIK)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["error"] == "edgar_timeout"


# ---------------------------------------------------------------------------
# companyfacts - 404 -> 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_companyfacts_not_found_raises_404():
    url = COMPANYFACTS_URL.format(cik_10=CIK)
    respx.get(url).mock(return_value=httpx.Response(404))

    with pytest.raises(HTTPException) as exc_info:
        await fetch_companyfacts(CIK)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["error"] == "edgar_not_found"


# ---------------------------------------------------------------------------
# companyfacts - 429 retry succeeds on second attempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_companyfacts_429_retry_success():
    url = COMPANYFACTS_URL.format(cik_10=CIK)
    call_count = 0

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429)
        return httpx.Response(200, json=_companyfacts_fixture())

    with respx.mock:
        respx.get(url).mock(side_effect=_handler)
        # Patch the sleep so tests run instantly
        with patch("app.services.edgar.asyncio.sleep"):
            data = await fetch_companyfacts(CIK)

    assert call_count == 2
    assert "facts" in data


# ---------------------------------------------------------------------------
# companyfacts - 429 on both attempts -> 503
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_companyfacts_429_both_attempts_raises_503():
    url = COMPANYFACTS_URL.format(cik_10=CIK)
    respx.get(url).mock(return_value=httpx.Response(429))

    with patch("app.services.edgar.asyncio.sleep"):
        with pytest.raises(HTTPException) as exc_info:
            await fetch_companyfacts(CIK)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["error"] == "edgar_rate_limit"


# ---------------------------------------------------------------------------
# IFRS filer detection -> 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_companyfacts_ifrs_raises_422():
    url = COMPANYFACTS_URL.format(cik_10=CIK)
    respx.get(url).mock(
        return_value=httpx.Response(200, json=_companyfacts_fixture(taxonomy="ifrs-full"))
    )

    with pytest.raises(HTTPException) as exc_info:
        await fetch_companyfacts(CIK)

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["error"] == "ifrs_filer"


# ---------------------------------------------------------------------------
# metadata - happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_metadata_success():
    url = SUBMISSIONS_URL.format(cik_10=CIK)
    respx.get(url).mock(
        return_value=httpx.Response(200, json=_metadata_fixture())
    )

    data = await fetch_metadata(CIK)

    assert data["name"] == "Apple Inc."
    assert data["tickers"] == ["AAPL"]
    assert data["sic"] == "3571"


# ---------------------------------------------------------------------------
# User-Agent header is always sent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_agent_header_is_sent():
    url = COMPANYFACTS_URL.format(cik_10=CIK)
    captured_headers: dict = {}

    async def _capture(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(200, json=_companyfacts_fixture())

    with respx.mock:
        respx.get(url).mock(side_effect=_capture)
        with patch.dict(os.environ, {"EDGAR_USER_AGENT": "TestApp/1.0 test@example.com"}):
            await fetch_companyfacts(CIK)

    assert "user-agent" in captured_headers
    assert "TestApp" in captured_headers["user-agent"]
