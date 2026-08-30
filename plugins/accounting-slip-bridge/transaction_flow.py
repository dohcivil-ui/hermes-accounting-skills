"""Durable transaction state for the Lekza Telegram accounting flow.

This module owns no OCR or network calls. The accounting slip bridge supplies an
existing OCR result, while later production adapters consume durable state.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import unicodedata
import uuid


STATE_DB_ENV = "LEKZA_TRANSACTION_STATE_DB"
UPLOAD_ROOTS_ENV = "LEKZA_ALLOWED_UPLOAD_ROOTS"
MAX_SLIP_BYTES_ENV = "LEKZA_MAX_SLIP_BYTES"
DEFAULT_MAX_SLIP_BYTES = 10 * 1024 * 1024

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
    "project",
    "transaction_type",
    "category",
    "selected_user_id",
    "entry_mode",
    "new_project",
    "history_json",
    "current_state",
    "drive_file_id",
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


class UnsafeSourcePathError(TransactionStateError):
    """The source slip is outside the approved runtime file policy."""


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _normalize_reference(reference_no):
    normalized = unicodedata.normalize("NFKC", str(reference_no or ""))
    return "".join(normalized.split()).upper()


def _as_number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("Amount must be a number")
    try:
        amount = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("Amount must be a number") from exc
    if not math.isfinite(amount):
        raise ValueError("Amount must be a finite number")
    return amount


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
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=10000")
        self._create_schema()

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
                slip_url TEXT,
                sheets_row_identity TEXT,
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
            CREATE UNIQUE INDEX IF NOT EXISTS uq_active_reference
            ON transaction_state(tenant_id, reference_no_normalized)
            WHERE current_state <> 'cancelled';
            CREATE INDEX IF NOT EXISTS idx_transaction_actor
            ON transaction_state(platform, chat_id, telegram_user_id);
            CREATE INDEX IF NOT EXISTS idx_transaction_state
            ON transaction_state(current_state, updated_at);
            """
        )
        self._connection.commit()

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
        row = self._connection.execute(
            """
            SELECT * FROM transaction_state
            WHERE tenant_id = ? AND reference_no_normalized = ?
            """,
            (str(tenant_id), _normalize_reference(reference_no)),
        ).fetchone()
        return self._decode(row) if row is not None else None

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
        telegram_user_id,
        source_image_path,
        ocr_result,
    ):
        source_path = self._validate_source_path(source_image_path)
        parsed = dict(ocr_result.get("parsed") or {})
        reference_no = str(parsed.get("reference_no") or "").strip()
        reference_key = _normalize_reference(reference_no)
        if not reference_key:
            raise ValueError("OCR result requires reference_no")

        minimal_fields = {
            field: parsed[field]
            for field in OCR_FIELDS
            if field in parsed and parsed[field] is not None
        }
        minimal_fields["reference_no"] = reference_no
        if "amount" in minimal_fields:
            minimal_fields["amount"] = _as_number(minimal_fields["amount"])
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
                "telegram_user_id": str(telegram_user_id),
                "reference_no": reference_no,
                "reference_no_normalized": reference_key,
                "source_image_path": str(source_path),
                "ocr_fields_json": json.dumps(
                    minimal_fields, ensure_ascii=False, separators=(",", ":")
                ),
                "confidence": ocr_result.get("confidence"),
                "current_state": "waiting_project",
                "created_at": now,
                "updated_at": now,
            }
        )
        return self._view(record)

    def get_transaction(
        self, transaction_id, *, platform, chat_id, telegram_user_id
    ):
        record = self._require_authorized(
            transaction_id, platform, chat_id, telegram_user_id
        )
        return dict(record)

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
