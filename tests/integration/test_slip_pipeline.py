import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "plugins/accounting-slip-bridge/transaction_flow.py"


def load_module():
    spec = importlib.util.spec_from_file_location("lekza_transaction_flow", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeOcrReader:
    def __init__(self):
        self.calls = []

    def read(self, source_image_path):
        self.calls.append(source_image_path)
        return {
            "confidence": 0.98,
            "parsed": {
                "reference_no": "SYNTHETIC-REF-001",
                "amount": "1,250.50",
                "date": "2026-08-30",
                "payer": "Synthetic Payer",
                "payee": "Synthetic Payee",
                "note": "Synthetic fixture",
            },
        }


class NoWriteDrive:
    def upload(self, source_image_path, reference_no):
        raise AssertionError("Drive must not be called before Confirm")


class NoWriteSheets:
    def reference_exists(self, reference_no):
        raise AssertionError("Sheets must not be called before Confirm")

    def append_transaction(self, transaction):
        raise AssertionError("Sheets must not be called before Confirm")


class FakeDrive:
    def __init__(self, fail=False):
        self.fail = fail
        self.uploads = []

    def upload(self, source_image_path, reference_no):
        self.uploads.append((source_image_path, reference_no))
        if self.fail:
            raise RuntimeError("synthetic Drive failure")
        return {
            "file_id": "synthetic-file-001",
            "webViewLink": "https://drive.example/synthetic-file-001",
        }


class FakeSheets:
    def __init__(self, existing=None, fail_appends=0):
        self.existing = set(existing or [])
        self.fail_appends = fail_appends
        self.append_attempts = 0
        self.rows = []

    def reference_exists(self, reference_no):
        return reference_no in self.existing

    def append_transaction(self, transaction):
        self.append_attempts += 1
        if self.append_attempts <= self.fail_appends:
            raise RuntimeError("synthetic Sheets failure")
        self.rows.append(dict(transaction))
        self.existing.add(transaction["reference_no"])


class SlipPipelineIntegrationTests(unittest.TestCase):

    def setUp(self):
        self.module = load_module()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name) / "uploads"
        self.runtime_root.mkdir()
        self.slip_path = self.runtime_root / "synthetic-slip.jpg"
        self.slip_path.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
        self.store = self.module.SQLiteStateStore(
            Path(self.temp_dir.name) / "state" / "transactions.sqlite3"
        )
        self.flow = self.module.TransactionFlow(
            self.store,
            allowed_source_roots=[self.runtime_root],
            projects=["Project A"],
        )
        self.actor = {
            "platform": "telegram",
            "chat_id": "chat-1",
            "telegram_user_id": "user-1",
        }

    def tearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    def begin(self, reference_no="SYNTHETIC-REF-001", session_id="session-1", amount="1,250.50"):
        return self.flow.begin(
            tenant_id="tenant-a",
            platform="telegram",
            chat_id="chat-1",
            thread_id=None,
            session_id=session_id,
            telegram_user_id="user-1",
            source_image_path=self.slip_path,
            ocr_result={
                "confidence": 0.98,
                "parsed": {
                    "reference_no": reference_no,
                    "amount": amount,
                    "date": "2026-08-30",
                    "payer": "Synthetic Payer",
                    "payee": "Synthetic Payee",
                    "note": "Synthetic fixture",
                },
            },
        )

    def advance_to_review(self, reference_no="SYNTHETIC-REF-001"):
        view = self.begin(reference_no)
        view = self.flow.choose(
            view["transaction_id"],
            expected_version=view["version"],
            action="select_project",
            value="Project A",
            **self.actor,
        )
        view = self.flow.choose(
            view["transaction_id"],
            expected_version=view["version"],
            action="use_sender",
            **self.actor,
        )
        view = self.flow.choose(
            view["transaction_id"],
            expected_version=view["version"],
            action="expense",
            **self.actor,
        )
        return self.flow.choose(
            view["transaction_id"],
            expected_version=view["version"],
            action="materials",
            **self.actor,
        )

    def test_begin_reads_ocr_once_and_creates_pending_without_writes(self):
        view = self.begin()
        self.assertEqual(view["current_state"], "waiting_project")
        pending = self.flow.get_transaction(view["transaction_id"], **self.actor)
        self.assertEqual(pending["source_image_path"], str(self.slip_path.resolve()))
        self.assertEqual(pending["reference_no"], "SYNTHETIC-REF-001")
        self.assertEqual(pending["current_state"], "waiting_project")

    def test_missing_or_invalid_amount_waits_for_durable_manual_decimal(self):
        invalid_amounts = (
            None, "not-a-number", 0, -1, "NaN", "Infinity", "1,2,3",
            "9007199254740993", "0.123456789012345678901",
        )
        for index, amount in enumerate(invalid_amounts):
            view = self.begin(f"SYNTHETIC-AMOUNT-{index}", f"session-amount-{index}", amount=amount)
            pending = self.flow.get_transaction(view["transaction_id"], **self.actor)
            self.assertTrue(pending["needs_amount"])
            self.assertEqual(pending["entry_mode"], "amount")
            with self.assertRaises(self.module.InvalidTransitionError):
                self.flow.choose(
                    view["transaction_id"], expected_version=view["version"],
                    action="select_project", value="Project A", **self.actor
                )
            with self.assertRaises(ValueError):
                self.flow.submit_manual(
                    view["transaction_id"], expected_version=view["version"],
                    value="0", **self.actor
                )
            restarted_store = self.module.SQLiteStateStore(self.store.path)
            try:
                restarted_flow = self.module.TransactionFlow(
                    restarted_store, allowed_source_roots=[self.runtime_root],
                    projects=["Project A"]
                )
                recovered = restarted_flow.get_manual_pending(**self.actor)
                self.assertEqual(recovered["transaction_id"], view["transaction_id"])
                updated = restarted_flow.submit_manual(
                    view["transaction_id"], expected_version=view["version"],
                    value="123.45", **self.actor
                )
                durable = restarted_flow.get_transaction(updated["transaction_id"], **self.actor)
                self.assertFalse(durable["needs_amount"])
                self.assertEqual(durable["ocr_fields"]["amount"], 123.45)
            finally:
                restarted_store.close()

        exact = self.begin("SYNTHETIC-AMOUNT-EXACT", "session-amount-exact", amount="1,234.50")
        exact_record = self.flow.get_transaction(exact["transaction_id"], **self.actor)
        self.assertEqual(exact_record["ocr_fields"]["amount"], 1234.5)

    def test_project_selection_supports_back_and_cancel(self):
        view = self.begin()
        waiting_user = self.flow.choose(
            view["transaction_id"],
            expected_version=view["version"],
            action="select_project",
            value="Project A",
            **self.actor,
        )
        self.assertEqual(waiting_user["current_state"], "waiting_user")
        project_view = self.flow.back(
            waiting_user["transaction_id"],
            expected_version=waiting_user["version"],
            **self.actor,
        )
        self.assertEqual(project_view["current_state"], "waiting_project")
        cancelled = self.flow.cancel(
            project_view["transaction_id"],
            expected_version=project_view["version"],
            **self.actor,
        )
        self.assertEqual(cancelled["current_state"], "cancelled")
        durable = self.flow.get_transaction(cancelled["transaction_id"], **self.actor)
        self.assertEqual(durable["current_state"], "cancelled")

    def test_new_project_and_manual_entry_are_kept_pending(self):
        new_view = self.begin("SYNTHETIC-NEW-PROJECT", "session-new")
        entry_view = self.flow.choose(
            new_view["transaction_id"],
            expected_version=new_view["version"],
            action="new_project",
            **self.actor,
        )
        user_view = self.flow.submit_manual(
            entry_view["transaction_id"],
            expected_version=entry_view["version"],
            value="Project New",
            **self.actor,
        )
        self.assertEqual(user_view["current_state"], "waiting_user")
        new_pending = self.flow.get_transaction(user_view["transaction_id"], **self.actor)
        self.assertEqual(new_pending["project"], "Project New")
        self.assertTrue(new_pending["new_project"])

        manual_view = self.begin("SYNTHETIC-MANUAL-PROJECT", "session-manual")
        entry_view = self.flow.choose(
            manual_view["transaction_id"],
            expected_version=manual_view["version"],
            action="manual_entry",
            **self.actor,
        )
        user_view = self.flow.submit_manual(
            entry_view["transaction_id"],
            expected_version=entry_view["version"],
            value="Project Manual",
            **self.actor,
        )
        manual_pending = self.flow.get_transaction(user_view["transaction_id"], **self.actor)
        self.assertEqual(manual_pending["project"], "Project Manual")
        self.assertFalse(manual_pending["new_project"])

    def test_expense_reaches_review_through_category_buttons(self):
        review = self.advance_to_review()
        self.assertEqual(review["current_state"], "waiting_review")
        pending = self.flow.get_transaction(review["transaction_id"], **self.actor)
        self.assertEqual(pending["project"], "Project A")
        self.assertEqual(pending["transaction_type"], "expense")
        self.assertEqual(pending["category"], "materials")
        self.assertEqual(pending["reference_no"], "SYNTHETIC-REF-001")

    def test_income_supports_manual_category_before_review(self):
        view = self.begin("SYNTHETIC-INCOME")
        for action, value in (
            ("select_project", "Project A"),
            ("use_sender", None),
            ("income", None),
            ("manual_entry", None),
        ):
            view = self.flow.choose(
                view["transaction_id"],
                expected_version=view["version"],
                action=action,
                value=value,
                **self.actor,
            )
        review = self.flow.submit_manual(
            view["transaction_id"],
            expected_version=view["version"],
            value="Synthetic Income Category",
            **self.actor,
        )
        self.assertEqual(review["current_state"], "waiting_review")
        pending = self.flow.get_transaction(review["transaction_id"], **self.actor)
        self.assertEqual(pending["transaction_type"], "income")
        self.assertEqual(pending["category"], "Synthetic Income Category")

    def test_duplicate_reference_is_rejected_before_drive_upload(self):
        first = self.begin("SYNTHETIC-DUPLICATE", "session-duplicate-1")
        with self.assertRaises(self.module.DuplicateReferenceError):
            self.begin(" synthetic-duplicate ", "session-duplicate-2")
        pending = self.flow.get_transaction(first["transaction_id"], **self.actor)
        self.assertEqual(pending["current_state"], "waiting_project")
        self.assertIsNone(pending["drive_file_id"])
        self.assertIsNone(pending["sheets_row_identity"])

    def test_drive_failure_keeps_pending_and_never_appends_transaction(self):
        review = self.advance_to_review("SYNTHETIC-DRIVE-FAIL")
        intent = self.flow.confirm(
            review["transaction_id"],
            expected_version=review["version"],
            **self.actor,
        )
        drive_pending = self.flow.mark_drive_pending(
            intent["transaction_id"],
            expected_version=intent["version"],
            **self.actor,
        )
        failed = self.flow.mark_failed(
            drive_pending["transaction_id"],
            expected_version=drive_pending["version"],
            error_code="DRIVE_TRANSIENT",
            **self.actor,
        )
        pending = self.flow.get_transaction(failed["transaction_id"], **self.actor)
        self.assertEqual(pending["current_state"], "failed")
        self.assertEqual(pending["retry_count"], 1)
        self.assertIsNone(pending["drive_file_id"])
        self.assertIsNone(pending["sheets_row_identity"])

    def test_sheets_retry_reuses_drive_upload_and_confirms_exactly_once(self):
        review = self.advance_to_review("SYNTHETIC-SHEETS-RETRY")
        intent = self.flow.confirm(
            review["transaction_id"],
            expected_version=review["version"],
            **self.actor,
        )
        drive_pending = self.flow.mark_drive_pending(
            intent["transaction_id"],
            expected_version=intent["version"],
            **self.actor,
        )
        uploaded = self.flow.mark_drive_uploaded(
            drive_pending["transaction_id"],
            expected_version=drive_pending["version"],
            file_id="synthetic-file-001",
            web_view_link="https://drive.example/synthetic-file-001",
            **self.actor,
        )
        sheets_pending = self.flow.mark_sheets_pending(
            uploaded["transaction_id"],
            expected_version=uploaded["version"],
            **self.actor,
        )
        failed = self.flow.mark_failed(
            sheets_pending["transaction_id"],
            expected_version=sheets_pending["version"],
            error_code="SHEETS_TRANSIENT",
            **self.actor,
        )
        durable_failure = self.flow.get_transaction(failed["transaction_id"], **self.actor)
        retrying = self.flow.retry(
            failed["transaction_id"],
            expected_version=failed["version"],
            **self.actor,
        )
        result = self.flow.mark_confirmed(
            retrying["transaction_id"],
            expected_version=retrying["version"],
            sheets_row_identity="Transactions!A42:Q42",
            **self.actor,
        )

        self.assertEqual(result["current_state"], "confirmed")
        self.assertEqual(durable_failure["drive_file_id"], "synthetic-file-001")
        self.assertEqual(
            durable_failure["slip_url"], "https://drive.example/synthetic-file-001"
        )
        confirmed = self.flow.get_transaction(result["transaction_id"], **self.actor)
        self.assertEqual(confirmed["sheets_row_identity"], "Transactions!A42:Q42")
        self.assertEqual(confirmed["retry_count"], 1)
        self.assertIsInstance(confirmed["ocr_fields"]["amount"], (int, float))
        replay = self.flow.confirm(
            result["transaction_id"],
            expected_version=result["version"],
            **self.actor,
        )
        self.assertEqual(replay, result)
