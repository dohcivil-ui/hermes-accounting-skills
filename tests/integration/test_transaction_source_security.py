import importlib.util
import os
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "plugins/accounting-slip-bridge/transaction_flow.py"


def load_module():
    spec = importlib.util.spec_from_file_location("lekza_source_security", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TransactionSourceSecurityTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.upload_root = self.root / "uploads"
        self.upload_root.mkdir()
        self.db_path = self.root / "state" / "transactions.sqlite3"
        self.stores = []

    def tearDown(self):
        for store in reversed(self.stores):
            store.close()
        self.temp_dir.cleanup()

    def flow(self, **kwargs):
        store = self.module.SQLiteStateStore(self.db_path)
        self.stores.append(store)
        return self.module.TransactionFlow(
            store, allowed_source_roots=[self.upload_root], **kwargs
        )

    def begin(self, flow, source_path, reference_no):
        return flow.begin(
            tenant_id="tenant-a",
            platform="telegram",
            chat_id="chat-1",
            thread_id=None,
            session_id="session-1",
            telegram_user_id="user-1",
            source_image_path=source_path,
            ocr_result={
                "confidence": 0.95,
                "parsed": {"reference_no": reference_no, "amount": "100"},
            },
        )

    def test_path_outside_allowed_root_is_rejected(self):
        outside = self.root / "outside.jpg"
        outside.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
        with self.assertRaises(self.module.UnsafeSourcePathError):
            self.begin(self.flow(), outside, "SECURITY-OUTSIDE")

    def test_path_traversal_is_rejected_after_resolution(self):
        outside = self.root / "outside.jpg"
        outside.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
        traversal = self.upload_root / ".." / "outside.jpg"
        with self.assertRaises(self.module.UnsafeSourcePathError):
            self.begin(self.flow(), traversal, "SECURITY-TRAVERSAL")

    def test_symlink_source_is_rejected(self):
        target = self.upload_root / "target.jpg"
        target.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
        link = self.upload_root / "link.jpg"
        os.symlink(target, link)
        with self.assertRaises(self.module.UnsafeSourcePathError):
            self.begin(self.flow(), link, "SECURITY-SYMLINK")

    def test_mime_and_extension_mismatch_is_rejected(self):
        invalid = self.upload_root / "not-an-image.jpg"
        invalid.write_bytes(b"synthetic plain text")
        with self.assertRaises(self.module.UnsafeSourcePathError):
            self.begin(self.flow(), invalid, "SECURITY-MIME")

    def test_source_size_limit_is_enforced(self):
        oversized = self.upload_root / "oversized.jpg"
        oversized.write_bytes(b"\xff\xd8\xff" + b"x" * 32)
        with self.assertRaises(self.module.UnsafeSourcePathError):
            self.begin(
                self.flow(max_source_size=16), oversized, "SECURITY-SIZE"
            )

    def test_db_and_upload_policy_can_be_loaded_from_environment(self):
        store = self.module.SQLiteStateStore.from_environment(
            {self.module.STATE_DB_ENV: str(self.db_path)}
        )
        self.stores.append(store)
        flow = self.module.TransactionFlow.from_environment(
            store,
            environ={
                self.module.UPLOAD_ROOTS_ENV: str(self.upload_root),
                self.module.MAX_SLIP_BYTES_ENV: "1024",
            },
        )
        slip = self.upload_root / "environment.jpg"
        slip.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
        created = self.begin(flow, slip, "SECURITY-ENV")
        self.assertEqual(created["current_state"], "waiting_project")


if __name__ == "__main__":
    unittest.main()
