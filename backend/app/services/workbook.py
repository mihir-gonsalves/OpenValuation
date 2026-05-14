# backend/app/services/workbook.py
"""
Excel workbook builder - Phase 4 implementation target.

Generates a .xlsx workbook with three sheets:

  1. Summary        - Final multiples table, company metadata, timestamp, warnings.
  2. Raw Financials - All extracted XBRL values by period (concept, tag, fallback,
                      unit, context, period end date).
  3. Calculations   - Live Excel formulas referencing Raw Financials. No hardcoded
                      values, all results recompute if inputs are edited.

Design principle: the workbook reproduces all backend calculations as Excel formulas,
making every number fully auditable and adjustable by the end user.

Phase 1 status: stub defined. Raises NotImplementedError.  
Phase 4: implement using openpyxl.
"""

from __future__ import annotations

from app.models.financials import FinancialsResponse


def build_workbook(response: FinancialsResponse) -> bytes:
    """
    Build and return a .xlsx workbook as raw bytes.

    Parameters
    ----------
    response : The fully computed FinancialsResponse for the company.

    Returns
    -------
    Raw bytes of the .xlsx file, suitable for streaming as a binary HTTP response.

    Phase 1: raises NotImplementedError.  
    Phase 4: implement using openpyxl with three sheets (Summary, Raw Financials,
    Calculations).
    """
    raise NotImplementedError(
        "Excel export not yet implemented (Phase 4)."
    )