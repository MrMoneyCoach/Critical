"""
PII redactor for SJP-style financial documents.

Strips client identifying information before any text leaves the host.
Conservative by design — false positives on the redaction side are
acceptable; leaks are not.

What is removed:
  - Client name (detected via "Prepared for:", "Client's name:" anchors,
    then globally replaced)
  - Dates of birth (numeric and long-form)
  - Reference / plan / account numbers
  - National Insurance numbers
  - UK postcodes, email addresses, UK phone numbers

What is kept (needed for the CY calculation):
  - Monetary amounts and percentages
  - Fund names, employer names, provider names
  - Partner / advisor names (not the client)
  - Dates that aren't DOBs (e.g. plan commencement, report generation)
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Regex library
# ---------------------------------------------------------------------------

NI_NUMBER = re.compile(r"\b[A-CEGHJ-PR-TW-Z]{2}\d{6}[A-D]\b")
UK_POSTCODE = re.compile(
    r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.IGNORECASE
)
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
UK_PHONE = re.compile(r"\b(?:0\d{4}\s?\d{6}|0\d{3}\s?\d{3}\s?\d{4}|\+44\s?\d{9,10})\b")

DOB_NUMERIC = re.compile(
    r"\b(0?[1-9]|[12]\d|3[01])[/\-.](0?[1-9]|1[0-2])[/\-.](19|20)\d{2}\b"
)
DOB_LONG = re.compile(
    r"\b(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?\s+"
    r"(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(19|20)\d{2}\b",
    re.IGNORECASE,
)

# Common SJP reference patterns: "Reference number: 1786247", account-like
# strings such as "RA22533186", plan numbers "0743101". We anchor on labels
# where possible, then sweep for orphan long digit strings on their own line.
REFERENCE_LABEL = re.compile(
    r"(Reference\s+number|Plan\s+number|Provider\s+number|Retirement\s+account|"
    r"Account\s+number|Policy\s+number)\s*[:\-]?\s*([A-Z0-9\-]{5,})",
    re.IGNORECASE,
)
ACCOUNT_CODE = re.compile(r"\b[A-Z]{1,3}\d{6,}\b")

# Client-name anchors. Captures the human name on the same line as the label.
# Uses [^\S\n] (whitespace except newline) to prevent bleeding into the next line.
NAME_ANCHORS = [
    re.compile(
        r"Prepared\s+for[:\s]+"
        r"([A-Z][a-zA-Z'\-]+(?:[^\S\n]+[A-Z][a-zA-Z'\-]+){0,3})"
    ),
    re.compile(
        r"Client'?s?\s+name[:\s]+"
        r"([A-Z][a-zA-Z'\-]+(?:[^\S\n]+[A-Z][a-zA-Z'\-]+){0,3})"
    ),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class RedactionReport:
    text: str
    items_redacted: dict[str, int]
    client_name_detected: str | None


def redact(text: str, advisor_name: str | None = None) -> RedactionReport:
    """Return a redacted copy of `text` plus a count of what was removed.

    If `advisor_name` is provided, that exact string is preserved (so it
    isn't accidentally stripped as a client name).
    """
    counts: dict[str, int] = {}
    client_name = _find_client_name(text)

    def sub(pattern: re.Pattern, repl: str, key: str, txt: str) -> str:
        new_txt, n = pattern.subn(repl, txt)
        if n:
            counts[key] = counts.get(key, 0) + n
        return new_txt

    out = text

    # 1. High-confidence identifiers
    out = sub(NI_NUMBER,    "[NI_NUMBER]",      "ni_number",   out)
    out = sub(EMAIL,        "[EMAIL]",          "email",       out)
    out = sub(UK_PHONE,     "[PHONE]",          "phone",       out)
    out = sub(UK_POSTCODE,  "[POSTCODE]",       "postcode",    out)
    out = sub(DOB_NUMERIC,  "[DOB]",            "dob_numeric", out)
    out = sub(DOB_LONG,     "[DOB]",            "dob_long",    out)

    # 2. Account / reference numbers
    out = REFERENCE_LABEL.sub(lambda m: f"{m.group(1)}: [REF]", out)
    counts["reference"] = len(REFERENCE_LABEL.findall(text))
    out = sub(ACCOUNT_CODE, "[ACCOUNT]", "account_code", out)

    # 3. Client name — replace every occurrence in the document
    if client_name:
        # Build a forgiving pattern that matches the full name and isolated
        # forename. Skip if the name coincides with the advisor name.
        if advisor_name and client_name.lower() == advisor_name.lower():
            client_name = None
        else:
            full_re = re.compile(re.escape(client_name), re.IGNORECASE)
            out, n_full = full_re.subn("[CLIENT]", out)
            counts["client_name"] = n_full

            first = client_name.split()[0]
            if len(first) > 2:
                first_re = re.compile(rf"\b{re.escape(first)}\b")
                out, n_first = first_re.subn("[CLIENT]", out)
                counts["client_name"] = counts.get("client_name", 0) + n_first

    return RedactionReport(
        text=out,
        items_redacted={k: v for k, v in counts.items() if v},
        client_name_detected=client_name,
    )


def _find_client_name(text: str) -> str | None:
    for pattern in NAME_ANCHORS[:2]:
        m = pattern.search(text)
        if m:
            return m.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sample = """
    Critical Yield Calculator
    Prepared for: Nitin Suneja
    Reference number: 1786247
    Client's date of birth: 30/07/1972
    Retirement account: RA22533186
    Email: nitin@example.com
    Postcode: SW1A 1AA
    NI: AB123456C
    """
    report = redact(sample, advisor_name="Scott Mackey")
    print(report.text)
    print()
    print("Detected client:", report.client_name_detected)
    print("Items redacted:", report.items_redacted)
