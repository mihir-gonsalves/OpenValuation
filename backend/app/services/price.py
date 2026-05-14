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

Why adjusted close?
  Stock splits distort raw prices over time. yfinance's history(auto_adjust=True)
  returns split-adjusted close prices, making values comparable across periods.

Why a 14-day window?
  The US market can be closed for 4-5 consecutive calendar days around
  Christmas-New Year. 14 calendar days guarantees at least 4 trading days
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
import pandas as pd
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

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


async def get_price(ticker: str, filing_date: date) -> Optional[Decimal]:
    """
    Return the adjusted close price on the first trading day after `filing_date`.

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


def _fetch_price_sync(
    ticker: str, window_start: date, window_end: date
) -> Optional[Decimal]:
    """
    Synchronous yfinance call - run in a thread pool via asyncio.to_thread().

    Fetches OHLCV data for the window [window_start, window_end] and returns
    the adjusted close of the first available trading day.

    Returns None if:
      - The ticker is not recognised by yfinance
      - No trading days fall within the window
      - The returned price is <= 0
      - Any exception occurs inside yfinance
    """
    try:
        import yfinance as yf  # lazy import to allow mocking in tests

        df = yf.download(
            ticker,
            start=window_start,
            end=window_end,
            auto_adjust=True,
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

        close_col = "Close"
        if close_col not in df.columns:
            logger.debug("'Close' column missing for %s.", ticker)
            return None

        # First row = first trading day after the filing date.
        # After the MultiIndex normalisation above, df[close_col] is always a
        # plain Series, so .iloc[0] reliably returns a scalar.
        price_float = float(df[close_col].iloc[0])
        if price_float <= 0:
            logger.debug("Non-positive price %.4f for %s. Discarding.", price_float, ticker)
            return None

        return Decimal(str(price_float)).quantize(Decimal("0.0001"))

    except InvalidOperation as exc:
        logger.warning("Decimal conversion failed for %s: %s.", ticker, exc)
        return None
    except Exception as exc:
        logger.warning("yfinance error for %s: %s.", ticker, exc)
        return None