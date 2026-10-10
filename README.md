# Invoices

A modular, read-only email invoice and expense extraction project. The Python application, LLM prompts, synthetic examples, tests, and setup documentation live in [`code/`](code/README.md).

```sh
cd code
python -m venv .venv
. .venv/bin/activate
pip install -e ".[render,auth]"
python -m unittest discover -s tests -v
python -m invoices_scrapper --source eml --input examples/mail --config config.example.json
```

See the [project README](code/README.md) for Gmail OAuth, IMAP, model-provider configuration, OCR, date ranges, resume behavior, and limitations. The offline mock extractor is a synthetic demonstration, not a real invoice model.

## Private output stays local

When this repository is cloned as `Invoices` and commands run inside `Invoices/code`, generated evidence belongs under `Invoices/YYYY/MM` and reports under `Invoices/reports`. These outputs are excluded from Git. If using a differently named checkout, explicitly choose your private archive location with `--output` and ensure it is excluded from version control.

Only generic code and synthetic test mail are published here. Never commit real mail, financial documents, generated reports, OAuth client secrets, tokens, or API keys. Review staged files before every push. OAuth authorization is performed locally by the account owner.
