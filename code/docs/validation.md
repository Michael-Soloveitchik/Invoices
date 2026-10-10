# Validation report

Validated in the development environment on 2026-10-10 with Python 3.12.14.

- Full synthetic unit suite: **151 tests passed** with optional WeasyPrint 70 / PyMuPDF installed.
- Python compileall: passed.
- Package wheel build: passed (rebuild after final edits recorded separately by build output).
- Offline CLI: five synthetic messages scanned, four evidence rows linked to three logical consumptions; linked-only mail flagged for review.
- Explicit date windows: inclusive/exclusive timezone boundaries, Gmail epoch query/internal timestamp, missing dates withheld from LLM, and visibly incomplete sample caps passed.
- Resume: rerun skipped all five processed unchanged messages, with no repeated extraction.
- Mandatory inline capture: all five synthetic messages produced PNG files. English and Hebrew sample PNGs visually inspected.
- HTML capture: actual WeasyPrint HTML→PDF→PNG rendering tested with HTTP CSS and local-file CSS resources denied; scripts/active tags and URL attributes removed.
- PDF smoke: generated synthetic PDF attachment was preserved and parsed using installed Poppler; extracted expected vendor/amount and verified the page count.
- Independent reviewer reproduced and verified fixes for stale-evidence overwrite and responsive-layout duplicate counting.

Not validated: live Gmail/IMAP authentication, actual LLM calls/accuracy, large mailbox concurrency/quotas, OCR accuracy on real invoices, or real GitHub publication/CI. Tesseract is installed here with English only; the required Hebrew OCR language pack was absent, so Hebrew OCR was not claimed tested. Gmail/IMAP/API behavior is covered by mocked protocol tests.

No real customer mail, credentials, receipt data, generated private exports, or API keys are included in the project or its ZIP.

Post-delivery archive revision: ZIP now uses `Invoices/code`; runtime evidence is under `Invoices/YYYY/MM`, reports/state under `Invoices/reports`. A new regression verifies current-plus-24-prior-month scaffolding without expanding source scan scope.

Local OAuth revision: 55 mocked tests cover exact read-only consent arguments, secure atomic 0600 storage, 0700 directory ownership, leaf/ancestor symlink refusal, malformed scope/endpoint preflight, redacted failures, file-token refresh, environment precedence, and bounded Gmail 401 retry. No real authorization flow, consent, persistent grant or network token refresh was performed. Credentials directories contain only README/.gitkeep.
