import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[2]
FLOW_PATH = ROOT / "plugins/accounting-slip-bridge/transaction_flow.py"
LEGACY_REFERENCE = "LEGACY-REFERENCE-001"
ASSIGNED_REFERENCE = "MANUAL-REFERENCE-001"

LEGACY_SCHEMA = """
CREATE TABLE transaction_state (
    transaction_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    thread_id TEXT,
    session_id TEXT NOT NULL,
    handoff_key TEXT,
    telegram_user_id TEXT NOT NULL,
    reference_no TEXT NOT NULL,
    reference_no_normalized TEXT NOT NULL,
    source_image_path TEXT NOT NULL,
    ocr_fields_json TEXT NOT NULL,
    confidence REAL,
    project TEXT,
    transaction_type TEXT,
    category TEXT,
    selected_user_id TEXT,
    entry_mode TEXT,
    new_project INTEGER NOT NULL DEFAULT 0,
    history_json TEXT NOT NULL DEFAULT '[]',
    current_state TEXT NOT NULL,
    drive_file_id TEXT,
    drive_upload_id TEXT,
    slip_url TEXT,
    sheets_row_identity TEXT,
    sheets_claim_owner TEXT,
    sheets_claim_expires_at TEXT,
    initial_prompt_state TEXT NOT NULL DEFAULT 'pending',
    initial_prompt_owner TEXT,
    initial_prompt_lease_expires_at TEXT,
    initial_prompt_attempt_count INTEGER NOT NULL DEFAULT 0,
    initial_prompt_message_id TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    retry_state TEXT,
    last_error_code TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (current_state IN (
        'waiting_project', 'waiting_user', 'waiting_type',
        'waiting_category', 'waiting_review', 'confirmed_intent',
        'drive_pending', 'drive_uploaded', 'sheets_pending',
        'confirmed', 'cancelled', 'failed'
    ))
);
CREATE UNIQUE INDEX uq_active_reference
ON transaction_state(tenant_id, reference_no_normalized)
WHERE current_state <> 'cancelled';
"""


def load_module():
    spec = importlib.util.spec_from_file_location(
        "lekza_missing_reference_migration_flow", FLOW_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class MissingReferenceSchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        self.slip = self.uploads / "synthetic-slip.jpg"
        self.slip.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
        self.db_path = self.root / "state" / "transactions.sqlite3"
        self.db_path.parent.mkdir()
        self.transaction_id = "00000000-0000-4000-8000-000000000001"
        self.tenant_id = "migration-test-tenant"
        self.actor = {
            "platform": "telegram",
            "chat_id": "migration-test-chat",
            "telegram_user_id": "migration-test-user",
        }
        self._create_legacy_database()

    def tearDown(self):
        self.temp.cleanup()

    def _create_legacy_database(self):
        connection = sqlite3.connect(self.db_path)
        try:
            connection.executescript(LEGACY_SCHEMA)
            connection.execute(
                """
                INSERT INTO transaction_state (
                    transaction_id, tenant_id, platform, chat_id, session_id,
                    telegram_user_id, reference_no, reference_no_normalized,
                    source_image_path, ocr_fields_json, current_state, version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.transaction_id,
                    self.tenant_id,
                    self.actor["platform"],
                    self.actor["chat_id"],
                    "legacy-session",
                    self.actor["telegram_user_id"],
                    LEGACY_REFERENCE,
                    LEGACY_REFERENCE,
                    str(self.slip),
                    json.dumps({"reference_no": LEGACY_REFERENCE}),
                    "waiting_review",
                    7,
                    "2026-08-31T00:00:00+00:00",
                    "2026-08-31T00:00:00+00:00",
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def _open(self):
        store = self.module.SQLiteStateStore(self.db_path)
        flow = self.module.TransactionFlow(
            store,
            allowed_source_roots=[self.uploads],
            projects=["Synthetic Project"],
        )
        return store, flow

    def _begin_missing(self, flow, handoff_key):
        return flow.begin(
            tenant_id=self.tenant_id,
            platform=self.actor["platform"],
            chat_id=self.actor["chat_id"],
            thread_id=None,
            session_id=f"session-{handoff_key}",
            handoff_key=handoff_key,
            telegram_user_id=self.actor["telegram_user_id"],
            source_image_path=self.slip,
            ocr_result={"parsed": {}, "confidence": 1.0},
        )

    def test_legacy_database_migrates_idempotently_without_losing_state(self):
        for _ in range(3):
            store, flow = self._open()
            record = flow.get_transaction(self.transaction_id, **self.actor)
            self.assertEqual(record["reference_no"], LEGACY_REFERENCE)
            self.assertEqual(record["current_state"], "waiting_review")
            self.assertEqual(record["version"], 7)
            self.assertFalse(record["needs_reference"])
            self.assertTrue(record["needs_amount"])
            self.assertEqual(record["entry_mode"], "amount")
            store.close()

    def test_legacy_missing_amount_is_recoverable_in_original_state(self):
        store, flow = self._open()
        try:
            pending = flow.get_manual_pending(**self.actor)
            self.assertEqual(pending["transaction_id"], self.transaction_id)
            self.assertEqual(pending["current_state"], "waiting_review")
            updated = flow.submit_manual(
                self.transaction_id,
                expected_version=pending["version"],
                value="99.95",
                **self.actor,
            )
            recovered = flow.get_transaction(updated["transaction_id"], **self.actor)
            self.assertEqual(recovered["current_state"], "waiting_review")
            self.assertEqual(recovered["version"], 8)
            self.assertFalse(recovered["needs_amount"])
            self.assertEqual(recovered["ocr_fields"]["amount"], 99.95)
        finally:
            store.close()

    def test_legacy_valid_amount_keeps_state_version_and_entry_mode(self):
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                "UPDATE transaction_state SET ocr_fields_json = ? WHERE transaction_id = ?",
                (json.dumps({"reference_no": LEGACY_REFERENCE, "amount": 10.25}), self.transaction_id),
            )
            connection.commit()
        finally:
            connection.close()
        store, flow = self._open()
        try:
            record = flow.get_transaction(self.transaction_id, **self.actor)
            self.assertEqual(record["current_state"], "waiting_review")
            self.assertEqual(record["version"], 7)
            self.assertFalse(record["needs_amount"])
            self.assertIsNone(record["entry_mode"])
            self.assertEqual(record["ocr_fields"]["amount"], 10.25)
        finally:
            store.close()

    def test_concurrent_legacy_database_open_serializes_additive_migration(self):
        start = threading.Barrier(3)
        errors = []
        states = []

        def worker():
            start.wait(timeout=5)
            try:
                store, flow = self._open()
                try:
                    record = flow.get_transaction(self.transaction_id, **self.actor)
                    states.append((record["current_state"], record["version"], record["needs_amount"]))
                finally:
                    store.close()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        start.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(states, [("waiting_review", 7, True)] * 2)

    def test_missing_rows_coexist_and_reference_assignment_is_unique(self):
        store, flow = self._open()
        try:
            first = self._begin_missing(flow, "missing-handoff-1")
            second = self._begin_missing(flow, "missing-handoff-2")
            self.assertNotEqual(first["transaction_id"], second["transaction_id"])

            assigned = flow.submit_manual(
                first["transaction_id"],
                expected_version=first["version"],
                value=ASSIGNED_REFERENCE,
                **self.actor,
            )
            self.assertEqual(assigned["version"], first["version"] + 1)
            with self.assertRaises(self.module.DuplicateReferenceError):
                flow.submit_manual(
                    second["transaction_id"],
                    expected_version=second["version"],
                    value=ASSIGNED_REFERENCE,
                    **self.actor,
                )

            third = self._begin_missing(flow, "missing-handoff-3")
            with self.assertRaises(self.module.DuplicateReferenceError):
                flow.submit_manual(
                    third["transaction_id"],
                    expected_version=third["version"],
                    value=LEGACY_REFERENCE,
                    **self.actor,
                )
        finally:
            store.close()

    def test_missing_reference_confirm_and_save_are_fail_closed(self):
        store, flow = self._open()
        try:
            missing = self._begin_missing(flow, "missing-handoff-gates")
            with self.assertRaises(self.module.InvalidTransitionError):
                flow.confirm(
                    missing["transaction_id"],
                    expected_version=missing["version"],
                    **self.actor,
                )
            with self.assertRaisesRegex(
                self.module.InvalidTransitionError, "requires reference"
            ):
                flow.mark_drive_pending(
                    missing["transaction_id"],
                    expected_version=missing["version"],
                    **self.actor,
                )
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
