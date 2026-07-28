# backend/app/routers/export.py
"""
GET /api/export/{cik_10}

Streams the .xlsx workbook built by services/workbook.py.
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
            "content": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}},
            "description": "Excel workbook (.xlsx) binary stream.",
        },
        404: {
            "description": "CIK not found or no data available."
        },
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
    Reuses the financials cache-or-fetch path, so the export matches what the
    results table shows and failures surface as the same structured HTTPExceptions.
    """
    response = await resolve_financials(request, cik_10)
    xlsx_bytes = workbook.build_workbook(response)
    filename = f"openvaluation_{cik_10}.xlsx"
    
    return Response(
        content=xlsx_bytes,
        media_type=_XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )