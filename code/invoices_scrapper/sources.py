"""Read-only RFC 5322 message sources using only the standard library.

Every call to ``messages()`` enumerates the entire source again. The consumer,
not a source cursor, owns durable completion state. A failed enumeration or
fetch raises SourceError: a partial run must never be reported as complete.

Gmail needs an existing OAuth access token with gmail.readonly permission in
GMAIL_ACCESS_TOKEN. The default environment mode does not create/save/refresh credentials. An
explicit token_file enables local authorized-user refresh through auth.py;
ordinary scans never open OAuth consent. If an environment token expires, replace it and rerun;
the consumer's successfully committed messages can be resumed safely.

References:
https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list
https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get
https://developers.google.com/workspace/gmail/api/reference/rest/v1/users/getProfile
https://docs.python.org/3/library/imaplib.html
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
from http.client import HTTPException
import imaplib
import json
import os
from pathlib import Path
import re
import ssl
from typing import Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import quote, quote_from_bytes, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class SourceError(RuntimeError):
    """An incomplete or inaccessible source, with credential-safe diagnostics."""


@dataclass(frozen=True)
class RawMessage:
    source_id: str
    raw: bytes
    url: str = ""
    received_at: float | None = None


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class EmlSource:
    """Read one .eml file, or every .eml file recursively in a directory.

    Directory symlinks are not followed (avoids recursive traversal cycles).
    They are rejected explicitly instead of silently skipping possible mail.
    A changed file at the same path receives a new content-aware source ID.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path).expanduser().absolute()

    def _paths(self) -> Iterator[Path]:
        if self.path.is_file():
            if self.path.suffix.lower() != ".eml":
                raise SourceError("The local input file must have an .eml extension.")
            yield self.path
            return
        if not self.path.is_dir():
            raise SourceError("The local .eml input does not exist or is not readable.")

        def walk_error(_error: OSError) -> None:
            raise SourceError("A directory in the .eml input could not be read.") from None

        for directory, dirs, files in os.walk(self.path, onerror=walk_error):
            dirs.sort()
            for name in dirs:
                if (Path(directory) / name).is_symlink():
                    raise SourceError(
                        "The .eml input contains a directory symlink. Provide the "
                        "actual export directory; directory symlinks are not followed."
                    )
            for name in sorted(files):
                if Path(name).suffix.lower() == ".eml":
                    yield Path(directory) / name

    def messages(self) -> Iterator[RawMessage]:
        try:
            for path in self._paths():
                raw = path.read_bytes()
                path_key = _digest(os.fsencode(path.resolve()))
                yield RawMessage(
                    f"eml:{path_key}:{_digest(raw)}", raw, path.resolve().as_uri()
                )
        except OSError:
            raise SourceError(
                "A local .eml file could not be read. Check export paths and permissions; "
                "the scan is incomplete."
            ) from None


class _NoRedirect(HTTPRedirectHandler):
    # Do not forward the bearer token if an unexpected redirect is returned.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GmailSource:
    """Paginated, unfiltered, read-only Gmail API scan, including Spam and Trash.

    ``opener`` is an optional urllib-compatible callable for offline testing.
    The default opener verifies TLS and rejects redirects. Only GET is used.
    """

    API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"

    def __init__(
        self,
        access_token: str | None = None,
        *,
        token_env: str = "GMAIL_ACCESS_TOKEN",
        token_file: str | os.PathLike[str] | None = None,
        start_epoch: float | None = None,
        end_epoch: float | None = None,
        timeout: float = 60,
        opener: Callable | None = None,
    ):
        self._token = access_token if access_token is not None else os.environ.get(token_env)
        self._token_provider=None
        if not self._token and token_file is not None:
            from .auth import TokenProvider,AuthError
            try:self._token_provider=TokenProvider(Path(token_file));self._token=self._token_provider.get()
            except AuthError as exc:raise SourceError(str(exc)) from None
        if not self._token or not re.fullmatch(r"[\x21-\x7e]+", self._token):
            raise SourceError(
                f"Set {token_env} to an existing Gmail OAuth access token with "
                "gmail.readonly scope. No token is created or refreshed automatically."
            )
        if timeout <= 0:
            raise SourceError("The Gmail request timeout must be positive.")
        self._start_epoch=start_epoch;self._end_epoch=end_epoch
        self._timeout = timeout
        self._open = opener if opener is not None else build_opener(_NoRedirect()).open

    def _get(self, path: str, params: dict[str, str] | None = None, *, _retried=False) -> dict:
        if self._token_provider is not None:
            from .auth import AuthError
            try:self._token=self._token_provider.get()
            except AuthError as exc:raise SourceError(str(exc)) from None
        url = self.API_ROOT + path
        if params:
            url += "?" + urlencode(params)
        request = Request(
            url,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with self._open(request, timeout=self._timeout) as response:
                result = json.load(response)
        except HTTPError as error:
            code = error.code
            if code == 401 and self._token_provider is not None and not _retried:
                from .auth import AuthError
                try:self._token=self._token_provider.get(force_refresh=True)
                except AuthError as exc:raise SourceError(str(exc)) from None
                return self._get(path,params,_retried=True)
            if code == 401:
                explanation = (
                    "The access token is expired or invalid. Replace the environment token, "
                    "or explicitly reauthorize your configured local token file, then rerun."
                )
            elif code == 403:
                explanation = (
                    "Check Gmail API access, gmail.readonly scope, and account/API quotas."
                )
            elif code == 429:
                explanation = "The API rate limit was reached. Wait and rerun."
            elif code == 404:
                explanation = "A listed message or mailbox is unavailable; the scan is incomplete."
            else:
                explanation = "The scan is incomplete. Check service availability and rerun."
            raise SourceError(f"Gmail API returned HTTP {code}. {explanation}") from None
        except (URLError, OSError, HTTPException):
            raise SourceError(
                "Gmail API connection failed. Check connectivity and rerun; the scan is incomplete."
            ) from None
        except (ValueError, UnicodeError):
            raise SourceError("Gmail API returned invalid JSON; the scan is incomplete.") from None
        if not isinstance(result, dict):
            raise SourceError("Gmail API returned an unexpected response; the scan is incomplete.")
        return result

    def messages(self) -> Iterator[RawMessage]:
        profile = self._get("/profile")
        email = profile.get("emailAddress")
        if not isinstance(email, str) or "@" not in email or any(c.isspace() for c in email):
            raise SourceError("Gmail profile did not identify the mailbox; refusing unsafe resume IDs.")
        account_id = _digest(email.casefold().encode("utf-8"))
        page_token = None
        page_tokens: set[str] = set()
        seen_ids: set[str] = set()
        while True:
            params = {"maxResults": "500", "includeSpamTrash": "true"}
            bounds=[]
            if self._start_epoch is not None:bounds.append(f"after:{int(self._start_epoch)-1}")
            if self._end_epoch is not None:bounds.append(f"before:{int(self._end_epoch)}")
            if bounds:params["q"]=" ".join(bounds)
            if page_token:
                params["pageToken"] = page_token
            page = self._get("/messages", params)
            entries = page.get("messages", [])
            if not isinstance(entries, list):
                raise SourceError("Gmail message listing was malformed; the scan is incomplete.")
            for entry in entries:
                message_id = entry.get("id") if isinstance(entry, dict) else None
                if not isinstance(message_id, str) or not message_id:
                    raise SourceError("Gmail listed a message without an ID; the scan is incomplete.")
                if message_id in seen_ids:
                    continue
                data = self._get("/messages/" + quote(message_id, safe=""), {"format": "raw"})
                encoded = data.get("raw")
                if not isinstance(encoded, str) or not encoded:
                    raise SourceError("Gmail omitted the raw message body; the scan is incomplete.")
                try:
                    raw = base64.b64decode(
                        encoded.encode("ascii") + b"=" * (-len(encoded) % 4),
                        altchars=b"-_",
                        validate=True,
                    )
                except (ValueError, UnicodeError, binascii.Error):
                    raise SourceError("Gmail returned invalid raw message data; the scan is incomplete.") from None
                seen_ids.add(message_id)
                yield RawMessage(
                    f"gmail:{account_id}:{message_id}",
                    raw,
                    f"https://mail.google.com/mail/?authuser={quote(email, safe='')}#all/"
                    + quote(message_id, safe=""),
                    float(data["internalDate"])/1000 if str(data.get("internalDate","")).isdigit() else None,
                )
            page_token = page.get("nextPageToken")
            if page_token is None or page_token == "":
                return
            if not isinstance(page_token, str) or page_token in page_tokens:
                raise SourceError("Gmail pagination did not advance; the scan is incomplete.")
            page_tokens.add(page_token)


_LIST_ROW = re.compile(rb'^\(([^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(.+)$', re.I | re.S)
_LITERAL = re.compile(rb"\{(\d+)\+?\}$")


def _mailbox_from_list(row) -> bytes | None:
    """Parse LIST names without altering modified UTF-7 bytes or quoted escapes."""
    if row is None or row == b"":
        return None
    literal = None
    if isinstance(row, tuple) and len(row) == 2:
        row, literal = row
    if not isinstance(row, bytes):
        raise SourceError("IMAP returned an unsupported mailbox listing; the scan is incomplete.")
    match = _LIST_ROW.fullmatch(row)
    if not match:
        raise SourceError("IMAP returned a malformed mailbox listing; the scan is incomplete.")
    flags, name = match.groups()
    if b"\\noselect" in flags.lower().split():
        return None
    if literal is not None:
        size = _LITERAL.fullmatch(name)
        if not size or not isinstance(literal, bytes) or len(literal) != int(size.group(1)):
            raise SourceError("IMAP returned an invalid mailbox literal; the scan is incomplete.")
        name = literal
    elif name.startswith(b'"'):
        if not re.fullmatch(rb'"(?:[^"\\]|\\["\\])*"', name):
            raise SourceError("IMAP returned an invalid quoted mailbox name; the scan is incomplete.")
        name = re.sub(rb'\\(["\\])', rb'\1', name[1:-1])
    elif _LITERAL.fullmatch(name):
        raise SourceError("IMAP omitted a mailbox-name literal; the scan is incomplete.")
    if not name or any(char in name for char in (b"\r", b"\n", b"\0")):
        raise SourceError("IMAP returned an unsupported mailbox name; the scan is incomplete.")
    return b"INBOX" if name.upper() == b"INBOX" else name


def _positive_number(value: bytes, description: str) -> str:
    if not re.fullmatch(rb"[0-9]{1,10}", value) or not 0 < int(value) <= 4294967295:
        raise SourceError(f"IMAP returned an invalid {description}; the scan is incomplete.")
    return str(int(value))


class ImapSource:
    """Scan every selectable IMAP folder over verified TLS, without changing mail.

    Credentials default to IMAP_HOST, IMAP_USER and IMAP_PASSWORD. An app-specific
    password may be required by the provider. Uses LIST (not subscribed folders),
    read-only SELECT/EXAMINE, UID SEARCH ALL and UID FETCH BODY.PEEK[]. There is no
    CLOSE, STORE, EXPUNGE, MOVE, subscription change, or flag change.

    IDs include the server/account, exact mailbox name, UIDVALIDITY and UID. An
    IMAP mailbox reset therefore cannot accidentally reuse old completion IDs.
    """

    def __init__(
        self,
        host: str | None = None,
        user: str | None = None,
        password: str | None = None,
        *,
        port: int = 993,
        timeout: float = 60,
        client_factory: Callable | None = None,
    ):
        self._host = host if host is not None else os.environ.get("IMAP_HOST")
        self._user = user if user is not None else os.environ.get("IMAP_USER")
        self._password = password if password is not None else os.environ.get("IMAP_PASSWORD")
        if not self._host or not self._user or not self._password:
            raise SourceError("Set IMAP_HOST, IMAP_USER and IMAP_PASSWORD before using IMAP.")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise SourceError("The IMAP SSL port must be between 1 and 65535.")
        if timeout <= 0:
            raise SourceError("The IMAP connection timeout must be positive.")
        self._port = port
        self._timeout = timeout
        self._factory = client_factory if client_factory is not None else imaplib.IMAP4_SSL

    @staticmethod
    def _call(stage: str, method, *args, **kwargs):
        try:
            status, data = method(*args, **kwargs)
        except (imaplib.IMAP4.error, OSError, ValueError, UnicodeError):
            raise SourceError(f"IMAP {stage} failed; the scan is incomplete.") from None
        if status not in ("OK", b"OK"):
            raise SourceError(f"IMAP {stage} was rejected; the scan is incomplete.")
        return data

    @staticmethod
    def _body(data, uid: str) -> bytes:
        if not isinstance(data, list):
            raise SourceError("IMAP returned no message data; the scan is incomplete.")
        bodies = []
        metadata = []
        for part in data:
            if isinstance(part, tuple) and len(part) == 2:
                header, body = part
                if not isinstance(header, bytes):
                    continue
                metadata.append(header)
                marker = re.search(rb"(?:^|[\s(])BODY\[\]\s+\{(\d+)\}$", header, re.I)
                if marker:
                    if not isinstance(body, bytes) or len(body) != int(marker.group(1)):
                        raise SourceError("IMAP returned a truncated message body; the scan is incomplete.")
                    bodies.append(body)
            elif isinstance(part, bytes):
                metadata.append(part)
        uids = re.findall(rb"(?:^|[\s(])UID\s+(\d+)(?=[\s)]|$)", b" ".join(metadata), re.I)
        if len(bodies) != 1 or {item.decode("ascii") for item in uids} != {uid}:
            raise SourceError(
                "IMAP did not return the requested UID and full message body; the scan is incomplete."
            )
        return bodies[0]

    def messages(self) -> Iterator[RawMessage]:
        try:
            client = self._factory(
                self._host,
                self._port,
                ssl_context=ssl.create_default_context(),
                timeout=self._timeout,
            )
        except (imaplib.IMAP4.error, OSError, ValueError):
            raise SourceError("IMAP TLS connection failed. Check the host, port and certificate.") from None
        try:
            self._call("login", client.login, self._user, self._password)
            rows = self._call("folder listing", client.list, '""', '"*"')
            if not isinstance(rows, list):
                raise SourceError("IMAP omitted its folder listing; the scan is incomplete.")
            account_id = _digest(
                f"{self._host.casefold()}\0{self._port}\0{self._user}".encode("utf-8")
            )
            seen_mailboxes: set[bytes] = set()
            for row in rows:
                mailbox = _mailbox_from_list(row)
                if mailbox is None or mailbox in seen_mailboxes:
                    continue
                seen_mailboxes.add(mailbox)
                quoted = b'"' + mailbox.replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'
                self._call("read-only folder selection", client.select, quoted, readonly=True)
                try:
                    _, values = client.response("UIDVALIDITY")
                except (imaplib.IMAP4.error, OSError, ValueError):
                    raise SourceError("IMAP UIDVALIDITY lookup failed; the scan is incomplete.") from None
                if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], bytes):
                    raise SourceError("IMAP omitted UIDVALIDITY; refusing unsafe resume IDs.")
                validity = _positive_number(values[0], "UIDVALIDITY")
                results = self._call("UID search", client.uid, "SEARCH", None, "ALL")
                if not isinstance(results, list) or not results or any(not isinstance(r, bytes) for r in results):
                    raise SourceError("IMAP omitted UID search results; the scan is incomplete.")
                seen_uids: set[str] = set()
                for value in b" ".join(results).split():
                    uid = _positive_number(value, "UID")
                    if uid in seen_uids:
                        continue
                    seen_uids.add(uid)
                    data = self._call("message fetch", client.uid, "FETCH", uid, "(UID BODY.PEEK[])")
                    raw = self._body(data, uid)
                    source_id = (
                        f"imap:{account_id}:{quote_from_bytes(mailbox, safe='')}:"
                        f"{validity}:{uid}"
                    )
                    yield RawMessage(source_id, raw)
        finally:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                # A disconnected socket during logout cannot mutate mail and
                # must not hide a more useful enumeration/fetch error.
                pass
