# backend/app/routers/export.py
"""
GET /api/export/{cik_10}

Streams a .xlsx workbook containing valuation multiples, raw XBRL inputs,
and live Excel formulas for full auditability.

Three sheets (Phase 4):
  1. Summary        - Final multiples, company metadata, timestamp, warnings.
  2. Raw Financials - Extracted XBRL values with tags, fallback status, unit, context.
  3. Calculations   - Live Excel formulas referencing Raw Financials. No hardcoded values.

Phase 1: returns HTTP 501 (not yet implemented). Full Excel generation is Phase 4.  
Phase 4: calls workbook.build_workbook(response) and streams the binary result.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Path, Request
from fastapi.responses import Response

from app.routers.financials import resolve_financials
from app.services import workbook

logger = logging.getLogger(__name__)

router = APIRouter()

_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get(
    "/export/{cik_10}",
    summary="Download valuation data as an Excel workbook.",
    description=(
        "Returns a .xlsx workbook: a Summary sheet (company metadata and a "
        "live multiples matrix) plus one sheet per TTM period holding the raw "
        "XBRL inputs, live Excel formulas, and warnings."
    ),
    responses={
        200: {
            "content": {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}
            },
            "description": "Excel workbook (.xlsx) binary stream.",
        },
        404: {"description": "CIK not found or no data available."},
    },
)
async def export_workbook(
    request: Request,
    cik_10: str = Path(
        ...,
        description="10-digit zero-padded EDGAR CIK, e.g. '0000320193'.",
        pattern=r"^\d{10}$",
        examples=["0000320193"],
    ),
) -> Response:
    """
    Stream a .xlsx workbook for the given CIK.

    Reuses the financials cache-or-fetch path (resolve_financials) so the export
    reflects exactly what the results table shows, then serialises it to a
    formula-driven workbook. EDGAR/extraction failures propagate as the same
    structured HTTPExceptions the financials endpoint raises.
    """
    response = await resolve_financials(request, cik_10)
    xlsx_bytes = workbook.build_workbook(response)

    filename = f"openvaluation_{cik_10}.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type=_XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )