# backend/tests/test_price.py
"""
Tests for app/services/price.py

yfinance is mocked via unittest.mock.patch - no real network calls.

Coverage:
  - Ticker normalization: BRK.A -> BRK-A
  - Successful price fetch -> Decimal returned, rounded to 4dp
  - Empty DataFrame -> None returned
  - Non-positive price -> None returned
  - yfinance exception -> PriceFetchError (get_price still degrades to None)
  - Fetch timeout -> None returned
  - get_prices batch: single download, one result per filing date
  - get_prices batch: transient failure -> retried, succeeds on a later attempt
  - get_prices batch: exhausted retries -> all-None dict
  - get_prices batch: empty response -> all-None dict without retrying
  - get_prices batch: empty list -> empty dict
"""

from __future__ import annotations

import asyncio
import threading
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, call, patch

import pandas as pd
import pytest

from app.services.price import (
    PriceFetchError,
    _download_window_sync,
    _fetch_price_sync,
    _first_close_after,
    _normalize_ticker,
    get_price,
    get_prices,
)


# ---------------------------------------------------------------------------
# Ticker normalization
# ---------------------------------------------------------------------------


def test_normalize_ticker_dot_to_dash():
    assert _normalize_ticker("BRK.A") == "BRK-A"


def test_normalize_ticker_no_change():
    assert _normalize_ticker("AAPL") == "AAPL"


def test_normalize_ticker_multiple_dots():
    assert _normalize_ticker("A.B.C") == "A-B-C"


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


def test_fetch_price_sync_uses_auto_adjust_false():
    """yfinance must be called with auto_adjust=False (split-adjusted only, no dividends)."""
    mock_df = pd.DataFrame(
        {"Close": [150.0]},
        index=[pd.Timestamp("2024-02-02")],
    )
    with patch("yfinance.download", return_value=mock_df) as mock_download:
        _fetch_price_sync("AAPL", date(2024, 2, 2), date(2024, 2, 16))
    _, kwargs = mock_download.call_args
    assert kwargs.get("auto_adjust") is False


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


def test_fetch_price_sync_yfinance_exception_raises():
    """A yfinance failure is retryable, so it raises rather than reading as no-data."""
    with patch("yfinance.download", side_effect=Exception("connection refused")):
        with pytest.raises(PriceFetchError):
            _fetch_price_sync("AAPL", date(2024, 2, 2), date(2024, 2, 16))


@pytest.mark.asyncio
async def test_get_price_degrades_to_none_on_fetch_error():
    """get_price keeps its never-raises contract despite _fetch_price_sync raising."""
    with patch("yfinance.download", side_effect=Exception("connection refused")):
        assert await get_price("AAPL", date(2024, 2, 1)) is None


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
        with patch("app.services.price._download_window_sync", side_effect=_block), \
             patch("app.services.price.PRICE_FETCH_TIMEOUT_SECONDS", 0.01):
            result = await get_price("AAPL", date(2024, 2, 1))
        assert result is None
    finally:
        release.set()


@pytest.mark.asyncio
async def test_get_price_normalizes_ticker_before_call():
    """Verify BRK.A is normalized to BRK-A before being passed to _download_window_sync."""
    captured = {}

    def _fake_download(ticker, start, end):
        captured["ticker"] = ticker
        return pd.DataFrame({"Close": [500.0]}, index=[pd.Timestamp("2024-01-02")])

    with patch("app.services.price._download_window_sync", side_effect=_fake_download):
        await get_price("BRK.A", date(2024, 1, 1))

    assert captured["ticker"] == "BRK-A"


# ---------------------------------------------------------------------------
# get_prices (batch) - happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_prices_returns_price_per_date():
    """Single batch download resolves a price for each filing date."""
    filing_dates = [date(2024, 1, 1), date(2024, 4, 1)]
    mock_df = pd.DataFrame(
        {"Close": [150.0, 160.0, 155.0]},
        index=[
            pd.Timestamp("2024-01-02"),
            pd.Timestamp("2024-04-02"),
            pd.Timestamp("2024-04-03"),
        ],
    )

    with patch("app.services.price._download_window_sync", return_value=mock_df):
        result = await get_prices("AAPL", filing_dates)

    assert result[date(2024, 1, 1)] == Decimal("150.0000")
    assert result[date(2024, 4, 1)] == Decimal("160.0000")


@pytest.mark.asyncio
async def test_get_prices_single_download():
    """get_prices issues exactly one _download_window_sync call regardless of how many dates."""
    filing_dates = [date(2024, 1, 1), date(2024, 4, 1), date(2024, 7, 1)]
    mock_df = pd.DataFrame(
        {"Close": [100.0, 110.0, 120.0]},
        index=[
            pd.Timestamp("2024-01-02"),
            pd.Timestamp("2024-04-02"),
            pd.Timestamp("2024-07-02"),
        ],
    )

    with patch("app.services.price._download_window_sync", return_value=mock_df) as mock_dl:
        await get_prices("AAPL", filing_dates)

    assert mock_dl.call_count == 1


@pytest.mark.asyncio
async def test_get_prices_empty_list_returns_empty_dict():
    result = await get_prices("AAPL", [])
    assert result == {}


@pytest.mark.asyncio
async def test_get_prices_empty_response_returns_all_none_without_retrying():
    """An empty response is Yahoo answering 'no data', which a retry cannot change."""
    filing_dates = [date(2024, 1, 1), date(2024, 4, 1)]

    with patch("app.services.price._download_window_sync", return_value=None) as mock_dl:
        result = await get_prices("AAPL", filing_dates)

    assert result == {date(2024, 1, 1): None, date(2024, 4, 1): None}
    assert mock_dl.call_count == 1


@pytest.mark.asyncio
async def test_get_prices_retries_transient_failure_then_succeeds():
    """A PriceFetchError is retried, so one bad attempt does not blank every multiple."""
    filing_dates = [date(2024, 1, 1)]
    good_df = pd.DataFrame({"Close": [150.0]}, index=[pd.Timestamp("2024-01-02")])
    attempts = []

    def _flaky(ticker, start, end):
        attempts.append(ticker)
        if len(attempts) < 2:
            raise PriceFetchError("rate limited")
        return good_df

    with patch("app.services.price._download_window_sync", side_effect=_flaky), \
         patch("app.services.price.PRICE_RETRY_BACKOFF_SECONDS", 0.0):
        result = await get_prices("AAPL", filing_dates)

    assert len(attempts) == 2
    assert result[date(2024, 1, 1)] == Decimal("150.0000")


@pytest.mark.asyncio
async def test_get_prices_exhausted_retries_returns_all_none():
    """After the retry budget is spent, the caller still gets a clean all-None map."""
    filing_dates = [date(2024, 1, 1), date(2024, 4, 1)]

    with patch(
        "app.services.price._download_window_sync",
        side_effect=PriceFetchError("rate limited"),
    ) as mock_dl, patch("app.services.price.PRICE_RETRY_BACKOFF_SECONDS", 0.0):
        result = await get_prices("AAPL", filing_dates)

    assert result == {date(2024, 1, 1): None, date(2024, 4, 1): None}
    assert mock_dl.call_count == 2


@pytest.mark.asyncio
async def test_get_prices_normalizes_ticker():
    """Batch fetch normalizes the ticker the same way as get_price."""
    captured = {}

    def _fake_download(ticker, start, end):
        captured["ticker"] = ticker
        return pd.DataFrame({"Close": [500.0]}, index=[pd.Timestamp("2024-01-02")])

    with patch("app.services.price._download_window_sync", side_effect=_fake_download):
        await get_prices("BRK.A", [date(2024, 1, 1)])

    assert captured["ticker"] == "BRK-A"


@pytest.mark.asyncio
async def test_get_prices_timeout_returns_all_none():
    """On timeout, all dates map to None."""
    release = threading.Event()

    def _block(*_a, **_k):
        release.wait(timeout=30)

    filing_dates = [date(2024, 1, 1), date(2024, 4, 1)]
    try:
        with patch("app.services.price._download_window_sync", side_effect=_block) as mock_dl, \
             patch("app.services.price.PRICE_FETCH_TIMEOUT_SECONDS", 0.01), \
             patch("app.services.price.PRICE_RETRY_BACKOFF_SECONDS", 0.0):
            result = await get_prices("AAPL", filing_dates)
        assert result == {date(2024, 1, 1): None, date(2024, 4, 1): None}
        # A timeout is retried too - a slow Yahoo is the case this exists for.
        assert mock_dl.call_count == 2
    finally:
        release.set()
