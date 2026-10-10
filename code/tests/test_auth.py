"""Offline OAuth security tests using synthetic credentials and fake Google clients.

No test opens a browser, performs network I/O, or reads a real credential file.
Every credential-shaped fixture lives only under a TemporaryDirectory.
"""

from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import traceback
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from invoices_scrapper import auth
from invoices_scrapper.sources import GmailSource, SourceError


READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SYNTHETIC_SECRET = "synthetic-secret-never-a-real-credential"


def client_config():
    return {
        "installed": {
            "client_id": "synthetic-client.apps.googleusercontent.com",
            "project_id": "synthetic-offline-test",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_secret": SYNTHETIC_SECRET,
            "redirect_uris": ["http://localhost"],
        }
    }


def token_info(**changes):
    result = {
        "type": "authorized_user",
        "token": "synthetic-access-token",
        "refresh_token": "synthetic-refresh-token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "synthetic-client.apps.googleusercontent.com",
        "client_secret": SYNTHETIC_SECRET,
        "scopes": [READONLY_SCOPE],
        "expiry": "2099-01-01T00:00:00Z",
    }
    result.update(changes)
    return result


class FakeCredentials:
    def __init__(self, *, expired=False, valid=True, refresh_token="synthetic-refresh-token"):
        self.token = "synthetic-access-token"
        self.refresh_token = refresh_token
        self.valid = valid
        self.expired = expired
        self.scopes = [READONLY_SCOPE]
        self.granted_scopes = [READONLY_SCOPE]
        self.refresh_calls = []
        self.refresh_error = None

    def has_scopes(self, scopes):
        return set(scopes).issubset(self.scopes)

    def to_json(self):
        return json.dumps(token_info(token=self.token, refresh_token=self.refresh_token))

    def refresh(self, request):
        self.refresh_calls.append(request)
        if self.refresh_error is not None:
            raise self.refresh_error
        self.token = "synthetic-refreshed-access-token"
        self.valid = True
        self.expired = False


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.client_file = self.root / "desktop-client.json"
        self.client_file.write_text(json.dumps(client_config()), encoding="utf-8")
        self.client_file.chmod(0o600)
        self.token_file = self.root / "private" / "gmail-token.json"
        self.credentials = FakeCredentials()
        self.flow = Mock(name="flow")
        self.flow.run_local_server.return_value = self.credentials
        self.flow_class = Mock(name="InstalledAppFlow")
        self.flow_class.from_client_config.return_value = self.flow
        self.credentials_class = Mock(name="Credentials")
        self.credentials_class.from_authorized_user_info.return_value = self.credentials
        self.request = object()
        self.request_class = Mock(name="Request", return_value=self.request)
        self.libraries = self.stack.enter_context(
            patch.object(auth, "_libraries", return_value=(
                self.flow_class, self.credentials_class, self.request_class,
            ))
        )
        # A regression that bypasses the patched optional libraries must fail
        # without making a real connection or launching a real browser.
        self.stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("Network is forbidden in OAuth tests")))
        self.stack.enter_context(patch("webbrowser.open", side_effect=AssertionError("A real browser is forbidden in OAuth tests")))

    def save_token(self, info=None):
        self.token_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps(token_info() if info is None else info), encoding="utf-8")
        self.token_file.chmod(0o600)
        return self.token_file.read_bytes()

    def assert_redacted(self, error):
        rendered = "".join(traceback.format_exception(error))
        for secret in (SYNTHETIC_SECRET, "synthetic-refresh-token", "synthetic-access-token"):
            self.assertNotIn(secret, str(error))
            self.assertNotIn(secret, rendered)

    def assert_no_consent(self):
        self.flow_class.from_client_config.assert_not_called()
        self.flow.run_local_server.assert_not_called()

    def test_public_contract_is_read_only(self):
        self.assertEqual(auth.SCOPES, (READONLY_SCOPE,))
        self.assertTrue(issubclass(auth.AuthError, ValueError))

    def test_authorize_uses_desktop_config_and_loopback_consent(self):
        before = self.client_file.read_bytes()
        auth.authorize(self.client_file, self.token_file)
        self.flow_class.from_client_config.assert_called_once_with(client_config(), scopes=[READONLY_SCOPE])
        self.flow.run_local_server.assert_called_once_with(
            host="127.0.0.1",
            port=0,
            open_browser=True,
            authorization_prompt_message="Open the Google consent page in your browser.",
            success_message="Authorization complete. You can close this window.",
            access_type="offline",
            prompt="consent",
            include_granted_scopes="false",
            timeout_seconds=300,
        )
        saved = json.loads(self.token_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["token"], self.credentials.token)
        self.assertEqual(saved["scopes"], [READONLY_SCOPE])
        self.assertEqual(stat.S_IMODE(self.token_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.token_file.parent.stat().st_mode), 0o700)
        self.assertEqual(self.client_file.read_bytes(), before)

    def test_existing_token_requires_explicit_replace_without_consent(self):
        before = self.save_token()
        with self.assertRaises(auth.AuthError):
            auth.authorize(self.client_file, self.token_file)
        self.assertEqual(self.token_file.read_bytes(), before)
        self.assert_no_consent()

    def test_explicit_replace_updates_existing_token_securely(self):
        self.save_token(token_info(token="synthetic-old-token"))
        auth.authorize(self.client_file, self.token_file, replace=True)
        self.assertEqual(json.loads(self.token_file.read_text())["token"], self.credentials.token)
        self.assertEqual(stat.S_IMODE(self.token_file.stat().st_mode), 0o600)

    def test_client_config_requires_installed_desktop_credentials(self):
        for config in ({"web": client_config()["installed"]}, {}, [], None):
            with self.subTest(config=config):
                self.client_file.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaises(auth.AuthError):
                    auth.authorize(self.client_file, self.token_file)
                self.assert_no_consent()
                self.assertFalse(self.token_file.exists())

    def test_client_config_rejects_unofficial_authorization_and_token_endpoints(self):
        replacements = (
            ("auth_uri", "https://example.test/authorize"),
            ("auth_uri", "http://accounts.google.com/o/oauth2/auth"),
            ("auth_uri", "https://accounts.google.com.example.test/o/oauth2/auth"),
            ("auth_uri", "https://accounts.google.com/o/oauth2/auth?redirect=example.test"),
            ("token_uri", "https://example.test/token"),
            ("token_uri", "http://oauth2.googleapis.com/token"),
            ("token_uri", "https://oauth2.googleapis.com.example.test/token"),
            ("token_uri", "https://oauth2.googleapis.com/token?redirect=example.test"),
        )
        for field, value in replacements:
            with self.subTest(field=field, value=value):
                config = client_config()
                config["installed"][field] = value
                self.client_file.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaises(auth.AuthError):
                    auth.authorize(self.client_file, self.token_file)
                self.assert_no_consent()
                self.assertFalse(self.token_file.exists())

    def test_malformed_client_endpoints_fail_with_redacted_auth_error(self):
        for endpoint in ([SYNTHETIC_SECRET], {"secret": SYNTHETIC_SECRET}, None):
            with self.subTest(endpoint=endpoint):
                config = client_config()
                config["installed"]["auth_uri"] = endpoint
                self.client_file.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaises(auth.AuthError) as caught:
                    auth.authorize(self.client_file, self.token_file)
                self.assert_redacted(caught.exception)
                self.assert_no_consent()

    def test_current_official_v2_authorization_endpoint_is_supported(self):
        config = client_config()
        config["installed"]["auth_uri"] = "https://accounts.google.com/o/oauth2/v2/auth"
        self.client_file.write_text(json.dumps(config), encoding="utf-8")
        auth.authorize(self.client_file, self.token_file)
        self.flow_class.from_client_config.assert_called_once_with(config, scopes=[READONLY_SCOPE])
        self.assertTrue(self.token_file.exists())

    def test_consent_without_refresh_token_cannot_overwrite_existing_token(self):
        before = self.save_token()
        self.credentials.refresh_token = None
        with self.assertRaises(auth.AuthError):
            auth.authorize(self.client_file, self.token_file, replace=True)
        self.assertEqual(self.token_file.read_bytes(), before)

    def test_large_credential_files_are_rejected_before_loading_or_consent(self):
        self.save_token()
        oversized = json.dumps({"padding": "x" * 65537})
        for path, action in (
            (self.client_file, lambda: auth.authorize(self.client_file, self.token_file, replace=True)),
            (self.token_file, lambda: auth.TokenProvider(self.token_file).get()),
        ):
            with self.subTest(path=path):
                path.write_text(oversized, encoding="utf-8")
                with self.assertRaises(auth.AuthError):
                    action()
                self.credentials_class.from_authorized_user_info.assert_not_called()
                self.assert_no_consent()

    def test_authorization_error_is_redacted_and_leaves_no_token(self):
        self.flow.run_local_server.side_effect = RuntimeError("provider failure " + SYNTHETIC_SECRET)
        with self.assertRaises(auth.AuthError) as caught:
            auth.authorize(self.client_file, self.token_file)
        self.assert_redacted(caught.exception)
        self.assertFalse(self.token_file.exists())

    def test_malformed_client_json_is_redacted_without_consent(self):
        self.client_file.write_text('{"secret": "' + SYNTHETIC_SECRET, encoding="utf-8")
        with self.assertRaises(auth.AuthError) as caught:
            auth.authorize(self.client_file, self.token_file)
        self.assert_redacted(caught.exception)
        self.assert_no_consent()

    def test_missing_client_file_never_starts_consent(self):
        self.client_file.unlink()
        with self.assertRaises(auth.AuthError):
            auth.authorize(self.client_file, self.token_file)
        self.assert_no_consent()

    def test_valid_token_is_loaded_without_refresh_or_consent(self):
        before = self.save_token()
        provider = auth.TokenProvider(self.token_file)
        self.assertEqual(provider.get(), "synthetic-access-token")
        self.assertFalse(self.credentials.refresh_calls)
        self.credentials_class.from_authorized_user_info.assert_called_once()
        self.assertEqual(self.token_file.read_bytes(), before)
        self.assert_no_consent()

    def test_expired_token_is_refreshed_and_persisted_securely(self):
        self.save_token()
        self.credentials.valid = False
        self.credentials.expired = True
        provider = auth.TokenProvider(self.token_file)
        self.assertEqual(provider.get(), "synthetic-refreshed-access-token")
        self.assertEqual(self.credentials.refresh_calls, [self.request])
        self.assertEqual(json.loads(self.token_file.read_text())["token"], "synthetic-refreshed-access-token")
        self.assertEqual(stat.S_IMODE(self.token_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.token_file.parent.stat().st_mode), 0o700)
        self.assert_no_consent()

    def test_forced_refresh_renews_a_still_valid_token(self):
        self.save_token()
        self.assertEqual(auth.TokenProvider(self.token_file).get(force_refresh=True), "synthetic-refreshed-access-token")
        self.assertEqual(self.credentials.refresh_calls, [self.request])
        self.assert_no_consent()

    def test_refresh_replaces_the_token_atomically(self):
        before = self.save_token()
        self.credentials.valid = False
        self.credentials.expired = True
        replacements = []
        real_replace = os.replace

        def replace(src, dst, *args, **kwargs):
            src, dst = Path(src), Path(dst)
            replacements.append((src, dst))
            self.assertEqual(src.parent, self.token_file.parent)
            self.assertEqual(dst, self.token_file)
            self.assertEqual(dst.read_bytes(), before)
            self.assertEqual(stat.S_IMODE(src.stat().st_mode), 0o600)
            self.assertEqual(json.loads(src.read_text())["token"], "synthetic-refreshed-access-token")
            return real_replace(src, dst, *args, **kwargs)

        with patch("os.replace", side_effect=replace):
            auth.TokenProvider(self.token_file).get()
        self.assertEqual(len(replacements), 1)
        self.assertEqual(list(self.token_file.parent.iterdir()), [self.token_file])

    def test_scope_must_be_exactly_gmail_readonly_before_credentials_load(self):
        for scopes in ([], ["https://mail.google.com/"], ["https://www.googleapis.com/auth/gmail.modify"], [READONLY_SCOPE, "openid"], None):
            with self.subTest(scopes=scopes):
                self.save_token(token_info(scopes=scopes))
                with self.assertRaises(auth.AuthError):
                    auth.TokenProvider(self.token_file).get()
                self.credentials_class.from_authorized_user_info.assert_not_called()
                self.assert_no_consent()

    def test_missing_scope_is_rejected_without_consent(self):
        info = token_info()
        del info["scopes"]
        self.save_token(info)
        with self.assertRaises(auth.AuthError):
            auth.TokenProvider(self.token_file).get()
        self.credentials_class.from_authorized_user_info.assert_not_called()
        self.assert_no_consent()

    def test_token_endpoint_must_be_official_before_credentials_load(self):
        endpoints = (
            "https://example.test/token",
            "http://oauth2.googleapis.com/token",
            "https://oauth2.googleapis.com.example.test/token",
            "https://oauth2.googleapis.com/token?redirect=example.test",
            None,
        )
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                self.save_token(token_info(token_uri=endpoint))
                with self.assertRaises(auth.AuthError):
                    auth.TokenProvider(self.token_file).get()
                self.credentials_class.from_authorized_user_info.assert_not_called()
                self.assert_no_consent()

    def test_non_user_credentials_are_rejected_without_consent(self):
        for credential_type in ("service_account", "external_account", "installed", None):
            with self.subTest(credential_type=credential_type):
                self.save_token(token_info(type=credential_type))
                with self.assertRaises(auth.AuthError):
                    auth.TokenProvider(self.token_file).get()
                self.credentials_class.from_authorized_user_info.assert_not_called()
                self.assert_no_consent()

    def test_missing_token_never_starts_consent(self):
        with self.assertRaises(auth.AuthError):
            auth.TokenProvider(self.token_file).get()
        self.assert_no_consent()
        self.assertFalse(self.token_file.exists())

    def test_invalid_token_without_refresh_token_never_starts_consent(self):
        self.save_token(token_info(refresh_token=None))
        self.credentials.valid = False
        self.credentials.expired = True
        self.credentials.refresh_token = None
        with self.assertRaises(auth.AuthError):
            auth.TokenProvider(self.token_file).get()
        self.assert_no_consent()
        self.assertFalse(self.credentials.refresh_calls)

    def test_credentials_library_error_is_redacted_without_consent(self):
        before = self.save_token()
        self.credentials_class.from_authorized_user_info.side_effect = ValueError("bad " + SYNTHETIC_SECRET)
        with self.assertRaises(auth.AuthError) as caught:
            auth.TokenProvider(self.token_file).get()
        self.assert_redacted(caught.exception)
        self.assertEqual(self.token_file.read_bytes(), before)
        self.assert_no_consent()

    def test_refresh_error_is_redacted_and_preserves_existing_token(self):
        before = self.save_token()
        self.credentials.valid = False
        self.credentials.expired = True
        self.credentials.refresh_error = RuntimeError("failed " + SYNTHETIC_SECRET + " synthetic-refresh-token")
        with self.assertRaises(auth.AuthError) as caught:
            auth.TokenProvider(self.token_file).get()
        self.assert_redacted(caught.exception)
        self.assertEqual(self.token_file.read_bytes(), before)
        self.assert_no_consent()

    def test_persist_error_is_redacted_preserves_token_and_cleans_temporary_file(self):
        before = self.save_token()
        self.credentials.valid = False
        self.credentials.expired = True
        with patch("os.replace", side_effect=OSError("failed " + SYNTHETIC_SECRET)):
            with self.assertRaises(auth.AuthError) as caught:
                auth.TokenProvider(self.token_file).get()
        self.assert_redacted(caught.exception)
        self.assertEqual(self.token_file.read_bytes(), before)
        self.assertEqual(list(self.token_file.parent.iterdir()), [self.token_file])
        self.assert_no_consent()

    def test_malformed_token_json_is_redacted_without_consent(self):
        self.save_token()
        self.token_file.write_text('{"secret": "' + SYNTHETIC_SECRET, encoding="utf-8")
        with self.assertRaises(auth.AuthError) as caught:
            auth.TokenProvider(self.token_file).get()
        self.assert_redacted(caught.exception)
        self.assert_no_consent()

    def test_symlink_token_is_refused_for_loading_and_replacement(self):
        target = self.root / "target.json"
        original = json.dumps(token_info()).encode("utf-8")
        target.write_bytes(original)
        target.chmod(0o600)
        self.token_file.parent.mkdir(mode=0o700)
        self.token_file.symlink_to(target)
        with self.assertRaises(auth.AuthError):
            auth.TokenProvider(self.token_file).get()
        with self.assertRaises(auth.AuthError):
            auth.authorize(self.client_file, self.token_file, replace=True)
        self.assertEqual(target.read_bytes(), original)
        self.assertTrue(self.token_file.is_symlink())
        self.assert_no_consent()

    def test_dangling_token_symlink_is_refused_without_consent(self):
        target = self.root / "must-not-be-created.json"
        self.token_file.parent.mkdir(mode=0o700)
        self.token_file.symlink_to(target)
        with self.assertRaises(auth.AuthError):
            auth.authorize(self.client_file, self.token_file, replace=True)
        self.assertFalse(target.exists())
        self.assert_no_consent()

    def test_insecure_token_mode_is_rejected_without_loading_credentials(self):
        self.save_token()
        self.token_file.chmod(0o644)
        with self.assertRaises(auth.AuthError):
            auth.TokenProvider(self.token_file).get()
        self.credentials_class.from_authorized_user_info.assert_not_called()
        self.assert_no_consent()

    def test_authorize_protects_client_and_existing_credential_directories(self):
        self.client_file.chmod(0o644)
        self.token_file.parent.mkdir(mode=0o755)
        auth.authorize(self.client_file, self.token_file)
        self.assertEqual(stat.S_IMODE(self.client_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.client_file.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.token_file.parent.stat().st_mode), 0o700)

    def test_symlink_client_is_refused_without_changing_the_target(self):
        target = self.root / "real-client.json"
        self.client_file.rename(target)
        target.chmod(0o644)
        before = target.read_bytes()
        self.client_file.symlink_to(target)
        with self.assertRaises(auth.AuthError):
            auth.authorize(self.client_file, self.token_file)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        self.assert_no_consent()

    def test_symlink_token_parent_is_refused_without_consent(self):
        target = self.root / "real-private"
        target.mkdir(mode=0o755)
        self.token_file.parent.symlink_to(target, target_is_directory=True)
        with self.assertRaises(auth.AuthError):
            auth.authorize(self.client_file, self.token_file)
        with self.assertRaises(auth.AuthError):
            auth.TokenProvider(self.token_file).get()
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assert_no_consent()

    def test_symlink_client_parent_is_refused_without_consent(self):
        target = self.root / "real-clients"
        target.mkdir(mode=0o755)
        linked = self.root / "linked-clients"
        linked.symlink_to(target, target_is_directory=True)
        destination = target / "client.json"
        destination.write_text(json.dumps(client_config()), encoding="utf-8")
        with self.assertRaises(auth.AuthError):
            auth.authorize(linked / "client.json", self.token_file)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assert_no_consent()

    def test_client_and_token_cannot_be_the_same_file(self):
        before = self.client_file.read_bytes()
        with self.assertRaises(auth.AuthError):
            auth.authorize(self.client_file, self.client_file, replace=True)
        self.assertEqual(self.client_file.read_bytes(), before)
        self.assert_no_consent()

    def test_unexpected_consent_scopes_are_not_persisted(self):
        self.credentials.granted_scopes = [READONLY_SCOPE, "openid"]
        with self.assertRaises(auth.AuthError):
            auth.authorize(self.client_file, self.token_file)
        self.assertFalse(self.token_file.exists())

    def test_unexpected_refresh_scopes_preserve_previous_token(self):
        before = self.save_token()
        self.credentials.valid = False
        self.credentials.granted_scopes = [READONLY_SCOPE, "openid"]
        with self.assertRaises(auth.AuthError):
            auth.TokenProvider(self.token_file).get()
        self.assertEqual(self.token_file.read_bytes(), before)
        self.assert_no_consent()

    def test_empty_or_whitespace_access_token_is_not_returned(self):
        self.save_token()
        for token in (None, "", "synthetic token", "synthetic-token\n"):
            with self.subTest(token=token):
                self.credentials.token = token
                with self.assertRaises(auth.AuthError):
                    auth.TokenProvider(self.token_file).get()
                self.assert_no_consent()

    def test_malformed_scope_values_fail_with_redacted_auth_error(self):
        for scopes in ([{"sensitive": SYNTHETIC_SECRET}], [[READONLY_SCOPE]], 123):
            with self.subTest(scopes=scopes):
                self.save_token(token_info(scopes=scopes))
                with self.assertRaises(auth.AuthError) as caught:
                    auth.TokenProvider(self.token_file).get()
                self.assert_redacted(caught.exception)
                self.assert_no_consent()

    def test_main_passes_explicit_directory_and_replace_flag(self):
        directory = self.root / "explicit-credentials"
        stdout = io.StringIO()
        with patch.object(auth, "authorize") as authorize, patch("sys.stdout", stdout):
            self.assertEqual(auth.main(["--credentials-dir", str(directory), "--replace-token"]), 0)
        authorize.assert_called_once_with(directory / "client_secret.json", directory / "token.json", replace=True)
        self.assertIn("read-only", stdout.getvalue())
        self.assertNotIn(SYNTHETIC_SECRET, stdout.getvalue())

    def test_main_default_directory_for_workspace_and_packaged_code(self):
        cases = (
            (self.root, self.root / "Invoices" / "credentials"),
            (self.root / "Invoices" / "code", self.root / "Invoices" / "credentials"),
        )
        for cwd, directory in cases:
            with self.subTest(cwd=cwd), patch.object(Path, "cwd", return_value=cwd), patch.object(auth, "authorize") as authorize, patch("sys.stdout", io.StringIO()):
                self.assertEqual(auth.main([]), 0)
                authorize.assert_called_once_with(directory / "client_secret.json", directory / "token.json", replace=False)

    def test_main_reports_failure_without_printing_credentials_or_traceback(self):
        self.flow.run_local_server.side_effect = RuntimeError("bad " + SYNTHETIC_SECRET)
        self.client_file.rename(self.root / "client_secret.json")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            self.assertEqual(auth.main(["--credentials-dir", str(self.root)]), 2)
        output = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(SYNTHETIC_SECRET, output)
        self.assertNotIn("Traceback", output)
        self.assertTrue(output.strip())
        self.assertFalse((self.root / "token.json").exists())

    def test_insecure_token_directory_mode_is_rejected_without_consent(self):
        self.save_token()
        self.token_file.parent.chmod(0o755)
        with self.assertRaises(auth.AuthError):
            auth.TokenProvider(self.token_file).get()
        self.credentials_class.from_authorized_user_info.assert_not_called()
        self.assert_no_consent()

    def test_symlink_ancestor_is_refused_for_client_and_token(self):
        before = self.save_token()
        link = self.root / "linked-root"
        link.symlink_to(self.root, target_is_directory=True)
        linked_token = link / "private" / self.token_file.name
        with self.assertRaises(auth.AuthError):
            auth.TokenProvider(linked_token).get()
        with self.assertRaises(auth.AuthError):
            auth.authorize(self.client_file, linked_token, replace=True)
        nested_clients = self.root / "nested-clients"
        nested_clients.mkdir(mode=0o700)
        nested_client = nested_clients / self.client_file.name
        nested_client.write_bytes(self.client_file.read_bytes())
        with self.assertRaises(auth.AuthError):
            auth.authorize(link / "nested-clients" / self.client_file.name, self.token_file, replace=True)
        self.assertEqual(self.token_file.read_bytes(), before)
        self.assert_no_consent()

    def test_gmail_explicit_access_token_takes_precedence_over_environment_and_file(self):
        http = Mock(return_value=io.BytesIO(b'{}'))
        with patch.dict(os.environ, {"GMAIL_ACCESS_TOKEN": "synthetic-env-token"}), patch.object(auth, "TokenProvider") as provider:
            source = GmailSource("synthetic-explicit-token", token_file=self.token_file, opener=http)
            self.assertEqual(source._get("/profile"), {})
            provider.assert_not_called()
        request = http.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-explicit-token")
        self.assert_no_consent()

    def test_gmail_environment_token_takes_precedence_over_file(self):
        http = Mock(return_value=io.BytesIO(b'{}'))
        with patch.dict(os.environ, {"GMAIL_ACCESS_TOKEN": "synthetic-env-token"}), patch.object(auth, "TokenProvider") as provider:
            source = GmailSource(token_file=self.token_file, opener=http)
            self.assertEqual(source._get("/profile"), {})
            provider.assert_not_called()
        self.assertEqual(http.call_args.args[0].get_header("Authorization"), "Bearer synthetic-env-token")
        self.assert_no_consent()

    def test_gmail_checks_existing_provider_before_each_request(self):
        self.save_token()
        http = Mock(side_effect=[io.BytesIO(b'{}'), io.BytesIO(b'{}')])
        with patch.dict(os.environ, {"GMAIL_ACCESS_TOKEN": ""}):
            source = GmailSource(token_file=self.token_file, opener=http)
        source._get("/profile")
        self.credentials.valid = False
        self.credentials.expired = True
        source._get("/messages")
        self.assertEqual(self.credentials.refresh_calls, [self.request])
        headers = [call.args[0].get_header("Authorization") for call in http.call_args_list]
        self.assertEqual(headers, ["Bearer synthetic-access-token", "Bearer synthetic-refreshed-access-token"])
        self.assert_no_consent()

    def test_gmail_401_forces_one_refresh_and_retries_with_new_token(self):
        self.save_token()
        http = Mock(side_effect=[HTTPError("https://gmail.googleapis.com/", 401, SYNTHETIC_SECRET, {}, None), io.BytesIO(b'{}')])
        with patch.dict(os.environ, {"GMAIL_ACCESS_TOKEN": ""}):
            source = GmailSource(token_file=self.token_file, opener=http)
        self.assertEqual(source._get("/profile"), {})
        self.assertEqual(http.call_count, 2)
        self.assertEqual(self.credentials.refresh_calls, [self.request])
        headers = [call.args[0].get_header("Authorization") for call in http.call_args_list]
        self.assertEqual(headers, ["Bearer synthetic-access-token", "Bearer synthetic-refreshed-access-token"])
        self.assertEqual(json.loads(self.token_file.read_text())["token"], "synthetic-refreshed-access-token")
        self.assert_no_consent()

    def test_gmail_repeated_401_does_not_refresh_or_retry_forever(self):
        self.save_token()
        http = Mock(side_effect=HTTPError("https://gmail.googleapis.com/", 401, SYNTHETIC_SECRET, {}, None))
        with patch.dict(os.environ, {"GMAIL_ACCESS_TOKEN": ""}):
            source = GmailSource(token_file=self.token_file, opener=http)
        with self.assertRaises(SourceError) as caught:
            source._get("/profile")
        self.assertIn("401", str(caught.exception))
        self.assert_redacted(caught.exception)
        self.assertEqual(http.call_count, 2)
        self.assertEqual(self.credentials.refresh_calls, [self.request])
        self.assert_no_consent()

    def test_gmail_403_does_not_start_refresh_or_consent(self):
        self.save_token()
        http = Mock(side_effect=HTTPError("https://gmail.googleapis.com/", 403, SYNTHETIC_SECRET, {}, None))
        with patch.dict(os.environ, {"GMAIL_ACCESS_TOKEN": ""}):
            source = GmailSource(token_file=self.token_file, opener=http)
        with self.assertRaises(SourceError) as caught:
            source._get("/profile")
        self.assert_redacted(caught.exception)
        self.assertEqual(http.call_count, 1)
        self.assertFalse(self.credentials.refresh_calls)
        self.assert_no_consent()

    def test_gmail_bare_access_token_401_does_not_attempt_file_auth(self):
        http = Mock(side_effect=HTTPError("https://gmail.googleapis.com/", 401, SYNTHETIC_SECRET, {}, None))
        with patch.object(auth, "TokenProvider") as provider:
            source = GmailSource("synthetic-explicit-token", opener=http)
            with self.assertRaises(SourceError):
                source._get("/profile")
            provider.assert_not_called()
        self.assertEqual(http.call_count, 1)
        self.assert_no_consent()

    def test_gmail_does_not_discover_credentials_or_consent_by_default(self):
        self.save_token()
        with patch.dict(os.environ, {"GMAIL_ACCESS_TOKEN": ""}), patch.object(Path, "cwd", return_value=self.root), patch.object(auth, "TokenProvider") as provider:
            with self.assertRaises(SourceError):
                GmailSource(opener=Mock())
            provider.assert_not_called()
        self.libraries.assert_not_called()
        self.assert_no_consent()

    def test_gmail_missing_configured_token_fails_without_consent(self):
        with patch.dict(os.environ, {"GMAIL_ACCESS_TOKEN": ""}):
            with self.assertRaises(SourceError):
                GmailSource(token_file=self.token_file, opener=Mock())
        self.libraries.assert_not_called()
        self.assert_no_consent()

    def test_gmail_refresh_failure_stops_before_request_and_is_redacted(self):
        self.save_token()
        http = Mock()
        with patch.dict(os.environ, {"GMAIL_ACCESS_TOKEN": ""}):
            source = GmailSource(token_file=self.token_file, opener=http)
        self.credentials.valid = False
        self.credentials.refresh_error = RuntimeError(SYNTHETIC_SECRET)
        with self.assertRaises(SourceError) as caught:
            source._get("/profile")
        self.assert_redacted(caught.exception)
        http.assert_not_called()
        self.assert_no_consent()

    def test_token_directory_is_not_treated_as_a_file(self):
        self.token_file.mkdir(parents=True)
        with self.assertRaises(auth.AuthError):
            auth.TokenProvider(self.token_file).get()
        with self.assertRaises(auth.AuthError):
            auth.authorize(self.client_file, self.token_file, replace=True)
        self.assertTrue(self.token_file.is_dir())
        self.assert_no_consent()


if __name__ == "__main__":
    unittest.main()
