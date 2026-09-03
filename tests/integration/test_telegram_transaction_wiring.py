import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import mock_open, patch


ROOT = Path(__file__).resolve().parents[2]
FLOW_PATH = ROOT / "plugins/accounting-slip-bridge/transaction_flow.py"
WIRING_PATH = ROOT / "plugins/accounting-slip-bridge/telegram_wiring.py"
PLUGIN_PATH = ROOT / "plugins/accounting-transaction-buttons/__init__.py"
BRIDGE_PATH = ROOT / "plugins/accounting-slip-bridge/__init__.py"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeSavePipeline:
    """Exercises the real durable save states without external APIs."""

    def __init__(self, flow, fail_once=False):
        self.flow = flow
        self.fail_once = fail_once
        self.calls = 0

    def save(self, transaction_id, **actor):
        self.calls += 1
        record = self.flow.get_transaction(transaction_id, **actor)
        if record["current_state"] == "confirmed_intent":
            record = self.flow.mark_drive_pending(
                transaction_id, expected_version=record["version"], **actor
            )
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("synthetic external failure")
        if record["current_state"] == "drive_pending":
            record = self.flow.reserve_drive_upload(
                transaction_id,
                expected_version=record["version"],
                file_id=f"drive-{transaction_id}",
                **actor,
            )
            record = self.flow.mark_drive_uploaded(
                transaction_id,
                expected_version=record["version"],
                file_id=f"drive-{transaction_id}",
                web_view_link=f"https://drive.test/{transaction_id}",
                **actor,
            )
        if record["current_state"] == "drive_uploaded":
            record = self.flow.mark_sheets_pending(
                transaction_id, expected_version=record["version"], **actor
            )
        if record["current_state"] == "sheets_pending":
            record = self.flow.mark_confirmed(
                transaction_id,
                expected_version=record["version"],
                sheets_row_identity=f"Transactions!A:{transaction_id}",
                **actor,
            )
        return record


class TelegramTransactionWiringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        self.slip = self.uploads / "synthetic-slip.jpg"
        self.slip.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
        self.flow_module = load_module("lekza_phase_c_flow", FLOW_PATH)
        self.wiring = load_module("lekza_phase_c_wiring", WIRING_PATH)
        self.db_path = self.root / "state" / "transactions.sqlite3"
        self.store = self.flow_module.SQLiteStateStore(self.db_path)
        self.flow = self.flow_module.TransactionFlow(
            self.store,
            allowed_source_roots=[self.uploads],
            projects=["Project A", "Project B"],
        )
        self.actor = {
            "platform": "telegram",
            "chat_id": "1001",
            "telegram_user_id": "2002",
        }
        self.pipeline = FakeSavePipeline(self.flow)
        self.controller = self.wiring.TelegramTransactionController(
            self.flow, self.pipeline, projects=["Project A", "Project B"]
        )
        self.record = self.flow.begin(
            tenant_id="tenant-test",
            platform="telegram",
            chat_id="1001",
            thread_id=None,
            session_id="session-test",
            telegram_user_id="2002",
            source_image_path=self.slip,
            ocr_result={
                "confidence": 0.99,
                "parsed": {"reference_no": "PHASE-C-001", "amount": "1250.50"},
            },
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def callback(self, prompt, action, label=None):
        for button in prompt["buttons"]:
            identity = self.wiring.decode_callback(button["callback_data"])
            if identity.action == action and (label is None or button["label"] == label):
                return button["callback_data"]
        self.fail(f"callback action {action!r} not rendered")

    def click(self, prompt, action, label=None, actor=None):
        return self.controller.handle_callback(
            self.callback(prompt, action, label), **(actor or self.actor)
        )

    def advance_to_review(self):
        prompt = self.controller.render(self.record["transaction_id"], **self.actor)
        prompt = self.click(prompt, "select_project", "Project A")["prompt"]
        prompt = self.click(prompt, "use_sender")["prompt"]
        prompt = self.click(prompt, "expense")["prompt"]
        return self.click(prompt, "materials")["prompt"]

    def expire_prompt_lease(self, transaction_id):
        with self.store._connection:
            self.store._connection.execute(
                """
                UPDATE transaction_state
                SET initial_prompt_lease_expires_at = '2000-01-01T00:00:00+00:00'
                WHERE transaction_id = ?
                """,
                (transaction_id,),
            )

    def test_project_selection_callback_uses_transaction_identity(self):
        prompt = self.controller.render(self.record["transaction_id"], **self.actor)
        payload = self.callback(prompt, "select_project", "Project A")
        identity = self.wiring.decode_callback(payload)
        result = self.controller.handle_callback(payload, **self.actor)

        self.assertEqual(identity.transaction_id, self.record["transaction_id"])
        self.assertEqual(identity.expected_version, self.record["version"])
        self.assertLessEqual(len(payload.encode("utf-8")), 64)
        self.assertTrue(result["ok"])
        self.assertEqual(result["prompt"]["current_state"], "waiting_user")

    def test_user_type_and_category_callbacks_follow_durable_transitions(self):
        prompt = self.controller.render(self.record["transaction_id"], **self.actor)
        prompt = self.click(prompt, "select_project", "Project A")["prompt"]
        prompt = self.click(prompt, "use_sender")["prompt"]
        prompt = self.click(prompt, "expense")["prompt"]
        result = self.click(prompt, "materials")

        durable = self.flow.get_transaction(self.record["transaction_id"], **self.actor)
        self.assertEqual(result["prompt"]["current_state"], "waiting_review")
        self.assertEqual(durable["selected_user_id"], "2002")
        self.assertEqual(durable["transaction_type"], "expense")
        self.assertEqual(durable["category"], "materials")

    def test_missing_amount_prompts_for_manual_value_before_any_save(self):
        record = self.flow.begin(
            tenant_id="tenant-test", platform="telegram", chat_id="1001",
            thread_id=None, session_id="missing-amount", telegram_user_id="2002",
            source_image_path=self.slip,
            ocr_result={"parsed": {"reference_no": "PHASE-C-NO-AMOUNT", "amount": None}},
        )
        prompt = self.controller.render(record["transaction_id"], **self.actor)
        self.assertTrue(prompt["manual_input_required"])
        self.assertIn("ยอดเงิน", prompt["text"])
        self.assertEqual(self.pipeline.calls, 0)

        invalid = self.controller.handle_manual_message("not-a-number", **self.actor)
        self.assertEqual(invalid["error_code"], "validation_error")
        self.assertNotIn(invalid["error_code"], {"DRIVE_TRANSIENT", "SHEETS_TRANSIENT"})
        self.assertEqual(self.pipeline.calls, 0)

        valid = self.controller.handle_manual_message("250.75", **self.actor)
        self.assertTrue(valid["ok"])
        self.assertEqual(valid["prompt"]["current_state"], "waiting_project")
        self.assertEqual(self.pipeline.calls, 0)

    def test_save_validation_error_is_not_classified_as_external_transient(self):
        class ValidationPipeline:
            def save(self, transaction_id, **actor):
                raise ValueError("Transaction amount must be valid")

        controller = self.wiring.TelegramTransactionController(
            self.flow, ValidationPipeline(), projects=["Project A", "Project B"]
        )
        review = self.advance_to_review()
        result = controller.handle_callback(
            self.callback(review, "confirm"), **self.actor
        )
        durable = self.flow.get_transaction(self.record["transaction_id"], **self.actor)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "validation_error")
        self.assertIsNone(durable["last_error_code"])
        self.assertEqual(durable["current_state"], "confirmed_intent")

    def test_back_and_cancel_use_durable_state(self):
        prompt = self.controller.render(self.record["transaction_id"], **self.actor)
        prompt = self.click(prompt, "select_project", "Project A")["prompt"]
        prompt = self.click(prompt, "back")["prompt"]
        cancelled = self.click(prompt, "cancel")

        self.assertEqual(prompt["current_state"], "waiting_project")
        self.assertEqual(cancelled["prompt"]["current_state"], "cancelled")

    def test_manual_entry_survives_restart_and_back_clears_entry_mode(self):
        prompt = self.controller.render(self.record["transaction_id"], **self.actor)
        manual = self.click(prompt, "manual_project")["prompt"]
        self.assertTrue(manual["manual_input_required"])
        self.assertIn("พิมพ์ชื่อโครงการ", manual["text"])

        restarted_store = self.flow_module.SQLiteStateStore(self.db_path)
        try:
            restarted_flow = self.flow_module.TransactionFlow(
                restarted_store,
                allowed_source_roots=[self.uploads],
                projects=["Project A", "Project B"],
            )
            restarted = self.wiring.TelegramTransactionController(
                restarted_flow, FakeSavePipeline(restarted_flow),
                projects=["Project A", "Project B"],
            )
            submitted = restarted.handle_manual_message(
                "Project Restarted", **self.actor
            )
            self.assertEqual(submitted["prompt"]["current_state"], "waiting_user")
        finally:
            restarted_store.close()

        second = self.flow.begin(
            tenant_id="tenant-test",
            platform="telegram",
            chat_id="1001",
            thread_id=None,
            session_id="session-back",
            telegram_user_id="2002",
            source_image_path=self.slip,
            ocr_result={"parsed": {"reference_no": "PHASE-C-002", "amount": 1}},
        )
        manual = self.controller.render(second["transaction_id"], **self.actor)
        manual = self.click(manual, "manual_project")["prompt"]
        backed = self.click(manual, "back")["prompt"]
        self.assertFalse(backed["manual_input_required"])

    def test_confirm_invokes_save_pipeline_and_is_idempotent(self):
        review = self.advance_to_review()
        confirm_payload = self.callback(review, "confirm")
        first = self.controller.handle_callback(confirm_payload, **self.actor)
        duplicate = self.controller.handle_callback(confirm_payload, **self.actor)

        self.assertEqual(first["prompt"]["current_state"], "confirmed")
        self.assertEqual(duplicate["prompt"]["current_state"], "confirmed")
        self.assertEqual(self.pipeline.calls, 1)

    def test_prompt_crash_before_telegram_send_is_retryable(self):
        transaction_id = self.record["transaction_id"]
        abandoned = self.controller.acquire_initial_prompt_delivery(
            transaction_id, **self.actor
        )
        self.expire_prompt_lease(transaction_id)
        recovered = self.controller.acquire_initial_prompt_delivery(
            transaction_id, **self.actor
        )

        self.assertIsNotNone(abandoned)
        self.assertIsNotNone(recovered)
        self.assertNotEqual(abandoned.owner_id, recovered.owner_id)
        durable = self.flow.get_transaction(transaction_id, **self.actor)
        self.assertEqual(durable["initial_prompt_state"], "delivering")
        self.assertEqual(durable["initial_prompt_attempt_count"], 2)
        self.controller.complete_initial_prompt(
            recovered, message_id="recovered-before-send", **self.actor
        )

    def test_prompt_crash_after_send_before_persistence_may_retry_delivery(self):
        transaction_id = self.record["transaction_id"]
        accepted_messages = []
        first = self.controller.acquire_initial_prompt_delivery(
            transaction_id, **self.actor
        )
        accepted_messages.append("telegram-accepted-1")
        self.expire_prompt_lease(transaction_id)
        second = self.controller.acquire_initial_prompt_delivery(
            transaction_id, **self.actor
        )
        accepted_messages.append("telegram-accepted-2")
        self.controller.complete_initial_prompt(
            second, message_id=accepted_messages[-1], **self.actor
        )

        durable = self.flow.get_transaction(transaction_id, **self.actor)
        self.assertIsNotNone(first)
        self.assertEqual(len(accepted_messages), 2)
        self.assertEqual(durable["initial_prompt_state"], "delivered")
        self.assertEqual(durable["initial_prompt_attempt_count"], 2)
        self.assertEqual(
            durable["initial_prompt_message_id"], "telegram-accepted-2"
        )

    def test_restart_takes_over_expired_prompt_lease(self):
        transaction_id = self.record["transaction_id"]
        original = self.controller.acquire_initial_prompt_delivery(
            transaction_id, **self.actor
        )
        self.assertIsNone(
            self.controller.acquire_initial_prompt_delivery(
                transaction_id, **self.actor
            )
        )
        self.expire_prompt_lease(transaction_id)

        restarted_store = self.flow_module.SQLiteStateStore(self.db_path)
        try:
            restarted_flow = self.flow_module.TransactionFlow(
                restarted_store,
                allowed_source_roots=[self.uploads],
                projects=["Project A", "Project B"],
            )
            restarted = self.wiring.TelegramTransactionController(
                restarted_flow,
                FakeSavePipeline(restarted_flow),
                projects=["Project A", "Project B"],
            )
            takeover = restarted.acquire_initial_prompt_delivery(
                transaction_id, **self.actor
            )
            self.assertIsNotNone(takeover)
            self.assertNotEqual(original.owner_id, takeover.owner_id)
            restarted.release_initial_prompt_delivery(takeover)
        finally:
            restarted_store.close()

    def test_already_delivered_prompt_replay_does_not_acquire_or_resend(self):
        transaction_id = self.record["transaction_id"]
        claim = self.controller.acquire_initial_prompt_delivery(
            transaction_id, **self.actor
        )
        self.controller.complete_initial_prompt(
            claim, message_id="telegram-delivered", **self.actor
        )

        replay = self.controller.acquire_initial_prompt_delivery(
            transaction_id, **self.actor
        )
        durable = self.flow.get_transaction(transaction_id, **self.actor)
        self.assertIsNone(replay)
        self.assertEqual(durable["initial_prompt_state"], "delivered")
        self.assertEqual(durable["initial_prompt_attempt_count"], 1)
        self.assertEqual(durable["initial_prompt_message_id"], "telegram-delivered")

    def test_retry_after_external_failure_resumes_durable_save(self):
        self.pipeline.fail_once = True
        review = self.advance_to_review()
        failed = self.click(review, "confirm")
        retried = self.click(failed["prompt"], "retry")

        self.assertFalse(failed["ok"])
        self.assertEqual(failed["prompt"]["current_state"], "failed")
        self.assertEqual(retried["prompt"]["current_state"], "confirmed")
        self.assertEqual(self.pipeline.calls, 2)

    def test_stale_callback_does_not_mutate(self):
        initial = self.controller.render(self.record["transaction_id"], **self.actor)
        stale_cancel = self.callback(initial, "cancel")
        self.click(initial, "select_project", "Project A")
        before = self.flow.get_transaction(self.record["transaction_id"], **self.actor)
        result = self.controller.handle_callback(stale_cancel, **self.actor)
        after = self.flow.get_transaction(self.record["transaction_id"], **self.actor)

        self.assertEqual(result["error_code"], "stale_callback")
        self.assertEqual(after["version"], before["version"])
        self.assertEqual(after["current_state"], "waiting_user")

    def test_wrong_telegram_user_does_not_mutate(self):
        prompt = self.controller.render(self.record["transaction_id"], **self.actor)
        before = self.flow.get_transaction(self.record["transaction_id"], **self.actor)
        wrong_actor = dict(self.actor, telegram_user_id="intruder")
        result = self.click(prompt, "cancel", actor=wrong_actor)
        after = self.flow.get_transaction(self.record["transaction_id"], **self.actor)

        self.assertEqual(result["error_code"], "unauthorized")
        self.assertEqual(after["version"], before["version"])

    def test_restart_between_callbacks_renders_from_sqlite(self):
        prompt = self.controller.render(self.record["transaction_id"], **self.actor)
        waiting_user = self.click(prompt, "select_project", "Project A")["prompt"]
        payload = self.callback(waiting_user, "use_sender")

        restarted_store = self.flow_module.SQLiteStateStore(self.db_path)
        try:
            restarted_flow = self.flow_module.TransactionFlow(
                restarted_store,
                allowed_source_roots=[self.uploads],
                projects=["Project A", "Project B"],
            )
            restarted = self.wiring.TelegramTransactionController(
                restarted_flow, FakeSavePipeline(restarted_flow),
                projects=["Project A", "Project B"],
            )
            result = restarted.handle_callback(payload, **self.actor)
            self.assertEqual(result["prompt"]["current_state"], "waiting_type")
        finally:
            restarted_store.close()

    def test_malformed_callback_fails_closed_without_mutation(self):
        before = self.flow.get_transaction(self.record["transaction_id"], **self.actor)
        result = self.controller.handle_callback("lk:not-valid", **self.actor)
        after = self.flow.get_transaction(self.record["transaction_id"], **self.actor)

        self.assertEqual(result["error_code"], "malformed_callback")
        self.assertEqual(after["version"], before["version"])
        self.assertEqual(after["current_state"], before["current_state"])

    def test_runtime_patch_intercepts_lekza_callback_and_delegates_others(self):
        prompt = self.controller.render(self.record["transaction_id"], **self.actor)
        cancel_payload = self.callback(prompt, "cancel")
        original_calls = []

        class Adapter:
            async def _handle_callback_query(self, update, context):
                original_calls.append(update.callback_query.data)

            async def _handle_text_message(self, update, context):
                original_calls.append("text")

        class Button:
            def __init__(self, text, callback_data):
                self.text = text
                self.callback_data = callback_data

        class Markup:
            def __init__(self, rows):
                self.inline_keyboard = rows

        fake_name = "phase_c_fake_telegram_adapter"
        fake_module = types.ModuleType(fake_name)
        fake_module.TelegramAdapter = Adapter
        fake_module.InlineKeyboardButton = Button
        fake_module.InlineKeyboardMarkup = Markup
        sys.modules[fake_name] = fake_module
        plugin = load_module("lekza_phase_c_plugin", PLUGIN_PATH)
        plugin._set_controller_for_tests(self.controller)
        self.assertTrue(plugin._patch_module(fake_name))

        class Query:
            def __init__(self, data):
                self.data = data
                self.message = types.SimpleNamespace(chat_id=1001)
                self.from_user = types.SimpleNamespace(id=2002)
                self.answers = []
                self.edits = []

            async def answer(self, **kwargs):
                self.answers.append(kwargs)

            async def edit_message_text(self, **kwargs):
                self.edits.append(kwargs)

        try:
            with patch.dict(os.environ, {"LEKZA_RUNTIME_ENV": "production"}):
                adapter = Adapter()
                lekza_query = Query(cancel_payload)
                asyncio.run(adapter._handle_callback_query(
                    types.SimpleNamespace(callback_query=lekza_query), None
                ))
                other_query = Query("cl:existing:0")
                asyncio.run(adapter._handle_callback_query(
                    types.SimpleNamespace(callback_query=other_query), None
                ))
        finally:
            sys.modules.pop(fake_name, None)

        self.assertEqual(original_calls, ["cl:existing:0"])
        self.assertEqual(len(lekza_query.answers), 1)
        self.assertEqual(len(lekza_query.edits), 1)
        durable = self.flow.get_transaction(self.record["transaction_id"], **self.actor)
        self.assertEqual(durable["current_state"], "cancelled")

    def test_runtime_patch_consumes_pending_reference_from_effective_message(self):
        pending = self.controller.begin_from_ocr(
            tenant_id="1001",
            chat_id="1001",
            thread_id=None,
            session_id="effective-message-session",
            handoff_id="effective-message-handoff",
            telegram_user_id="2002",
            source_image_path=self.slip,
            ocr_result={"parsed": {}, "confidence": 1.0},
        )
        original_calls = []
        replies = []

        class Adapter:
            async def _handle_callback_query(self, update, context):
                return None

            async def _handle_text_message(self, update, context):
                original_calls.append("text")

        class Button:
            def __init__(self, text, callback_data):
                self.text = text
                self.callback_data = callback_data

        class Markup:
            def __init__(self, rows):
                self.inline_keyboard = rows

        class Message:
            text = "MANUAL-REFERENCE-001"
            chat_id = 1001
            from_user = types.SimpleNamespace(id=2002)

            async def reply_text(self, text, **kwargs):
                replies.append((text, kwargs))

        fake_name = "phase_c_effective_message_adapter"
        fake_module = types.ModuleType(fake_name)
        fake_module.TelegramAdapter = Adapter
        fake_module.InlineKeyboardButton = Button
        fake_module.InlineKeyboardMarkup = Markup
        sys.modules[fake_name] = fake_module
        plugin = load_module("lekza_phase_c_effective_message_plugin", PLUGIN_PATH)
        plugin._set_controller_for_tests(self.controller)
        self.assertTrue(plugin._patch_module(fake_name))

        update = types.SimpleNamespace(
            effective_message=Message(),
            effective_user=types.SimpleNamespace(id=2002),
        )
        try:
            with patch.dict(
                os.environ, {"LEKZA_RUNTIME_ENV": "production"}
            ), self.assertLogs(
                "lekza.accounting_transaction_buttons", level="INFO"
            ) as captured:
                adapter = Adapter()
                asyncio.run(adapter._handle_text_message(update, None))

            success_logs = "\n".join(captured.output)
            self.assertIn("message_present=True", success_logs)
            self.assertIn("text_present=True", success_logs)
            self.assertIn("manual result present=True", success_logs)
            self.assertIn("fallback original=false", success_logs)
            self.assertNotIn(Message.text, success_logs)
            self.assertNotIn("1001", success_logs)
            self.assertNotIn("2002", success_logs)

            sensitive_exception = "token=synthetic-secret raw customer text"
            with patch.dict(
                os.environ, {"LEKZA_RUNTIME_ENV": "production"}
            ), patch.object(
                self.controller,
                "handle_manual_message",
                side_effect=RuntimeError(sensitive_exception),
            ), self.assertLogs(
                "lekza.accounting_transaction_buttons", level="WARNING"
            ) as failed:
                asyncio.run(adapter._handle_text_message(update, None))

            failure_logs = "\n".join(failed.output)
            self.assertIn("error_type=RuntimeError", failure_logs)
            self.assertIn("fallback=true", failure_logs)
            self.assertNotIn(sensitive_exception, failure_logs)
            self.assertNotIn(Message.text, failure_logs)
            self.assertNotIn("1001", failure_logs)
            self.assertNotIn("2002", failure_logs)
        finally:
            sys.modules.pop(fake_name, None)

        durable = self.flow.get_transaction(pending["transaction_id"], **self.actor)
        self.assertEqual(original_calls, ["text"])
        self.assertEqual(durable["reference_no"], "MANUAL-REFERENCE-001")
        self.assertFalse(durable["needs_reference"])
        self.assertEqual(len(replies), 1)

    def test_runtime_patch_rebinds_already_registered_text_handler(self):
        pending = self.controller.begin_from_ocr(
            tenant_id="1001",
            chat_id="1001",
            thread_id=None,
            session_id="registered-handler-session",
            handoff_id="registered-handler-handoff",
            telegram_user_id="2002",
            source_image_path=self.slip,
            ocr_result={"parsed": {}, "confidence": 1.0},
        )
        original_calls = []
        replies = []

        class Adapter:
            async def _handle_callback_query(self, update, context):
                original_calls.append("callback")

            async def _handle_text_message(self, update, context):
                original_calls.append("text")

        class Handler:
            def __init__(self, callback):
                self.callback = callback

        class Button:
            def __init__(self, text, callback_data):
                self.text = text
                self.callback_data = callback_data

        class Markup:
            def __init__(self, rows):
                self.inline_keyboard = rows

        class Message:
            text = "REGISTERED-HANDLER-REFERENCE"
            chat_id = 1001
            from_user = types.SimpleNamespace(id=2002)

            async def reply_text(self, text, **kwargs):
                replies.append((text, kwargs))

        fake_name = "phase_c_registered_telegram_adapter"
        Adapter.__module__ = fake_name
        fake_module = types.ModuleType(fake_name)
        fake_module.TelegramAdapter = Adapter
        fake_module.InlineKeyboardButton = Button
        fake_module.InlineKeyboardMarkup = Markup
        sys.modules[fake_name] = fake_module
        plugin = load_module("lekza_phase_c_registered_plugin", PLUGIN_PATH)
        plugin._set_controller_for_tests(self.controller)

        adapter = Adapter()
        registered_text = Handler(adapter._handle_text_message)
        registered_callback = Handler(adapter._handle_callback_query)
        adapter._app = types.SimpleNamespace(
            handlers={0: [registered_text, registered_callback]}
        )
        gateway = types.SimpleNamespace(adapters={"telegram": adapter})
        update = types.SimpleNamespace(
            effective_message=Message(),
            effective_user=types.SimpleNamespace(id=2002),
        )

        try:
            with patch.dict(
                os.environ, {"LEKZA_RUNTIME_ENV": "production"}
            ), self.assertLogs(
                "lekza.accounting_transaction_buttons", level="INFO"
            ) as captured:
                self.assertIs(plugin._telegram_adapter(gateway), adapter)
                asyncio.run(registered_text.callback(update, None))

            logs = "\n".join(captured.output)
            self.assertIn("registered handler rebind text=1 callback=1", logs)
            self.assertIn("fallback original=false", logs)
            self.assertEqual(original_calls, [])
            self.assertIs(
                registered_text.callback.__func__, Adapter._handle_text_message
            )
            self.assertIs(
                registered_callback.callback.__func__, Adapter._handle_callback_query
            )
        finally:
            sys.modules.pop(fake_name, None)

        durable = self.flow.get_transaction(pending["transaction_id"], **self.actor)
        self.assertEqual(durable["reference_no"], "REGISTERED-HANDLER-REFERENCE")
        self.assertFalse(durable["needs_reference"])
        self.assertEqual(len(replies), 1)

    def test_plugin_registration_rebinds_handlers_captured_before_patch(self):
        original_calls = []

        class Adapter:
            async def _handle_callback_query(self, update, context):
                original_calls.append("callback")

            async def _handle_text_message(self, update, context):
                original_calls.append("text")

        class Handler:
            def __init__(self, callback):
                self.callback = callback

        fake_name = "hermes_plugins.telegram_platform.adapter"
        Adapter.__module__ = fake_name
        fake_module = types.ModuleType(fake_name)
        fake_module.TelegramAdapter = Adapter
        sys.modules[fake_name] = fake_module
        adapter = Adapter()
        text_handler = Handler(adapter._handle_text_message)
        callback_handler = Handler(adapter._handle_callback_query)
        adapter._app = types.SimpleNamespace(
            handlers={0: [text_handler, callback_handler]}
        )
        plugin = load_module(
            "lekza_phase_c_registration_rebind_plugin", PLUGIN_PATH
        )

        class Context:
            def register_hook(self, name, callback):
                self.name = name
                self.callback = callback

        try:
            with self.assertLogs(
                "lekza.accounting_transaction_buttons", level="INFO"
            ) as captured, patch.object(
                plugin, "_start_registered_handler_watch", return_value=None
            ):
                plugin.register(Context())
            self.assertIn(
                "registered handler rebind text=1 callback=1",
                "\n".join(captured.output),
            )
            self.assertIs(
                text_handler.callback.__func__, Adapter._handle_text_message
            )
            self.assertIs(
                callback_handler.callback.__func__,
                Adapter._handle_callback_query,
            )
            self.assertEqual(original_calls, [])
        finally:
            sys.modules.pop(fake_name, None)

    def test_startup_watch_rebinds_adapter_created_after_plugin_registration(self):
        class Handler:
            def __init__(self, callback):
                self.callback = callback

        fake_name = "hermes_plugins.telegram_platform.adapter"
        previous = sys.modules.pop(fake_name, None)
        plugin = load_module("lekza_phase_c_startup_watch_plugin", PLUGIN_PATH)
        plugin._STARTUP_REBIND_TIMEOUT_SECONDS = 2.0

        class Context:
            def register_hook(self, name, callback):
                self.name = name
                self.callback = callback

        try:
            with self.assertLogs(
                "lekza.accounting_transaction_buttons", level="INFO"
            ) as captured:
                plugin.register(Context())

                class Adapter:
                    async def _handle_callback_query(self, update, context):
                        return None

                    async def _handle_text_message(self, update, context):
                        return None

                Adapter.__module__ = fake_name
                fake_module = types.ModuleType(fake_name)
                fake_module.TelegramAdapter = Adapter
                sys.modules[fake_name] = fake_module
                adapter = Adapter()
                text_handler = Handler(adapter._handle_text_message)
                callback_handler = Handler(adapter._handle_callback_query)
                adapter._app = types.SimpleNamespace(
                    handlers={0: [text_handler, callback_handler]}
                )
                deadline = time.monotonic() + 2.0
                while "startup handler ready text=1 callback=1" not in "\n".join(
                    captured.output
                ):
                    if time.monotonic() >= deadline:
                        self.fail("startup handler watch did not become ready")
                    time.sleep(0.02)

            self.assertIs(
                text_handler.callback.__func__, Adapter._handle_text_message
            )
            self.assertIs(
                callback_handler.callback.__func__,
                Adapter._handle_callback_query,
            )
        finally:
            sys.modules.pop(fake_name, None)
            if previous is not None:
                sys.modules[fake_name] = previous

    def test_image_ocr_handoff_creates_once_and_renders_initial_buttons(self):
        sent = []

        class Adapter:
            async def _handle_callback_query(self, update, context):
                return None

            async def _handle_text_message(self, update, context):
                return None

        class Button:
            def __init__(self, text, callback_data):
                self.text = text
                self.callback_data = callback_data

        class Markup:
            def __init__(self, rows):
                self.inline_keyboard = rows

        class Bot:
            async def send_message(self, **kwargs):
                sent.append(kwargs)
                return types.SimpleNamespace(message_id="telegram-message-1")

        fake_name = "phase_c_handoff_telegram_adapter"
        Adapter.__module__ = fake_name
        fake_module = types.ModuleType(fake_name)
        fake_module.TelegramAdapter = Adapter
        fake_module.InlineKeyboardButton = Button
        fake_module.InlineKeyboardMarkup = Markup
        sys.modules[fake_name] = fake_module
        plugin = load_module("lekza_phase_c_handoff_plugin", PLUGIN_PATH)
        plugin._set_controller_for_tests(self.controller)

        class Context:
            def __init__(self):
                self.hooks = {}

            def register_hook(self, name, callback):
                self.hooks[name] = callback

        plugin_context = Context()
        plugin.register(plugin_context)
        bridge = load_module("lekza_phase_c_bridge", BRIDGE_PATH)
        bridge_context = Context()
        bridge.register(bridge_context)
        adapter = Adapter()
        adapter._bot = Bot()
        adapter._bot.id = "3001"
        gateway = types.SimpleNamespace(adapters={"telegram": adapter})
        source = types.SimpleNamespace(
            platform="telegram",
            chat_id="1001",
            user_id="2002",
            thread_id=None,
        )
        event = types.SimpleNamespace(
            source=source,
            media_urls=["https://example.invalid/synthetic-slip.jpg"],
            media_types=["image/jpeg"],
            message_id="image-message-1",
        )
        retry_slip = self.uploads / "synthetic-slip-recached.jpg"
        retry_slip.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg-recached")
        session_store = types.SimpleNamespace(
            get_or_create_session=lambda source: types.SimpleNamespace(
                session_id="image-session-1"
            )
        )
        ocr_result = {
            "akson_called": True,
            "http_status": 200,
            "confidence": 0.97,
            "raw_ocr_text": "synthetic",
            "parsed": {"reference_no": "PHASE-C-HANDOFF", "amount": "9.50"},
            "usage": {},
        }
        restarted_stores = []
        observed_transaction_ids = []

        async def invoke_twice():
            hook = bridge_context.hooks["pre_gateway_dispatch"]
            hook(event, gateway=gateway, session_store=session_store)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            observed_transaction_ids.append(
                self.store.get_by_reference("1001", "PHASE-C-HANDOFF")[
                    "transaction_id"
                ]
            )
            restarted_store = self.flow_module.SQLiteStateStore(self.db_path)
            restarted_stores.append(restarted_store)
            restarted_flow = self.flow_module.TransactionFlow(
                restarted_store,
                allowed_source_roots=[self.uploads],
                projects=["Project A", "Project B"],
            )
            plugin._set_controller_for_tests(
                self.wiring.TelegramTransactionController(
                    restarted_flow,
                    FakeSavePipeline(restarted_flow),
                    projects=["Project A", "Project B"],
                )
            )
            event.media_urls = [str(retry_slip)]
            hook(event, gateway=gateway, session_store=session_store)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            observed_transaction_ids.append(
                restarted_store.get_by_reference("1001", "PHASE-C-HANDOFF")[
                    "transaction_id"
                ]
            )

        try:
            with patch.dict(os.environ, {"LEKZA_RUNTIME_ENV": "production"}), \
                    patch.object(
                        bridge,
                        "_materialize_media",
                        side_effect=lambda value: str(self.slip)
                        if value.startswith("https://") else value,
                    ), \
                    patch.object(bridge, "call_akson_ocr", return_value=ocr_result), \
                    patch.object(bridge.os, "makedirs"), \
                    patch("builtins.open", mock_open()):
                asyncio.run(invoke_twice())
        finally:
            for restarted_store in restarted_stores:
                restarted_store.close()
            sys.modules.pop(fake_name, None)
            sys.modules.pop("lekza_accounting_transaction_buttons", None)

        durable = self.store.get_by_reference("1001", "PHASE-C-HANDOFF")
        self.assertIsNotNone(durable)
        self.assertEqual(durable["initial_prompt_message_id"], "telegram-message-1")
        self.assertEqual(observed_transaction_ids, [durable["transaction_id"]] * 2)
        self.assertEqual(len(sent), 1)
        keyboard = sent[0]["reply_markup"].inline_keyboard
        callbacks = [button.callback_data for row in keyboard for button in row]
        self.assertTrue(callbacks)
        identities = [self.wiring.decode_callback(value) for value in callbacks]
        self.assertTrue(all(item.transaction_id == durable["transaction_id"] for item in identities))

    def test_incompatible_adapter_shape_fails_with_clear_diagnostic(self):
        class IncompatibleAdapter:
            async def _handle_callback_query(self, update, context):
                return None

        fake_name = "phase_c_incompatible_telegram_adapter"
        fake_module = types.ModuleType(fake_name)
        fake_module.TelegramAdapter = IncompatibleAdapter
        sys.modules[fake_name] = fake_module
        plugin = load_module("lekza_phase_c_incompatible_plugin", PLUGIN_PATH)
        try:
            with self.assertRaisesRegex(
                plugin.AdapterCompatibilityError,
                "missing callable handler.*_handle_text_message",
            ):
                plugin._patch_module(fake_name, strict=True)
        finally:
            sys.modules.pop(fake_name, None)


if __name__ == "__main__":
    unittest.main()
