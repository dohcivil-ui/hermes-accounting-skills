import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = ROOT / "plugins/accounting-slip-bridge/__init__.py"
FLOW_PATH = ROOT / "plugins/accounting-slip-bridge/transaction_flow.py"
WIRING_PATH = ROOT / "plugins/accounting-slip-bridge/telegram_wiring.py"
FIXTURE_PATH = ROOT / "tests/fixtures/phase_d_aksonocr_handoff.json"
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

    def test_phase_d_unlabeled_ocr_fails_before_durable_transaction(self):
        fixture = self.fixture()
        fixture["raw_ocr_text"] = (
            "วันที่ทำรายการ: 2026-09-01\nจำนวนเงิน: 1.00\n"
            "PHASED-SMOKE-20260901-01"
        )
        fixture["raw_response"] = {}

        with self.assertRaisesRegex(
            ValueError, "requires reference_no after normalization"
        ):
            self.bridge._normalize_ocr_result_for_handoff(fixture)

        self.assertIsNone(
            self.store.get_by_reference("phase-d-synthetic-tenant", REFERENCE_NO)
        )


if __name__ == "__main__":
    unittest.main()
