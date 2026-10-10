# Folder layout

The archive has one case-sensitive root:

```text
Invoices/
  code/       Python project, prompts, tests and documentation
  reports/    expenses.csv, documents.csv, review.csv and private resume state
  2024/10/    Evidence folders (example scaffold)
  ...
  2026/10/
```

The supplied ZIP puts this project under `Invoices/code`. Run installation and commands from that directory. Without `--output`, running inside `Invoices/code` uses its parent `Invoices`; elsewhere the CLI creates `Invoices` in the current working directory. `--output` explicitly selects a different archive root. Each run scaffolds the current month and preceding 24 months; empty folders do not imply those months were scanned. Only the requested date scope is read/extracted. Originals use source/content-version subfolders inside each month to prevent filename collisions; no per-consumption folder is assumed.

```sh
cd Invoices/code
python -m venv .venv
. .venv/bin/activate
pip install -e ".[render]"
python -m invoices_scrapper --source eml --input examples/mail --config config.example.json
```

Only code may be pushed to GitHub. Root and code `.gitignore` files exclude year folders, reports, originals and credentials. An explicit different output directory must be excluded separately.

# invoices_scrapper

A modular, read-only Python email-to-expenses pipeline with an audit trail. Gmail is the primary live source; IMAP and local `.eml` archives also work. It scans **every enumerated message**, including Gmail spam/trash, rather than silently limiting coverage to invoice keywords. It preserves source material and exports reviewable financial evidence.

This is an extraction assistant, **not guaranteed-complete bookkeeping or proof of payment**. Run a small sample and reconcile important totals against your accounts. A clean run means enumeration/extraction completed under configured limits, not that every expense was identified.

## Quick start (fully offline synthetic demo)

Requires Python 3.11+. Pillow is required for mandatory inline receipt PNG capture; source adapters, JSON LLM calls and CSV otherwise use the standard library.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e .
python -m invoices_scrapper --source eml --input examples/mail --config config.example.json --output ..
python -m unittest discover -s tests -v
```

The default `mock` extractor recognizes synthetic key-value examples only. It does **not** understand arbitrary invoices and is not a substitute for a real model. No credentials or external calls are needed for this demo.

## Outputs

- `expenses.csv`: requested one-row-per-expense view with amounts, payment dates/status, period, category and review/countable flags. Unknown amounts are empty cells, not zero.
- `documents.csv`: requested evidence view with document/order IDs, email/file links, content hashes and extraction/match reasons. One document can link to multiple expenses.
- `evidence.csv`: every extracted financial record, supplier/category, document and logical-consumption IDs, amounts, status, confidence, exact supporting quote and provenance.
- `consumptions.csv`: one row per conservatively linked expense; **only sum rows with `include_in_totals=yes`, separately by currency and with explicit treatment of refunds/status**. It is not a cash-flow ledger; refunds are positive magnitudes and must be interpreted using evidence `kind/status`. No exchange-rate conversion or blanket sum is performed.
- `review.csv`: extraction errors, missing tools, linked-only receipts, conflicting totals/statuses, low confidence and possible duplicates.
- `Invoices/YYYY/MM/<source-hash>/<raw-content-version>/`: original `.eml`, original attachments, headers, source metadata, inline UTF-8 text, safe text-reconstructed HTML and mandatory PNG and optional PDF. Original HTML is saved as `.txt` so it does not execute on opening.
- `Invoices/reports/state.sqlite3`: processed-message hashes, evidence, issues and runs. `Invoices/reports/last-run.json` reports seen/processed/skipped/failed/document/evidence/review counts and whether enumeration completed.

All CSV files are saved in `Invoices/reports/`. CSV uses UTF-8 with BOM (Excel/Hebrew friendly) and neutralizes leading spreadsheet formulas. Evidence rows intentionally repeat when the same document appears in multiple messages; do not sum evidence rows. All source links are preserved in their respective evidence rows. Gmail links are account-dependent; use the correct signed-in account.

## Gmail (read-only)

1. In your own Google Cloud project, enable Gmail API and configure OAuth with the **`https://www.googleapis.com/auth/gmail.readonly`** scope. Obtain an access token for your account using Google's supported OAuth setup. The default environment-token mode does not generate/store refresh tokens. An optional explicit local `auth` command supports user-approved OAuth and refresh; see Local OAuth below. Ordinary scans never start consent.
2. Export `GMAIL_ACCESS_TOKEN` in your local shell. Never paste it into chat, commit it, or put it in config JSON.
3. Select a real LLM provider below or test a local export first.

```sh
export GMAIL_ACCESS_TOKEN='your-short-lived-token'
python -m invoices_scrapper --source gmail --config config.local.json --output .. --allow-external-llm
```

Environment access tokens expire (often after about an hour). A failed page/fetch stops enumeration visibly, retains completed work, and returns nonzero. Renew the environment token and rerun, or configure the optional local OAuth token file for automatic refresh; completed unchanged messages are skipped. All Gmail pages are enumerated afresh on each run, so new mail cannot be skipped by a stale pagination checkpoint. This is resumable processing, not a point-in-time Gmail snapshot; mail arriving/deleting during a scan may require another scan. No filters or keyword-only fast mode are implemented. Large mailboxes can be slow and costly with an external model.

Official references: [Gmail list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list), [raw message get](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get), [OAuth scopes](https://developers.google.com/workspace/gmail/api/auth/scopes).

## IMAP / local email

IMAP requires `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD`; use an app password if your provider requires it. It uses TLS, read-only mailbox selection and `BODY.PEEK[]`, preserving unread flags. It enumerates selectable folders, and IDs include mailbox UIDVALIDITY to avoid UID reuse corruption. Virtual/all-mail folders can overlap; evidence is preserved and explicit document references/content hashes can link repeats. Folder/provider quirks may require local `.eml` export. Passwords remain in environment only; OAuth IMAP is not implemented.

```sh
python -m invoices_scrapper --source imap --config config.local.json --allow-external-llm
python -m invoices_scrapper --source eml --input private-input --config config.local.json --allow-external-llm
```

## LLM choice and editable prompts

Copy `config.example.json` to `config.local.json`. Use `llm.provider` = `anthropic`, `openai`, `openai-compatible` or `mock`, and explicitly choose a model available in your account. Model names/capabilities change; there is no hidden default production model.

```json
{
  "llm": {
    "provider": "anthropic",
    "model": "YOUR_CLAUDE_MODEL_ID",
    "max_input_chars": 60000,
    "max_tokens": 6000
  },
  "ocr": true,
  "render_formats": ["png"],
  "max_pdf_pages": 30,
  "supplier_aliases": {
    "example ltd": {"supplier_id": "sup_example", "category": "software"}
  }
}
```

Export `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. For an OpenAI-compatible server, set `base_url` and, if necessary, `api_key_env` to the name of an environment variable. Defaults target official providers. Check endpoint ownership before sending receipts. Configure model structured-output support using provider options documented in `extraction.py`. Custom endpoints may not implement schema enforcement; all outputs still undergo local validation.

Prompts live in `invoices_scrapper/prompts/system.txt`, separately from code. `llm.prompt_path` can select another local UTF-8 prompt. It instructs the model to treat mail as untrusted data, distinguish English/Hebrew payment evidence, return logical purchases rather than duplicate line-item/mobile-layout copies, and use unknown where information is missing. No model can guarantee prompt-injection resistance or accurate financial extraction; review output.

**External transmission is off by default.** `--allow-external-llm` explicitly authorizes sending ALL scanned message bodies and extracted attachment text, including non-financial mail, to the configured endpoint. Scanned text can include addresses, personal financial/health information and other sensitive data. Obtain all required consent and use a suitable provider/data policy. The flag is required even for a locally configured compatible endpoint so a mistaken URL cannot silently leak documents. Binary originals and full email headers are not sent by this pipeline. Oversized text fails visibly rather than being silently truncated.

Changing prompts/model/OCR/settings does not invalidate completed messages automatically. Use `--reprocess` to re-extract them. This can incur model costs. Failed extractions are retried next run. Old evidence retained after a failed reprocess is marked stale and excluded from countable totals.

## PDF, OCR and inline receipt images

- Text PDFs: install Poppler (`pdftotext`, `pdftoppm`) using your OS's trusted package manager.
- Scanned PDFs/images: install Tesseract and both English **and Hebrew** language packs (`eng`, `heb`), then set `ocr: true`. OCR is local and fallible; the pipeline preserves originals. It OCRs PDFs when extracted text is nearly empty; mixed scanned/text pages can still be missed and need review.
- PNG/PDF text reconstructions: `pip install -e '.[render]'`, install DejaVu fonts, and optionally set `render_formats: ["pdf"]`; PNG generation is always attempted, regardless of this setting.
- Inline rendering never starts a browser or executes JavaScript. With the render extra installed, WeasyPrint 70 preserves sanitized HTML tables and embedded CSS, generates PDF, then PyMuPDF renders PNG pages. A deny-all URL fetcher blocks HTTP, file, data and every other resource URL; images and active elements are removed. This is a **rendered email capture**, not a screenshot of the Gmail interface or a guaranteed pixel-identical original. Without these dependencies or on a render error, Pillow generates an explicitly labelled text fallback. Hebrew UTF-8 originals are authoritative; PDF right-to-left layout can need review. PNG text shaping depends on Pillow/font support.
- Processing limits: attachments over `max_attachment_bytes` are preserved but not parsed. PDFs process at most `max_pdf_pages`; PDFs exceeding the limit or with an unverified page count get a review notice. Tool timeouts and unsupported/encrypted/corrupt documents are reported. Originals are never deleted.
- Links in email are recorded in `links-review.json` and flagged; **nothing follows them**, including signed receipt URLs, trackers and shortened links. Download linked-only receipts yourself or request a narrowly allowlisted authenticated downloader in a future extension.

## Supplier identity and overlapping duplicates

`file_hash` is the full SHA-256 identity of original attachment bytes or combined inline text. `document_id` uses supplier, document kind and document number when present, otherwise the content hash. `supplier_id` uses an explicit tax ID when available, otherwise normalized supplier name. `supplier_aliases` is the reviewed way to resolve spelling variants, assign a stable supplier ID, and attach a category. Names alone are not legal identity verification; tax-ID presence/absence can create separate IDs until aliases are curated.

`id_consumption` links explicit invoice references within supplier and currency; an explicitly referenced invoice in a receipt can link to its invoice. A receipt's own document number is kept separately. No merging is based on thread, sender, date/amount, or supplier alone. In particular, two receipts in the same email thread with different order/document IDs remain distinct. Without explicit references, exact document identity plus record position provides stable linkage. Exact repeated extraction objects within one document link to the same logical record; all evidence remains visible.

Same supplier/date/amount with distinct identifiers becomes a review candidate, not an automatic merge. Different totals or statuses within a linked consumption trigger review; partial-payment amounts are **never blindly added**, and generic email text cannot silently override contradictory attachment evidence. The model extracts each source separately; conflict resolution is deliberately manual. Refunds have positive magnitudes and refund status/kind. Unknown payment status stays unknown. Categories are suggestions unless set in reviewed aliases.

## Architecture and testing

- `sources.py`: local/Gmail/IMAP iteration of raw messages.
- `documents.py`: MIME preservation, HTML-to-text, attachment text/OCR and safe renderers.
- `extraction.py` and prompts: provider interfaces, structured validation, external-data gate.
- `pipeline.py`: SQLite resume, conservative IDs/linking, audit/review and CSV exports.
- `cli.py`: configuration, source selection, consent flag and exit codes.

Tests use synthetic email and mocked network/provider responses. Live Gmail/IMAP auth, real LLM accuracy, large-mailbox quotas, heterogeneous PDFs, OCR accuracy and Hebrew visual layout require account-specific validation. No real user mail or secrets are included.

## Security and operations

Run locally on a trusted machine with updated PDF/image tools; malicious attachments can exploit native parsers. For untrusted mail, use an OS/container sandbox with filesystem, CPU and memory limits. Subprocess timeouts are not a complete sandbox. Set restrictive output permissions (for example `umask 077`), use encrypted storage, and retain/delete exports according to your policy. This tool does not encrypt files or redact personal data. Do not put private output into a public repo; `.gitignore` covers default paths, **not arbitrary custom output folders**. Do not open original attachments unless you trust them. Concurrent runs against one output directory are unsupported; use a single process. No paid API requests, real mailbox scans, credential acquisition or repository publication were performed during development.

## Archive dates and evidence versions

Extracted documents are copied into `Invoices/YEAR/MONTH` based on the unique extracted document date. If absent or conflicting, use the email Date header; if unavailable, use `unknown/unknown`. Original mail stays in its email-date archive. Content-versioned directories ensure a failed reprocess cannot overwrite evidence referenced by older rows. Multiple inline PNG pages are saved alongside inline text; rendering failures go to review.csv; if no mandatory PNG exists the message fails and remains retryable, rather than committing as processed. The `capture_kind` and `rendered_files` columns identify `html_render` versus `text_fallback` and link saved images. Sanitized HTML renders preserve much of the email layout but omit remote assets and active content. Text fallbacks are always marked for review, never described as original screenshots.

Payment status claims lacking a dedicated literal `payment_evidence` quote are flagged for review. Quotes still require human interpretation, especially conditional receipt language and past-period payments. Grouping with conflicting document dates, tax-ID availability, totals or payment histories remains manual.

### HTML rendering dependencies

Install `pip install -e ".[render]"` for WeasyPrint 70, PyMuPDF and optional text PDF support. WeasyPrint may also require platform Pango/font libraries; see its [official installation guide](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html). The renderer uses the [official URLFetcher API](https://doc.courtbouillon.org/weasyprint/stable/api_reference.html) with every resource denied. HTML rendering is isolated in a subprocess with a 60-second timeout and size/page limits, but it is not an OS security sandbox. Preserve the original HTML for review; sanitization necessarily changes appearance.

## Explicit date windows and small samples

Use timezone-aware timestamps; `--start` is inclusive, `--end` exclusive. For example, August 10 through October 10 in UTC+03:00:

```sh
python -m invoices_scrapper --source gmail --config config.local.json --start '2026-08-10T00:00:00+03:00' --end '2026-10-11T00:00:00+03:00' --output ../Invoices-aug-oct --allow-external-llm
```

Equivalent config: `"scope": {"start": "2026-08-10T00:00:00+03:00", "end": "2026-10-11T00:00:00+03:00", "max_messages": 25}`. CLI values override config. No date flags means full scan. Gmail queries use epoch seconds, avoiding Gmail's implicit date-string timezone; exact boundaries are checked again against Gmail's internal received timestamp. For local `.eml` and IMAP, the filter uses the email's timezone-aware **Date header**, not server receive time; altered/misdated headers can affect inclusion. IMAP/local sources still enumerate/read mail to apply the local filter, but excluded text is never passed to the LLM. Missing/invalid dates are flagged, not transmitted, and mark coverage incomplete.

`--max-messages 25` explicitly caps an initial sample. If another matching message exists, the manifest sets `sample_truncated=true`, `scan_complete=false`, and the CLI returns nonzero. Remove the cap to complete the same scope; processed messages resume normally. Unknown dates and source errors also keep completion false. `Invoices/reports/last-run.json` records effective window, date basis, excluded count and sample state. Date-scoped CSV exports contain only source IDs enumerated in that run's scope, even if the SQLite store contains older broader scans. Prefer separate output directories for separate audits.

## Local OAuth (optional, user-run only)

The connected Gmail integration used for the separate audit is **not this Python program**. Its credentials cannot be exported into this project. No real credentials, token file or consent were created during development. The ZIP includes only an empty `Invoices/credentials` folder with instructions.

1. On your own computer, follow [Google’s official desktop OAuth setup](https://developers.google.com/workspace/gmail/api/quickstart/python): enable Gmail API, configure the Google Auth consent screen/audience, and create an OAuth client of type **Desktop app**. Add your account as a test user if your application is in testing. The scope this application requests is only `https://www.googleapis.com/auth/gmail.readonly`.
2. Download the client JSON yourself and save it locally as `Invoices/credentials/client_secret.json`. Do not send it in chat or upload it to GitHub/Library.
3. From `Invoices/code`, install optional dependencies and run the explicit authorization command:

```sh
pip install -e ".[auth]"
python -m invoices_scrapper auth --credentials-dir ../credentials
```

The command opens Google consent in your local browser and listens only on loopback (`127.0.0.1`, a temporary port). Review and approve it yourself. This creates persistent read-only Gmail access and saves `Invoices/credentials/token.json` with a refresh token. It requests offline access, does not request send/modify permissions, and suppresses the authorization URL and secrets in console output. If browser opening fails or consent is canceled/times out, rerun locally; do not paste redirect URLs or authorization codes into chat.

4. Add a local config setting (path is relative to the process working directory):

```json
{"gmail": {"token_file": "../credentials/token.json"}, "llm": {"provider": "anthropic", "model": "YOUR_MODEL_ID"}}
```

Then use the normal Gmail scan command and explicit date scope. Configuring `token_file` allows the program to refresh existing access with Google and atomically update that same token file. A rejected access token triggers at most one forced refresh/retry per request. Ordinary scans never open consent or broaden scopes. Missing, revoked, unexpected-scope or unsafe-permission credentials fail with redacted errors. Explicit access-token arguments and `GMAIL_ACCESS_TOKEN` take precedence over a configured token file; unset a stale environment token to use the file.

`auth` refuses to replace an existing token unless you intentionally run `--replace-token`. Only use that flag when you want new consent. Revoke unused access through your Google account’s security settings. Tokens for test-mode OAuth apps can expire/revoke according to Google’s current rules.

Local token storage currently requires POSIX owner-only permissions (macOS/Linux): credentials directory 0700, files 0600. The bootstrap sets these modes; existing token files with broader modes are refused. Use a dedicated private folder, encrypted storage and your own OS account. On Windows, use the existing environment-token route until ACL-aware secure storage is implemented. Files are not encrypted by this application. Nested credential files are ignored by Git except README/.gitkeep; ignore rules are a guardrail, not permission to publish secrets.

References: [Google InstalledAppFlow](https://google-auth-oauthlib.readthedocs.io/en/latest/reference/google_auth_oauthlib.flow.html), [Google credential refresh API](https://google-auth.readthedocs.io/en/latest/reference/google.oauth2.credentials.html).
