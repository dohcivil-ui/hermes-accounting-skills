"""Durable transaction state for the Lekza Telegram accounting flow.

This module owns no OCR or network calls. The accounting slip bridge supplies an
existing OCR result, while later production adapters consume durable state.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import unicodedata
import uuid


STATE_DB_ENV = "LEKZA_TRANSACTION_STATE_DB"
UPLOAD_ROOTS_ENV = "LEKZA_ALLOWED_UPLOAD_ROOTS"
MAX_SLIP_BYTES_ENV = "LEKZA_MAX_SLIP_BYTES"
DEFAULT_MAX_SLIP_BYTES = 10 * 1024 * 1024
DEFAULT_SHEETS_LEASE_SECONDS = 120
DEFAULT_PROMPT_LEASE_SECONDS = 120

OCR_FIELDS = ("reference_no", "amount", "date", "payer", "payee", "note")
ACTIVE_STATES = {
    "waiting_project",
    "waiting_user",
    "waiting_type",
    "waiting_category",
    "waiting_review",
    "confirmed_intent",
    "drive_pending",
    "drive_uploaded",
    "sheets_pending",
    "failed",
}
ALL_STATES = ACTIVE_STATES | {"confirmed", "cancelled"}
MUTABLE_STATE_COLUMNS = {
    "reference_no",
    "reference_no_normalized",
    "ocr_fields_json",
    "needs_reference",
    "needs_amount",
    "project",
    "transaction_type",
    "category",
    "selected_user_id",
    "entry_mode",
    "new_project",
    "history_json",
    "current_state",
    "drive_file_id",
    "drive_upload_id",
    "slip_url",
    "sheets_row_identity",
    "retry_count",
    "retry_state",
    "last_error_code",
}


class TransactionStateError(RuntimeError):
    """Base class for safe transaction-state failures."""


class AuthorizationError(TransactionStateError):
    """The callback actor does not own the transaction."""


class DuplicateReferenceError(TransactionStateError):
    """The tenant already has a transaction for this business reference."""


class StaleStateError(TransactionStateError):
    """The callback version no longer matches durable state."""


class InvalidTransitionError(TransactionStateError):
    """The requested state transition is invalid from the current state."""


class MultipleManualPendingError(TransactionStateError):
    """The actor must explicitly select one pending manual transaction."""

    def __init__(self, records):
        super().__init__("Multiple manual inputs are pending")
        self.records = tuple(records)


class UnsafeSourcePathError(TransactionStateError):
    """The source slip is outside the approved runtime file policy."""


class SheetsClaimBusyError(TransactionStateError):
    """Another live worker currently owns the Sheets write lease."""

    claim_busy = True

    def __init__(self, retry_after):
        super().__init__("Sheets write lease is held by another worker")
        self.retry_after = max(0.0, float(retry_after))


class SheetsWriteClaim:
    """Capability identifying the current durable Sheets lease owner."""

    def __init__(self, transaction_id, owner_id, expires_at, owner_check):
        self.transaction_id = str(transaction_id)
        self.owner_id = str(owner_id)
        self.expires_at = str(expires_at)
        self._owner_check = owner_check
        self.active = True

    def assert_owner(self, minimum_valid_seconds=0):
        if not self.active:
            raise StaleStateError("Sheets write lease is no longer active")
        self._owner_check(self, minimum_valid_seconds=minimum_valid_seconds)


class PromptDeliveryClaim:
    """Capability identifying the current initial-prompt delivery owner."""

    def __init__(self, transaction_id, owner_id, expires_at):
        self.transaction_id = str(transaction_id)
        self.owner_id = str(owner_id)
        self.expires_at = str(expires_at)
        self.active = True


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _normalize_reference(reference_no):
    normalized = unicodedata.normalize("NFKC", str(reference_no or ""))
    return "".join(normalized.split()).upper()


def _as_number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("Amount must be a number")
    text = str(value).strip()
    if not re.fullmatch(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", text):
        raise ValueError("Amount must be a number")
    try:
        amount = Decimal(text.replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError("Amount must be a number") from exc
    if not amount.is_finite():
        raise ValueError("Amount must be a finite number")
    if amount <= 0:
        raise ValueError("Amount must be greater than zero")
    if amount == amount.to_integral_value():
        integer = int(amount)
        if integer > 2**53 - 1:
            raise ValueError("Amount exceeds safe numeric precision")
        return integer
    numeric = float(amount)
    if not math.isfinite(numeric) or Decimal(str(numeric)) != amount:
        raise ValueError("Amount exceeds safe numeric precision")
    return numeric


class SQLiteStateStore:
    """SQLite-backed state with tenant-scoped business uniqueness."""

    def __init__(self, db_path):
        path = Path(db_path).expanduser()
        if not path.is_absolute():
            raise ValueError("Transaction state DB path must be absolute")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path.resolve()
        self._connection = sqlite3.connect(
            str(self.path), timeout=10, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout=10000")
        deadline = time.monotonic() + 10
        while True:
            try:
                self._connection.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    self._connection.close()
                    raise
                time.sleep(0.05)
        try:
            self._create_schema()
        except Exception:
            self._connection.close()
            raise

    @classmethod
    def from_environment(cls, environ=None):
        environment = os.environ if environ is None else environ
        db_path = str(environment.get(STATE_DB_ENV) or "").strip()
        if not db_path:
            raise ValueError(f"{STATE_DB_ENV} is required")
        return cls(db_path)

    def _create_schema(self):
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS transaction_state (
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
                needs_reference INTEGER NOT NULL DEFAULT 0,
                needs_amount INTEGER NOT NULL DEFAULT 0,
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
            CREATE INDEX IF NOT EXISTS idx_transaction_actor
            ON transaction_state(platform, chat_id, telegram_user_id);
            CREATE INDEX IF NOT EXISTS idx_transaction_state
            ON transaction_state(current_state, updated_at);
            CREATE TABLE IF NOT EXISTS manual_input_selection (
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                telegram_user_id TEXT NOT NULL,
                transaction_id TEXT NOT NULL,
                expected_version INTEGER NOT NULL,
                selected_at TEXT NOT NULL,
                PRIMARY KEY (platform, chat_id, telegram_user_id)
            );
            """
        )
        self._connection.commit()
        migrations = {
            "drive_upload_id": "TEXT",
            "sheets_claim_owner": "TEXT",
            "sheets_claim_expires_at": "TEXT",
            "initial_prompt_state": "TEXT NOT NULL DEFAULT 'pending'",
            "initial_prompt_owner": "TEXT",
            "initial_prompt_lease_expires_at": "TEXT",
            "initial_prompt_attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "initial_prompt_message_id": "TEXT",
            "handoff_key": "TEXT",
            "needs_reference": "INTEGER NOT NULL DEFAULT 0",
            "needs_amount": "INTEGER NOT NULL DEFAULT 0",
        }
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(transaction_state)"
                )
            }
            for column, definition in migrations.items():
                if column in columns:
                    continue
                self._connection.execute(
                    f"ALTER TABLE transaction_state ADD COLUMN {column} {definition}"
                )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS manual_input_selection (
                    platform TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    telegram_user_id TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    expected_version INTEGER NOT NULL,
                    selected_at TEXT NOT NULL,
                    PRIMARY KEY (platform, chat_id, telegram_user_id)
                )
                """
            )
            for row in self._connection.execute(
                """
                SELECT transaction_id, ocr_fields_json, current_state
                FROM transaction_state
                WHERE needs_amount = 0
                  AND current_state NOT IN ('confirmed', 'cancelled')
                """
            ).fetchall():
                try:
                    fields = json.loads(row["ocr_fields_json"])
                    _as_number(fields.get("amount"))
                except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                    self._connection.execute(
                        """
                        UPDATE transaction_state
                        SET needs_amount = 1,
                            entry_mode = CASE
                                WHEN needs_reference = 1 THEN entry_mode
                                ELSE 'amount'
                            END
                        WHERE transaction_id = ?
                        """,
                        (row["transaction_id"],),
                    )
            self._connection.execute("DROP INDEX IF EXISTS uq_active_reference")
            self._connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_active_reference
                ON transaction_state(tenant_id, reference_no_normalized)
                WHERE current_state <> 'cancelled' AND needs_reference = 0
                """
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
            self._connection.execute(
                """
                UPDATE transaction_state
                SET initial_prompt_state = 'delivered',
                    initial_prompt_owner = NULL,
                    initial_prompt_lease_expires_at = NULL
                WHERE initial_prompt_message_id IS NOT NULL
                """
            )
            self._connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_transaction_handoff
                ON transaction_state(handoff_key) WHERE handoff_key IS NOT NULL
                """
            )

    def create(self, record):
        columns = tuple(record)
        placeholders = ", ".join("?" for _ in columns)
        sql = (
            f"INSERT INTO transaction_state ({', '.join(columns)}) "
            f"VALUES ({placeholders})"
        )
        try:
            with self._connection:
                self._connection.execute(sql, tuple(record[column] for column in columns))
        except sqlite3.IntegrityError as exc:
            if "handoff_key" in str(exc) and record.get("handoff_key"):
                existing = self.get_by_handoff(record["handoff_key"])
                if existing is not None:
                    return existing
            if "reference_no_normalized" in str(exc):
                raise DuplicateReferenceError(
                    "Reference number already exists for this tenant"
                ) from exc
            raise
        return self.get(record["transaction_id"])

    def get(self, transaction_id):
        row = self._connection.execute(
            "SELECT * FROM transaction_state WHERE transaction_id = ?",
            (str(transaction_id),),
        ).fetchone()
        return self._decode(row) if row is not None else None

    def get_by_reference(self, tenant_id, reference_no):
        reference_key = _normalize_reference(reference_no)
        if not reference_key:
            return None
        row = self._connection.execute(
            """
            SELECT * FROM transaction_state
            WHERE tenant_id = ? AND reference_no_normalized = ?
            """,
            (str(tenant_id), reference_key),
        ).fetchone()
        return self._decode(row) if row is not None else None

    def get_by_handoff(self, handoff_key):
        row = self._connection.execute(
            "SELECT * FROM transaction_state WHERE handoff_key = ?",
            (str(handoff_key),),
        ).fetchone()
        return self._decode(row) if row is not None else None

    def list_manual_pending(self, *, platform, chat_id, telegram_user_id):
        rows = self._connection.execute(
            """
            SELECT * FROM transaction_state
            WHERE platform = ? AND chat_id = ? AND telegram_user_id = ?
              AND current_state NOT IN ('confirmed', 'cancelled', 'failed')
              AND entry_mode IS NOT NULL
            ORDER BY updated_at DESC
            """,
            (str(platform), str(chat_id), str(telegram_user_id)),
        ).fetchall()
        return [self._decode(row) for row in rows]

    def get_manual_selection(self, *, platform, chat_id, telegram_user_id):
        row = self._connection.execute(
            """
            SELECT transaction_id, expected_version
            FROM manual_input_selection
            WHERE platform = ? AND chat_id = ? AND telegram_user_id = ?
            """,
            (str(platform), str(chat_id), str(telegram_user_id)),
        ).fetchone()
        return dict(row) if row is not None else None

    def set_manual_selection(
        self, transaction_id, expected_version, *, platform, chat_id,
        telegram_user_id
    ):
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO manual_input_selection (
                    platform, chat_id, telegram_user_id, transaction_id,
                    expected_version, selected_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, chat_id, telegram_user_id) DO UPDATE SET
                    transaction_id = excluded.transaction_id,
                    expected_version = excluded.expected_version,
                    selected_at = excluded.selected_at
                """,
                (str(platform), str(chat_id), str(telegram_user_id),
                 str(transaction_id), int(expected_version), _utc_now()),
            )

    def clear_manual_selection(
        self, *, platform, chat_id, telegram_user_id, transaction_id=None
    ):
        sql = (
            "DELETE FROM manual_input_selection "
            "WHERE platform = ? AND chat_id = ? AND telegram_user_id = ?"
        )
        values = [str(platform), str(chat_id), str(telegram_user_id)]
        if transaction_id is not None:
            sql += " AND transaction_id = ?"
            values.append(str(transaction_id))
        with self._connection:
            self._connection.execute(sql, values)

    def acquire_initial_prompt_delivery(
        self,
        transaction_id,
        *,
        platform,
        chat_id,
        telegram_user_id,
        lease_seconds=DEFAULT_PROMPT_LEASE_SECONDS,
    ):
        """Acquire a recoverable lease and commit before Telegram I/O."""
        lease_seconds = float(lease_seconds)
        if lease_seconds <= 0:
            raise ValueError("Prompt delivery lease duration must be positive")
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        now = datetime.now(timezone.utc)
        owner_id = str(uuid.uuid4())
        expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT platform, chat_id, telegram_user_id,
                       initial_prompt_state, initial_prompt_owner,
                       initial_prompt_lease_expires_at,
                       initial_prompt_message_id
                FROM transaction_state WHERE transaction_id = ?
                """,
                (str(transaction_id),),
            ).fetchone()
            if row is None:
                raise KeyError("Transaction not found")
            owner = (row["platform"], row["chat_id"], row["telegram_user_id"])
            actor = (str(platform), str(chat_id), str(telegram_user_id))
            if owner != actor:
                raise AuthorizationError("Transaction actor is not authorized")
            if (
                row["initial_prompt_state"] == "delivered"
                or row["initial_prompt_message_id"]
            ):
                connection.commit()
                return None
            current_owner = str(row["initial_prompt_owner"] or "").strip()
            current_expiry = str(
                row["initial_prompt_lease_expires_at"] or ""
            ).strip()
            if current_owner and current_expiry:
                try:
                    expiry = datetime.fromisoformat(current_expiry)
                except ValueError:
                    expiry = now
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry > now:
                    connection.commit()
                    return None
            cursor = connection.execute(
                """
                UPDATE transaction_state
                SET initial_prompt_state = 'delivering',
                    initial_prompt_owner = ?,
                    initial_prompt_lease_expires_at = ?,
                    initial_prompt_attempt_count = initial_prompt_attempt_count + 1,
                    updated_at = ?
                WHERE transaction_id = ?
                """,
                (owner_id, expires_at, now.isoformat(), str(transaction_id)),
            )
            if cursor.rowcount != 1:
                raise StaleStateError("Initial prompt delivery claim was lost")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return PromptDeliveryClaim(transaction_id, owner_id, expires_at)

    def release_initial_prompt_delivery(self, claim):
        if not claim.active:
            return
        with self._connection:
            self._connection.execute(
                """
                UPDATE transaction_state
                SET initial_prompt_state = 'pending',
                    initial_prompt_owner = NULL,
                    initial_prompt_lease_expires_at = NULL,
                    updated_at = ?
                WHERE transaction_id = ?
                  AND initial_prompt_state = 'delivering'
                  AND initial_prompt_owner = ?
                """,
                (_utc_now(), claim.transaction_id, claim.owner_id),
            )
        claim.active = False

    def complete_initial_prompt(
        self,
        claim,
        *,
        platform,
        chat_id,
        telegram_user_id,
        message_id,
    ):
        safe_message_id = str(message_id or "").strip()
        if not safe_message_id:
            raise ValueError("Telegram prompt result requires message_id")
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE transaction_state
                SET initial_prompt_state = 'delivered',
                    initial_prompt_owner = NULL,
                    initial_prompt_lease_expires_at = NULL,
                    initial_prompt_message_id = ?, updated_at = ?
                WHERE transaction_id = ?
                  AND platform = ? AND chat_id = ? AND telegram_user_id = ?
                  AND initial_prompt_state = 'delivering'
                  AND initial_prompt_owner = ?
                  AND initial_prompt_message_id IS NULL
                """,
                (
                    safe_message_id,
                    _utc_now(),
                    claim.transaction_id,
                    str(platform),
                    str(chat_id),
                    str(telegram_user_id),
                    claim.owner_id,
                ),
            )
        if cursor.rowcount != 1:
            record = self.get(claim.transaction_id)
            if record is None:
                raise KeyError("Transaction not found")
            owner = (
                record["platform"],
                record["chat_id"],
                record["telegram_user_id"],
            )
            actor = (str(platform), str(chat_id), str(telegram_user_id))
            if owner != actor:
                raise AuthorizationError("Transaction actor is not authorized")
            if record.get("initial_prompt_message_id") == safe_message_id:
                claim.active = False
                return record
            raise StaleStateError("Initial Telegram prompt claim is not active")
        claim.active = False
        return self.get(claim.transaction_id)

    def acquire_sheets_write_claim(
        self,
        transaction_id,
        *,
        platform,
        chat_id,
        telegram_user_id,
        lease_seconds=DEFAULT_SHEETS_LEASE_SECONDS,
    ):
        """Atomically claim one transaction, committing before Google I/O."""
        lease_seconds = float(lease_seconds)
        if lease_seconds <= 0:
            raise ValueError("Sheets lease duration must be positive")
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        now = datetime.now(timezone.utc)
        owner_id = str(uuid.uuid4())
        expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT platform, chat_id, telegram_user_id, current_state,
                       sheets_claim_owner, sheets_claim_expires_at
                FROM transaction_state WHERE transaction_id = ?
                """,
                (str(transaction_id),),
            ).fetchone()
            if row is None:
                raise KeyError("Transaction not found")
            owner = (row["platform"], row["chat_id"], row["telegram_user_id"])
            actor = (str(platform), str(chat_id), str(telegram_user_id))
            if owner != actor:
                raise AuthorizationError("Transaction actor is not authorized")
            if row["current_state"] != "sheets_pending":
                raise InvalidTransitionError("Sheets write is not pending")
            current_owner = str(row["sheets_claim_owner"] or "").strip()
            current_expiry = str(row["sheets_claim_expires_at"] or "").strip()
            if current_owner and current_expiry:
                try:
                    expiry = datetime.fromisoformat(current_expiry)
                except ValueError as exc:
                    raise TransactionStateError("Sheets lease expiry is malformed") from exc
                if expiry > now:
                    raise SheetsClaimBusyError((expiry - now).total_seconds())
            cursor = connection.execute(
                """
                UPDATE transaction_state
                SET sheets_claim_owner = ?, sheets_claim_expires_at = ?,
                    updated_at = ?, version = version + 1
                WHERE transaction_id = ? AND current_state = 'sheets_pending'
                """,
                (owner_id, expires_at, now.isoformat(), str(transaction_id)),
            )
            if cursor.rowcount != 1:
                raise StaleStateError("Sheets write claim changed concurrently")
            connection.commit()
        finally:
            connection.close()
        return SheetsWriteClaim(
            transaction_id,
            owner_id,
            expires_at,
            self.assert_sheets_claim_owner,
        )

    def assert_sheets_claim_owner(self, claim, *, minimum_valid_seconds=0):
        row = self._connection.execute(
            """
            SELECT sheets_claim_owner, sheets_claim_expires_at, current_state
            FROM transaction_state WHERE transaction_id = ?
            """,
            (claim.transaction_id,),
        ).fetchone()
        if row is None:
            raise KeyError("Transaction not found")
        expires_at = str(row["sheets_claim_expires_at"] or "")
        try:
            expiry = datetime.fromisoformat(expires_at)
        except ValueError as exc:
            raise StaleStateError("Sheets write lease is invalid") from exc
        now = datetime.now(timezone.utc)
        if (
            row["current_state"] != "sheets_pending"
            or row["sheets_claim_owner"] != claim.owner_id
            or (expiry - now).total_seconds() <= float(minimum_valid_seconds)
        ):
            claim.active = False
            raise StaleStateError("Sheets write lease is stale")

    def release_sheets_write_claim(self, claim):
        with self._connection:
            self._connection.execute(
                """
                UPDATE transaction_state
                SET sheets_claim_owner = NULL, sheets_claim_expires_at = NULL,
                    updated_at = ?, version = version + 1
                WHERE transaction_id = ? AND current_state = 'sheets_pending'
                  AND sheets_claim_owner = ?
                """,
                (_utc_now(), claim.transaction_id, claim.owner_id),
            )
        claim.active = False

    def complete_sheets_write_claim(
        self, claim, *, platform, chat_id, telegram_user_id, row_identity
    ):
        safe_identity = str(row_identity or "").strip()
        if not safe_identity:
            raise ValueError("Sheets row identity is required")
        record = self.get(claim.transaction_id)
        if record is None:
            raise KeyError("Transaction not found")
        owner = (record["platform"], record["chat_id"], record["telegram_user_id"])
        actor = (str(platform), str(chat_id), str(telegram_user_id))
        if owner != actor:
            raise AuthorizationError("Transaction actor is not authorized")
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE transaction_state
                SET current_state = 'confirmed', sheets_row_identity = ?,
                    sheets_claim_owner = NULL, sheets_claim_expires_at = NULL,
                    updated_at = ?, version = version + 1
                WHERE transaction_id = ? AND current_state = 'sheets_pending'
                  AND sheets_claim_owner = ?
                """,
                (safe_identity, _utc_now(), claim.transaction_id, claim.owner_id),
            )
        claim.active = False
        if cursor.rowcount != 1:
            raise StaleStateError("Sheets write lease is no longer owned")
        return self.get(claim.transaction_id)

    def transition(
        self,
        transaction_id,
        *,
        platform,
        chat_id,
        telegram_user_id,
        expected_version,
        allowed_from,
        changes,
    ):
        record = self.get(transaction_id)
        if record is None:
            raise KeyError("Transaction not found")
        actor = (str(platform), str(chat_id), str(telegram_user_id))
        owner = (
            record["platform"],
            record["chat_id"],
            record["telegram_user_id"],
        )
        if actor != owner:
            raise AuthorizationError("Transaction actor is not authorized")
        if record["version"] != int(expected_version):
            raise StaleStateError("Transaction callback is stale")
        if record["current_state"] not in set(allowed_from):
            raise InvalidTransitionError(
                f"Transition is invalid from {record['current_state']}"
            )

        updates = dict(changes)
        if not updates or not set(updates).issubset(MUTABLE_STATE_COLUMNS):
            raise InvalidTransitionError("Transition contains immutable fields")
        if "current_state" in updates and updates["current_state"] not in ALL_STATES:
            raise InvalidTransitionError("Unknown transaction state")
        updates["updated_at"] = _utc_now()
        updates["version"] = record["version"] + 1
        assignments = ", ".join(f"{column} = ?" for column in updates)
        parameters = tuple(updates.values()) + (
            str(transaction_id),
            record["version"],
            record["platform"],
            record["chat_id"],
            record["telegram_user_id"],
        )
        with self._connection:
            cursor = self._connection.execute(
                f"""
                UPDATE transaction_state
                SET {assignments}
                WHERE transaction_id = ? AND version = ?
                  AND platform = ? AND chat_id = ? AND telegram_user_id = ?
                """,
                parameters,
            )
        if cursor.rowcount != 1:
            raise StaleStateError("Transaction changed concurrently")
        return self.get(transaction_id)

    @staticmethod
    def _decode(row):
        record = dict(row)
        record["ocr_fields"] = json.loads(record.pop("ocr_fields_json"))
        record["history"] = json.loads(record.pop("history_json"))
        record["new_project"] = bool(record["new_project"])
        record["needs_reference"] = bool(record["needs_reference"])
        record["needs_amount"] = bool(record["needs_amount"])
        return record

    def close(self):
        self._connection.close()


class TransactionFlow:
    """Public durable-state interface used by Telegram and future adapters."""

    def __init__(
        self,
        state_store,
        *,
        allowed_source_roots,
        max_source_size=DEFAULT_MAX_SLIP_BYTES,
        projects=None,
    ):
        self._store = state_store
        self._allowed_source_roots = [
            Path(root).expanduser().resolve(strict=True)
            for root in allowed_source_roots
        ]
        if not self._allowed_source_roots:
            raise ValueError("At least one allowed source root is required")
        self._max_source_size = int(max_source_size)
        if self._max_source_size <= 0:
            raise ValueError("Maximum source size must be positive")
        self._projects = list(projects or [])

    @classmethod
    def from_environment(cls, state_store, *, environ=None, projects=None):
        environment = os.environ if environ is None else environ
        roots_value = str(environment.get(UPLOAD_ROOTS_ENV) or "").strip()
        if not roots_value:
            raise ValueError(f"{UPLOAD_ROOTS_ENV} is required")
        roots = [value for value in roots_value.split(os.pathsep) if value]
        max_size = int(environment.get(MAX_SLIP_BYTES_ENV, DEFAULT_MAX_SLIP_BYTES))
        return cls(
            state_store,
            allowed_source_roots=roots,
            max_source_size=max_size,
            projects=projects,
        )

    def begin(
        self,
        *,
        tenant_id,
        platform,
        chat_id,
        thread_id,
        session_id,
        handoff_key=None,
        telegram_user_id,
        source_image_path,
        ocr_result,
    ):
        source_path = self._validate_source_path(source_image_path)
        parsed = dict(ocr_result.get("parsed") or {})
        reference_no = str(parsed.get("reference_no") or "").strip()
        reference_key = _normalize_reference(reference_no)
        needs_reference = not reference_key
        if not needs_reference:
            reference_no = self._validated_reference(reference_no)
            reference_key = _normalize_reference(reference_no)

        minimal_fields = {
            field: parsed[field]
            for field in OCR_FIELDS
            if field in parsed and parsed[field] is not None
        }
        if not needs_reference:
            minimal_fields["reference_no"] = reference_no
        needs_amount = True
        if "amount" in minimal_fields:
            try:
                minimal_fields["amount"] = _as_number(minimal_fields["amount"])
                needs_amount = False
            except ValueError:
                minimal_fields.pop("amount", None)
        now = _utc_now()
        transaction_id = str(uuid.uuid4())
        record = self._store.create(
            {
                "transaction_id": transaction_id,
                "tenant_id": str(tenant_id),
                "platform": str(platform),
                "chat_id": str(chat_id),
                "thread_id": None if thread_id is None else str(thread_id),
                "session_id": str(session_id),
                "handoff_key": None if handoff_key is None else str(handoff_key),
                "telegram_user_id": str(telegram_user_id),
                "reference_no": reference_no,
                "reference_no_normalized": reference_key,
                "needs_reference": 1 if needs_reference else 0,
                "needs_amount": 1 if needs_amount else 0,
                "source_image_path": str(source_path),
                "ocr_fields_json": json.dumps(
                    minimal_fields, ensure_ascii=False, separators=(",", ":")
                ),
                "confidence": ocr_result.get("confidence"),
                "entry_mode": "reference" if needs_reference else ("amount" if needs_amount else None),
                "current_state": "waiting_project",
                "created_at": now,
                "updated_at": now,
            }
        )
        return self._view(record)

    def begin_or_recover(self, **handoff):
        """Create once, or recover the same retried Telegram/OCR handoff."""
        handoff = dict(handoff)
        handoff_id = str(handoff.pop("handoff_id", "") or "")
        source_path = str(self._validate_source_path(handoff["source_image_path"]))
        identity = {
            "platform": str(handoff["platform"]),
            "chat_id": str(handoff["chat_id"]),
            "telegram_user_id": str(handoff["telegram_user_id"]),
            "handoff_id": handoff_id,
        }
        if not handoff_id:
            identity.update({
                "thread_id": None
                if handoff.get("thread_id") is None
                else str(handoff["thread_id"]),
                "session_id": str(handoff["session_id"]),
                "source_image_path": source_path,
            })
        handoff_key = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        existing_handoff = self._store.get_by_handoff(handoff_key)
        if existing_handoff is not None:
            return self._view(existing_handoff)
        handoff = dict(handoff, handoff_key=handoff_key)
        try:
            return self.begin(**handoff)
        except DuplicateReferenceError:
            parsed = dict((handoff.get("ocr_result") or {}).get("parsed") or {})
            existing = self._store.get_by_reference(
                handoff["tenant_id"], parsed.get("reference_no")
            )
            if existing is None:
                raise
            expected = (
                str(handoff["platform"]),
                str(handoff["chat_id"]),
                None if handoff.get("thread_id") is None else str(handoff["thread_id"]),
                str(handoff["session_id"]),
                str(handoff["telegram_user_id"]),
                source_path,
            )
            actual = (
                existing["platform"],
                existing["chat_id"],
                existing["thread_id"],
                existing["session_id"],
                existing["telegram_user_id"],
                existing["source_image_path"],
            )
            if actual != expected:
                raise
            return self._view(existing)

    def get_transaction(
        self, transaction_id, *, platform, chat_id, telegram_user_id
    ):
        record = self._require_authorized(
            transaction_id, platform, chat_id, telegram_user_id
        )
        return dict(record)

    def get_manual_pending(self, *, platform, chat_id, telegram_user_id):
        actor = {
            "platform": platform, "chat_id": chat_id,
            "telegram_user_id": telegram_user_id,
        }
        selected = self._store.get_manual_selection(**actor)
        if selected is not None:
            record = self._require_authorized(selected["transaction_id"], **actor)
            if record["version"] != int(selected["expected_version"]):
                self._store.clear_manual_selection(**actor)
                raise StaleStateError("Selected manual transaction is stale")
            if record.get("entry_mode") is None or record["current_state"] in {
                "confirmed", "cancelled", "failed"
            }:
                self._store.clear_manual_selection(**actor)
                raise InvalidTransitionError("Selected manual input is no longer pending")
            return dict(record)
        records = self._store.list_manual_pending(
            **actor
        )
        if len(records) > 1:
            raise MultipleManualPendingError(records)
        return records[0] if records else None

    def select_manual_pending(
        self, transaction_id, *, expected_version, platform, chat_id,
        telegram_user_id
    ):
        record = self._require_authorized(
            transaction_id, platform, chat_id, telegram_user_id
        )
        self._require_current_version(record, expected_version)
        if record.get("entry_mode") is None or record["current_state"] in {
            "confirmed", "cancelled", "failed"
        }:
            raise InvalidTransitionError("Transaction has no pending manual input")
        self._store.set_manual_selection(
            transaction_id, expected_version,
            platform=platform,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
        )
        return self._view(record)

    def clear_manual_selection(
        self, transaction_id, *, platform, chat_id, telegram_user_id
    ):
        self._store.clear_manual_selection(
            platform=platform, chat_id=chat_id,
            telegram_user_id=telegram_user_id, transaction_id=transaction_id,
        )

    def acquire_initial_prompt_delivery(
        self,
        transaction_id,
        *,
        platform,
        chat_id,
        telegram_user_id,
        lease_seconds=DEFAULT_PROMPT_LEASE_SECONDS,
    ):
        self._require_authorized(transaction_id, platform, chat_id, telegram_user_id)
        return self._store.acquire_initial_prompt_delivery(
            transaction_id,
            platform=platform,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            lease_seconds=lease_seconds,
        )

    def release_initial_prompt_delivery(self, claim):
        self._store.release_initial_prompt_delivery(claim)

    def complete_initial_prompt(
        self,
        claim,
        *,
        platform,
        chat_id,
        telegram_user_id,
        message_id,
    ):
        self._require_authorized(
            claim.transaction_id, platform, chat_id, telegram_user_id
        )
        return self._store.complete_initial_prompt(
            claim,
            platform=platform,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            message_id=message_id,
        )

    def claim_sheets_write(
        self,
        transaction_id,
        *,
        platform,
        chat_id,
        telegram_user_id,
        lease_seconds=DEFAULT_SHEETS_LEASE_SECONDS,
    ):
        return self._store.acquire_sheets_write_claim(
            transaction_id,
            platform=platform,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            lease_seconds=lease_seconds,
        )

    def release_sheets_write(self, claim):
        self._store.release_sheets_write_claim(claim)

    def complete_sheets_write(
        self, claim, *, platform, chat_id, telegram_user_id, sheets_row_identity
    ):
        return self._store.complete_sheets_write_claim(
            claim,
            platform=platform,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            row_identity=sheets_row_identity,
        )

    def choose(
        self,
        transaction_id,
        *,
        expected_version,
        platform,
        chat_id,
        telegram_user_id,
        action,
        value=None,
    ):
        record = self._require_authorized(
            transaction_id, platform, chat_id, telegram_user_id
        )
        self._require_current_version(record, expected_version)
        if record.get("needs_amount"):
            raise InvalidTransitionError("Transaction requires a valid amount")
        if record["current_state"] == "waiting_project" and action == "select_project":
            if value not in self._projects:
                raise ValueError("Project is not available")
            updated = self._store.transition(
                transaction_id,
                platform=platform,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                expected_version=expected_version,
                allowed_from={"waiting_project"},
                changes={
                    "project": str(value),
                    "new_project": 0,
                    "history_json": json.dumps(["waiting_project"]),
                    "current_state": "waiting_user",
                },
            )
            return self._view(updated)
        if record["current_state"] == "waiting_project" and action in {
            "new_project",
            "manual_entry",
        }:
            updated = self._store.transition(
                transaction_id,
                platform=platform,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                expected_version=expected_version,
                allowed_from={"waiting_project"},
                changes={
                    "entry_mode": action,
                    "new_project": 1 if action == "new_project" else 0,
                },
            )
            return self._view(updated)
        if record["current_state"] == "waiting_user" and action == "use_sender":
            return self._transition_with_history(
                record,
                expected_version=expected_version,
                platform=platform,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                next_state="waiting_type",
                changes={"selected_user_id": record["telegram_user_id"]},
            )
        if record["current_state"] == "waiting_type" and action in {
            "income",
            "expense",
        }:
            return self._transition_with_history(
                record,
                expected_version=expected_version,
                platform=platform,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                next_state="waiting_category",
                changes={"transaction_type": action},
            )
        if record["current_state"] == "waiting_category":
            categories = {
                "expense": {"materials", "labor", "transport", "contractor", "other"},
                "income": {"installment", "advance_refund", "other"},
            }
            if action in categories.get(record["transaction_type"], set()):
                return self._transition_with_history(
                    record,
                    expected_version=expected_version,
                    platform=platform,
                    chat_id=chat_id,
                    telegram_user_id=telegram_user_id,
                    next_state="waiting_review",
                    changes={"category": action},
                )
            if action == "manual_entry":
                updated = self._store.transition(
                    transaction_id,
                    platform=platform,
                    chat_id=chat_id,
                    telegram_user_id=telegram_user_id,
                    expected_version=expected_version,
                    allowed_from={"waiting_category"},
                    changes={"entry_mode": "category"},
                )
                return self._view(updated)
        raise InvalidTransitionError(
            f"Action {action!r} is invalid from {record['current_state']}"
        )

    def submit_manual(
        self,
        transaction_id,
        *,
        expected_version,
        platform,
        chat_id,
        telegram_user_id,
        value,
    ):
        record = self._require_authorized(
            transaction_id, platform, chat_id, telegram_user_id
        )
        self._require_current_version(record, expected_version)
        manual_value = str(value or "").strip()
        if not manual_value:
            raise ValueError("Manual value is required")
        if record["needs_reference"] and record["entry_mode"] == "reference":
            reference_no = self._validated_reference(manual_value)
            ocr_fields = dict(record["ocr_fields"])
            ocr_fields["reference_no"] = reference_no
            try:
                updated = self._store.transition(
                    transaction_id,
                    platform=platform,
                    chat_id=chat_id,
                    telegram_user_id=telegram_user_id,
                    expected_version=expected_version,
                    allowed_from={"waiting_project"},
                    changes={
                        "reference_no": reference_no,
                        "reference_no_normalized": _normalize_reference(
                            reference_no
                        ),
                        "ocr_fields_json": json.dumps(
                            ocr_fields,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        "needs_reference": 0,
                        "entry_mode": "amount" if record.get("needs_amount") else None,
                    },
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateReferenceError(
                    "Reference number already exists for this tenant"
                ) from exc
            return self._view(updated)
        if record["entry_mode"] == "amount":
            amount = _as_number(manual_value)
            ocr_fields = dict(record["ocr_fields"])
            ocr_fields["amount"] = amount
            updated = self._store.transition(
                transaction_id,
                platform=platform,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                expected_version=expected_version,
                allowed_from={record["current_state"]},
                changes={
                    "ocr_fields_json": json.dumps(
                        ocr_fields, ensure_ascii=False, separators=(",", ":")
                    ),
                    "needs_amount": 0,
                    "entry_mode": None,
                },
            )
            return self._view(updated)
        if record["current_state"] == "waiting_project" and record[
            "entry_mode"
        ] in {"new_project", "manual_entry"}:
            return self._transition_with_history(
                record,
                expected_version=expected_version,
                platform=platform,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                next_state="waiting_user",
                changes={"project": manual_value, "entry_mode": None},
            )
        if (
            record["current_state"] == "waiting_category"
            and record["entry_mode"] == "category"
        ):
            return self._transition_with_history(
                record,
                expected_version=expected_version,
                platform=platform,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                next_state="waiting_review",
                changes={"category": manual_value, "entry_mode": None},
            )
        raise InvalidTransitionError("Manual input is not expected")

    def confirm(
        self,
        transaction_id,
        *,
        expected_version,
        platform,
        chat_id,
        telegram_user_id,
    ):
        record = self._require_authorized(
            transaction_id, platform, chat_id, telegram_user_id
        )
        post_confirm_states = {
            "confirmed_intent",
            "drive_pending",
            "drive_uploaded",
            "sheets_pending",
            "confirmed",
        }
        if record["current_state"] in post_confirm_states:
            return self._view(record)
        if record["current_state"] != "waiting_review":
            raise InvalidTransitionError("Transaction is not ready for confirmation")
        self._require_valid_reference(record)
        self._require_valid_amount(record)
        try:
            updated = self._store.transition(
                transaction_id,
                platform=platform,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                expected_version=expected_version,
                allowed_from={"waiting_review"},
                changes={"current_state": "confirmed_intent"},
            )
        except StaleStateError:
            concurrent = self._require_authorized(
                transaction_id, platform, chat_id, telegram_user_id
            )
            if concurrent["current_state"] in post_confirm_states:
                return self._view(concurrent)
            raise
        return self._view(updated)

    def mark_drive_pending(
        self,
        transaction_id,
        *,
        expected_version,
        platform,
        chat_id,
        telegram_user_id,
    ):
        record = self._require_authorized(
            transaction_id, platform, chat_id, telegram_user_id
        )
        self._require_current_version(record, expected_version)
        self._require_valid_reference(record)
        self._require_valid_amount(record)
        return self._transition_state(
            transaction_id,
            expected_version=expected_version,
            platform=platform,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            allowed_from={"confirmed_intent"},
            next_state="drive_pending",
        )

    def mark_drive_uploaded(
        self,
        transaction_id,
        *,
        expected_version,
        platform,
        chat_id,
        telegram_user_id,
        file_id,
        web_view_link,
    ):
        safe_file_id = str(file_id or "").strip()
        safe_link = str(web_view_link or "").strip()
        if not safe_file_id or not safe_link:
            raise ValueError("Drive result requires file_id and webViewLink")
        record = self._require_authorized(
            transaction_id, platform, chat_id, telegram_user_id
        )
        if record.get("drive_upload_id") and record["drive_upload_id"] != safe_file_id:
            raise InvalidTransitionError("Drive result does not match reserved file ID")
        return self._transition_state(
            transaction_id,
            expected_version=expected_version,
            platform=platform,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            allowed_from={"drive_pending"},
            next_state="drive_uploaded",
            changes={"drive_file_id": safe_file_id, "slip_url": safe_link},
        )

    def reserve_drive_upload(
        self,
        transaction_id,
        *,
        expected_version,
        platform,
        chat_id,
        telegram_user_id,
        file_id,
    ):
        """Durably reserve Drive's pre-generated ID before uploading bytes."""
        safe_file_id = str(file_id or "").strip()
        if not safe_file_id:
            raise ValueError("Drive upload reservation requires file_id")
        record = self._require_authorized(
            transaction_id, platform, chat_id, telegram_user_id
        )
        self._require_current_version(record, expected_version)
        if record["current_state"] != "drive_pending":
            raise InvalidTransitionError("Drive upload is not pending")
        if record.get("drive_upload_id"):
            if record["drive_upload_id"] != safe_file_id:
                raise InvalidTransitionError("Drive upload ID is already reserved")
            return self._view(record)
        updated = self._store.transition(
            transaction_id,
            platform=platform,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            expected_version=expected_version,
            allowed_from={"drive_pending"},
            changes={"drive_upload_id": safe_file_id},
        )
        return self._view(updated)

    def mark_sheets_pending(
        self,
        transaction_id,
        *,
        expected_version,
        platform,
        chat_id,
        telegram_user_id,
    ):
        return self._transition_state(
            transaction_id,
            expected_version=expected_version,
            platform=platform,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            allowed_from={"drive_uploaded"},
            next_state="sheets_pending",
        )

    def mark_confirmed(
        self,
        transaction_id,
        *,
        expected_version,
        platform,
        chat_id,
        telegram_user_id,
        sheets_row_identity,
    ):
        row_identity = str(sheets_row_identity or "").strip()
        if not row_identity:
            raise ValueError("Sheets row identity is required")
        record = self._require_authorized(
            transaction_id, platform, chat_id, telegram_user_id
        )
        if not record["drive_file_id"] or not record["slip_url"]:
            raise InvalidTransitionError("Drive upload identity is required")
        return self._transition_state(
            transaction_id,
            expected_version=expected_version,
            platform=platform,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            allowed_from={"sheets_pending"},
            next_state="confirmed",
            changes={"sheets_row_identity": row_identity},
        )

    def back(
        self,
        transaction_id,
        *,
        expected_version,
        platform,
        chat_id,
        telegram_user_id,
    ):
        record = self._require_authorized(
            transaction_id, platform, chat_id, telegram_user_id
        )
        self._require_current_version(record, expected_version)
        if record["current_state"] not in {
            "waiting_project",
            "waiting_user",
            "waiting_type",
            "waiting_category",
            "waiting_review",
        }:
            raise InvalidTransitionError("Back is not available from this state")
        if not record["history"]:
            if record.get("entry_mode"):
                updated = self._store.transition(
                    transaction_id,
                    platform=platform,
                    chat_id=chat_id,
                    telegram_user_id=telegram_user_id,
                    expected_version=expected_version,
                    allowed_from={record["current_state"]},
                    changes={"entry_mode": None},
                )
                return self._view(updated)
            return self._view(record)
        history = list(record["history"])
        previous_state = history.pop()
        updated = self._store.transition(
            transaction_id,
            platform=platform,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            expected_version=expected_version,
            allowed_from={record["current_state"]},
            changes={
                "current_state": previous_state,
                "history_json": json.dumps(history),
                "entry_mode": None,
            },
        )
        return self._view(updated)

    def cancel(
        self,
        transaction_id,
        *,
        expected_version,
        platform,
        chat_id,
        telegram_user_id,
    ):
        return self._transition_state(
            transaction_id,
            expected_version=expected_version,
            platform=platform,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            allowed_from={
                "waiting_project",
                "waiting_user",
                "waiting_type",
                "waiting_category",
                "waiting_review",
                "confirmed_intent",
            },
            next_state="cancelled",
        )

    def mark_failed(
        self,
        transaction_id,
        *,
        expected_version,
        platform,
        chat_id,
        telegram_user_id,
        error_code,
    ):
        record = self._require_authorized(
            transaction_id, platform, chat_id, telegram_user_id
        )
        self._require_current_version(record, expected_version)
        retryable_states = {
            "confirmed_intent",
            "drive_pending",
            "drive_uploaded",
            "sheets_pending",
        }
        if record["current_state"] not in retryable_states:
            raise InvalidTransitionError("Failure is not retryable from this state")
        safe_error_code = str(error_code or "").strip().upper()
        if (
            not safe_error_code
            or len(safe_error_code) > 64
            or any(
                not (character.isalnum() or character in {"_", "-"})
                for character in safe_error_code
            )
        ):
            raise ValueError("Error code must be a sanitized identifier")
        return self._transition_state(
            transaction_id,
            expected_version=expected_version,
            platform=platform,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            allowed_from={record["current_state"]},
            next_state="failed",
            changes={
                "retry_state": record["current_state"],
                "retry_count": record["retry_count"] + 1,
                "last_error_code": safe_error_code,
            },
        )

    def retry(
        self,
        transaction_id,
        *,
        expected_version,
        platform,
        chat_id,
        telegram_user_id,
    ):
        record = self._require_authorized(
            transaction_id, platform, chat_id, telegram_user_id
        )
        self._require_current_version(record, expected_version)
        if record["current_state"] != "failed" or record["retry_state"] not in {
            "confirmed_intent",
            "drive_pending",
            "drive_uploaded",
            "sheets_pending",
        }:
            raise InvalidTransitionError("Transaction has no retryable failure")
        return self._transition_state(
            transaction_id,
            expected_version=expected_version,
            platform=platform,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            allowed_from={"failed"},
            next_state=record["retry_state"],
            changes={"retry_state": None, "last_error_code": None},
        )

    def _transition_state(
        self,
        transaction_id,
        *,
        expected_version,
        platform,
        chat_id,
        telegram_user_id,
        allowed_from,
        next_state,
        changes=None,
    ):
        self._require_authorized(
            transaction_id, platform, chat_id, telegram_user_id
        )
        updates = dict(changes or {})
        updates["current_state"] = next_state
        updated = self._store.transition(
            transaction_id,
            platform=platform,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            expected_version=expected_version,
            allowed_from=allowed_from,
            changes=updates,
        )
        return self._view(updated)

    def _transition_with_history(
        self,
        record,
        *,
        expected_version,
        platform,
        chat_id,
        telegram_user_id,
        next_state,
        changes,
    ):
        updates = dict(changes)
        updates["history_json"] = json.dumps(
            record["history"] + [record["current_state"]]
        )
        updates["current_state"] = next_state
        updated = self._store.transition(
            record["transaction_id"],
            platform=platform,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            expected_version=expected_version,
            allowed_from={record["current_state"]},
            changes=updates,
        )
        return self._view(updated)

    def _require_authorized(
        self, transaction_id, platform, chat_id, telegram_user_id
    ):
        record = self._store.get(transaction_id)
        if record is None:
            raise KeyError("Transaction not found")
        identity = (
            record["platform"],
            record["chat_id"],
            record["telegram_user_id"],
        )
        actor = (str(platform), str(chat_id), str(telegram_user_id))
        if identity != actor:
            raise AuthorizationError("Transaction actor is not authorized")
        return record

    @staticmethod
    def _require_current_version(record, expected_version):
        if record["version"] != int(expected_version):
            raise StaleStateError("Transaction callback is stale")

    @staticmethod
    def _validated_reference(value):
        reference = unicodedata.normalize("NFKC", str(value or "")).strip()
        if not 3 <= len(reference) <= 128:
            raise ValueError("Reference number must be 3 to 128 characters")
        if any(
            not (character.isalnum() or character in "-._/")
            for character in reference
        ):
            raise ValueError("Reference number contains unsupported characters")
        return reference

    @staticmethod
    def _require_valid_reference(record):
        reference_no = str(record.get("reference_no") or "").strip()
        if record.get("needs_reference") or not _normalize_reference(reference_no):
            raise InvalidTransitionError(
                "Transaction requires reference number before confirmation"
            )
        try:
            TransactionFlow._validated_reference(reference_no)
        except ValueError as exc:
            raise InvalidTransitionError(
                "Transaction reference number is invalid"
            ) from exc

    @staticmethod
    def _require_valid_amount(record):
        if record.get("needs_amount"):
            raise InvalidTransitionError(
                "Transaction requires a valid amount before confirmation"
            )
        try:
            _as_number((record.get("ocr_fields") or {}).get("amount"))
        except ValueError as exc:
            raise InvalidTransitionError(
                "Transaction amount must be greater than zero"
            ) from exc

    def _validate_source_path(self, source_image_path):
        candidate = Path(source_image_path).expanduser()
        if not candidate.is_absolute():
            raise UnsafeSourcePathError("Source path must be absolute")
        absolute_candidate = candidate.absolute()

        matched_root = None
        for root in self._allowed_source_roots:
            try:
                relative_parts = absolute_candidate.relative_to(root).parts
            except ValueError:
                continue
            current = root
            if current.is_symlink():
                raise UnsafeSourcePathError("Allowed root must not be a symlink")
            for part in relative_parts:
                current = current / part
                if current.is_symlink():
                    raise UnsafeSourcePathError("Source path must not contain symlinks")
            matched_root = root
            break
        if matched_root is None:
            raise UnsafeSourcePathError("Source path is outside allowed runtime roots")

        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise UnsafeSourcePathError("Source image is not available") from exc
        if matched_root != resolved and matched_root not in resolved.parents:
            raise UnsafeSourcePathError("Resolved source escapes allowed runtime root")
        if not resolved.is_file():
            raise UnsafeSourcePathError("Source image must be a regular file")
        size = resolved.stat().st_size
        if size <= 0 or size > self._max_source_size:
            raise UnsafeSourcePathError("Source image size is not allowed")

        header = resolved.read_bytes()[:16]
        detected_type = None
        if header.startswith(b"\xff\xd8\xff"):
            detected_type = "image/jpeg"
        elif header.startswith(b"\x89PNG\r\n\x1a\n"):
            detected_type = "image/png"
        elif len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
            detected_type = "image/webp"
        if detected_type is None:
            raise UnsafeSourcePathError("Source MIME type is not allowed")

        allowed_suffixes = {
            "image/jpeg": {".jpg", ".jpeg"},
            "image/png": {".png"},
            "image/webp": {".webp"},
        }
        if resolved.suffix.lower() not in allowed_suffixes[detected_type]:
            raise UnsafeSourcePathError("Source extension does not match MIME type")
        return resolved

    @staticmethod
    def _view(record):
        return {
            "transaction_id": record["transaction_id"],
            "current_state": record["current_state"],
            "version": record["version"],
        }
