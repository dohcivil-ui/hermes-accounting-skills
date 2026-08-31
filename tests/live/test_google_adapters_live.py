"""Opt-in smoke test for designated, disposable Google test resources only."""

import importlib.util
import os
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = ROOT / "plugins/accounting-slip-bridge"


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(
    os.getenv("LEKZA_RUN_GOOGLE_LIVE_TESTS") == "1",
    "set LEKZA_RUN_GOOGLE_LIVE_TESTS=1 for designated Google test resources",
)
class GoogleAdaptersLiveSmokeTests(unittest.TestCase):
    def test_confirmed_transaction_reaches_designated_test_drive_and_sheet(self):
        required = (
            "LEKZA_GOOGLE_ACCESS_TOKEN",
            "LEKZA_TEST_SLIP_FOLDER_ID",
            "LEKZA_TEST_ACCOUNTING_SPREADSHEET_ID",
            "LEKZA_TEST_TRANSACTION_STATE_DB",
            "LEKZA_TEST_UPLOAD_ROOT",
        )
        missing = [name for name in required if not str(os.getenv(name) or "").strip()]
        if missing:
            self.skipTest("missing explicit live-test settings: " + ", ".join(missing))
        if os.getenv("LEKZA_LIVE_TEST_RESOURCE_ACK") != "designated-test-resources":
            self.skipTest(
                "set LEKZA_LIVE_TEST_RESOURCE_ACK=designated-test-resources"
            )

        test_folder = os.environ["LEKZA_TEST_SLIP_FOLDER_ID"].strip()
        test_spreadsheet = os.environ["LEKZA_TEST_ACCOUNTING_SPREADSHEET_ID"].strip()
        if test_folder == str(os.getenv("LEKZA_SLIP_FOLDER_ID") or "").strip():
            self.fail("test Drive folder must differ from production")
        if test_spreadsheet == str(
            os.getenv("LEKZA_ACCOUNTING_SPREADSHEET_ID") or ""
        ).strip():
            self.fail("test spreadsheet must differ from production")

        upload_root = Path(os.environ["LEKZA_TEST_UPLOAD_ROOT"]).expanduser().resolve()
        db_path = Path(os.environ["LEKZA_TEST_TRANSACTION_STATE_DB"]).expanduser()
        if not upload_root.is_dir() or not db_path.is_absolute():
            self.fail("test upload root must exist and test DB path must be absolute")
        resolved_db = db_path.resolve()
        if upload_root != resolved_db.parent and upload_root not in resolved_db.parents:
            self.fail("test DB must be contained by LEKZA_TEST_UPLOAD_ROOT")

        adapters = load_module("lekza_live_google_adapters", "google_adapters.py")
        flow_module = load_module("lekza_live_transaction_flow", "transaction_flow.py")
        slip_path = upload_root / "lekza-google-live-smoke-synthetic.jpg"
        slip_path.write_bytes(b"\xff\xd8\xff\xe0lekza-live-smoke-synthetic")
        actor = {
            "platform": "telegram",
            "chat_id": "lekza-live-smoke-chat",
            "telegram_user_id": "lekza-live-smoke-user",
        }
        store = flow_module.SQLiteStateStore(resolved_db)
        try:
            flow = flow_module.TransactionFlow(
                store, allowed_source_roots=[upload_root], projects=["Live Smoke Project"]
            )
            record = store.get_by_reference(
                "lekza-live-smoke-tenant", "LEKZA-GOOGLE-LIVE-SMOKE-V1"
            )
            if record is None:
                view = flow.begin(
                    tenant_id="lekza-live-smoke-tenant",
                    thread_id=None,
                    session_id="lekza-live-smoke-session",
                    source_image_path=slip_path,
                    ocr_result={
                        "confidence": 1.0,
                        "parsed": {
                            "reference_no": "LEKZA-GOOGLE-LIVE-SMOKE-V1",
                            "amount": 1.0,
                            "date": "2026-08-30",
                            "payer": "Synthetic Live Test",
                            "payee": "Synthetic Live Test",
                            "note": "Designated test resources only",
                        },
                    },
                    **actor,
                )
                for action, value in (
                    ("select_project", "Live Smoke Project"),
                    ("use_sender", None),
                    ("expense", None),
                    ("other", None),
                ):
                    view = flow.choose(
                        view["transaction_id"], expected_version=view["version"],
                        action=action, value=value, **actor,
                    )
                view = flow.confirm(
                    view["transaction_id"], expected_version=view["version"], **actor
                )
                transaction_id = view["transaction_id"]
            else:
                transaction_id = record["transaction_id"]

            pipeline = adapters.ProductionSavePipeline(
                flow,
                adapters.GoogleDriveAdapter(
                    test_folder,
                    adapters.BearerTokenProvider(os.environ["LEKZA_GOOGLE_ACCESS_TOKEN"]),
                ),
                adapters.GoogleSheetsAdapter(
                    test_spreadsheet,
                    adapters.BearerTokenProvider(os.environ["LEKZA_GOOGLE_ACCESS_TOKEN"]),
                ),
            )
            result = pipeline.save(transaction_id, **actor)
            self.assertEqual(result["current_state"], "confirmed")
            self.assertTrue(result["drive_file_id"])
            self.assertTrue(result["sheets_row_identity"])
        finally:
            store.close()
            if slip_path.exists():
                slip_path.unlink()


if __name__ == "__main__":
    unittest.main()
