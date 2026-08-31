import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "plugins/accounting-slip-bridge/transaction_flow.py"


def load_module():
    spec = importlib.util.spec_from_file_location("lekza_button_state", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DurableButtonStateTests(unittest.TestCase):
    def test_new_project_and_manual_category_reach_review_durably(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "uploads"
            runtime_root.mkdir()
            slip_path = runtime_root / "synthetic-slip.jpg"
            slip_path.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
            store = module.SQLiteStateStore(
                Path(temp_dir) / "state" / "transactions.sqlite3"
            )
            flow = module.TransactionFlow(store, allowed_source_roots=[runtime_root])
            actor = {
                "platform": "telegram",
                "chat_id": "chat-1",
                "telegram_user_id": "user-1",
            }
            view = flow.begin(
                tenant_id="tenant-a",
                platform="telegram",
                chat_id="chat-1",
                thread_id=None,
                session_id="session-1",
                telegram_user_id="user-1",
                source_image_path=slip_path,
                ocr_result={
                    "confidence": 0.98,
                    "parsed": {
                        "reference_no": "SYNTHETIC-MANUAL-001",
                        "amount": "1000",
                    },
                },
            )
            try:
                entry = flow.choose(
                    view["transaction_id"],
                    expected_version=view["version"],
                    action="new_project",
                    **actor,
                )
                user_step = flow.submit_manual(
                    entry["transaction_id"],
                    expected_version=entry["version"],
                    value="Project New",
                    **actor,
                )
                type_step = flow.choose(
                    user_step["transaction_id"],
                    expected_version=user_step["version"],
                    action="use_sender",
                    **actor,
                )
                category_step = flow.choose(
                    type_step["transaction_id"],
                    expected_version=type_step["version"],
                    action="expense",
                    **actor,
                )
                category_entry = flow.choose(
                    category_step["transaction_id"],
                    expected_version=category_step["version"],
                    action="manual_entry",
                    **actor,
                )
                review = flow.submit_manual(
                    category_entry["transaction_id"],
                    expected_version=category_entry["version"],
                    value="Synthetic Category",
                    **actor,
                )

                self.assertEqual(review["current_state"], "waiting_review")
                durable = flow.get_transaction(review["transaction_id"], **actor)
                self.assertEqual(durable["project"], "Project New")
                self.assertTrue(durable["new_project"])
                self.assertEqual(durable["selected_user_id"], "user-1")
                self.assertEqual(durable["transaction_type"], "expense")
                self.assertEqual(durable["category"], "Synthetic Category")
            finally:
                store.close()

    def test_cancelled_transaction_does_not_block_a_new_attempt(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "uploads"
            runtime_root.mkdir()
            slip_path = runtime_root / "synthetic-slip.jpg"
            slip_path.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
            store = module.SQLiteStateStore(
                Path(temp_dir) / "state" / "transactions.sqlite3"
            )
            flow = module.TransactionFlow(store, allowed_source_roots=[runtime_root])
            actor = {
                "platform": "telegram",
                "chat_id": "chat-1",
                "telegram_user_id": "user-1",
            }

            def begin(session_id):
                return flow.begin(
                    tenant_id="tenant-a",
                    platform="telegram",
                    chat_id="chat-1",
                    thread_id=None,
                    session_id=session_id,
                    telegram_user_id="user-1",
                    source_image_path=slip_path,
                    ocr_result={
                        "confidence": 0.98,
                        "parsed": {
                            "reference_no": "SYNTHETIC-CANCEL-RETRY",
                            "amount": "100",
                        },
                    },
                )

            try:
                first = begin("session-1")
                cancelled = flow.cancel(
                    first["transaction_id"],
                    expected_version=first["version"],
                    **actor,
                )
                second = begin("session-2")
                self.assertEqual(cancelled["current_state"], "cancelled")
                self.assertEqual(second["current_state"], "waiting_project")
                self.assertNotEqual(first["transaction_id"], second["transaction_id"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
