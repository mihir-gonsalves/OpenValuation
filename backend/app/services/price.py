# backend/app/services/price.py
"""
Price service, the only module that knows about yfinance.

**Next trading day's close.** Filings usually land after 4:00 PM ET, so the
filing day's own close predates the market seeing the data. The next session is
the first that could price it in. Filings submitted before 9:30 AM ET could
technically use the same day, which is not handled and leaves a small timing
bias for pre-market submissions.

**Split-adjusted, not dividend-adjusted.** `auto_adjust=False` makes `Close`
split-adjusted only. Total-return prices understate historical prices for
dividend payers, which would systematically understate older TTM multiples.

**A 14 day window** (end-exclusive, so 13 effective) guarantees at least four
trading days even across the Christmas to New Year closure.

**Transient failures are retried.** Every multiple is price-dependent, so an
empty price map blanks the entire table rather than degrading part of it. A
slow or rate-limited response is therefore retried with backoff instead
of being turned into N/A on the first miss. A response that simply carries no
rows is not retried - that means the ticker has no data in the window, which
another attempt cannot change.

yfinance is synchronous, so every call goes through asyncio.to_thread.
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
"""Calendar days to request, starting the day after the filing date."""

PRICE_FETCH_TIMEOUT_SECONDS = 20.0
"""Seconds to wait for a single yfinance attempt before abandoning it."""

PRICE_FETCH_ATTEMPTS = 2
"""Attempts per batch download, counting the first."""

PRICE_RETRY_BACKOFF_SECONDS = 2.0
"""Delay before the retry."""


class PriceFetchError(Exception):
    """
    yfinance could not answer: network error, rate limit, or an internal error.

    Distinct from a successful response with no rows, which means the ticker has
    no data in the window. Only this is worth retrying.
    """


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_price(ticker: str, filing_date: date) -> Decimal | None:
    """
    Split-adjusted close on the first trading day after `filing_date`, to 4dp.

    Every failure mode (network error, unknown ticker, no trading day in the
    window, a non-positive price) returns None rather than raising. The caller
    turns that into a `price_unavailable` warning.
    """
    normalized = _normalize_ticker(ticker)
    window_start = filing_date + timedelta(days=1)
    window_end = filing_date + timedelta(days=PRICE_WINDOW_DAYS)

    try:
        # On timeout the event loop unblocks but the thread runs on until
        # yfinance's own network timeout fires. No way around a sync library.
        price = await asyncio.wait_for(
            asyncio.to_thread(_fetch_price_sync, normalized, window_start, window_end),
            timeout=PRICE_FETCH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("Price fetch timed out for %s (filing %s).", normalized, filing_date)
        return None
    except Exception as exc:
        logger.warning("Unexpected error fetching price for %s (filing %s): %s.", normalized, filing_date, exc)
        return None

    return price


async def get_prices(ticker: str, filing_dates: list[date]) -> dict[date, Decimal | None]:
    """
    Batch variant of get_price: one download covering every filing date.

    A single window spanning all the dates replaces up to 12 concurrent
    downloads, which is what keeps yfinance from rate-limiting the request.
    Per-date semantics are unchanged.

    A timeout or a PriceFetchError is retried up to PRICE_FETCH_ATTEMPTS times.
    Only an exhausted retry budget, or a response with no usable prices, returns all-None.
    """
    if not filing_dates:
        return {}
    normalized = _normalize_ticker(ticker)
    window_start = min(filing_dates) + timedelta(days=1)
    window_end = max(filing_dates) + timedelta(days=PRICE_WINDOW_DAYS)

    df = None
    for attempt in range(1, PRICE_FETCH_ATTEMPTS + 1):
        try:
            df = await asyncio.wait_for(
                asyncio.to_thread(_download_window_sync, normalized, window_start, window_end),
                timeout=PRICE_FETCH_TIMEOUT_SECONDS,
            )
            # A None df here means yfinance answered with nothing, so stop.
            break
        except (asyncio.TimeoutError, PriceFetchError) as exc:
            # Logged at each attempt because the cause differs per attempt, and a
            # single collapsed message cannot tell a timeout from a rate limit.
            if attempt == PRICE_FETCH_ATTEMPTS:
                logger.warning(
                    "Batch price fetch for %s failed on final attempt %d/%d: %r.",
                    normalized, attempt, PRICE_FETCH_ATTEMPTS, exc,
                )
                return {d: None for d in filing_dates}
            logger.info(
                "Batch price fetch for %s failed on attempt %d/%d (%r), retrying in %.0fs.",
                normalized, attempt, PRICE_FETCH_ATTEMPTS, exc, PRICE_RETRY_BACKOFF_SECONDS,
            )
            await asyncio.sleep(PRICE_RETRY_BACKOFF_SECONDS)

    if df is None:
        logger.warning("No price data for %s in [%s, %s).", normalized, window_start, window_end)
        return {d: None for d in filing_dates}
    return {d: _first_close_after(df, d) for d in filing_dates}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalize_ticker(ticker: str) -> str:
    """
    EDGAR stores `BRK.A`, yfinance expects `BRK-A`.

    >>> _normalize_ticker('BRK.A')
    'BRK-A'
    """
    return ticker.replace(".", "-")


def _download_window_sync(ticker: str, window_start: date, window_end: date):
    """
    Fetch OHLCV data for [window_start, window_end).

    Returns a DataFrame with a DatetimeIndex and a plain 'Close' column, or
    None on any failure.
    """
    try:
        import yfinance as yf  # lazy so tests can mock it
        import pandas as pd    # lazy: defers ~1s of import work to the first call

        df = yf.download(
            ticker,
            start=window_start,
            end=window_end,
            auto_adjust=False,   # keeps Close split-adjusted only
            progress=False,
        )

        if df is None or df.empty:
            logger.debug("No price data returned for %s (%s - %s).", ticker, window_start, window_end)
            return None

        # yfinance >=0.2.x returns MultiIndex columns even for one ticker.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)

        if "Close" not in df.columns:
            logger.debug("'Close' column missing for %s.", ticker)
            return None

        return df

    except Exception as exc:
        raise PriceFetchError(f"yfinance error for {ticker}: {exc}") from exc


def _first_close_after(df, filing_date: date) -> Decimal | None:
    """
    Close of the first row strictly after `filing_date` and within
    PRICE_WINDOW_DAYS. None if there is no such row or the price is <= 0.
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


def _fetch_price_sync(ticker: str, window_start: date, window_end: date) -> Decimal | None:
    """
    Synchronous single-date fetch, run via asyncio.to_thread. None on any failure.
    """
    df = _download_window_sync(ticker, window_start, window_end)
    if df is None:
        return None

    # window_start is filing_date + 1.
    filing_date = window_start - timedelta(days=1)
    return _first_close_after(df, filing_date)