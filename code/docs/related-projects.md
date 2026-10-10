# Related projects and design choices

Primary documentation reviewed during development:

- [invoice2data](https://github.com/invoice-x/invoice2data): PDF-to-structured-data/CSV extraction, pluggable text/OCR readers, YAML/JSON templates. A useful future deterministic supplier-template adapter; not copied or added as a dependency.
- [Paperless-ngx usage](https://docs.paperless-ngx.com/usage/): original-document preservation, OCR, email ingestion and content-checksum duplicate handling. Inspired the audit-first separation of originals from extracted data. Checksum deduplication does not alone identify one purchase represented by an invoice and a different receipt.
- [paperless-gmail-receipts](https://github.com/jamesj2/paperless-gmail-receipts): Gmail IMAP, Puppeteer HTML-to-PDF, SHA-256 deduplication. Its unread/subject filtering and read/archive mutations are unsuitable for this read-only full-scan requirement. This implementation neither uses those filters nor changes mailbox flags.
- [OCRmyPDF](https://ocrmypdf.readthedocs.io/en/latest/): adds searchable text layers to scanned PDFs. Suitable as a future optional adapter; current local OCR uses Poppler and Tesseract and preserves originals.

Our custom `id_consumption` addresses a separate accounting problem: linking financial evidence for a logical purchase while preserving every original. Byte-identical documents can be recognized mechanically; overlapping invoices, receipts, payment confirmations and statements require explicit references and conservative review. No project code was copied.
