"""Offline contract tests: no keys, documents, or requests leave this process."""

import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib import error

from invoices_scrapper import extraction as ex


TEXT = "Vendor: Example Shop\nInvoice Number: INV-7\nTotal: 120.00\nCurrency: ILS\n"


def record(**changes):
    value = {field: "" for field in ex.FIELDS}
    value.update(vendor="Example Shop", invoice_number="INV-7", category="unknown",
                 total="120.00", currency="ILS", status="unknown", kind="bill",
                 confidence=0.95, evidence="Total: 120.00")
    value.update(changes)
    return value


def completion(records=None, finish_reason="stop", raw=None):
    content = raw if raw is not None else json.dumps({"records": records if records is not None else [record()]})
    return {"choices": [{"finish_reason": finish_reason, "message": {"content": content}}]}


class RecordValidationTests(unittest.TestCase):
    def test_exact_schema_accepted_and_not_mutated(self):
        data = record()
        before = copy.deepcopy(data)
        self.assertEqual(ex.validate_records([data], TEXT), [before])
        self.assertEqual(data, before)

    def test_missing_and_extra_fields_rejected(self):
        for field in ex.FIELDS:
            data = record()
            del data[field]
            with self.subTest(field=field), self.assertRaises(ex.ExtractionError):
                ex.validate_records([data], TEXT)
        with self.assertRaises(ex.ExtractionError):
            ex.validate_records([record(unexpected="value")], TEXT)

    def test_no_coercion_of_numbers_nulls_or_booleans(self):
        for field, bad in (("total", 120), ("vendor", None), ("invoice_number", 7),
                           ("confidence", True), ("confidence", "0.5")):
            with self.subTest(field=field, bad=bad), self.assertRaises(ex.ExtractionError):
                ex.validate_records([record(**{field: bad})], TEXT)

    def test_amounts_are_finite_nonnegative_decimal_strings(self):
        for bad in ("-1", "-0", "NaN", "Infinity", "1e3", "$10", "1,000", " 1", "01.0", ".5", "1.", "nine", "1" * 65):
            with self.subTest(bad=bad), self.assertRaises(ex.ExtractionError):
                ex.validate_records([record(total=bad)], TEXT)
        for good in ("", "0", "0.00", "12.345", "1000000"):
            self.assertEqual(ex.validate_records([record(total=good)], TEXT)[0]["total"], good)

    def test_refunds_use_positive_magnitudes(self):
        self.assertEqual(ex.validate_records([record(status="refund", kind="refund")], TEXT)[0]["total"], "120.00")
        with self.assertRaises(ex.ExtractionError):
            ex.validate_records([record(status="refund", kind="refund", total="-120")], TEXT)

    def test_confidence_bounds_and_finiteness(self):
        for value in (-0.1, 1.1, float("inf"), float("nan"), 10 ** 1000):
            with self.subTest(value=str(value)[:20]), self.assertRaises(ex.ExtractionError):
                ex.validate_records([record(confidence=value)], TEXT)
        for value in (0, 1, 0.3):
            self.assertEqual(ex.validate_records([record(confidence=value)], TEXT)[0]["confidence"], value)

    def test_iso_dates_are_actual_calendar_dates(self):
        for bad in ("2026-02-30", "26-01-01", "01/02/2026", "20260101", "2026-1-1", "0000-01-01"):
            with self.subTest(bad=bad), self.assertRaises(ex.ExtractionError):
                ex.validate_records([record(date=bad)], TEXT)
        ex.validate_records([record(date="2024-02-29", due_date="2026-12-31")], TEXT)

    def test_optional_period_tax_payment_and_order_fields(self):
        data = record(description="Paper", period_start="2026-01-01", period_end="2026-01-31",
                      amount_vat="18.00", payment_date="2026-02-05", order_id="ORDER-7")
        self.assertEqual(ex.validate_records([data], TEXT), [data])
        for change in ({"period_start": "2026-02-01", "period_end": "2026-01-01"},
                       {"payment_date": "yesterday"}, {"amount_vat": "-1"}):
            with self.subTest(change=change), self.assertRaises(ex.ExtractionError):
                ex.validate_records([record(**change)], TEXT)
        self.assertEqual(ex.validate_records([record(status="pending")], TEXT)[0]["status"], "pending")

    def test_currency_and_enums(self):
        for changes in ({"currency": "ils"}, {"currency": "$"}, {"status": "overdue"}, {"kind": "purchase"}):
            with self.subTest(changes=changes), self.assertRaises(ex.ExtractionError):
                ex.validate_records([record(**changes)], TEXT)
        for kind in ex.KINDS:
            ex.validate_records([record(kind=kind)], TEXT)

    def test_evidence_is_literal_and_nonempty(self):
        for evidence in ("", " ", "Total is 120.00", "total: 120.00", "Total:\u00a0120.00"):
            with self.subTest(evidence=evidence), self.assertRaises(ex.ExtractionError):
                ex.validate_records([record(evidence=evidence)], TEXT)
        ex.validate_records([record(evidence=TEXT)], TEXT)

    def test_payment_evidence_must_be_literal_but_may_be_blank(self):
        source = TEXT + "Payment received for INV-7: 120.00 ILS"
        data = record(status="paid", payment_evidence="Payment received for INV-7: 120.00 ILS")
        self.assertEqual(ex.validate_records([data], source), [data])
        for bad in ("Payment was received", " ", "Payment received for INV-8: 120.00 ILS"):
            with self.subTest(bad=bad), self.assertRaises(ex.ExtractionError):
                ex.validate_records([record(payment_evidence=bad)], source)
        for status in ("paid", "unknown"):
            self.assertEqual(ex.validate_records([record(status=status, payment_evidence="")], TEXT)[0]["payment_evidence"], "")

    def test_empty_nonsense_and_too_many_records(self):
        data = record(vendor="", invoice_number="", total="", evidence="Total: 120.00")
        with self.assertRaises(ex.ExtractionError):
            ex.validate_records([data], TEXT)
        with self.assertRaises(ex.ExtractionError):
            ex.validate_records([record()] * 101, TEXT)
        self.assertEqual(ex.validate_records([], TEXT), [])

    def test_obvious_payment_contradictions_rejected(self):
        for changes in ({"status": "paid", "amount_due": "1"}, {"status": "unpaid", "amount_paid": "1"}):
            with self.subTest(changes=changes), self.assertRaises(ex.ExtractionError):
                ex.validate_records([record(**changes)], TEXT)
        result = ex.validate_records([record(status="unknown", amount_due="120.00")], TEXT)
        self.assertEqual(result[0]["status"], "unknown")
        self.assertEqual(result[0]["amount_paid"], "")


class MockTests(unittest.TestCase):
    def test_mock_is_offline_and_does_not_infer_payments(self):
        with patch.object(ex.request, "build_opener", side_effect=AssertionError("No network")):
            result = ex.extract(TEXT + "Amount Due: 120.00\n", {"provider": "mock"})
        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0]), set(ex.FIELDS))
        self.assertEqual(result[0]["status"], "unknown")
        self.assertEqual(result[0]["kind"], "unknown")
        self.assertEqual(result[0]["amount_paid"], "")
        self.assertEqual(result[0]["category"], "unknown")
        self.assertIn(result[0]["evidence"], TEXT + "Amount Due: 120.00\n")

    def test_separate_same_date_receipts_are_not_merged(self):
        source = "\n---\n".join(
            f"Vendor: Shop\nDocument Number: ORDER-{i}\nDate: 2026-10-10\nKind: receipt\nStatus: paid\nTotal: 10\n"
            for i in (1, 2)
        )
        result = ex.extract(source, {"provider": "mock"})
        self.assertEqual([r["document_number"] for r in result], ["ORDER-1", "ORDER-2"])
        self.assertEqual([r["invoice_number"] for r in result], ["", ""])
        self.assertEqual([r["related_invoice_number"] for r in result], ["", ""])

    def test_mock_supplier_tax_category_and_explicit_cross_reference(self):
        source = ("Vendor: Shop\nSupplier Tax ID: 123456789\nCategory: office\n"
                  "Document Number: RCPT-7\nRelated Invoice Number: INV-7\nKind: receipt\n")
        result = ex.extract(source, {"provider": "mock"})[0]
        self.assertEqual(result["supplier_tax_id"], "123456789")
        self.assertEqual(result["category"], "office")
        self.assertEqual(result["related_invoice_number"], "INV-7")

    def test_mock_optional_fields(self):
        source = ("Vendor: Shop\nDescription: Paper\nPeriod Start: 2026-01-01\nPeriod End: 2026-01-31\n"
                  "Amount VAT: 18.00\nPayment Date: 2026-02-05\nOrder ID: ORDER-1\nStatus: pending\n")
        result = ex.extract(source, {"provider": "mock"})[0]
        self.assertEqual(result["description"], "Paper")
        self.assertEqual(result["amount_vat"], "18.00")
        self.assertEqual(result["order_id"], "ORDER-1")
        self.assertEqual(result["status"], "pending")

    def test_mock_payment_evidence_english_and_hebrew(self):
        for label, evidence in (("Payment Evidence", "Payment received"), ("אסמכתת תשלום", "התשלום התקבל")):
            source = TEXT + f"{label}: {evidence}\n"
            result = ex.extract(source, {"provider": "mock"})[0]
            self.assertEqual(result["payment_evidence"], evidence)

    def test_hebrew_fixture(self):
        source = ('ספק: חנות לדוגמה\nמספר חשבונית: 123\nתאריך: 10/09/2026\n'
                  'מטבע: ₪\nסה"כ: 1,200.50\nסכום ששולם: 200.50\nיתרה לתשלום: 1000.00\n'
                  'סטטוס: שולם חלקית\nסוג: חשבונית\n')
        result = ex.extract(source, {"provider": "mock"})[0]
        self.assertEqual(result["date"], "2026-09-10")
        self.assertEqual(result["currency"], "ILS")
        self.assertEqual(result["total"], "1200.50")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["kind"], "bill")

    def test_narrative_is_not_a_production_model(self):
        self.assertEqual(ex.extract("Thank you for paying your bill.", {"provider": "mock"}), [])

    def test_duplicate_fields_and_bad_confidence_fail(self):
        for source in (TEXT + "Total: 999", TEXT + "Confidence: maybe"):
            with self.assertRaises(ex.ExtractionError):
                ex.extract(source, {"provider": "mock"})

    def test_empty_input_and_input_limit(self):
        self.assertEqual(ex.extract("  ", {"provider": "mock"}), [])
        with self.assertRaises(ex.ExtractionError):
            ex.extract(TEXT, {"provider": "mock", "max_input_chars": 5})


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"OPENAI_API_KEY": "test-not-a-real-key", "ANTHROPIC_API_KEY": "test-not-a-real-key"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = {"provider": "openai", "model": "example-test-model"}

    def test_external_gate_precedes_network_and_credentials(self):
        with patch.object(ex, "_post_json") as post, patch.dict(os.environ, {}, clear=True):
            for provider in ("openai", "openai-compatible", "anthropic"):
                for permission in (False, 1, "yes", None):
                    with self.subTest(provider=provider, permission=permission), self.assertRaises(ex.ExternalTransmissionRequired):
                        ex.extract(TEXT, {"provider": provider}, allow_external=permission)
            post.assert_not_called()

    def test_environment_keys_only(self):
        with self.assertRaises(ex.ExtractionError):
            ex.extract(TEXT, {**self.config, "api_key": "do-not-store"}, True)
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ex.ExtractionError, "OPENAI_API_KEY"):
            ex.extract(TEXT, self.config, True)

    def test_openai_payload_and_response(self):
        with patch.object(ex, "_post_json", return_value=completion()) as post:
            self.assertEqual(ex.extract(TEXT, self.config, True), [record()])
        url, headers, payload, timeout, size = post.call_args.args
        self.assertEqual(url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer test-not-a-real-key")
        self.assertEqual(payload["max_completion_tokens"], 4096)
        self.assertEqual(payload["response_format"]["json_schema"]["strict"], True)
        self.assertIn("untrusted", payload["messages"][0]["content"])
        self.assertIn("Never infer payment", payload["messages"][0]["content"])
        self.assertEqual(json.loads(payload["messages"][1]["content"].split("\n", 1)[1]), TEXT)
        self.assertEqual((timeout, size), (60.0, 1048576))

    def test_compatible_payload_options(self):
        config = {**self.config, "provider": "openai-compatible", "base_url": "http://127.0.0.1:8000/v1/",
                  "structured_output": "json_object", "token_limit_field": "max_tokens", "max_tokens": 1000}
        with patch.object(ex, "_post_json", return_value=completion()) as post:
            ex.extract(TEXT, config, True)
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:8000/v1/chat/completions")
        self.assertEqual(post.call_args.args[2]["max_tokens"], 1000)
        self.assertEqual(post.call_args.args[2]["response_format"], {"type": "json_object"})

    def test_anthropic_payload_and_response(self):
        result = {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps({"records": [record()]})}]}
        with patch.object(ex, "_post_json", return_value=result) as post:
            self.assertEqual(ex.extract(TEXT, {"provider": "anthropic", "model": "example-claude"}, True), [record()])
        url, headers, payload, _, _ = post.call_args.args
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertIn("x-api-key", headers)
        self.assertEqual(payload["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(payload["max_tokens"], 4096)

    def test_truncated_or_refused_openai_response_rejected(self):
        for reason in ("length", "content_filter", "tool_calls", None):
            with self.subTest(reason=reason), patch.object(ex, "_post_json", return_value=completion(finish_reason=reason)), self.assertRaises(ex.ExtractionError):
                ex.extract(TEXT, self.config, True)
        refused = completion()
        refused["choices"][0]["message"]["refusal"] = "No"
        with patch.object(ex, "_post_json", return_value=refused), self.assertRaises(ex.ExtractionError):
            ex.extract(TEXT, self.config, True)

    def test_truncated_or_tool_anthropic_response_rejected(self):
        for reason in ("max_tokens", "tool_use", "refusal", "pause_turn", "stop_sequence", None):
            response = {"stop_reason": reason, "content": [{"type": "text", "text": '{"records":[]}'}]}
            with self.subTest(reason=reason), patch.object(ex, "_post_json", return_value=response), self.assertRaises(ex.ExtractionError):
                ex.extract(TEXT, {"provider": "anthropic", "model": "example"}, True)

    def test_malformed_structured_responses_rejected(self):
        for raw in ('{"records":[', '```json\n{"records":[]}\n```', '[]', '{"records":[],"extra":1}',
                    '{"records":[],"records":[]}', '{"records":NaN}', '{"records":{}}'):
            with self.subTest(raw=raw), patch.object(ex, "_post_json", return_value=completion(raw=raw)), self.assertRaises(ex.ExtractionError):
                ex.extract(TEXT, self.config, True)

    def test_prompt_override_keeps_guard_and_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prompt.txt"
            path.write_text("Custom categorization guidance", encoding="utf-8")
            with patch.object(ex, "_post_json", return_value=completion()) as post:
                ex.extract(TEXT, {**self.config, "prompt_path": str(path)}, True)
            system = post.call_args.args[2]["messages"][0]["content"]
            self.assertIn("Custom categorization guidance", system)
            self.assertIn("untrusted documents", system)
            self.assertIn("Required JSON schema", system)

    def test_config_limits(self):
        for change in ({"timeout_seconds": 0}, {"timeout_seconds": float("nan")}, {"max_response_bytes": 0},
                       {"max_tokens": True}, {"max_input_chars": -1}, {"model": ""},
                       {"api_key_env": "contains spaces"}, {"structured_output": "bad"}):
            with self.subTest(change=change), patch.object(ex, "_post_json") as post, self.assertRaises(ex.ExtractionError):
                ex.extract(TEXT, {**self.config, **change}, True)
            post.assert_not_called()

    def test_untrusted_endpoint_rejected(self):
        for url in ("http://example.com/v1", "https://user:pass@example.com/v1", "https://example.com/v1?key=secret",
                    "https://example.com/#fragment", "file:///tmp/foo", "https://example.com:bad", "https://example.com/\nfoo"):
            with self.subTest(url=url), self.assertRaises(ex.ExtractionError):
                ex._endpoint(url, "/chat/completions")
        self.assertEqual(ex._endpoint("https://example.com", "/messages"), "https://example.com/v1/messages")
        self.assertEqual(ex._endpoint("http://[::1]:8000/v1", "/messages"), "http://[::1]:8000/v1/messages")


class FakeResponse(io.BytesIO):
    def __init__(self, data, headers=None, status=200):
        super().__init__(data)
        self.headers = headers or {}
        self.status = status


class TransportTests(unittest.TestCase):
    def post(self, response, limit=1024):
        with patch.object(ex.request, "build_opener") as factory:
            factory.return_value.open.return_value = response
            value = ex._post_json("https://example.com/v1/messages", {"x-api-key": "fake-key"}, {"x": "text"}, 12, limit)
            req = factory.return_value.open.call_args.args[0]
            self.assertEqual(req.method, "POST")
            self.assertEqual(factory.return_value.open.call_args.kwargs["timeout"], 12)
            self.assertIsInstance(factory.call_args.args[0], ex._NoRedirect)
            return value

    def test_valid_transport(self):
        self.assertEqual(self.post(FakeResponse(b'{"ok":true}')), {"ok": True})

    def test_oversize_header_and_body_refused(self):
        for response in (FakeResponse(b"{}", {"Content-Length": "1000"}), FakeResponse(b"x" * 100)):
            with self.assertRaises(ex.ExtractionError):
                self.post(response, limit=10)

    def test_truncated_compressed_or_invalid_json_response_refused(self):
        for response in (FakeResponse(b"{}", {"Content-Length": "3"}), FakeResponse(b"{}", {"Content-Encoding": "gzip"}),
                         FakeResponse(b"\xff"), FakeResponse(b'{"a":1,"a":2}'), FakeResponse(b"NaN"), FakeResponse(b"[]")):
            with self.subTest(response=response), self.assertRaises(ex.ExtractionError):
                self.post(response)

    def test_timeout_and_http_errors_are_sanitized(self):
        for err in (error.URLError("secret document"), TimeoutError("secret document"),
                    error.HTTPError("https://example.com/secret", 401, "secret document", {}, None)):
            with patch.object(ex.request, "build_opener") as factory:
                factory.return_value.open.side_effect = err
                with self.assertRaises(ex.ExtractionError) as caught:
                    ex._post_json("https://example.com", {}, {}, 1, 100)
                self.assertNotIn("secret", str(caught.exception))

    def test_redirect_is_never_followed(self):
        with self.assertRaises(ex.ExtractionError):
            ex._NoRedirect().redirect_request(None, None, 302, "", {}, "https://attacker.invalid")


if __name__ == "__main__":
    unittest.main()
