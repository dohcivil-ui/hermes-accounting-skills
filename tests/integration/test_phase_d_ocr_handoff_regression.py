import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import mock_open, patch


ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = ROOT / "plugins/accounting-slip-bridge/__init__.py"
INGRESS_PATH = ROOT / "plugins/accounting-slip-bridge/ocr_ingress.py"
FLOW_PATH = ROOT / "plugins/accounting-slip-bridge/transaction_flow.py"
WIRING_PATH = ROOT / "plugins/accounting-slip-bridge/telegram_wiring.py"
FIXTURE_PATH = ROOT / "tests/fixtures/phase_d_aksonocr_handoff.json"
REAL_SHAPE_FIXTURE_PATH = (
    ROOT / "tests/fixtures/aksonocr_real_response_shape.json"
)
MISSING_REFERENCE_FIXTURE_PATH = (
    ROOT / "tests/fixtures/aksonocr_real_missing_reference_shape.json"
)
REFERENCE_NO = "PHASED-SMOKE-20260901-01"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PhaseDOcrHandoffRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        self.slip = self.uploads / "phase-d-synthetic-slip.jpg"
        self.slip.write_bytes(b"\xff\xd8\xff\xe0phase-d-synthetic-jpeg")
        self.bridge = load_module("lekza_phase_d_regression_bridge", BRIDGE_PATH)
        self.flow_module = load_module("lekza_phase_d_regression_flow", FLOW_PATH)
        self.wiring = load_module("lekza_phase_d_regression_wiring", WIRING_PATH)
        self.store = self.flow_module.SQLiteStateStore(
            self.root / "state" / "transactions.sqlite3"
        )
        self.flow = self.flow_module.TransactionFlow(
            self.store,
            allowed_source_roots=[self.uploads],
            projects=["Synthetic Project"],
        )
        self.controller = self.wiring.TelegramTransactionController(
            self.flow,
            object(),
            projects=["Synthetic Project"],
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def fixture(self):
        return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def real_shape_fixture(self):
        return json.loads(
            REAL_SHAPE_FIXTURE_PATH.read_text(encoding="utf-8")
        )

    def missing_reference_fixture(self):
        return json.loads(
            MISSING_REFERENCE_FIXTURE_PATH.read_text(encoding="utf-8")
        )

    def test_missing_reference_is_supplied_before_confirm_ready(self):
        api_response = self.missing_reference_fixture()
        ocr_result = {
            "akson_called": True,
            "http_status": 201,
            "confidence": api_response["confidence"],
            "raw_ocr_text": api_response["pages"][0]["markdown"],
            "parsed": api_response["parsed"],
            "usage": api_response["usage"],
            "raw_response": api_response,
        }
        normalized = self.bridge._normalize_ocr_result_for_handoff(ocr_result)
        created = self.controller.begin_from_ocr(
            tenant_id="phase-d-missing-reference-tenant",
            chat_id="phase-d-synthetic-chat",
            thread_id=None,
            session_id="phase-d-missing-reference-session",
            handoff_id="phase-d-missing-reference-message",
            telegram_user_id="phase-d-synthetic-user",
            source_image_path=self.slip,
            ocr_result=normalized,
        )
        actor = {
            "platform": "telegram",
            "chat_id": "phase-d-synthetic-chat",
            "telegram_user_id": "phase-d-synthetic-user",
        }
        prompt = self.controller.render(created["transaction_id"], **actor)
        self.assertTrue(prompt["manual_input_required"])
        self.assertIn("อ้างอิง", prompt["text"])

        replayed = self.controller.begin_from_ocr(
            tenant_id="phase-d-missing-reference-tenant",
            chat_id="phase-d-synthetic-chat",
            thread_id=None,
            session_id="phase-d-missing-reference-session",
            handoff_id="phase-d-missing-reference-message",
            telegram_user_id="phase-d-synthetic-user",
            source_image_path=self.slip,
            ocr_result=normalized,
        )
        self.assertEqual(replayed["transaction_id"], created["transaction_id"])

        forged_confirm = self.wiring.encode_callback(
            created["transaction_id"], created["version"], "confirm"
        )
        rejected = self.controller.handle_callback(forged_confirm, **actor)
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["error_code"], "invalid_transition")
        with self.assertRaises(self.flow_module.InvalidTransitionError):
            self.flow.confirm(
                created["transaction_id"],
                expected_version=created["version"],
                **actor,
            )

        unauthorized = self.controller.handle_manual_message(
            REFERENCE_NO,
            platform="telegram",
            chat_id="phase-d-synthetic-chat",
            telegram_user_id="different-user",
        )
        self.assertIsNone(unauthorized)

        invalid = self.controller.handle_manual_message(
            "not a reference!", **actor
        )
        self.assertFalse(invalid["ok"])
        still_missing = self.flow.get_transaction(
            created["transaction_id"], **actor
        )
        self.assertTrue(still_missing["needs_reference"])

        self.store.close()
        self.store = self.flow_module.SQLiteStateStore(
            self.root / "state" / "transactions.sqlite3"
        )
        self.flow = self.flow_module.TransactionFlow(
            self.store,
            allowed_source_roots=[self.uploads],
            projects=["Synthetic Project"],
        )
        self.controller = self.wiring.TelegramTransactionController(
            self.flow,
            object(),
            projects=["Synthetic Project"],
        )
        supplied = self.controller.handle_manual_message(
            REFERENCE_NO, **actor
        )
        self.assertTrue(supplied["ok"])
        prompt = supplied["prompt"]
        self.assertEqual(prompt["current_state"], "waiting_project")
        self.assertIn("วันที่", prompt["text"])
        supplied_date = self.controller.handle_manual_message(
            "2026-09-01", **actor
        )
        self.assertTrue(supplied_date["ok"])
        prompt = supplied_date["prompt"]

        second = self.controller.begin_from_ocr(
            tenant_id="phase-d-missing-reference-tenant",
            chat_id="phase-d-synthetic-chat",
            thread_id=None,
            session_id="phase-d-second-missing-session",
            handoff_id="phase-d-second-missing-message",
            telegram_user_id="phase-d-synthetic-user",
            source_image_path=self.slip,
            ocr_result=normalized,
        )
        duplicate = self.controller.handle_manual_message(REFERENCE_NO, **actor)
        self.assertFalse(duplicate["ok"])
        second_durable = self.flow.get_transaction(
            second["transaction_id"], **actor
        )
        self.assertTrue(second_durable["needs_reference"])

        for action in ("select_project", "use_sender", "expense", "materials"):
            button = next(
                item
                for item in prompt["buttons"]
                if self.wiring.decode_callback(item["callback_data"]).action
                == action
            )
            result = self.controller.handle_callback(
                button["callback_data"], **actor
            )
            self.assertTrue(result["ok"])
            prompt = result["prompt"]

        self.assertEqual(prompt["current_state"], "waiting_review")
        actions = {
            self.wiring.decode_callback(item["callback_data"]).action
            for item in prompt["buttons"]
        }
        self.assertIn("confirm", actions)
        durable = self.store.get_by_reference(
            "phase-d-missing-reference-tenant", REFERENCE_NO
        )
        self.assertEqual(durable["reference_no"], REFERENCE_NO)

    def test_real_api_shape_survives_adapter_and_creates_transaction(self):
        api_response = self.real_shape_fixture()
        response = types.SimpleNamespace(
            status_code=201,
            json=lambda: api_response,
        )
        with patch.dict(
            os.environ, {"AKSONOCR_API_KEY": "synthetic-test-key"}, clear=True
        ), patch.object(
            self.bridge.requests, "post", return_value=response
        ):
            ocr_result = self.bridge.call_akson_ocr(str(self.slip))

        self.assertEqual(ocr_result["parsed"], {})
        self.assertEqual(
            ocr_result["raw_ocr_text"],
            api_response["pages"][0]["markdown"],
        )
        normalized = self.bridge._normalize_ocr_result_for_handoff(ocr_result)
        self.assertEqual(normalized["parsed"]["reference_no"], REFERENCE_NO)
        self.assertEqual(normalized["parsed"]["amount"], "1.00")

        tenant_id = "phase-d-real-shape-tenant"
        created = self.controller.begin_from_ocr(
            tenant_id=tenant_id,
            chat_id="phase-d-synthetic-chat",
            thread_id=None,
            session_id="phase-d-real-shape-session",
            handoff_id="phase-d-real-shape-message",
            telegram_user_id="phase-d-synthetic-user",
            source_image_path=self.slip,
            ocr_result=normalized,
        )
        durable = self.store.get_by_reference(tenant_id, REFERENCE_NO)
        self.assertEqual(created["transaction_id"], durable["transaction_id"])
        self.assertEqual(durable["reference_no"], REFERENCE_NO)
        self.assertEqual(durable["ocr_fields"]["amount"], 1)
        self.assertFalse(durable["needs_amount"])

    def test_amount_normalization_priority_and_requires_label(self):
        cases = (
            (
                "parsed",
                {
                    "parsed": {"amount": "4.00"},
                    "raw_response": {
                        "parsed": {"amount": "3.00"},
                        "data": {"amount": "2.00"},
                    },
                    "raw_ocr_text": "Amount: 1.00",
                },
                "4.00",
            ),
            (
                "raw_response.parsed",
                {
                    "parsed": {},
                    "raw_response": {
                        "parsed": {"amount": "3.00"},
                        "data": {"amount": "2.00"},
                    },
                    "raw_ocr_text": "Amount: 1.00",
                },
                "3.00",
            ),
            (
                "raw_response.data",
                {
                    "parsed": {},
                    "raw_response": {
                        "parsed": {},
                        "data": {"amount": "2.00"},
                    },
                    "raw_ocr_text": "Amount: 1.00",
                },
                "2.00",
            ),
            (
                "english_markdown",
                {"parsed": {}, "raw_ocr_text": "Amount: 1.00"},
                "1.00",
            ),
            (
                "english_markdown_with_grouping",
                {"parsed": {}, "raw_ocr_text": "Amount: 1,234.56"},
                "1,234.56",
            ),
            (
                "thai_amount_markdown",
                {"parsed": {}, "raw_ocr_text": "จำนวนเงิน: 1.00 บาท"},
                "1.00",
            ),
            (
                "thai_total_markdown",
                {"parsed": {}, "raw_ocr_text": "ยอดเงิน: 1.00 บาท"},
                "1.00",
            ),
            (
                "unlabeled_number",
                {"parsed": {}, "raw_ocr_text": "20260901.01\n1.00"},
                None,
            ),
            (
                "malformed_grouping",
                {"parsed": {}, "raw_ocr_text": "Amount: 1,23"},
                None,
            ),
            (
                "malformed_decimal",
                {"parsed": {}, "raw_ocr_text": "Amount: 1.2.3"},
                None,
            ),
            (
                "malformed_suffix",
                {"parsed": {}, "raw_ocr_text": "Amount: 1.00abc"},
                None,
            ),
        )
        for name, ocr_result, expected in cases:
            with self.subTest(name=name):
                normalized = self.bridge._normalize_ocr_result_for_handoff(
                    ocr_result
                )
                if expected is None:
                    self.assertNotIn("amount", normalized["parsed"])
                else:
                    self.assertEqual(normalized["parsed"]["amount"], expected)

    def test_invalid_or_ambiguous_ocr_date_keeps_manual_date_fallback(self):
        cases = (
            (
                "invalid",
                {"parsed": {
                    "reference_no": "DATE-INVALID",
                    "amount": "1.00",
                    "date": "31/02/2569",
                }},
            ),
            (
                "ambiguous",
                {
                    "parsed": {
                        "reference_no": "DATE-AMBIGUOUS",
                        "amount": "1.00",
                        "date": "05/09/2026",
                    },
                    "raw_response": {
                        "data": {"date": "06/09/2026"}
                    },
                },
            ),
        )
        for name, ocr_result in cases:
            with self.subTest(name=name):
                normalized = self.bridge._normalize_ocr_result_for_handoff(
                    ocr_result
                )
                created = self.controller.begin_from_ocr(
                    tenant_id=f"phase-d-date-{name}-tenant",
                    chat_id="phase-d-synthetic-chat",
                    thread_id=None,
                    session_id=f"phase-d-date-{name}-session",
                    handoff_id=f"phase-d-date-{name}-message",
                    telegram_user_id="phase-d-synthetic-user",
                    source_image_path=self.slip,
                    ocr_result=normalized,
                )
                durable = self.flow.get_transaction(
                    created["transaction_id"],
                    platform="telegram",
                    chat_id="phase-d-synthetic-chat",
                    telegram_user_id="phase-d-synthetic-user",
                )
                self.assertEqual(durable["entry_mode"], "date")
                self.assertNotIn("date", durable["ocr_fields"])

    def test_invalid_date_key_extraction_keeps_manual_date_fallback(self):
        for index, extracted_date in enumerate(("05/09/69", "31/02/2569")):
            with self.subTest(extracted_date=extracted_date), patch.object(
                self.bridge,
                "call_akson_date_extraction",
                return_value=extracted_date,
            ):
                normalized = self.bridge._normalize_ocr_result_for_handoff({
                    "parsed": {
                        "reference_no": f"DATE-EXTRACTED-INVALID-{index}",
                        "amount": "1.00",
                    }
                })
                self.bridge._add_extracted_date(normalized, self.slip)
                created = self.controller.begin_from_ocr(
                    tenant_id=f"phase-d-extracted-date-{index}-tenant",
                    chat_id="phase-d-synthetic-chat",
                    thread_id=None,
                    session_id=f"phase-d-extracted-date-{index}-session",
                    handoff_id=f"phase-d-extracted-date-{index}-message",
                    telegram_user_id="phase-d-synthetic-user",
                    source_image_path=self.slip,
                    ocr_result=normalized,
                )
                durable = self.flow.get_transaction(
                    created["transaction_id"],
                    platform="telegram",
                    chat_id="phase-d-synthetic-chat",
                    telegram_user_id="phase-d-synthetic-user",
                )
                self.assertEqual(durable["entry_mode"], "date")
                self.assertNotIn("date", durable["ocr_fields"])

    def test_ambiguous_date_persists_across_crash_without_reextraction(self):
        crash_before_handoff = {"value": True}
        handed_off = []
        ingress_module = load_module(
            "lekza_phase_d_ambiguous_ingress", INGRESS_PATH
        )
        ingress_ledger = ingress_module.OcrIngressLedger(
            self.root / "ambiguous-ingress.sqlite3"
        )
        ingress_identity = {
            "tenant_id": "phase-d-ambiguous-resume-tenant",
            "message_identity": "phase-d-ambiguous-resume-message",
        }

        def lookup(*args):
            return ingress_ledger.lookup_message(**ingress_identity)

        def obtain(*args, **kwargs):
            return ingress_ledger.obtain(
                **ingress_identity,
                source_image_path=kwargs["source_image_path"],
                ocr_reader=kwargs["ocr_reader"],
            )

        def find_candidates(outcome):
            if crash_before_handoff["value"]:
                crash_before_handoff["value"] = False
                raise RuntimeError("synthetic crash before handoff")
            return []

        def handoff_ocr_result(
            event, gateway, session_store, *, source_image_path, ocr_result
        ):
            transaction = self.controller.begin_from_ocr(
                tenant_id="phase-d-ambiguous-resume-tenant",
                chat_id="phase-d-synthetic-chat",
                thread_id=None,
                session_id="phase-d-ambiguous-resume-session",
                handoff_id="phase-d-ambiguous-resume-message",
                telegram_user_id="phase-d-synthetic-user",
                source_image_path=source_image_path,
                ocr_result=ocr_result,
            )
            handed_off.append(transaction["transaction_id"])
            return {"transaction": transaction}

        buttons = types.SimpleNamespace(
            lookup_ocr_ingress=lookup,
            obtain_ocr_ingress=obtain,
            find_ocr_duplicate_candidates=find_candidates,
            persist_ocr_ingress_result=lambda outcome: (
                ingress_ledger.persist_result(outcome)
            ),
            complete_ocr_ingress=lambda outcome, transaction_id: (
                ingress_ledger.complete(
                    outcome, transaction_id=transaction_id
                )
            ),
            handoff_ocr_result=handoff_ocr_result,
        )
        event = types.SimpleNamespace(
            source=types.SimpleNamespace(
                platform="telegram",
                chat_id="phase-d-synthetic-chat",
                user_id="phase-d-synthetic-user",
            ),
            media_urls=[str(self.slip)],
            media_types=["image/jpeg"],
            message_id="phase-d-ambiguous-resume-message",
        )

        first_context = types.SimpleNamespace(hooks={})
        first_context.register_hook = (
            lambda name, callback: first_context.hooks.__setitem__(name, callback)
        )
        self.bridge.register(first_context)
        with patch.dict(
            os.environ, {"LEKZA_RUNTIME_ENV": "production"}, clear=True
        ), patch.dict(
            sys.modules, {"lekza_accounting_transaction_buttons": buttons}
        ), patch.object(
            self.bridge, "_materialize_media", return_value=str(self.slip)
        ), patch.object(
            self.bridge,
            "call_akson_ocr",
            return_value={
                "akson_called": True,
                "confidence": 1.0,
                "parsed": {
                    "reference_no": "DATE-AMBIGUOUS-RESUME",
                    "amount": "1.00",
                    "date": "05/09/2026",
                },
                "raw_response": {"data": {"date": "06/09/2026"}},
            },
        ), patch.object(
            self.bridge, "call_akson_date_extraction"
        ) as first_extraction, patch.object(
            self.bridge.os, "makedirs"
        ), patch(
            "builtins.open", mock_open()
        ):
            first_context.hooks["pre_gateway_dispatch"](event)
        first_extraction.assert_not_called()

        connection = ingress_ledger._connect()
        try:
            persisted_date = json.loads(connection.execute(
                "SELECT ocr_result_json FROM ocr_ingress"
            ).fetchone()["ocr_result_json"])["parsed"]["date"]
            self.assertEqual(
                persisted_date, self.bridge._AMBIGUOUS_STRUCTURED_DATE
            )
            with connection:
                connection.execute(
                    "UPDATE ocr_ingress SET claim_expires_at = ?",
                    ("2000-01-01T00:00:00+00:00",),
                )
        finally:
            connection.close()

        restarted_bridge = load_module(
            "lekza_phase_d_ambiguous_restart_bridge", BRIDGE_PATH
        )
        restarted_context = types.SimpleNamespace(hooks={})
        restarted_context.register_hook = (
            lambda name, callback: restarted_context.hooks.__setitem__(
                name, callback
            )
        )
        restarted_bridge.register(restarted_context)
        with patch.dict(
            os.environ, {"LEKZA_RUNTIME_ENV": "production"}, clear=True
        ), patch.dict(
            sys.modules, {"lekza_accounting_transaction_buttons": buttons}
        ), patch.object(
            restarted_bridge, "_materialize_media", return_value=str(self.slip)
        ), patch.object(
            restarted_bridge, "call_akson_ocr"
        ) as restarted_ocr, patch.object(
            restarted_bridge, "call_akson_date_extraction"
        ) as restarted_extraction, patch.object(
            restarted_bridge.os, "makedirs"
        ), patch(
            "builtins.open", mock_open()
        ):
            result = restarted_context.hooks["pre_gateway_dispatch"](event)

        self.assertEqual(result, {"action": "skip"})
        restarted_ocr.assert_not_called()
        restarted_extraction.assert_not_called()
        durable = self.flow.get_transaction(
            handed_off[0],
            platform="telegram",
            chat_id="phase-d-synthetic-chat",
            telegram_user_id="phase-d-synthetic-user",
        )
        self.assertEqual(durable["entry_mode"], "date")
        self.assertNotIn("date", durable["ocr_fields"])

    def test_party_note_enrichment_persists_across_crash_before_handoff(self):
        ingress_module = load_module(
            "lekza_phase_d_party_note_ingress", INGRESS_PATH
        )
        ingress_ledger = ingress_module.OcrIngressLedger(
            self.root / "party-note-ingress.sqlite3"
        )
        ingress_identity = {
            "tenant_id": "phase-d-party-note-resume-tenant",
            "message_identity": "phase-d-party-note-resume-message",
        }
        handoff_attempts = {"count": 0}

        def lookup(*args):
            return ingress_ledger.lookup_message(**ingress_identity)

        def obtain(*args, **kwargs):
            return ingress_ledger.obtain(
                **ingress_identity,
                source_image_path=kwargs["source_image_path"],
                ocr_reader=kwargs["ocr_reader"],
            )

        def handoff_ocr_result(
            event, gateway, session_store, *, source_image_path, ocr_result
        ):
            handoff_attempts["count"] += 1
            if handoff_attempts["count"] == 1:
                raise RuntimeError("synthetic crash before durable handoff")
            transaction = self.controller.begin_from_ocr(
                tenant_id="phase-d-party-note-resume-tenant",
                chat_id="phase-d-synthetic-chat",
                thread_id=None,
                session_id="phase-d-party-note-resume-session",
                handoff_id="phase-d-party-note-resume-message",
                telegram_user_id="phase-d-synthetic-user",
                source_image_path=source_image_path,
                ocr_result=ocr_result,
            )
            return {"transaction": transaction}

        buttons = types.SimpleNamespace(
            lookup_ocr_ingress=lookup,
            obtain_ocr_ingress=obtain,
            find_ocr_duplicate_candidates=lambda outcome: [],
            persist_ocr_ingress_result=lambda outcome: (
                ingress_ledger.persist_result(outcome)
            ),
            complete_ocr_ingress=lambda outcome, transaction_id: (
                ingress_ledger.complete(
                    outcome, transaction_id=transaction_id
                )
            ),
            handoff_ocr_result=handoff_ocr_result,
        )
        event = types.SimpleNamespace(
            source=types.SimpleNamespace(
                platform="telegram",
                chat_id="phase-d-synthetic-chat",
                user_id="phase-d-synthetic-user",
            ),
            media_urls=[str(self.slip)],
            media_types=["image/jpeg"],
            message_id="phase-d-party-note-resume-message",
        )

        first_context = types.SimpleNamespace(hooks={})
        first_context.register_hook = (
            lambda name, callback: first_context.hooks.__setitem__(name, callback)
        )
        self.bridge.register(first_context)
        with patch.dict(
            os.environ, {"LEKZA_RUNTIME_ENV": "production"}, clear=True
        ), patch.dict(
            sys.modules, {"lekza_accounting_transaction_buttons": buttons}
        ), patch.object(
            self.bridge, "_materialize_media", return_value=str(self.slip)
        ), patch.object(
            self.bridge,
            "call_akson_ocr",
            return_value={
                "akson_called": True,
                "confidence": 1.0,
                "parsed": {
                    "reference_no": "PARTY-NOTE-RESUME",
                    "amount": "1.00",
                    "date": "2026-09-05",
                },
            },
        ) as first_ocr, patch.object(
            self.bridge,
            "call_akson_party_note_extraction",
            return_value={
                "payer": "Synthetic Extracted Payer",
                "payee": "Synthetic Extracted Payee",
                "note": "Synthetic extracted note",
            },
        ) as party_note_fallback, patch.object(
            self.bridge.os, "makedirs"
        ), patch(
            "builtins.open", mock_open()
        ):
            first_result = first_context.hooks["pre_gateway_dispatch"](event)

        self.assertEqual(first_result["action"], "rewrite")
        first_ocr.assert_called_once()
        party_note_fallback.assert_called_once_with(
            str(self.slip), ("payer", "payee", "note")
        )
        connection = ingress_ledger._connect()
        try:
            persisted = json.loads(connection.execute(
                "SELECT ocr_result_json FROM ocr_ingress"
            ).fetchone()["ocr_result_json"])
            self.assertEqual(persisted["parsed"], {
                "reference_no": "PARTY-NOTE-RESUME",
                "amount": "1.00",
                "date": "2026-09-05",
                "payer": "Synthetic Extracted Payer",
                "payee": "Synthetic Extracted Payee",
                "note": "Synthetic extracted note",
            })
            with connection:
                connection.execute(
                    "UPDATE ocr_ingress SET claim_expires_at = ?",
                    ("2000-01-01T00:00:00+00:00",),
                )
        finally:
            connection.close()

        restarted_bridge = load_module(
            "lekza_phase_d_party_note_restart_bridge", BRIDGE_PATH
        )
        restarted_context = types.SimpleNamespace(hooks={})
        restarted_context.register_hook = (
            lambda name, callback: restarted_context.hooks.__setitem__(
                name, callback
            )
        )
        restarted_bridge.register(restarted_context)
        resumed_handoffs = []
        original_handoff = buttons.handoff_ocr_result

        def capture_resumed_handoff(*args, **kwargs):
            resumed_handoffs.append(kwargs["ocr_result"])
            return original_handoff(*args, **kwargs)

        buttons.handoff_ocr_result = capture_resumed_handoff
        with patch.dict(
            os.environ, {"LEKZA_RUNTIME_ENV": "production"}, clear=True
        ), patch.dict(
            sys.modules, {"lekza_accounting_transaction_buttons": buttons}
        ), patch.object(
            restarted_bridge, "_materialize_media", return_value=str(self.slip)
        ), patch.object(
            restarted_bridge, "call_akson_ocr"
        ) as restarted_ocr, patch.object(
            restarted_bridge, "call_akson_party_note_extraction"
        ) as restarted_fallback, patch.object(
            restarted_bridge.os, "makedirs"
        ), patch(
            "builtins.open", mock_open()
        ):
            resumed_result = restarted_context.hooks["pre_gateway_dispatch"](event)

        self.assertEqual(resumed_result, {"action": "skip"})
        restarted_ocr.assert_not_called()
        restarted_fallback.assert_not_called()
        self.assertEqual(resumed_handoffs[0]["parsed"], persisted["parsed"])
        durable = self.store.get_by_reference(
            "phase-d-party-note-resume-tenant", "PARTY-NOTE-RESUME"
        )
        self.assertEqual(durable["ocr_fields"]["payer"], persisted["parsed"]["payer"])
        self.assertEqual(durable["ocr_fields"]["payee"], persisted["parsed"]["payee"])
        self.assertEqual(durable["ocr_fields"]["note"], persisted["parsed"]["note"])

    def test_existing_handoff_is_idempotent_and_does_not_enrich_ocr_fields(self):
        tenant_id = "phase-d-idempotent-tenant"
        handoff = {
            "tenant_id": tenant_id,
            "chat_id": "phase-d-synthetic-chat",
            "thread_id": None,
            "session_id": "phase-d-idempotent-session",
            "handoff_id": "phase-d-idempotent-message",
            "telegram_user_id": "phase-d-synthetic-user",
            "source_image_path": self.slip,
        }
        created = self.controller.begin_from_ocr(
            **handoff,
            ocr_result={"parsed": {"reference_no": REFERENCE_NO}},
        )
        original = self.store.get_by_reference(tenant_id, REFERENCE_NO)
        self.assertTrue(original["needs_amount"])
        self.assertNotIn("amount", original["ocr_fields"])

        replayed = self.controller.begin_from_ocr(
            **handoff,
            ocr_result={
                "parsed": {
                    "reference_no": REFERENCE_NO,
                    "amount": "99.00",
                }
            },
        )

        durable = self.store.get_by_reference(tenant_id, REFERENCE_NO)
        self.assertEqual(replayed["transaction_id"], created["transaction_id"])
        self.assertTrue(durable["needs_amount"])
        self.assertNotIn("amount", durable["ocr_fields"])

    def test_telegram_hook_preserves_real_shape_before_normalization(self):
        api_response = self.real_shape_fixture()
        ocr_result = {
            "akson_called": True,
            "http_status": 201,
            "confidence": api_response["confidence"],
            "raw_ocr_text": api_response["pages"][0]["markdown"],
            "parsed": api_response["parsed"],
            "usage": api_response["usage"],
            "raw_response": api_response,
        }
        handed_off = []
        def obtain(*args, **kwargs):
            return types.SimpleNamespace(
                status="ready", ocr_result=kwargs["ocr_reader"](),
                transaction_id=None,
            )
        buttons = types.SimpleNamespace(
            lookup_ocr_ingress=lambda *args: None,
            obtain_ocr_ingress=obtain,
            find_ocr_duplicate_candidates=lambda outcome: [],
            persist_ocr_ingress_result=lambda outcome: outcome,
            complete_ocr_ingress=lambda outcome, transaction_id: None,
            handoff_ocr_result=lambda *args, **kwargs: handed_off.append(
                kwargs["ocr_result"]
            )
            or {"transaction": {"transaction_id": "synthetic-id"}}
        )
        context = types.SimpleNamespace(hooks={})
        context.register_hook = lambda name, callback: context.hooks.__setitem__(
            name, callback
        )
        self.bridge.register(context)
        event = types.SimpleNamespace(
            source=types.SimpleNamespace(
                platform="telegram",
                chat_id="synthetic-chat",
                user_id="synthetic-user",
            ),
            media_urls=[str(self.slip)],
            media_types=["image/jpeg"],
            message_id="synthetic-message",
        )

        with patch.dict(
            os.environ, {"LEKZA_RUNTIME_ENV": "production"}, clear=True
        ), patch.dict(
            sys.modules, {"lekza_accounting_transaction_buttons": buttons}
        ), patch.object(
            self.bridge, "_materialize_media", return_value=str(self.slip)
        ), patch.object(
            self.bridge, "call_akson_ocr", return_value=ocr_result
        ), patch.object(self.bridge.os, "makedirs"), patch(
            "builtins.open", mock_open()
        ):
            result = context.hooks["pre_gateway_dispatch"](
                event, gateway=types.SimpleNamespace(adapters={})
            )

        self.assertEqual(result, {"action": "skip"})
        self.assertEqual(len(handed_off), 1)
        self.assertEqual(handed_off[0]["parsed"]["reference_no"], REFERENCE_NO)
        self.assertEqual(ocr_result["parsed"], {})
        self.assertEqual(
            handed_off[0]["raw_ocr_text"], ocr_result["raw_ocr_text"]
        )

    def test_phase_d_markdown_ocr_creates_durable_transaction(self):
        fixture = self.fixture()
        variants = [fixture["raw_ocr_text"], *fixture["markdown_variants"]]
        for index, markdown in enumerate(variants):
            with self.subTest(markdown=markdown):
                ocr_result = dict(fixture, raw_ocr_text=markdown)
                normalized = self.bridge._normalize_ocr_result_for_handoff(
                    ocr_result
                )
                tenant_id = f"phase-d-synthetic-tenant-{index}"
                created = self.controller.begin_from_ocr(
                    tenant_id=tenant_id,
                    chat_id="phase-d-synthetic-chat",
                    thread_id=None,
                    session_id=f"phase-d-synthetic-session-{index}",
                    handoff_id=f"phase-d-synthetic-message-{index}",
                    telegram_user_id="phase-d-synthetic-user",
                    source_image_path=self.slip,
                    ocr_result=normalized,
                )

                durable = self.store.get_by_reference(tenant_id, REFERENCE_NO)
                self.assertIsNotNone(durable)
                self.assertEqual(
                    created["transaction_id"], durable["transaction_id"]
                )
                self.assertEqual(created["current_state"], "waiting_project")
                self.assertEqual(durable["reference_no"], REFERENCE_NO)
                self.assertEqual(
                    durable["ocr_fields"]["reference_no"], REFERENCE_NO
                )

    def test_phase_d_unlabeled_ocr_does_not_fabricate_reference(self):
        fixture = self.fixture()
        fixture["raw_ocr_text"] = (
            "วันที่ทำรายการ: 2026-09-01\nจำนวนเงิน: 1.00\n"
            "PHASED-SMOKE-20260901-01"
        )
        fixture["raw_response"] = {}

        normalized = self.bridge._normalize_ocr_result_for_handoff(fixture)

        self.assertNotIn("reference_no", normalized["parsed"])
        self.assertIsNone(
            self.store.get_by_reference("phase-d-synthetic-tenant", REFERENCE_NO)
        )


if __name__ == "__main__":
    unittest.main()
