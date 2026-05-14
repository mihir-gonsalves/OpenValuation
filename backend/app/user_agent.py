# backend/app/user_agent.py
"""
User-Agent setup for the OpenValuation API.

Configure header via the EDGAR_USER_AGENT environment variable.
"""

from __future__ import annotations

import os


def sec_headers() -> dict[str, str]:
    """
    Build the User-Agent header required by all SEC.gov endpoints.

    See: https://www.sec.gov/developer
    """
    user_agent = os.getenv(
        "EDGAR_USER_AGENT",
        "OpenValuation/0.1 openvaluation@example.com",
    )
    return {
        "User-Agent": user_agent,
        "Accept": "application/json",
    }