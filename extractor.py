"""
ESS field extractor.

Pipeline:
  1. PDF -> text (pdfplumber, local)
  2. Redact PII (regex, local)
  3. Send redacted text to Claude with a structured-output tool schema
  4. Return a typed dict of extracted fields + a list of fields the model
     couldn't find with high confidence (so the UI can prompt the user)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from pypdf import PdfReader

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None  # type: ignore

from redactor import redact, RedactionReport


# Sonnet handles structured extraction cleanly; Haiku is the cheaper fallback.
DEFAULT_MODEL = "claude-sonnet-4-6"

# Tool schema — Claude returns one tool_use block matching this shape.
EXTRACTION_TOOL = {
    "name": "record_ess_fields",
    "description": (
        "Record the extracted Employer Sponsored Scheme (ESS) details from a "
        "pension document. Use null for any field that is not clearly stated "
        "in the document."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "employer_name":      {"type": ["string", "null"]},
            "scheme_provider":    {"type": ["string", "null"]},
            "plan_name":          {"type": ["string", "null"]},
            "current_fund_value": {
                "type": ["number", "null"],
                "description": "Total transfer/fund value in GBP.",
            },
            "wrapper_charge_pct": {
                "type": ["number", "null"],
                "description": (
                    "Product/wrapper Annual Management Charge as a decimal "
                    "(e.g. 0.0035 for 0.35%)."
                ),
            },
            "regular_contribution_monthly": {"type": ["number", "null"]},
            "funds": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name":          {"type": "string"},
                        "allocation":    {
                            "type": "number",
                            "description": "GBP value of holding in this fund.",
                        },
                        "annual_charge": {
                            "type": "number",
                            "description": "Fund's annual charge as a decimal.",
                        },
                    },
                    "required": ["name", "allocation", "annual_charge"],
                },
            },
            "missing_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of fields that could not be confidently extracted "
                    "from the document and should be requested from the user."
                ),
            },
            "notes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Any caveats or assumptions made during extraction."
                ),
            },
        },
        "required": ["funds", "missing_fields", "notes"],
    },
}

EXTRACTION_PROMPT = """\
You are extracting Employer Sponsored Scheme details from a pension document
for a UK financial-advice critical-yield calculation.

The document text below has already had personal identifying information
redacted ([CLIENT], [DOB], [REF], etc.). Do not invent values to fill those.

Extract every field you can find with high confidence. For anything ambiguous,
unclear, or absent, add the field name to `missing_fields` rather than
guessing. Annual charges are decimals (0.0035 = 0.35%). Monetary values are
GBP without currency symbols or commas.

Call the `record_ess_fields` tool exactly once with your structured answer.

Document text:
---
{text}
---
"""


# ---------------------------------------------------------------------------
# Stage 1 — PDF text extraction (local)
# ---------------------------------------------------------------------------

def pdf_to_text(pdf_path: str) -> str:
    reader = PdfReader(pdf_path)
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages)


# ---------------------------------------------------------------------------
# Stage 2/3 — redact + send to Claude
# ---------------------------------------------------------------------------

@dataclass
class ExtractionResult:
    fields: dict[str, Any]
    missing_fields: list[str]
    notes: list[str]
    redaction: RedactionReport
    model_used: str
    raw_response: Any = None


class ExtractorError(RuntimeError):
    pass


def extract_from_pdf(
    pdf_path: str,
    advisor_name: str | None = None,
    model: str = DEFAULT_MODEL,
) -> ExtractionResult:
    raw_text = pdf_to_text(pdf_path)
    return extract_from_text(raw_text, advisor_name=advisor_name, model=model)


def extract_from_text(
    text: str,
    advisor_name: str | None = None,
    model: str = DEFAULT_MODEL,
) -> ExtractionResult:
    if Anthropic is None:
        raise ExtractorError(
            "anthropic SDK not installed. Run: pip install anthropic"
        )
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ExtractorError(
            "ANTHROPIC_API_KEY environment variable is not set."
        )

    redaction = redact(text, advisor_name=advisor_name)

    client = Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        tools=[EXTRACTION_TOOL],
        tool_choice={"type": "tool", "name": "record_ess_fields"},
        messages=[
            {
                "role": "user",
                "content": EXTRACTION_PROMPT.format(text=redaction.text),
            }
        ],
    )

    tool_block = next(
        (b for b in response.content if getattr(b, "type", None) == "tool_use"),
        None,
    )
    if tool_block is None:
        raise ExtractorError(
            "Model did not return a tool_use block. Raw: "
            + json.dumps([b.model_dump() for b in response.content])[:500]
        )

    payload = tool_block.input
    missing = payload.pop("missing_fields", [])
    notes = payload.pop("notes", [])

    return ExtractionResult(
        fields=payload,
        missing_fields=missing,
        notes=notes,
        redaction=redaction,
        model_used=model,
        raw_response=response,
    )
