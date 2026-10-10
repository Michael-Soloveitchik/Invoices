# Local credentials directory

This directory intentionally contains no credentials or example secrets.

On your own computer, create/download a Google OAuth **Desktop app** client using Google's official setup guide, then save the downloaded file here as `client_secret.json`:
https://developers.google.com/workspace/gmail/api/quickstart/python

From `Invoices/code` run:

    pip install -e ".[auth]"
    python -m invoices_scrapper auth --credentials-dir ../credentials

You must review and approve Google's local browser consent yourself. The command requests only gmail.readonly and creates `token.json` locally. Never upload either JSON file to chat, Library or GitHub. The previously connected Gmail integration's credentials cannot be exported for this program.

The helper requires a private macOS/Linux POSIX directory, uses directory mode 0700 and file mode 0600, rejects symlinks and unexpected scopes/endpoints, and never begins consent from an ordinary scan. It can refresh an already-authorized token when explicitly configured using `gmail.token_file`.

Configure `"gmail": {"token_file": "../credentials/token.json"}` in your local configuration. Environment access tokens take precedence. See the code README for date scopes, transmission consent and reauthorization instructions.

Keep only this README and `.gitkeep` in source control. Real credentials remain on your computer.
