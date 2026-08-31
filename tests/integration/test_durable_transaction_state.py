import importlib.util
from pathlib import Path
import tempfile
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "plugins/accounting-slip-bridge/transaction_flow.py"


def load_module():
    spec = importlib.util.spec_from_file_location("lekza_durable_transaction_flow", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DurableTransactionStateTests(unittest.TestCase):
    def test_restart_after_ocr_recovers_minimal_pending_state(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "uploads"
            runtime_root.mkdir()
            slip_path = runtime_root / "synthetic-slip.jpg"
            slip_path.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
            db_path = Path(temp_dir) / "state" / "transactions.sqlite3"

            store = module.SQLiteStateStore(db_path)
            flow = module.TransactionFlow(store, allowed_source_roots=[runtime_root])
            view = flow.begin(
                tenant_id="tenant-a",
                platform="telegram",
                chat_id="chat-1",
                thread_id="thread-1",
                session_id="session-1",
                telegram_user_id="user-1",
                source_image_path=slip_path,
                ocr_result={
                    "confidence": 0.98,
                    "parsed": {
                        "reference_no": " SYNTHETIC-REF-001 ",
                        "amount": "1,250.50",
                        "date": "2026-08-30",
                        "payer": "Synthetic Payer",
                        "payee": "Synthetic Payee",
                        "note": "Synthetic fixture",
                        "provider_extra": "must-not-persist",
                    },
                    "raw_ocr_text": "must-not-persist",
                    "raw_response": {"must": "not-persist"},
                    "usage": {"credits": 1},
                },
            )
            transaction_id = view["transaction_id"]
            self.assertEqual(view["current_state"], "waiting_project")
            store.close()

            reopened = module.SQLiteStateStore(db_path)
            recovered = module.TransactionFlow(
                reopened, allowed_source_roots=[runtime_root]
            ).get_transaction(
                transaction_id,
                platform="telegram",
                chat_id="chat-1",
                telegram_user_id="user-1",
            )

            self.assertEqual(recovered["transaction_id"], transaction_id)
            self.assertEqual(str(uuid.UUID(transaction_id)), transaction_id)
            self.assertEqual(recovered["tenant_id"], "tenant-a")
            self.assertEqual(recovered["platform"], "telegram")
            self.assertEqual(recovered["chat_id"], "chat-1")
            self.assertEqual(recovered["thread_id"], "thread-1")
            self.assertEqual(recovered["session_id"], "session-1")
            self.assertEqual(recovered["telegram_user_id"], "user-1")
            self.assertEqual(recovered["reference_no"], "SYNTHETIC-REF-001")
            self.assertEqual(recovered["current_state"], "waiting_project")
            self.assertEqual(recovered["source_image_path"], str(slip_path.resolve()))
            self.assertEqual(recovered["confidence"], 0.98)
            self.assertEqual(
                recovered["ocr_fields"],
                {
                    "reference_no": "SYNTHETIC-REF-001",
                    "amount": 1250.50,
                    "date": "2026-08-30",
                    "payer": "Synthetic Payer",
                    "payee": "Synthetic Payee",
                    "note": "Synthetic fixture",
                },
            )
            self.assertNotIn("raw_ocr_text", recovered)
            self.assertNotIn("raw_response", recovered)
            self.assertNotIn("usage", recovered)
            self.assertIsNone(recovered["project"])
            self.assertIsNone(recovered["transaction_type"])
            self.assertIsNone(recovered["category"])
            self.assertIsNone(recovered["drive_file_id"])
            self.assertIsNone(recovered["slip_url"])
            self.assertIsNone(recovered["sheets_row_identity"])
            self.assertEqual(recovered["retry_count"], 0)
            self.assertEqual(recovered["version"], 1)
            self.assertTrue(recovered["created_at"])
            self.assertTrue(recovered["updated_at"])
            reopened.close()

    def test_wrong_user_cannot_transition_transaction_state(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir) / "uploads"
            runtime_root.mkdir()
            slip_path = runtime_root / "synthetic-slip.jpg"
            slip_path.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
            store = module.SQLiteStateStore(
                Path(temp_dir) / "state" / "transactions.sqlite3"
            )
            flow = module.TransactionFlow(
                store,
                allowed_source_roots=[runtime_root],
                projects=["Project A"],
            )
            view = flow.begin(
                tenant_id="tenant-a",
                platform="telegram",
                chat_id="chat-1",
                thread_id=None,
                session_id="session-1",
                telegram_user_id="owner-user",
                source_image_path=slip_path,
                ocr_result={
                    "confidence": 0.98,
                    "parsed": {
                        "reference_no": "SYNTHETIC-REF-002",
                        "amount": "500.00",
                    },
                },
            )

            try:
                unauthorized_actors = (
                    ("web", "chat-1", "owner-user"),
                    ("telegram", "different-chat", "owner-user"),
                    ("telegram", "chat-1", "different-user"),
                )
                for platform, chat_id, telegram_user_id in unauthorized_actors:
                    with self.subTest(
                        platform=platform,
                        chat_id=chat_id,
                        telegram_user_id=telegram_user_id,
                    ):
                        with self.assertRaises(module.AuthorizationError):
                            flow.choose(
                                view["transaction_id"],
                                expected_version=view["version"],
                                platform=platform,
                                chat_id=chat_id,
                                telegram_user_id=telegram_user_id,
                                action="select_project",
                                value="Project A",
                            )

                unchanged = flow.get_transaction(
                    view["transaction_id"],
                    platform="telegram",
                    chat_id="chat-1",
                    telegram_user_id="owner-user",
                )
                self.assertEqual(unchanged["current_state"], "waiting_project")
                self.assertEqual(unchanged["version"], 1)
            finally:
                store.close()

    def test_reference_number_is_unique_per_tenant_after_normalization(self):
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

            def begin(tenant_id, reference_no, session_id):
                return flow.begin(
                    tenant_id=tenant_id,
                    platform="telegram",
                    chat_id="chat-1",
                    thread_id=None,
                    session_id=session_id,
                    telegram_user_id="user-1",
                    source_image_path=slip_path,
                    ocr_result={
                        "confidence": 0.98,
                        "parsed": {"reference_no": reference_no, "amount": "100"},
                    },
                )

            try:
                first = begin("tenant-a", " ref-001 ", "session-1")
                with self.assertRaises(module.DuplicateReferenceError):
                    begin("tenant-a", "REF-001", "session-2")
                other_tenant = begin("tenant-b", "REF-001", "session-3")
                self.assertNotEqual(
                    first["transaction_id"], other_tenant["transaction_id"]
                )
            finally:
                store.close()

    def test_state_store_rejects_owner_identity_mutation(self):
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
            view = flow.begin(
                tenant_id="tenant-a",
                platform="telegram",
                chat_id="chat-1",
                thread_id=None,
                session_id="session-1",
                telegram_user_id="owner-user",
                source_image_path=slip_path,
                ocr_result={
                    "confidence": 0.98,
                    "parsed": {"reference_no": "OWNER-MUTATION", "amount": "100"},
                },
            )
            try:
                with self.assertRaises(module.InvalidTransitionError):
                    store.transition(
                        view["transaction_id"],
                        platform="telegram",
                        chat_id="chat-1",
                        telegram_user_id="owner-user",
                        expected_version=view["version"],
                        allowed_from={"waiting_project"},
                        changes={"telegram_user_id": "different-user"},
                    )
                durable = flow.get_transaction(
                    view["transaction_id"],
                    platform="telegram",
                    chat_id="chat-1",
                    telegram_user_id="owner-user",
                )
                self.assertEqual(durable["telegram_user_id"], "owner-user")
                self.assertEqual(durable["version"], view["version"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
