"""Synthetic, offline coverage for every message-source adapter."""

import base64
import io
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from invoices_scrapper.sources import (
    EmlSource,
    GmailSource,
    ImapSource,
    RawMessage,
    SourceError,
    _mailbox_from_list,
    _NoRedirect,
)


RAW = b"From: shop@example.test\r\nSubject: Invoice\r\n\r\nTotal: $10.00\r\n"


def encoded(raw=RAW):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, bytes):
            return io.BytesIO(response)
        return io.BytesIO(json.dumps(response).encode("utf-8"))


class EmlSourceTests(unittest.TestCase):
    def test_recursive_case_insensitive_and_stable_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.eml").write_bytes(RAW)
            (root / "ignore.txt").write_text("not mail")
            (root / "nested").mkdir()
            (root / "nested" / "b.EML").write_bytes(RAW + b"second")
            source = EmlSource(root)
            first = list(source.messages())
            self.assertEqual([m.raw for m in first], [RAW, RAW + b"second"])
            self.assertEqual(first, list(source.messages()))
            self.assertTrue(all(m.url.startswith("file://") for m in first))

    def test_same_path_changed_content_gets_new_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "one.eml"
            path.write_bytes(RAW)
            first = list(EmlSource(path).messages())[0]
            path.write_bytes(RAW + b"changed")
            second = list(EmlSource(path).messages())[0]
            self.assertNotEqual(first.source_id, second.source_id)

    def test_same_content_different_paths_retains_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("one.eml", "two.eml"):
                (root / name).write_bytes(RAW)
            messages = list(EmlSource(root).messages())
            self.assertEqual(len(messages), 2)
            self.assertNotEqual(messages[0].source_id, messages[1].source_id)

    def test_nonexistent_and_non_eml_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wrong.txt"
            with self.assertRaises(SourceError):
                list(EmlSource(path).messages())
            path.write_text("not mail")
            with self.assertRaises(SourceError):
                list(EmlSource(path).messages())

    def test_unreadable_file_fails_visibly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "one.eml"
            path.write_bytes(RAW)
            with patch.object(Path, "read_bytes", side_effect=PermissionError("secret context")):
                with self.assertRaisesRegex(SourceError, "scan is incomplete") as caught:
                    list(EmlSource(path).messages())
            self.assertNotIn("secret context", str(caught.exception))

    def test_directory_symlink_does_not_silently_skip_mail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "real").mkdir()
            (root / "linked").symlink_to(root / "real", target_is_directory=True)
            with self.assertRaisesRegex(SourceError, "directory symlink"):
                list(EmlSource(root).messages())


class GmailSourceTests(unittest.TestCase):
    def test_all_pages_raw_decoding_and_only_read_only_requests(self):
        http = FakeHTTP([
            {"emailAddress": "owner@example.test"},
            {"messages": [{"id": "one"}], "nextPageToken": "page two/+"},
            {"raw": encoded()},
            {"messages": [{"id": "two"}]},
            {"raw": encoded(RAW + b"\xff\xff")},
        ])
        messages = list(GmailSource("test-token", opener=http).messages())
        self.assertEqual([m.raw for m in messages], [RAW, RAW + b"\xff\xff"])
        self.assertTrue(messages[0].source_id.startswith("gmail:"))
        self.assertIn("owner%40example.test", messages[0].url)
        self.assertIn("#all/one", messages[0].url)
        self.assertEqual(len(http.requests), 5)
        for request, timeout in http.requests:
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
            self.assertEqual(timeout, 60)
            self.assertIsNone(request.data)
        pages = [req for req, _ in http.requests if urlsplit(req.full_url).path.endswith("/messages")]
        self.assertEqual(len(pages), 2)
        for request in pages:
            params = parse_qs(urlsplit(request.full_url).query)
            self.assertEqual(params["includeSpamTrash"], ["true"])
            self.assertEqual(params["maxResults"], ["500"])
            self.assertNotIn("q", params)
            self.assertNotIn("labelIds", params)
        self.assertEqual(parse_qs(urlsplit(pages[1].full_url).query)["pageToken"], ["page two/+"])
        self.assertEqual(parse_qs(urlsplit(http.requests[2][0].full_url).query), {"format": ["raw"]})

    def test_empty_mailbox(self):
        http = FakeHTTP([{"emailAddress": "a@example.test"}, {"resultSizeEstimate": 0}])
        self.assertEqual(list(GmailSource("token", opener=http).messages()), [])

    def test_each_run_reenumerates_instead_of_saving_cursor(self):
        responses = [
            {"emailAddress": "a@example.test"},
            {"messages": [{"id": "one"}]},
            {"raw": encoded()},
        ]
        http = FakeHTTP(responses + responses)
        source = GmailSource("token", opener=http)
        self.assertEqual(list(source.messages()), list(source.messages()))
        self.assertEqual(len(http.requests), 6)

    def test_accounts_have_different_resume_namespaces(self):
        ids = []
        for email in ("one@example.test", "two@example.test"):
            http = FakeHTTP([{"emailAddress": email}, {"messages": [{"id": "same"}]}, {"raw": encoded()}])
            ids.append(next(GmailSource("token", opener=http).messages()).source_id)
        self.assertNotEqual(*ids)

    def test_env_token_required(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SourceError, "GMAIL_ACCESS_TOKEN"):
                GmailSource()
        with patch.dict(os.environ, {"GMAIL_ACCESS_TOKEN": "env-token"}, clear=True):
            http = FakeHTTP([{"emailAddress": "a@example.test"}, {}])
            list(GmailSource(opener=http).messages())
            self.assertEqual(http.requests[0][0].get_header("Authorization"), "Bearer env-token")

    def test_http_errors_are_actionable_without_secrets(self):
        for code in (401, 403, 404, 429, 500, 302):
            with self.subTest(code=code):
                error = HTTPError("https://invalid.test/token-secret", code, "secret-password", {}, None)
                http = FakeHTTP([error])
                with self.assertRaisesRegex(SourceError, f"HTTP {code}") as caught:
                    list(GmailSource("token-secret", opener=http).messages())
                text = str(caught.exception)
                self.assertNotIn("token-secret", text)
                self.assertNotIn("secret-password", text)
                if code == 401:
                    self.assertIn("expired", text)
                    self.assertIn("environment", text)

    def test_fetch_error_is_not_skipped(self):
        http = FakeHTTP([
            {"emailAddress": "a@example.test"},
            {"messages": [{"id": "one"}, {"id": "two"}]},
            HTTPError("https://invalid.test", 404, "missing", {}, None),
            {"raw": encoded()},
        ])
        with self.assertRaisesRegex(SourceError, "scan is incomplete"):
            list(GmailSource("token", opener=http).messages())
        self.assertEqual(len(http.requests), 3)

    def test_network_error_redacts_server_text(self):
        http = FakeHTTP([URLError("secret-token")])
        with self.assertRaises(SourceError) as caught:
            list(GmailSource("secret-token", opener=http).messages())
        self.assertNotIn("secret-token", str(caught.exception))

    def test_incomplete_http_read_fails_visibly(self):
        http = FakeHTTP([IncompleteRead(b"partial-secret-body", 100)])
        with self.assertRaisesRegex(SourceError, "connection failed") as caught:
            list(GmailSource("token", opener=http).messages())
        self.assertNotIn("partial-secret-body", str(caught.exception))

    def test_control_characters_in_token_are_rejected(self):
        for token in ("abc\x00def", "abc\r\ndef", "abc\tdef", "a b", "café"):
            with self.subTest(token=token):
                with self.assertRaises(SourceError):
                    GmailSource(token)

    def test_malformed_falsy_page_token_is_not_treated_as_complete(self):
        for token in (False, 0, []):
            with self.subTest(token=token):
                http = FakeHTTP([{"emailAddress": "a@example.test"}, {"nextPageToken": token}])
                with self.assertRaises(SourceError):
                    list(GmailSource("token", opener=http).messages())

    def test_malformed_json_profile_listing_and_raw_fail(self):
        response_sets = [
            [b"not JSON"],
            [["not a dictionary"]],
            [{}],
            [{"emailAddress": "a@example.test"}, {"messages": {}}],
            [{"emailAddress": "a@example.test"}, {"messages": [{}]}],
            [{"emailAddress": "a@example.test"}, {"messages": [{"id": "a"}]}, {}],
            [{"emailAddress": "a@example.test"}, {"messages": [{"id": "a"}]}, {"raw": "!!!"}],
        ]
        for responses in response_sets:
            with self.subTest(responses=responses):
                with self.assertRaises(SourceError):
                    list(GmailSource("token", opener=FakeHTTP(responses)).messages())

    def test_repeated_page_token_fails_instead_of_hanging(self):
        http = FakeHTTP([
            {"emailAddress": "a@example.test"},
            {"nextPageToken": "repeat"},
            {"nextPageToken": "repeat"},
        ])
        with self.assertRaisesRegex(SourceError, "pagination"):
            list(GmailSource("token", opener=http).messages())

    def test_duplicate_ids_in_pagination_fetch_only_once(self):
        http = FakeHTTP([
            {"emailAddress": "a@example.test"},
            {"messages": [{"id": "one"}], "nextPageToken": "next"},
            {"raw": encoded()},
            {"messages": [{"id": "one"}]},
        ])
        self.assertEqual(len(list(GmailSource("token", opener=http).messages())), 1)

    def test_redirect_handler_does_not_forward_authorization(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test"))


class FakeIMAP:
    def __init__(self):
        self.rows = [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Old Mail"']
        self.calls = []
        self.selected = None
        self.validity = b"1234"
        self.fetch_status = "OK"
        self.fetch_override = None
        self.select_status = "OK"
        self.list_status = "OK"
        self.search_data = [b"10 20"]
        self.login_error = None
        self.logged_out = False

    def login(self, user, password):
        self.calls.append(("login", user, password))
        if self.login_error:
            raise self.login_error
        return "OK", [b"logged in"]

    def list(self, reference, pattern):
        self.calls.append(("list", reference, pattern))
        return self.list_status, self.rows

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        self.selected = mailbox
        return self.select_status, [b"2"]

    def response(self, code):
        self.calls.append(("response", code))
        return code, [self.validity]

    def uid(self, command, *args):
        self.calls.append(("uid", command) + args)
        if command == "SEARCH":
            return "OK", self.search_data
        if command == "FETCH":
            if self.fetch_override is not None:
                return self.fetch_status, self.fetch_override
            uid = args[0]
            header = f"1 (UID {uid} BODY[] {{{len(RAW)}}}".encode("ascii")
            return self.fetch_status, [(header, RAW), b")"]
        raise AssertionError("Unexpected IMAP operation")

    def logout(self):
        self.logged_out = True
        self.calls.append(("logout",))
        return "BYE", [b"logged out"]


class ImapSourceTests(unittest.TestCase):
    def source(self, fake):
        self.connection_args = None

        def factory(*args, **kwargs):
            self.connection_args = (args, kwargs)
            return fake

        return ImapSource("imap.example.test", "owner", "private-password", client_factory=factory)

    def test_every_folder_all_uids_readonly_verified_tls(self):
        fake = FakeIMAP()
        messages = list(self.source(fake).messages())
        self.assertEqual(len(messages), 4)
        self.assertTrue(all(m.raw == RAW for m in messages))
        self.assertEqual(len({m.source_id for m in messages}), 4)
        self.assertIn(":INBOX:1234:10", messages[0].source_id)
        self.assertIn(":Old%20Mail:1234:10", messages[2].source_id)
        self.assertIn(("list", '""', '"*"'), fake.calls)
        selects = [call for call in fake.calls if call[0] == "select"]
        self.assertEqual(selects, [("select", b'"INBOX"', True), ("select", b'"Old Mail"', True)])
        searches = [call for call in fake.calls if call[:2] == ("uid", "SEARCH")]
        self.assertEqual(searches, [("uid", "SEARCH", None, "ALL")] * 2)
        fetches = [call for call in fake.calls if call[:2] == ("uid", "FETCH")]
        self.assertTrue(all(call[3] == "(UID BODY.PEEK[])" for call in fetches))
        args, kwargs = self.connection_args
        self.assertEqual(args, ("imap.example.test", 993))
        self.assertTrue(kwargs["ssl_context"].check_hostname)
        self.assertEqual(kwargs["ssl_context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(fake.logged_out)

    def test_each_run_reenumerates(self):
        fake = FakeIMAP()
        source = self.source(fake)
        self.assertEqual(list(source.messages()), list(source.messages()))
        self.assertEqual(len([c for c in fake.calls if c[0] == "list"]), 2)

    def test_uidvalidity_reset_changes_ids(self):
        fake = FakeIMAP()
        source = self.source(fake)
        first = list(source.messages())
        fake.validity = b"5678"
        second = list(source.messages())
        self.assertTrue(set(m.source_id for m in first).isdisjoint(m.source_id for m in second))

    def test_missing_and_invalid_uidvalidity_fail(self):
        for value in (None, b"", b"0", b"-1", b"not-a-number", b"4294967296"):
            with self.subTest(value=value):
                fake = FakeIMAP()
                fake.validity = value
                with self.assertRaises(SourceError):
                    list(self.source(fake).messages())
                self.assertTrue(fake.logged_out)

    def test_fetch_failure_is_not_skipped(self):
        fake = FakeIMAP()
        fake.fetch_status = "NO"
        with self.assertRaisesRegex(SourceError, "message fetch"):
            list(self.source(fake).messages())
        self.assertEqual(len([c for c in fake.calls if c[:2] == ("uid", "FETCH")]), 1)
        self.assertTrue(fake.logged_out)

    def test_partial_wrong_and_missing_body_fail(self):
        for data in ([None], [], [b")"], [(b"1 (UID 999 BODY[] {3}", b"abc"), b")"],
                     [(b"1 (UID 10 BODY[] {99}", b"abc"), b")"]):
            with self.subTest(data=data):
                fake = FakeIMAP()
                fake.fetch_override = data
                with self.assertRaises(SourceError):
                    list(self.source(fake).messages())

    def test_uid_after_literal_is_supported(self):
        fake = FakeIMAP()
        fake.search_data = [b"10"]
        fake.fetch_override = [(b"1 (BODY[] {3}", b"abc"), b" UID 10)"]
        self.assertEqual([m.raw for m in self.source(fake).messages()], [b"abc", b"abc"])

    def test_empty_search_and_noselect_folders(self):
        fake = FakeIMAP()
        fake.rows.insert(0, b'(\\Noselect \\HasChildren) "/" "Parent"')
        fake.search_data = [b""]
        self.assertEqual(list(self.source(fake).messages()), [])
        self.assertEqual(len([c for c in fake.calls if c[0] == "select"]), 2)

    def test_invalid_search_is_not_treated_as_empty(self):
        for data in ([None], [], [b"0"], [b"1:2"], [b"garbage"]):
            with self.subTest(data=data):
                fake = FakeIMAP()
                fake.search_data = data
                with self.assertRaises(SourceError):
                    list(self.source(fake).messages())

    def test_select_and_list_failures_are_visible(self):
        for field in ("select_status", "list_status"):
            fake = FakeIMAP()
            setattr(fake, field, "NO")
            with self.assertRaises(SourceError):
                list(self.source(fake).messages())
            self.assertTrue(fake.logged_out)

    def test_credentials_from_env_and_missing_validation(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SourceError, "IMAP_HOST"):
                ImapSource()
        fake = FakeIMAP()
        env = {"IMAP_HOST": "imap.example.test", "IMAP_USER": "owner", "IMAP_PASSWORD": "private"}
        with patch.dict(os.environ, env, clear=True):
            list(ImapSource(client_factory=lambda *a, **k: fake).messages())
        self.assertIn(("login", "owner", "private"), fake.calls)

    def test_login_failure_does_not_expose_secrets(self):
        import imaplib

        fake = FakeIMAP()
        fake.login_error = imaplib.IMAP4.error("private-password secret details")
        with self.assertRaisesRegex(SourceError, "login") as caught:
            list(self.source(fake).messages())
        self.assertNotIn("private-password", str(caught.exception))
        self.assertNotIn("secret details", str(caught.exception))
        self.assertTrue(fake.logged_out)

    def test_list_parser_quoted_escaped_literal_and_modified_utf7(self):
        self.assertEqual(_mailbox_from_list(b'() "/" INBOX'), b"INBOX")
        self.assertEqual(_mailbox_from_list(b'() NIL "with space"'), b"with space")
        self.assertEqual(_mailbox_from_list(b'() "/" "quote\\\"slash\\\\"'), b'quote"slash\\')
        self.assertEqual(_mailbox_from_list((b'() "/" {10}', b"Old Mail 2")), b"Old Mail 2")
        self.assertEqual(_mailbox_from_list(b'() "/" "&ZeVnLIqe-"'), b"&ZeVnLIqe-")
        self.assertIsNone(_mailbox_from_list(b'(\\NOSELECT) "/" "Parent"'))
        self.assertIsNone(_mailbox_from_list(b""))

    def test_list_parser_rejects_malformed_responses(self):
        for row in (b"garbage", b'() "/" {10}', (b'() "/" {10}', b"short"), b'() "/" "unclosed'):
            with self.subTest(row=row):
                with self.assertRaises(SourceError):
                    _mailbox_from_list(row)


class RawMessageTests(unittest.TestCase):
    def test_defaults(self):
        message = RawMessage("example:1", RAW)
        self.assertEqual(message.url, "")
        self.assertEqual(message.raw, RAW)


if __name__ == "__main__":
    unittest.main()
