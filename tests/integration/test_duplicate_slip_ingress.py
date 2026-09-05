import importlib.util
from pathlib import Path
import tempfile
import threading
import unittest

from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
INGRESS_PATH = ROOT / "plugins/accounting-slip-bridge/ocr_ingress.py"
FLOW_PATH = ROOT / "plugins/accounting-slip-bridge/transaction_flow.py"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DuplicateSlipIngressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "state.sqlite3"
        self.image = self.root / "synthetic.png"
        canvas = Image.new("RGB", (96, 160), "white")
        for x in range(15, 80):
            for y in range(25, 135):
                if (x + y) % 11 < 4:
                    canvas.putpixel((x, y), (20, 80, 140))
        canvas.save(self.image)
        self.module = load_module("lekza_duplicate_slip_ingress", INGRESS_PATH)
        self.ledger = self.module.OcrIngressLedger(self.db_path, lease_seconds=5)
        self.calls = 0

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def read_ocr(self, reference_no="SYNTHETIC-REF-001"):
        self.calls += 1
        return {
            "confidence": 0.98,
            "parsed": {
                "reference_no": reference_no,
                "amount": 1250.5,
                "date": "2026-09-05",
                "payer": "Synthetic Payer",
                "payee": "Synthetic Payee",
            },
        }

    def finish(self, outcome, transaction_id="00000000-0000-4000-8000-000000000001"):
        self.ledger.complete(outcome, transaction_id=transaction_id)

    def test_same_message_replay_calls_ocr_once(self):
        first = self.ledger.obtain(
            tenant_id="tenant-1", message_identity="message-1",
            source_image_path=self.image, ocr_reader=self.read_ocr,
        )
        self.finish(first)
        replay = self.ledger.obtain(
            tenant_id="tenant-1", message_identity="message-1",
            source_image_path=self.image, ocr_reader=self.read_ocr,
        )
        self.assertEqual((self.calls, replay.status), (1, "duplicate"))

    def test_new_message_with_identical_bytes_calls_ocr_once(self):
        first = self.ledger.obtain(
            tenant_id="tenant-1", message_identity="message-1",
            source_image_path=self.image, ocr_reader=self.read_ocr,
        )
        self.finish(first)
        replay = self.ledger.obtain(
            tenant_id="tenant-1", message_identity="message-2",
            source_image_path=self.image, ocr_reader=self.read_ocr,
        )
        self.assertEqual((self.calls, replay.status), (1, "duplicate"))

    def test_concurrent_identical_images_call_ocr_once(self):
        entered = threading.Event()
        release = threading.Event()
        results = []

        def slow_reader():
            self.calls += 1
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return {
                "confidence": 0.98,
                "parsed": {
                    "reference_no": "SYNTHETIC-REF-001",
                    "amount": 1250.5,
                    "date": "2026-09-05",
                    "payer": "Synthetic Payer",
                    "payee": "Synthetic Payee",
                },
            }

        def first_worker():
            ledger = self.module.OcrIngressLedger(self.db_path, lease_seconds=5)
            try:
                results.append(ledger.obtain(
                    tenant_id="tenant-1", message_identity="message-1",
                    source_image_path=self.image, ocr_reader=slow_reader,
                ))
            finally:
                ledger.close()

        worker = threading.Thread(target=first_worker)
        worker.start()
        self.assertTrue(entered.wait(timeout=5))
        second = self.ledger.obtain(
            tenant_id="tenant-1", message_identity="message-2",
            source_image_path=self.image, ocr_reader=self.read_ocr,
        )
        release.set()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(second.status, "processing")
        self.assertEqual(self.calls, 1)

    def test_restart_replay_calls_ocr_once(self):
        first = self.ledger.obtain(
            tenant_id="tenant-1", message_identity="message-1",
            source_image_path=self.image, ocr_reader=self.read_ocr,
        )
        self.finish(first)
        self.ledger.close()
        self.ledger = self.module.OcrIngressLedger(self.db_path, lease_seconds=5)
        replay = self.ledger.obtain(
            tenant_id="tenant-1", message_identity="message-2",
            source_image_path=self.image, ocr_reader=self.read_ocr,
        )
        self.assertEqual((self.calls, replay.status), (1, "duplicate"))

    def test_near_reference_is_candidate_and_not_automatically_merged(self):
        first = self.ledger.obtain(
            tenant_id="tenant-1", message_identity="message-1",
            source_image_path=self.image, ocr_reader=self.read_ocr,
        )
        self.finish(first)

        changed = self.root / "changed.png"
        with Image.open(self.image) as source:
            modified = source.copy()
            modified.putpixel((1, 1), (1, 2, 3))
            modified.save(changed, compress_level=9)
        second = self.ledger.obtain(
            tenant_id="tenant-1", message_identity="message-2",
            source_image_path=changed,
            ocr_reader=lambda: self.read_ocr("SYNTHETIC-REF-002"),
        )
        candidates = self.ledger.find_candidates(second)
        self.assertEqual(second.status, "ready")
        self.assertEqual(candidates[0]["transaction_id"], first.transaction_id)
        self.assertIn("near_reference", candidates[0]["reasons"])

    def test_shared_transaction_reference_normalization_is_a_hard_duplicate(self):
        flow_module = load_module("lekza_duplicate_shared_flow", FLOW_PATH)
        store = flow_module.SQLiteStateStore(self.db_path)
        try:
            flow = flow_module.TransactionFlow(
                store, allowed_source_roots=[self.root], projects=["Project A"]
            )
            existing = flow.begin(
                tenant_id="tenant-1", platform="telegram", chat_id="chat-1",
                thread_id=None, session_id="session-1",
                telegram_user_id="user-1", source_image_path=self.image,
                ocr_result={"parsed": {
                    "reference_no": "AbC123", "amount": 1250.5,
                    "date": "2026-09-05", "payer": "Synthetic Payer",
                    "payee": "Synthetic Payee",
                }},
            )
            changed = self.root / "different-bytes.png"
            with Image.open(self.image) as source:
                modified = source.copy()
                modified.putpixel((2, 2), (9, 8, 7))
                modified.save(changed)
            incoming = self.ledger.obtain(
                tenant_id="tenant-1", message_identity="message-new",
                source_image_path=changed,
                ocr_reader=lambda: self.read_ocr("abc 123"),
            )

            candidates = self.ledger.find_candidates(incoming)

            self.assertEqual(candidates[0]["transaction_id"], existing["transaction_id"])
            self.assertIn("exact_reference", candidates[0]["reasons"])
        finally:
            store.close()

    def test_resized_compressed_image_requires_business_evidence_for_candidate(self):
        first = self.ledger.obtain(
            tenant_id="tenant-1", message_identity="message-original",
            source_image_path=self.image, ocr_reader=self.read_ocr,
        )
        self.finish(first)

        resized = self.root / "resized.jpg"
        with Image.open(self.image) as source:
            source.resize((72, 120)).save(resized, quality=72)
        related = self.ledger.obtain(
            tenant_id="tenant-1", message_identity="message-resized",
            source_image_path=resized,
            ocr_reader=lambda: self.read_ocr("DIFFERENT-900"),
        )
        candidates = self.ledger.find_candidates(related)
        self.assertIn("perceptual_image", candidates[0]["reasons"])
        self.assertIn("amount", candidates[0]["reasons"])
        self.assertIn("date", candidates[0]["reasons"])

        cropped = self.root / "cropped.jpg"
        with Image.open(self.image) as source:
            source.crop((8, 12, 88, 148)).resize((96, 160)).save(
                cropped, quality=75
            )
        cropped_outcome = self.ledger.obtain(
            tenant_id="tenant-1", message_identity="message-cropped",
            source_image_path=cropped,
            ocr_reader=lambda: self.read_ocr("CROPPED-900"),
        )
        cropped_candidates = self.ledger.find_candidates(cropped_outcome)
        self.assertIn("perceptual_image", cropped_candidates[0]["reasons"])

        unrelated = self.root / "unrelated-copy.jpg"
        with Image.open(self.image) as source:
            source.resize((64, 100)).save(unrelated, quality=65)
        different_business_data = self.ledger.obtain(
            tenant_id="tenant-1", message_identity="message-unrelated",
            source_image_path=unrelated,
            ocr_reader=lambda: {
                "confidence": 0.9,
                "parsed": {
                    "reference_no": "UNRELATED-500",
                    "amount": 1,
                    "date": "2025-01-01",
                    "payer": "Other Payer",
                    "payee": "Other Payee",
                },
            },
        )
        self.assertEqual(
            self.ledger.find_candidates(different_business_data), []
        )


if __name__ == "__main__":
    unittest.main()
