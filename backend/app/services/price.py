# backend/app/services/price.py
"""
Price service - yfinance wrapper.

All price logic is isolated in this module. Replacing yfinance means editing
only this file.

Why next trading day's close?
  EDGAR filings are typically submitted after 4:00 PM ET. The filing day's
  own close means the market had not yet seen the data. The next trading day
  is the first session where the disclosed information could be fully priced
  in. This aligns with standard academic and professional valuation practice.

  Edge case: filings submitted before 9:30 AM ET could technically use the
  same day. This is not handled - the tool always uses the next trading day.
  This introduces a small systematic timing bias for pre-market submissions.

Why split-adjusted close?
  Stock splits distort raw prices over time. yfinance's download(auto_adjust=False)
  returns the `Close` column as split-adjusted-only close, making values comparable
  across periods without the dividend-adjustment that total-return prices carry.
  Dividend-adjusted prices understate historical prices for dividend payers, which
  would systematically understate valuation multiples for older TTM periods.

Why a 14-day window?
  The US market can be closed for 4-5 consecutive calendar days around
  Christmas-New Year. 14 calendar days (window constant 14, end-exclusive,
  so the effective window is 13 days) guarantees at least 4 trading days
  regardless of holiday placement.

Ticker normalisation:
  EDGAR stores 'BRK.A', yfinance expects 'BRK-A'.
  The service normalises '.' -> '-' before querying.

yfinance is synchronous. All calls are dispatched to a thread pool via
asyncio.to_thread() to avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRICE_WINDOW_DAYS = 14
"""
Number of calendar days to request from yfinance starting the day after
the filing date.

Wide enough to capture at least 4 trading days over any
US market holiday period.
"""

PRICE_FETCH_TIMEOUT_SECONDS = 15.0
"""Maximum seconds to wait for yfinance before giving up and returning None."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_price(ticker: str, filing_date: date) -> Decimal | None:
    """
    Return the split-adjusted close price on the first trading day after `filing_date`.

    Parameters
    ----------
    ticker : Exchange ticker as stored in EDGAR (e.g. 'AAPL', 'BRK.A').
    filing_date : Submission date of the quarterly filing (date object, not datetime).

    Returns
    -------
    Decimal price rounded to 4 decimal places, or None if unavailable.

    None is returned (not raised) on all failure modes:
      - yfinance network error
      - No trading days in window
      - Price <= 0
      - Ticker not found

        The caller attaches a `price_unavailable` warning in these cases.
    """
    normalised = _normalise_ticker(ticker)
    window_start = filing_date + timedelta(days=1)
    window_end = filing_date + timedelta(days=PRICE_WINDOW_DAYS)

    try:
        # NOTE: on TimeoutError, the underlying thread continues running until
        # yfinance finishes. The event loop is unblocked, but thread resources
        # are not freed immediately. yfinance's own network timeout is the
        # practical backstop.
        price = await asyncio.wait_for(
            asyncio.to_thread(_fetch_price_sync, normalised, window_start, window_end),
            timeout=PRICE_FETCH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Price fetch timed out for %s (filing %s).", normalised, filing_date
        )
        return None
    except Exception as exc:
        logger.warning(
            "Unexpected error fetching price for %s (filing %s): %s.",
            normalised,
            filing_date,
            exc,
        )
        return None

    return price


async def get_prices(
    ticker: str, filing_dates: list[date]
) -> dict[date, Decimal | None]:
    """
    Batch variant of get_price: one yfinance download covering every filing date.

    Instead of N concurrent downloads (one per period), fetches a single window
    spanning [min(filing_dates)+1, max(filing_dates)+PRICE_WINDOW_DAYS] and
    resolves each filing date from the returned DataFrame. This avoids Yahoo
    Finance rate-limiting from simultaneous requests and reduces network round-trips
    from up to 12 to 1.

    Returns {filing_date: price or None}. Falls back to all-None on any failure.
    Per-date semantics are preserved: each date gets the first close strictly after
    that date, within PRICE_WINDOW_DAYS calendar days.
    """
    if not filing_dates:
        return {}
    normalised = _normalise_ticker(ticker)
    window_start = min(filing_dates) + timedelta(days=1)
    window_end = max(filing_dates) + timedelta(days=PRICE_WINDOW_DAYS)
    try:
        df = await asyncio.wait_for(
            asyncio.to_thread(_download_window_sync, normalised, window_start, window_end),
            timeout=PRICE_FETCH_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, Exception):
        logger.warning("Batch price fetch failed for %s.", normalised)
        return {d: None for d in filing_dates}
    if df is None:
        return {d: None for d in filing_dates}
    return {d: _first_close_after(df, d) for d in filing_dates}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalise_ticker(ticker: str) -> str:
    """
    Normalise a ticker for yfinance.

    EDGAR stores `BRK.A`, yfinance expects `BRK-A`.
    Rule: replace `.` with `-`.

    >>> _normalise_ticker('BRK.A')
    'BRK-A'
    >>> _normalise_ticker('AAPL')
    'AAPL'
    """
    return ticker.replace(".", "-")


def _download_window_sync(ticker: str, window_start: date, window_end: date):
    """
    Fetch OHLCV data for [window_start, window_end) from yfinance.

    Returns a pandas DataFrame with a DatetimeIndex and a plain 'Close' column,
    or None on any failure. Shared by both `_fetch_price_sync` and `get_prices`.
    """
    try:
        import yfinance as yf  # lazy import to allow mocking in tests
        import pandas as pd    # lazy: defers ~1 s of import-time work to first call

        df = yf.download(
            ticker,
            start=window_start,
            end=window_end,
            auto_adjust=False,   # Close is split-adjusted only, Adj Close (ignored) adds dividends
            progress=False,
        )

        if df is None or df.empty:
            logger.debug("No price data returned for %s (%s - %s).", ticker, window_start, window_end)
            return None

        # yfinance >=0.2.x returns a MultiIndex (Price, Ticker) for the columns
        # even when a single ticker is requested. Drop the ticker level so the
        # rest of the code can address columns by their plain name ("Close").
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)

        if "Close" not in df.columns:
            logger.debug("'Close' column missing for %s.", ticker)
            return None

        return df

    except Exception as exc:
        logger.warning("yfinance error for %s: %s.", ticker, exc)
        return None


def _first_close_after(df, filing_date: date) -> Decimal | None:
    """
    Return the split-adjusted close of the first row in `df` whose index date
    is strictly after `filing_date` and within PRICE_WINDOW_DAYS calendar days.

    Returns None if no qualifying row exists or the price is <= 0.
    """
    cutoff = filing_date + timedelta(days=PRICE_WINDOW_DAYS)
    try:
        for ts, row in df.iterrows():
            row_date = ts.date()
            if row_date <= filing_date:
                continue
            if row_date > cutoff:
                break
            price_float = float(row["Close"])
            if price_float <= 0:
                logger.debug("Non-positive price %.4f for filing %s. Discarding.", price_float, filing_date)
                return None
            return Decimal(str(price_float)).quantize(Decimal("0.0001"))
    except (InvalidOperation, Exception) as exc:
        logger.warning("Price extraction failed for filing %s: %s.", filing_date, exc)
    return None


def _fetch_price_sync(
    ticker: str, window_start: date, window_end: date
) -> Decimal | None:
    """
    Synchronous yfinance call - run in a thread pool via asyncio.to_thread().

    Fetches OHLCV data for [window_start, window_end) (yfinance end is exclusive)
    and returns the split-adjusted close of the first available trading day.

    Returns None if:
      - The ticker is not recognised by yfinance
      - No trading days fall within the window
      - The returned price is <= 0
      - Any exception occurs inside yfinance
    """
    df = _download_window_sync(ticker, window_start, window_end)
    if df is None:
        return None

    # Derive the filing_date from window_start (window_start = filing_date + 1).
    filing_date = window_start - timedelta(days=1)
    return _first_close_after(df, filing_date)
