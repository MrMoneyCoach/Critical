"""
ESS Extraction Tool — FastAPI backend.

Endpoints:
  GET  /              -> serves the single-page UI
  POST /api/extract   -> accepts a PDF upload, returns structured fields
  POST /api/calculate -> runs the CYC engine on confirmed fields
  GET  /api/health    -> liveness + Claude API key status

Run with:
    uvicorn app:app --reload --port 8000
"""

from __future__ import annotations

import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from critical_yield import (
    CedingPlan,
    Client,
    EmployerScheme,
    FundHolding,
    RecommendedPlan,
    DEFAULT_CY_LIMIT_TRANSFER,
    DEFAULT_GROSS_RATE,
    run_cyc,
)
from extractor import ExtractorError, extract_from_pdf, extract_from_pdf_bytes


BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="SJP ESS Extraction Tool")


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "claude_api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
    }


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

@app.post("/api/extract")
async def api_extract(
    file: UploadFile = File(...),
    advisor_name: str | None = None,
) -> dict[str, Any]:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF uploads are supported")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        result = extract_from_pdf(tmp_path, advisor_name=advisor_name)
    except ExtractorError as e:
        raise HTTPException(503, str(e))
    finally:
        os.unlink(tmp_path)

    return {
        "fields": result.fields,
        "missing_fields": result.missing_fields,
        "notes": result.notes,
        "redaction": {
            "client_name_detected": result.redaction.client_name_detected,
            "items_redacted": result.redaction.items_redacted,
            "redacted_preview": result.redaction.text[:2000],
        },
        "model_used": result.model_used,
    }


@app.post("/api/extract_redacted")
async def api_extract_redacted(file: UploadFile = File(...)) -> dict[str, Any]:
    """Accept a PDF that the user has already redacted in the browser viewer."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF uploads are supported")
    pdf_bytes = await file.read()
    try:
        result = extract_from_pdf_bytes(pdf_bytes)
    except ExtractorError as e:
        raise HTTPException(503, str(e))
    return {
        "fields": result.fields,
        "missing_fields": result.missing_fields,
        "notes": result.notes,
        "model_used": result.model_used,
    }


# ---------------------------------------------------------------------------
# Calculation
# ---------------------------------------------------------------------------

class FundIn(BaseModel):
    name: str
    allocation: float
    annual_charge: float


class CalcRequest(BaseModel):
    client_dob: date
    retirement_age: int = 67
    projection_start: date = Field(default_factory=date.today)

    ceding_plan_name: str
    ceding_provider: str
    transfer_value: float
    ceding_wrapper_charge: float = 0.0
    ceding_funds: list[FundIn]
    regular_contribution_monthly: float = 0.0

    existing_sjp_holdings: float = 0.0
    ongoing_advice_charge: float = 0.008
    sjp_fund_charge: float = 0.0052
    iac: float = 0.03

    cy_limit: float = DEFAULT_CY_LIMIT_TRANSFER
    base_gross_rate: float = DEFAULT_GROSS_RATE

    ess_employer: str | None = None
    ess_provider: str | None = None
    ess_wrapper_charge: float = 0.0
    ess_funds: list[FundIn] = Field(default_factory=list)


@app.post("/api/calculate")
def api_calculate(req: CalcRequest) -> dict[str, Any]:
    client = Client(name="[CLIENT]", dob=req.client_dob, retirement_age=req.retirement_age)
    ceding = CedingPlan(
        name=req.ceding_plan_name,
        provider=req.ceding_provider,
        transfer_value=req.transfer_value,
        wrapper_charge=req.ceding_wrapper_charge,
        funds=[FundHolding(f.name, f.allocation, f.annual_charge) for f in req.ceding_funds],
        regular_contribution=req.regular_contribution_monthly,
    )
    recommended = RecommendedPlan(
        ongoing_advice_charge=req.ongoing_advice_charge,
        fund_charge=req.sjp_fund_charge,
        existing_sjp_holdings=req.existing_sjp_holdings,
        iac=req.iac,
    )
    ess = None
    if req.ess_employer and req.ess_funds:
        ess = EmployerScheme(
            employer=req.ess_employer,
            provider=req.ess_provider or "",
            wrapper_charge=req.ess_wrapper_charge,
            funds=[FundHolding(f.name, f.allocation, f.annual_charge) for f in req.ess_funds],
        )

    result = run_cyc(
        client=client,
        ceding=ceding,
        recommended=recommended,
        projection_start=req.projection_start,
        base_gross_rate=req.base_gross_rate,
        cy_limit=req.cy_limit,
        ess=ess,
    )

    out: dict[str, Any] = {
        "pass_fail": result.pass_fail,
        "transfer_value": result.transfer_value,
        "iac_required": result.iac_required_to_proceed,
        "product_charge": result.product_charge,
        "ongoing_advice_charge": result.ongoing_advice_charge,
        "fund_charge": result.fund_charge,
        "term_years": result.term_years,
        "term_months": result.term_months,
        "critical_yield": result.critical_yield_single,
        "critical_yield_limit": result.critical_yield_limit,
        "monetary_year_one": result.monetary_year_one,
        "notes": result.notes,
    }
    if result.regular:
        out["regular"] = {
            "monthly": result.regular.monthly_contribution,
            "cy": result.regular.cy_required,
            "monetary": result.regular.monetary_year_one,
            "excluded": result.regular.excluded,
            "pass_fail": result.regular.pass_fail,
        }
    if result.ess:
        out["ess"] = {
            "employer": result.ess.employer,
            "provider": result.ess.provider,
            "ess_charge": result.ess.ess_total_charge,
            "sjp_charge": result.ess.sjp_total_charge,
            "additional_pct": result.ess.additional_growth_pct,
            "additional_gbp": result.ess.additional_growth_gbp,
        }
    return out
