import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "plugins/accounting-slip-bridge/transaction_flow.py"


def load_module():
    spec = importlib.util.spec_from_file_location("lekza_crash_recovery", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DurableCrashRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name) / "uploads"
        self.runtime_root.mkdir()
        self.slip_path = self.runtime_root / "synthetic-slip.jpg"
        self.slip_path.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
        self.db_path = Path(self.temp_dir.name) / "state" / "transactions.sqlite3"
        self.stores = []

    def tearDown(self):
        for store in reversed(self.stores):
            store.close()
        self.temp_dir.cleanup()

    def open_flow(self):
        store = self.module.SQLiteStateStore(self.db_path)
        self.stores.append(store)
        return self.module.TransactionFlow(
            store,
            allowed_source_roots=[self.runtime_root],
            projects=["Project A"],
        )

    @staticmethod
    def actor():
        return {
            "platform": "telegram",
            "chat_id": "chat-1",
            "telegram_user_id": "user-1",
        }

    def begin(self, flow, reference_no="SYNTHETIC-RECOVERY-001"):
        return flow.begin(
            tenant_id="tenant-a",
            platform="telegram",
            chat_id="chat-1",
            thread_id="thread-1",
            session_id="session-1",
            telegram_user_id="user-1",
            source_image_path=self.slip_path,
            ocr_result={
                "confidence": 0.97,
                "parsed": {
                    "reference_no": reference_no,
                    "amount": "750.00",
                    "date": "2026-08-30",
                    "payer": "Synthetic Payer",
                    "payee": "Synthetic Payee",
                    "note": "Synthetic recovery fixture",
                },
            },
        )

    def advance_to_review(self, flow, view):
        view = flow.choose(
            view["transaction_id"],
            expected_version=view["version"],
            action="select_project",
            value="Project A",
            **self.actor(),
        )
        self.assertEqual(view["current_state"], "waiting_user")
        view = flow.choose(
            view["transaction_id"],
            expected_version=view["version"],
            action="use_sender",
            **self.actor(),
        )
        self.assertEqual(view["current_state"], "waiting_type")
        view = flow.choose(
            view["transaction_id"],
            expected_version=view["version"],
            action="expense",
            **self.actor(),
        )
        self.assertEqual(view["current_state"], "waiting_category")
        view = flow.choose(
            view["transaction_id"],
            expected_version=view["version"],
            action="materials",
            **self.actor(),
        )
        self.assertEqual(view["current_state"], "waiting_review")
        return view

    def test_restart_after_confirm_before_drive_recovers_confirmed_intent(self):
        flow = self.open_flow()
        review = self.advance_to_review(flow, self.begin(flow))

        intent = flow.confirm(
            review["transaction_id"],
            expected_version=review["version"],
            **self.actor(),
        )
        self.assertEqual(intent["current_state"], "confirmed_intent")

        self.stores.pop().close()
        recovered_flow = self.open_flow()
        recovered = recovered_flow.get_transaction(
            intent["transaction_id"], **self.actor()
        )
        self.assertEqual(recovered["current_state"], "confirmed_intent")
        self.assertIsNone(recovered["drive_file_id"])
        self.assertIsNone(recovered["slip_url"])
        self.assertIsNone(recovered["sheets_row_identity"])

    def test_restart_after_drive_before_sheets_recovers_upload_identity(self):
        flow = self.open_flow()
        review = self.advance_to_review(
            flow, self.begin(flow, "SYNTHETIC-RECOVERY-DRIVE")
        )
        intent = flow.confirm(
            review["transaction_id"],
            expected_version=review["version"],
            **self.actor(),
        )
        drive_pending = flow.mark_drive_pending(
            intent["transaction_id"],
            expected_version=intent["version"],
            **self.actor(),
        )
        uploaded = flow.mark_drive_uploaded(
            drive_pending["transaction_id"],
            expected_version=drive_pending["version"],
            file_id="synthetic-file-001",
            web_view_link="https://drive.example/synthetic-file-001",
            **self.actor(),
        )
        self.assertEqual(uploaded["current_state"], "drive_uploaded")

        self.stores.pop().close()
        recovered_flow = self.open_flow()
        recovered = recovered_flow.get_transaction(
            uploaded["transaction_id"], **self.actor()
        )
        self.assertEqual(recovered["current_state"], "drive_uploaded")
        self.assertEqual(recovered["drive_file_id"], "synthetic-file-001")
        self.assertEqual(
            recovered["slip_url"], "https://drive.example/synthetic-file-001"
        )

        sheets_pending = recovered_flow.mark_sheets_pending(
            recovered["transaction_id"],
            expected_version=recovered["version"],
            **self.actor(),
        )
        self.assertEqual(sheets_pending["current_state"], "sheets_pending")

    def test_restart_after_sheets_before_cleanup_recovers_confirmed_row(self):
        flow = self.open_flow()
        review = self.advance_to_review(
            flow, self.begin(flow, "SYNTHETIC-RECOVERY-SHEETS")
        )
        intent = flow.confirm(
            review["transaction_id"],
            expected_version=review["version"],
            **self.actor(),
        )
        drive_pending = flow.mark_drive_pending(
            intent["transaction_id"],
            expected_version=intent["version"],
            **self.actor(),
        )
        uploaded = flow.mark_drive_uploaded(
            drive_pending["transaction_id"],
            expected_version=drive_pending["version"],
            file_id="synthetic-file-002",
            web_view_link="https://drive.example/synthetic-file-002",
            **self.actor(),
        )
        sheets_pending = flow.mark_sheets_pending(
            uploaded["transaction_id"],
            expected_version=uploaded["version"],
            **self.actor(),
        )
        confirmed = flow.mark_confirmed(
            sheets_pending["transaction_id"],
            expected_version=sheets_pending["version"],
            sheets_row_identity="Transactions!A42:Q42",
            **self.actor(),
        )
        self.assertEqual(confirmed["current_state"], "confirmed")

        self.stores.pop().close()
        recovered_flow = self.open_flow()
        recovered = recovered_flow.get_transaction(
            confirmed["transaction_id"], **self.actor()
        )
        self.assertEqual(recovered["current_state"], "confirmed")
        self.assertEqual(recovered["sheets_row_identity"], "Transactions!A42:Q42")
        self.assertEqual(recovered["drive_file_id"], "synthetic-file-002")
        self.assertEqual(
            recovered["slip_url"], "https://drive.example/synthetic-file-002"
        )

    def test_callback_replay_is_rejected_without_second_transition(self):
        flow = self.open_flow()
        initial = self.begin(flow, "SYNTHETIC-REPLAY-001")
        advanced = flow.choose(
            initial["transaction_id"],
            expected_version=initial["version"],
            action="select_project",
            value="Project A",
            **self.actor(),
        )

        with self.assertRaises(self.module.StaleStateError):
            flow.choose(
                initial["transaction_id"],
                expected_version=initial["version"],
                action="select_project",
                value="Project A",
                **self.actor(),
            )

        recovered = flow.get_transaction(initial["transaction_id"], **self.actor())
        self.assertEqual(recovered["current_state"], "waiting_user")
        self.assertEqual(recovered["version"], advanced["version"])

    def test_concurrent_confirm_requests_converge_on_one_durable_intent(self):
        first_flow = self.open_flow()
        review = self.advance_to_review(
            first_flow, self.begin(first_flow, "SYNTHETIC-CONCURRENT-CONFIRM")
        )
        second_flow = self.open_flow()
        second_view = second_flow.get_transaction(
            review["transaction_id"], **self.actor()
        )
        self.assertEqual(second_view["version"], review["version"])

        first_result = first_flow.confirm(
            review["transaction_id"],
            expected_version=review["version"],
            **self.actor(),
        )
        second_result = second_flow.confirm(
            review["transaction_id"],
            expected_version=second_view["version"],
            **self.actor(),
        )

        self.assertEqual(first_result["current_state"], "confirmed_intent")
        self.assertEqual(second_result, first_result)
        durable = first_flow.get_transaction(review["transaction_id"], **self.actor())
        self.assertEqual(durable["version"], review["version"] + 1)

    def test_stale_button_cannot_rewind_newer_state(self):
        flow = self.open_flow()
        initial = self.begin(flow, "SYNTHETIC-STALE-BUTTON")
        waiting_user = flow.choose(
            initial["transaction_id"],
            expected_version=initial["version"],
            action="select_project",
            value="Project A",
            **self.actor(),
        )
        waiting_type = flow.choose(
            waiting_user["transaction_id"],
            expected_version=waiting_user["version"],
            action="use_sender",
            **self.actor(),
        )

        with self.assertRaises(self.module.StaleStateError):
            flow.back(
                waiting_user["transaction_id"],
                expected_version=waiting_user["version"],
                **self.actor(),
            )

        durable = flow.get_transaction(waiting_type["transaction_id"], **self.actor())
        self.assertEqual(durable["current_state"], "waiting_type")
        self.assertEqual(durable["version"], waiting_type["version"])

    def test_failed_external_stage_is_restart_safe_and_retryable(self):
        flow = self.open_flow()
        review = self.advance_to_review(
            flow, self.begin(flow, "SYNTHETIC-RETRY-STATE")
        )
        intent = flow.confirm(
            review["transaction_id"],
            expected_version=review["version"],
            **self.actor(),
        )
        drive_pending = flow.mark_drive_pending(
            intent["transaction_id"],
            expected_version=intent["version"],
            **self.actor(),
        )
        failed = flow.mark_failed(
            drive_pending["transaction_id"],
            expected_version=drive_pending["version"],
            error_code="DRIVE_TRANSIENT",
            **self.actor(),
        )
        self.assertEqual(failed["current_state"], "failed")

        self.stores.pop().close()
        recovered_flow = self.open_flow()
        recovered = recovered_flow.get_transaction(
            failed["transaction_id"], **self.actor()
        )
        self.assertEqual(recovered["retry_count"], 1)
        self.assertEqual(recovered["retry_state"], "drive_pending")
        self.assertEqual(recovered["last_error_code"], "DRIVE_TRANSIENT")

        retrying = recovered_flow.retry(
            recovered["transaction_id"],
            expected_version=recovered["version"],
            **self.actor(),
        )
        self.assertEqual(retrying["current_state"], "drive_pending")


if __name__ == "__main__":
    unittest.main()
