# SJP Critical Yield Tool

Local PDF-extraction + Critical Yield Calculator for pension transfer reviews.

## What it does

1. **Upload a PDF** (ESS statement, ceding-scheme illustration, or existing CYC report).
2. **Local redaction** strips client name, DOB, account numbers, NI numbers, postcodes, emails and phone numbers before any text leaves the host.
3. **Claude API** receives only the redacted text and returns the ESS fields as structured JSON. Anything the model can't extract confidently is flagged so you can fill it in manually.
4. **Review & confirm** the fields in an editable form.
5. **Critical Yield calculation** runs the same RIY engine calibrated against report ref. 1786247 (CY 1.74%, ESS 1.57% — exact match).

## Run

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn app:app --port 8000
```

Then open <http://localhost:8000>.

## Files

| File | Role |
|---|---|
| `critical_yield.py` | RIY engine, tiered product charge, ESS comparison, regular contributions |
| `redactor.py` | Local PII redaction (regex-based, no network) |
| `extractor.py` | PDF → text → redact → Claude tool-use → structured JSON |
| `app.py` | FastAPI backend (`/api/extract`, `/api/calculate`, `/api/health`) |
| `static/index.html` | Drag-drop UI, redaction preview, editable field form, results |

## Privacy

Only redacted text is sent to Claude. The redaction step runs locally before any network call. The redaction preview in the UI shows exactly what the model sees — if anything personal is visible there, the redactor needs another pattern.

## Known calibration gaps

- Regular CY is ~5bp below the published figure (SJP annuity-factor methodology not fully disclosed)
- IAC is a Partner input; the auto-solver would need SJP's internal rulebook to be exact
- CY limit table only covers the disclosed 0.75% transfer band
