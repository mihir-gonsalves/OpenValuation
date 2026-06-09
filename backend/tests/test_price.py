# backend/tests/test_price.py
"""
Tests for app/services/price.py

yfinance is mocked via unittest.mock.patch - no real network calls.

Coverage:
  - Ticker normalisation: BRK.A -> BRK-A
  - Successful price fetch -> Decimal returned, rounded to 4dp
  - Empty DataFrame -> None returned
  - Non-positive price -> None returned
  - yfinance exception -> None returned (price_unavailable surfaced by caller)
  - Fetch timeout -> None returned
"""

from __future__ import annotations

import asyncio
import threading
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pandas as pd
import pytest

from app.services.price import _fetch_price_sync, _normalise_ticker, get_price


# ---------------------------------------------------------------------------
# Ticker normalisation
# ---------------------------------------------------------------------------


def test_normalise_ticker_dot_to_dash():
    assert _normalise_ticker("BRK.A") == "BRK-A"


def test_normalise_ticker_no_change():
    assert _normalise_ticker("AAPL") == "AAPL"


def test_normalise_ticker_multiple_dots():
    assert _normalise_ticker("A.B.C") == "A-B-C"


# ---------------------------------------------------------------------------
# _fetch_price_sync - happy path
# ---------------------------------------------------------------------------


def test_fetch_price_sync_returns_decimal():
    mock_df = pd.DataFrame(
        {"Close": [182.3456]},
        index=[pd.Timestamp("2024-02-02")],
    )

    with patch("yfinance.download", return_value=mock_df):
        result = _fetch_price_sync("AAPL", date(2024, 2, 2), date(2024, 2, 16))

    assert result == Decimal("182.3456")
    assert isinstance(result, Decimal)


def test_fetch_price_sync_rounds_to_4dp():
    mock_df = pd.DataFrame(
        {"Close": [100.123456789]},
        index=[pd.Timestamp("2024-02-02")],
    )

    with patch("yfinance.download", return_value=mock_df):
        result = _fetch_price_sync("AAPL", date(2024, 2, 2), date(2024, 2, 16))

    assert result == Decimal("100.1235")


# ---------------------------------------------------------------------------
# _fetch_price_sync - empty / invalid responses -> None
# ---------------------------------------------------------------------------


def test_fetch_price_sync_empty_dataframe_returns_none():
    mock_df = pd.DataFrame()

    with patch("yfinance.download", return_value=mock_df):
        result = _fetch_price_sync("AAPL", date(2024, 2, 2), date(2024, 2, 16))

    assert result is None


def test_fetch_price_sync_none_dataframe_returns_none():
    with patch("yfinance.download", return_value=None):
        result = _fetch_price_sync("AAPL", date(2024, 2, 2), date(2024, 2, 16))

    assert result is None


def test_fetch_price_sync_zero_price_returns_none():
    mock_df = pd.DataFrame(
        {"Close": [0.0]},
        index=[pd.Timestamp("2024-02-02")],
    )

    with patch("yfinance.download", return_value=mock_df):
        result = _fetch_price_sync("AAPL", date(2024, 2, 2), date(2024, 2, 16))

    assert result is None


def test_fetch_price_sync_negative_price_returns_none():
    mock_df = pd.DataFrame(
        {"Close": [-5.0]},
        index=[pd.Timestamp("2024-02-02")],
    )

    with patch("yfinance.download", return_value=mock_df):
        result = _fetch_price_sync("AAPL", date(2024, 2, 2), date(2024, 2, 16))

    assert result is None


def test_fetch_price_sync_yfinance_exception_returns_none():
    with patch("yfinance.download", side_effect=Exception("connection refused")):
        result = _fetch_price_sync("AAPL", date(2024, 2, 2), date(2024, 2, 16))

    assert result is None


# ---------------------------------------------------------------------------
# get_price (async wrapper) - timeout -> None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_price_timeout_returns_none():
    # The worker thread must outlive the wait_for timeout to trigger
    # TimeoutError, but it must NOT block forever: asyncio.to_thread runs it on
    # the default ThreadPoolExecutor, whose atexit hook joins every worker
    # thread at interpreter shutdown. A bare time.sleep(9999) would therefore
    # hang the pytest process after all tests pass. Use a releasable Event so
    # the thread can finish in teardown.
    release = threading.Event()

    def _block(*_a, **_k):
        release.wait(timeout=30)

    try:
        with patch("app.services.price._fetch_price_sync", side_effect=_block), \
             patch("app.services.price.PRICE_FETCH_TIMEOUT_SECONDS", 0.01):
            result = await get_price("AAPL", date(2024, 2, 1))
        assert result is None
    finally:
        release.set()


@pytest.mark.asyncio
async def test_get_price_normalises_ticker_before_call():
    """Verify BRK.A is normalised to BRK-A before being passed to _fetch_price_sync."""
    captured = {}

    def _fake_fetch(ticker, start, end):
        captured["ticker"] = ticker
        return pd.DataFrame({"Close": [500.0]}, index=[pd.Timestamp("2024-01-02")])

    with patch("app.services.price._fetch_price_sync", side_effect=_fake_fetch):
        await get_price("BRK.A", date(2024, 1, 1))

    assert captured["ticker"] == "BRK-A"
