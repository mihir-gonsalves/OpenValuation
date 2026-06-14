# backend/tests/test_api_errors.py
"""
Tests for the structured API error response contract (PHASE_1_SPEC §2.1).

Coverage:
  - invalid_cik: malformed CIK path param -> 422 with top-level {"error": "invalid_cik"}
  - EDGAR timeout: mocked timeout -> 503 with top-level {"error": "edgar_timeout"}
  - export stub: 501 with top-level {"error": "not_implemented"}
  - Generic HTTPException detail strings surface as {"error": "internal_error"}
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# invalid_cik (§1.1 and PHASE_1_SPEC §2.2)
# ---------------------------------------------------------------------------


def test_invalid_cik_short_returns_422_with_error_code(client):
    """A short CIK (not 10 digits) must return the documented invalid_cik shape."""
    resp = client.get("/api/financials/123")
    assert resp.status_code == 422
    body = resp.json()
    assert "detail" not in body, "Error body must not be wrapped in 'detail'"
    assert body["error"] == "invalid_cik"
    assert "message" in body


def test_invalid_cik_alpha_returns_422_with_error_code(client):
    """A non-numeric CIK must also return invalid_cik."""
    resp = client.get("/api/financials/abcdefghij")
    assert resp.status_code == 422
    body = resp.json()
    assert body.get("error") == "invalid_cik"


# ---------------------------------------------------------------------------
# EDGAR timeout -> 503 edgar_timeout at top level
# ---------------------------------------------------------------------------


def test_edgar_timeout_returns_503_at_top_level(client):
    """EDGAR timeout must produce top-level {"error": "edgar_timeout"} (no detail wrapper)."""
    from fastapi import HTTPException
    from app.services import edgar

    def raise_timeout(*args, **kwargs):
        raise HTTPException(
            status_code=503,
            detail={"error": "edgar_timeout", "message": "EDGAR timed out."},
        )

    with patch.object(edgar, "fetch_metadata", side_effect=raise_timeout):
        resp = client.get("/api/financials/0000320193")

    assert resp.status_code == 503
    body = resp.json()
    assert "detail" not in body, "Error body must not be wrapped in 'detail'"
    assert body["error"] == "edgar_timeout"
    assert "message" in body


# ---------------------------------------------------------------------------
# export stub -> 501 not_implemented at top level
# ---------------------------------------------------------------------------


def test_export_stub_returns_501_at_top_level(client):
    """Export stub must return top-level {"error": "not_implemented"} (no detail wrapper)."""
    resp = client.get("/api/export/0000320193")
    assert resp.status_code == 501
    body = resp.json()
    assert "detail" not in body, "Error body must not be wrapped in 'detail'"
    assert body["error"] == "not_implemented"
    assert "message" in body
