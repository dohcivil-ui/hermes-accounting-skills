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
        buttons = types.SimpleNamespace(
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

        self.assertEqual(result["action"], "rewrite")
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
