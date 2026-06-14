# backend/tests/test_financials_route.py
"""
Router-layer tests for GET /api/financials/{cik_10}.

Coverage:
  - Cache miss: EDGAR is called, response is well-formed.
  - Cache hit: EDGAR is NOT called again; same response structure.
  - Warning merge contract: extraction warnings + multiples warnings both appear.
  - NotImplementedError in compute_all: periods returned with empty multiples, no 500.
  - GET /health: shape (status, cache, company_index_size).
  - POST /api/search: returns results list.
  - edgar._check_taxonomy: ifrs_filer -> 422.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.models.errors import Warning, WarningCode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures"


def _load_companyfacts(name: str) -> dict:
    path = next(_FIXTURES.glob(f"{name}_CIK*.json"), None)
    if path is None:
        raise FileNotFoundError(f"No fixture for {name!r} in {_FIXTURES}")
    return json.loads(path.read_text())


# EDGAR submissions endpoint returns tickers/exchanges as flat lists of strings.
_AAPL_SUBMISSIONS = {
    "name": "Apple Inc.",
    "tickers": ["AAPL"],
    "sic": "3571",
    "sicDescription": "Electronic Computers",
    "exchanges": ["Nasdaq"],
}


@pytest.fixture
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Cache miss: EDGAR called, response well-formed
# ---------------------------------------------------------------------------


def test_cache_miss_calls_edgar_and_returns_200(client):
    import app.cache as cache_store
    cache_store.invalidate("0000320193")

    aapl_facts = _load_companyfacts("aapl")
    with (
        patch("app.services.edgar.fetch_metadata", new=AsyncMock(return_value=_AAPL_SUBMISSIONS)),
        patch("app.services.edgar.fetch_companyfacts", new=AsyncMock(return_value=aapl_facts)),
    ):
        resp = client.get("/api/financials/0000320193")

    assert resp.status_code == 200
    body = resp.json()
    assert body["company"]["cik_10"] == "0000320193"
    assert "periods" in body
    assert "cached_at" in body
    assert "data_as_of" in body


# ---------------------------------------------------------------------------
# Cache hit: EDGAR is NOT called again
# ---------------------------------------------------------------------------


def test_cache_hit_skips_edgar(client):
    import app.cache as cache_store
    cache_store.invalidate("0000320193")

    aapl_facts = _load_companyfacts("aapl")
    meta_mock = AsyncMock(return_value=_AAPL_SUBMISSIONS)
    facts_mock = AsyncMock(return_value=aapl_facts)

    with (
        patch("app.services.edgar.fetch_metadata", new=meta_mock),
        patch("app.services.edgar.fetch_companyfacts", new=facts_mock),
    ):
        client.get("/api/financials/0000320193")  # cache miss
        client.get("/api/financials/0000320193")  # cache hit

    assert meta_mock.call_count == 1, (
        f"fetch_metadata called {meta_mock.call_count} times; expected 1 (cache should hit on second request)"
    )


# ---------------------------------------------------------------------------
# Warning merge: extraction + multiples warnings both in TTMPeriod.warnings
# ---------------------------------------------------------------------------


def test_warning_merge_includes_extraction_and_multiples_warnings(client):
    import app.cache as cache_store
    cache_store.invalidate("0000320193")

    from app.models.financials import ExtractedFinancials, MultipleSet, EVComponents, MultipleValue

    extraction_warning = Warning(
        code=WarningCode.TTM_ANNUALIZED,
        message="Annualized.",
        concept="Revenue",
    )
    multiples_warning = Warning(
        code=WarningCode.DENOMINATOR_NEAR_ZERO,
        message="Near zero.",
    )

    fake_ef = ExtractedFinancials(
        period_end=date(2024, 9, 28),
        filing_date=date(2024, 11, 1),
        warnings=[extraction_warning],
    )

    fake_multiple_set = MultipleSet(
        pe=MultipleValue(label="P/E", warnings=[multiples_warning]),
    )

    async def fake_extract(*args, **kwargs):
        return [fake_ef]

    def fake_compute_all(_ef):
        return fake_multiple_set, EVComponents()

    with (
        patch("app.services.edgar.fetch_metadata", new=AsyncMock(return_value=_AAPL_SUBMISSIONS)),
        patch("app.services.edgar.fetch_companyfacts", new=AsyncMock(return_value={})),
        patch("app.services.xbrl.extract_ttm_periods", side_effect=fake_extract),
        patch("app.services.multiples.compute_all", side_effect=fake_compute_all),
    ):
        resp = client.get("/api/financials/0000320193")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["periods"]) == 1
    codes = {w["code"] for w in body["periods"][0]["warnings"]}
    assert "ttm_annualized" in codes
    assert "denominator_near_zero" in codes


# ---------------------------------------------------------------------------
# NotImplementedError in compute_all: periods returned, no 500
# ---------------------------------------------------------------------------


def test_not_implemented_multiples_returns_empty_multiples_not_500(client):
    import app.cache as cache_store
    cache_store.invalidate("0000320193")

    from app.models.financials import ExtractedFinancials

    fake_ef = ExtractedFinancials(
        period_end=date(2024, 9, 28),
        filing_date=date(2024, 11, 1),
    )

    async def fake_extract(*args, **kwargs):
        return [fake_ef]

    def raise_not_implemented(_ef):
        raise NotImplementedError

    with (
        patch("app.services.edgar.fetch_metadata", new=AsyncMock(return_value=_AAPL_SUBMISSIONS)),
        patch("app.services.edgar.fetch_companyfacts", new=AsyncMock(return_value={})),
        patch("app.services.xbrl.extract_ttm_periods", side_effect=fake_extract),
        patch("app.services.multiples.compute_all", side_effect=raise_not_implemented),
    ):
        resp = client.get("/api/financials/0000320193")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["periods"]) == 1
    assert body["periods"][0]["multiples"]["pe"]["value"] is None


# ---------------------------------------------------------------------------
# GET /health shape
# ---------------------------------------------------------------------------


def test_health_shape(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "cache" in body
    assert "company_index_size" in body
    cache = body["cache"]
    assert "total_entries" in cache
    assert "live_entries" in cache
    assert "expired_entries" in cache


# ---------------------------------------------------------------------------
# POST /api/search returns results list
# ---------------------------------------------------------------------------


def test_search_returns_results_list(client):
    resp = client.post("/api/search", json={"query": "Apple"})
    assert resp.status_code == 200
    body = resp.json()
    assert "results" in body
    assert isinstance(body["results"], list)


# ---------------------------------------------------------------------------
# edgar._check_taxonomy: ifrs_filer -> 422
# ---------------------------------------------------------------------------


def test_ifrs_filer_returns_422(client):
    import app.cache as cache_store
    cache_store.invalidate("0000320193")

    def raise_ifrs(*args, **kwargs):
        raise HTTPException(
            status_code=422,
            detail={"error": "ifrs_filer", "message": "IFRS filer."},
        )

    with (
        patch("app.services.edgar.fetch_metadata", new=AsyncMock(return_value=_AAPL_SUBMISSIONS)),
        patch("app.services.edgar.fetch_companyfacts", new=AsyncMock(side_effect=raise_ifrs)),
    ):
        resp = client.get("/api/financials/0000320193")

    assert resp.status_code == 422
    body = resp.json()
    assert body.get("error") == "ifrs_filer"
    assert "detail" not in body
