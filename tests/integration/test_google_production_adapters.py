import importlib.util
from pathlib import Path
import tempfile
import threading
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[2]
ADAPTER_PATH = ROOT / "plugins/accounting-slip-bridge/google_adapters.py"
FLOW_PATH = ROOT / "plugins/accounting-slip-bridge/transaction_flow.py"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, status_code, payload=None, malformed=False):
        self.status_code = status_code
        self._payload = payload
        self._malformed = malformed

    def json(self):
        if self._malformed:
            raise ValueError("synthetic malformed JSON")
        return self._payload


class FakeClaim:
    def __init__(self, transaction_id):
        self.transaction_id = transaction_id
        self.active = True

    def assert_owner(self, minimum_valid_seconds=0):
        if not self.active:
            raise RuntimeError("synthetic stale claim")


class DriveSession:
    def __init__(self):
        self.files = {}
        self.generated = 0
        self.uploads = 0
        self.malformed_generate = False

    def get(self, url, **kwargs):
        if url.endswith("/files/generateIds"):
            self.generated += 1
            if self.malformed_generate:
                return FakeResponse(200, malformed=True)
            return FakeResponse(200, {"ids": [f"drive-id-{self.generated}"]})
        file_id = url.rsplit("/", 1)[-1]
        if file_id not in self.files:
            return FakeResponse(404, {})
        return FakeResponse(200, dict(self.files[file_id]))

    def post(self, url, **kwargs):
        self.uploads += 1
        raw = kwargs["data"]
        metadata_start = raw.index(b"\r\n\r\n") + 4
        metadata_end = raw.index(b"\r\n--", metadata_start)
        import json
        metadata = json.loads(raw[metadata_start:metadata_end])
        file_id = metadata["id"]
        if file_id in self.files:
            return FakeResponse(409, {})
        payload = {
            "id": file_id,
            "webViewLink": f"https://drive.google.test/{file_id}",
            "appProperties": metadata["appProperties"],
            "parents": metadata["parents"],
        }
        self.files[file_id] = payload
        return FakeResponse(200, dict(payload))


class SheetsSession:
    def __init__(self, module):
        self.module = module
        self.schemas = {name: list(schema) for name, schema in module.CANONICAL_SCHEMAS.items()}
        self.rows = [["transaction_id"]]
        self.batch_calls = 0
        self.concurrent_winner = False
        self.malformed_schema = False
        self.last_batch = None
        self.block_batch = False
        self.batch_entered = threading.Event()
        self.release_batch = threading.Event()

    def get(self, url, **kwargs):
        if "/values/" in url:
            range_value = url.split("/values/", 1)[1]
            if range_value.startswith("Transactions!A%3AA"):
                return FakeResponse(200, {"values": [[row[0]] for row in self.rows]})
            title = range_value.split("!", 1)[0]
            if self.malformed_schema:
                return FakeResponse(200, malformed=True)
            return FakeResponse(200, {"values": [self.schemas[title]]})
        return FakeResponse(200, {"sheets": [{"properties": {"sheetId": 17, "title": "Transactions"}}]})

    def post(self, url, **kwargs):
        if self.block_batch:
            self.batch_entered.set()
            if not self.release_batch.wait(timeout=5):
                raise RuntimeError("synthetic concurrent test timed out")
        self.batch_calls += 1
        self.last_batch = kwargs["json"]
        requests = self.last_batch["requests"]
        row_cells = requests[0]["appendCells"]["rows"][0]["values"]
        row = []
        for cell in row_cells:
            entered = cell["userEnteredValue"]
            row.append(next(iter(entered.values())))
        if self.concurrent_winner:
            self.rows.append(row)
            return FakeResponse(503, {"error": "synthetic ambiguous response"})
        self.rows.append(row)
        return FakeResponse(200, {"spreadsheetId": "sheet-1", "replies": [{}]})


class ParallelSheetsSession(SheetsSession):
    """Proves two different transaction leases can reach Google together."""

    def __init__(self, module):
        super().__init__(module)
        self.post_barrier = threading.Barrier(2)

    def post(self, url, **kwargs):
        self.post_barrier.wait(timeout=5)
        return super().post(url, **kwargs)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapters = load_module("lekza_google_adapters", ADAPTER_PATH)
        self.transaction_id = str(uuid.uuid4())
        self.temp_dir = tempfile.TemporaryDirectory()
        self.slip = Path(self.temp_dir.name) / "synthetic.jpg"
        self.slip.write_bytes(b"\xff\xd8\xff\xe0synthetic")

    def tearDown(self):
        self.temp_dir.cleanup()

    def transaction(self):
        return {
            "transaction_id": self.transaction_id,
            "reference_no": "SYNTHETIC-001",
            "ocr_fields": {
                "date": "2026-08-30", "payer": "Synthetic Payer",
                "payee": "Synthetic Payee", "amount": 1250.50,
                "note": "Synthetic fixture",
            },
            "project": "Project A", "transaction_type": "expense",
            "category": "materials", "confidence": 0.98,
            "selected_user_id": "user-1", "drive_file_id": "drive-id-1",
            "slip_url": "https://drive.google.test/drive-id-1",
            "created_at": "2026-08-30T01:00:00+00:00",
            "confirmed_at": "2026-08-30T01:01:00+00:00",
        }

    def claim(self):
        return FakeClaim(self.transaction_id)

    def test_canonical_schemas_are_frozen_exactly(self):
        self.assertEqual(self.adapters.TRANSACTIONS_SCHEMA, (
            "transaction_id", "reference_no", "date", "payer", "payee",
            "project_id", "project", "type", "category", "amount", "note",
            "confidence", "submitted_by", "drive_file_id", "slip_url", "status",
            "created_at", "confirmed_at",
        ))
        self.assertEqual(self.adapters.PROJECTS_SCHEMA, (
            "project_id", "project_name", "customer", "status", "start_date",
            "created_by", "created_at",
        ))
        self.assertEqual(self.adapters.USERS_SCHEMA, (
            "telegram_user_id", "name", "frequent_projects", "frequent_keywords",
            "last_actions", "created_at", "updated_at",
        ))

    def test_drive_normal_upload_returns_identity_and_link(self):
        session = DriveSession()
        adapter = self.adapters.GoogleDriveAdapter("folder-1", lambda: "token", session=session)
        reserved = adapter.reserve_file_id()
        result = adapter.upload(self.transaction_id, self.slip, reserved)
        self.assertEqual(result["file_id"], reserved)
        self.assertEqual(result["webViewLink"], f"https://drive.google.test/{reserved}")
        self.assertEqual(session.uploads, 1)

    def test_drive_retry_and_restart_recover_without_duplicate_upload(self):
        session = DriveSession()
        first = self.adapters.GoogleDriveAdapter("folder-1", lambda: "token", session=session)
        reserved = first.reserve_file_id()
        expected = first.upload(self.transaction_id, self.slip, reserved)
        restarted = self.adapters.GoogleDriveAdapter("folder-1", lambda: "token", session=session)
        self.assertEqual(restarted.upload(self.transaction_id, self.slip, reserved), expected)
        self.assertEqual(session.uploads, 1)

    def test_drive_read_back_verifies_reserved_identity_without_upload(self):
        session = DriveSession()
        adapter = self.adapters.GoogleDriveAdapter("folder-1", lambda: "token", session=session)
        reserved = adapter.reserve_file_id()
        expected = adapter.upload(self.transaction_id, self.slip, reserved)
        self.assertEqual(adapter.verify_upload(self.transaction_id, reserved), expected)
        self.assertEqual(session.uploads, 1)

    def test_drive_rejects_malformed_external_response(self):
        session = DriveSession()
        session.malformed_generate = True
        adapter = self.adapters.GoogleDriveAdapter("folder-1", lambda: "token", session=session)
        with self.assertRaises(self.adapters.GoogleAdapterError):
            adapter.reserve_file_id()

    def test_sheets_fails_closed_on_incompatible_schema(self):
        session = SheetsSession(self.adapters)
        session.schemas["Transactions"][0] = "wrong_id"
        adapter = self.adapters.GoogleSheetsAdapter("sheet-1", lambda: "token", session=session)
        with self.assertRaises(self.adapters.IncompatibleSchemaError):
            adapter.append_transaction(self.transaction(), write_claim=self.claim())
        self.assertEqual(session.batch_calls, 0)

    def test_sheets_appends_once_and_preserves_numeric_amount(self):
        session = SheetsSession(self.adapters)
        adapter = self.adapters.GoogleSheetsAdapter("sheet-1", lambda: "token", session=session)
        identity = adapter.append_transaction(self.transaction(), write_claim=self.claim())
        self.assertEqual(identity, "Transactions!A2:R2")
        cells = session.last_batch["requests"][0]["appendCells"]["rows"][0]["values"]
        self.assertEqual(cells[9], {"userEnteredValue": {"numberValue": 1250.5}})

    def test_sheets_retry_recovers_existing_row_without_second_append(self):
        session = SheetsSession(self.adapters)
        adapter = self.adapters.GoogleSheetsAdapter("sheet-1", lambda: "token", session=session)
        expected = adapter.append_transaction(self.transaction(), write_claim=self.claim())
        restarted = self.adapters.GoogleSheetsAdapter("sheet-1", lambda: "token", session=session)
        self.assertEqual(
            restarted.append_transaction(self.transaction(), write_claim=self.claim()),
            expected,
        )
        self.assertEqual(session.batch_calls, 1)
        self.assertEqual(len(session.rows), 2)

    def test_sheets_read_back_requires_exactly_one_matching_row(self):
        session = SheetsSession(self.adapters)
        adapter = self.adapters.GoogleSheetsAdapter("sheet-1", lambda: "token", session=session)
        expected = adapter.append_transaction(self.transaction(), write_claim=self.claim())
        self.assertEqual(adapter.find_transaction_row(self.transaction_id), expected)
        session.rows.append(list(session.rows[-1]))
        with self.assertRaisesRegex(self.adapters.GoogleAdapterError, "duplicated"):
            adapter.find_transaction_row(self.transaction_id)

    def test_sheets_concurrent_loser_recovers_atomic_winner(self):
        session = SheetsSession(self.adapters)
        session.concurrent_winner = True
        adapter = self.adapters.GoogleSheetsAdapter("sheet-1", lambda: "token", session=session)
        self.assertEqual(
            adapter.append_transaction(self.transaction(), write_claim=self.claim()),
            "Transactions!A2:R2",
        )
        self.assertEqual(len(session.rows), 2)

    def test_sheets_rejects_malformed_external_response(self):
        session = SheetsSession(self.adapters)
        session.malformed_schema = True
        adapter = self.adapters.GoogleSheetsAdapter("sheet-1", lambda: "token", session=session)
        with self.assertRaises(self.adapters.GoogleAdapterError):
            adapter.append_transaction(self.transaction(), write_claim=self.claim())

    def test_sheets_rejects_write_without_sqlite_claim(self):
        session = SheetsSession(self.adapters)
        adapter = self.adapters.GoogleSheetsAdapter("sheet-1", lambda: "token", session=session)
        with self.assertRaises(self.adapters.GoogleAdapterError):
            adapter.append_transaction(self.transaction(), write_claim=None)
        self.assertEqual(session.batch_calls, 0)


class SavePipelineRestartTests(unittest.TestCase):
    def setUp(self):
        self.adapters = load_module("lekza_pipeline_adapters", ADAPTER_PATH)
        self.flow_module = load_module("lekza_pipeline_flow", FLOW_PATH)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "uploads"
        self.root.mkdir()
        self.slip = self.root / "synthetic.jpg"
        self.slip.write_bytes(b"\xff\xd8\xff\xe0synthetic")
        self.db_path = Path(self.temp_dir.name) / "state.sqlite3"
        self.drive_session = DriveSession()
        self.sheets_session = SheetsSession(self.adapters)
        self.actor = {"platform": "telegram", "chat_id": "chat-1", "telegram_user_id": "user-1"}

    def tearDown(self):
        self.temp_dir.cleanup()

    def open_flow(self):
        store = self.flow_module.SQLiteStateStore(self.db_path)
        flow = self.flow_module.TransactionFlow(store, allowed_source_roots=[self.root], projects=["Project A"])
        return store, flow

    def advance(self, flow, reference_no="SYNTHETIC-PIPELINE"):
        view = flow.begin(
            tenant_id="tenant-1", thread_id=None, session_id="session-1",
            source_image_path=self.slip,
            ocr_result={"confidence": 0.9, "parsed": {
                "reference_no": reference_no, "amount": "42.50",
                "date": "2026-08-30", "payer": "Payer", "payee": "Payee", "note": "Fixture",
            }}, **self.actor,
        )
        for action, value in (("select_project", "Project A"), ("use_sender", None), ("expense", None), ("materials", None)):
            view = flow.choose(view["transaction_id"], expected_version=view["version"], action=action, value=value, **self.actor)
        return flow.confirm(view["transaction_id"], expected_version=view["version"], **self.actor)

    def prepare_sheets_pending(self, flow, reference_no, reserved):
        intent = self.advance(flow, reference_no)
        pending = flow.mark_drive_pending(
            intent["transaction_id"], expected_version=intent["version"], **self.actor
        )
        reserved_view = flow.reserve_drive_upload(
            pending["transaction_id"], expected_version=pending["version"],
            file_id=reserved, **self.actor,
        )
        uploaded = flow.mark_drive_uploaded(
            pending["transaction_id"], expected_version=reserved_view["version"],
            file_id=reserved, web_view_link=f"https://drive.google.test/{reserved}",
            **self.actor,
        )
        return flow.mark_sheets_pending(
            pending["transaction_id"], expected_version=uploaded["version"], **self.actor
        )

    def test_restart_recovery_runs_confirmed_intent_through_confirmed(self):
        first_store, first_flow = self.open_flow()
        intent = self.advance(first_flow)
        pending = first_flow.mark_drive_pending(intent["transaction_id"], expected_version=intent["version"], **self.actor)
        reserved = self.adapters.GoogleDriveAdapter("folder-1", lambda: "token", session=self.drive_session).reserve_file_id()
        first_flow.reserve_drive_upload(pending["transaction_id"], expected_version=pending["version"], file_id=reserved, **self.actor)
        first_store.close()

        store, flow = self.open_flow()
        pipeline = self.adapters.ProductionSavePipeline(
            flow,
            self.adapters.GoogleDriveAdapter("folder-1", lambda: "token", session=self.drive_session),
            self.adapters.GoogleSheetsAdapter("sheet-1", lambda: "token", session=self.sheets_session),
        )
        result = pipeline.save(intent["transaction_id"], **self.actor)
        self.assertEqual(result["current_state"], "confirmed")
        self.assertEqual(self.drive_session.uploads, 1)
        self.assertEqual(self.sheets_session.batch_calls, 1)
        store.close()

    def test_two_concurrent_save_workers_produce_exactly_one_sheets_row(self):
        setup_store, setup_flow = self.open_flow()
        sheets_pending = self.prepare_sheets_pending(
            setup_flow, "SYNTHETIC-CONCURRENT-SAME", "drive-id-concurrent"
        )
        setup_store.close()

        self.sheets_session.block_batch = True
        start = threading.Barrier(3)
        results = []
        errors = []
        stores = []

        def worker():
            store, flow = self.open_flow()
            stores.append(store)
            pipeline = self.adapters.ProductionSavePipeline(
                flow,
                self.adapters.GoogleDriveAdapter(
                    "folder-1", lambda: "token", session=self.drive_session
                ),
                self.adapters.GoogleSheetsAdapter(
                    "sheet-1", lambda: "token", session=self.sheets_session
                ),
            )
            try:
                start.wait(timeout=5)
                results.append(pipeline.save(sheets_pending["transaction_id"], **self.actor))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        start.wait(timeout=5)
        self.assertTrue(self.sheets_session.batch_entered.wait(timeout=5))
        self.sheets_session.release_batch.set()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        for store in stores:
            store.close()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["current_state"] == "confirmed" for result in results))
        self.assertEqual(self.sheets_session.batch_calls, 1)
        self.assertEqual(len(self.sheets_session.rows), 2)  # header + one data row

    def test_two_different_transactions_do_not_block_each_other(self):
        setup_store, setup_flow = self.open_flow()
        first = self.prepare_sheets_pending(
            setup_flow, "SYNTHETIC-PARALLEL-ONE", "drive-id-parallel-one"
        )
        second = self.prepare_sheets_pending(
            setup_flow, "SYNTHETIC-PARALLEL-TWO", "drive-id-parallel-two"
        )
        setup_store.close()
        session = ParallelSheetsSession(self.adapters)
        results = []
        errors = []
        stores = []

        def worker(transaction_id):
            store, flow = self.open_flow()
            stores.append(store)
            pipeline = self.adapters.ProductionSavePipeline(
                flow,
                self.adapters.GoogleDriveAdapter(
                    "folder-1", lambda: "token", session=self.drive_session
                ),
                self.adapters.GoogleSheetsAdapter(
                    "sheet-1", lambda: "token", session=session
                ),
            )
            try:
                results.append(pipeline.save(transaction_id, **self.actor))
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(first["transaction_id"],)),
            threading.Thread(target=worker, args=(second["transaction_id"],)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        for store in stores:
            store.close()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(session.batch_calls, 2)
        self.assertEqual(len(session.rows), 3)  # header + two distinct rows

    def test_expired_lease_can_be_taken_over_and_fences_stale_owner(self):
        setup_store, setup_flow = self.open_flow()
        pending = self.prepare_sheets_pending(
            setup_flow, "SYNTHETIC-STALE-LEASE", "drive-id-stale-lease"
        )
        first_claim = setup_flow.claim_sheets_write(
            pending["transaction_id"], lease_seconds=10, **self.actor
        )

        second_store, second_flow = self.open_flow()
        with self.assertRaises(self.flow_module.SheetsClaimBusyError):
            second_flow.claim_sheets_write(
                pending["transaction_id"], lease_seconds=1, **self.actor
            )
        with setup_store._connection:
            setup_store._connection.execute(
                """
                UPDATE transaction_state
                SET sheets_claim_expires_at = '2000-01-01T00:00:00+00:00'
                WHERE transaction_id = ?
                """,
                (pending["transaction_id"],),
            )
        second_claim = second_flow.claim_sheets_write(
            pending["transaction_id"], lease_seconds=1, **self.actor
        )

        self.assertNotEqual(first_claim.owner_id, second_claim.owner_id)
        with self.assertRaises(self.flow_module.StaleStateError):
            first_claim.assert_owner()
        second_flow.release_sheets_write(second_claim)
        setup_store.close()
        second_store.close()

    def test_crash_after_append_recovers_row_after_lease_expiry(self):
        first_store, first_flow = self.open_flow()
        pending = self.prepare_sheets_pending(
            first_flow, "SYNTHETIC-CRASH-AFTER-APPEND", "drive-id-crash-append"
        )
        claim = first_flow.claim_sheets_write(
            pending["transaction_id"], lease_seconds=10, **self.actor
        )
        adapter = self.adapters.GoogleSheetsAdapter(
            "sheet-1", lambda: "token", session=self.sheets_session, timeout=1
        )
        current = first_flow.get_transaction(pending["transaction_id"], **self.actor)
        self.assertEqual(
            adapter.append_transaction(current, write_claim=claim),
            "Transactions!A2:R2",
        )
        # Simulated process death: no complete/release call; durable lease remains.
        with first_store._connection:
            first_store._connection.execute(
                """
                UPDATE transaction_state
                SET sheets_claim_expires_at = '2000-01-01T00:00:00+00:00'
                WHERE transaction_id = ?
                """,
                (pending["transaction_id"],),
            )
        first_store.close()

        recovered_store, recovered_flow = self.open_flow()
        pipeline = self.adapters.ProductionSavePipeline(
            recovered_flow,
            self.adapters.GoogleDriveAdapter(
                "folder-1", lambda: "token", session=self.drive_session
            ),
            self.adapters.GoogleSheetsAdapter(
                "sheet-1", lambda: "token", session=self.sheets_session
            ),
        )
        result = pipeline.save(pending["transaction_id"], **self.actor)
        self.assertEqual(result["current_state"], "confirmed")
        self.assertEqual(self.sheets_session.batch_calls, 1)
        self.assertEqual(len(self.sheets_session.rows), 2)
        recovered_store.close()


if __name__ == "__main__":
    unittest.main()
