"""Production Google Drive/Sheets adapters for confirmed Lekza transactions.

The adapters use Google REST APIs directly and accept an injected HTTP session
and access-token provider so tests never contact external services.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import mimetypes
import os
from pathlib import Path
import time
import urllib.parse
import uuid

try:
    import requests
except ImportError:  # The production runtime supplies requests; tests inject a session.
    requests = None


SLIP_FOLDER_ENV = "LEKZA_SLIP_FOLDER_ID"
SPREADSHEET_ENV = "LEKZA_ACCOUNTING_SPREADSHEET_ID"
ACCESS_TOKEN_ENV = "LEKZA_GOOGLE_ACCESS_TOKEN"

TRANSACTIONS_SCHEMA = (
    "transaction_id", "reference_no", "date", "payer", "payee",
    "project_id", "project", "type", "category", "amount", "note",
    "confidence", "submitted_by", "drive_file_id", "slip_url", "status",
    "created_at", "confirmed_at",
)
PROJECTS_SCHEMA = (
    "project_id", "project_name", "customer", "status", "start_date",
    "created_by", "created_at",
)
USERS_SCHEMA = (
    "telegram_user_id", "name", "frequent_projects", "frequent_keywords",
    "last_actions", "created_at", "updated_at",
)
CANONICAL_SCHEMAS = {
    "Transactions": TRANSACTIONS_SCHEMA,
    "Projects": PROJECTS_SCHEMA,
    "Users": USERS_SCHEMA,
}


class GoogleAdapterError(RuntimeError):
    """External response is unsafe, malformed, or unsuccessful."""


class IncompatibleSchemaError(GoogleAdapterError):
    """A production sheet does not exactly match its frozen schema."""


class IdempotencyCollisionError(GoogleAdapterError):
    """An idempotency identity belongs to different data."""


def _required_environment(environment, name):
    value = str(environment.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _json_response(response, context):
    try:
        value = response.json()
    except (TypeError, ValueError) as exc:
        raise GoogleAdapterError(f"Malformed {context} response") from exc
    if not isinstance(value, dict):
        raise GoogleAdapterError(f"Malformed {context} response")
    return value


class BearerTokenProvider:
    def __init__(self, token):
        self._token = str(token or "").strip()
        if not self._token:
            raise ValueError("Google access token is required")

    @classmethod
    def from_environment(cls, environ=None):
        environment = os.environ if environ is None else environ
        return cls(_required_environment(environment, ACCESS_TOKEN_ENV))

    def __call__(self):
        return self._token


class _GoogleRestAdapter:
    def __init__(self, token_provider, *, session=None, timeout=30):
        if not callable(token_provider):
            raise TypeError("token_provider must be callable")
        self._token_provider = token_provider
        if session is None and requests is None:
            raise RuntimeError("requests is required for production Google adapters")
        self._session = session or requests.Session()
        self._timeout = timeout

    def _headers(self):
        token = str(self._token_provider() or "").strip()
        if not token:
            raise GoogleAdapterError("Google access token is unavailable")
        return {"Authorization": f"Bearer {token}"}


class GoogleDriveAdapter(_GoogleRestAdapter):
    """Idempotent Drive upload using a durable, pre-generated file ID."""

    API = "https://www.googleapis.com/drive/v3"
    UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"

    def __init__(self, folder_id, token_provider, **kwargs):
        super().__init__(token_provider, **kwargs)
        self.folder_id = str(folder_id or "").strip()
        if not self.folder_id:
            raise ValueError(f"{SLIP_FOLDER_ENV} is required")

    @classmethod
    def from_environment(cls, *, environ=None, session=None, token_provider=None):
        environment = os.environ if environ is None else environ
        provider = token_provider or BearerTokenProvider.from_environment(environment)
        return cls(
            _required_environment(environment, SLIP_FOLDER_ENV),
            provider,
            session=session,
        )

    def reserve_file_id(self):
        response = self._session.get(
            f"{self.API}/files/generateIds",
            headers=self._headers(),
            params={"count": 1, "space": "drive", "type": "files"},
            timeout=self._timeout,
        )
        if response.status_code != 200:
            raise GoogleAdapterError("Drive file ID reservation failed")
        payload = _json_response(response, "Drive file ID reservation")
        ids = payload.get("ids")
        if not isinstance(ids, list) or len(ids) != 1 or not str(ids[0]).strip():
            raise GoogleAdapterError("Malformed Drive file ID reservation response")
        return str(ids[0]).strip()

    def upload(self, transaction_id, source_image_path, reserved_file_id):
        transaction_key = str(uuid.UUID(str(transaction_id)))
        file_id = str(reserved_file_id or "").strip()
        if not file_id:
            raise ValueError("reserved_file_id is required")
        existing = self._get_existing(file_id, transaction_key)
        if existing is not None:
            return existing

        source = Path(source_image_path)
        mime_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        metadata = {
            "id": file_id,
            "name": source.name,
            "parents": [self.folder_id],
            "appProperties": {"lekza_transaction_id": transaction_key},
        }
        boundary = f"lekza_{uuid.uuid4().hex}"
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata, separators=(',', ':'))}\r\n"
            f"--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8") + source.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        headers = self._headers()
        headers["Content-Type"] = f"multipart/related; boundary={boundary}"
        response = self._session.post(
            f"{self.UPLOAD_API}/files",
            headers=headers,
            params={"uploadType": "multipart", "fields": "id,webViewLink,appProperties,parents"},
            data=body,
            timeout=self._timeout,
        )
        if response.status_code in {200, 201}:
            return self._validate_file(_json_response(response, "Drive upload"), file_id, transaction_key)
        if response.status_code == 409:
            recovered = self._get_existing(file_id, transaction_key)
            if recovered is not None:
                return recovered
        raise GoogleAdapterError("Drive upload failed")

    def _get_existing(self, file_id, transaction_id):
        response = self._session.get(
            f"{self.API}/files/{urllib.parse.quote(file_id, safe='')}",
            headers=self._headers(),
            params={"fields": "id,webViewLink,appProperties,parents"},
            timeout=self._timeout,
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise GoogleAdapterError("Drive recovery lookup failed")
        return self._validate_file(_json_response(response, "Drive recovery"), file_id, transaction_id)

    def _validate_file(self, payload, file_id, transaction_id):
        if payload.get("id") != file_id:
            raise IdempotencyCollisionError("Drive file ID belongs to another file")
        properties = payload.get("appProperties")
        if not isinstance(properties, dict) or properties.get("lekza_transaction_id") != transaction_id:
            raise IdempotencyCollisionError("Drive file identity does not match transaction_id")
        if self.folder_id not in (payload.get("parents") or []):
            raise IdempotencyCollisionError("Drive file is outside the configured folder")
        link = str(payload.get("webViewLink") or "").strip()
        if not link:
            raise GoogleAdapterError("Malformed Drive file response")
        return {"file_id": file_id, "webViewLink": link}


class GoogleSheetsAdapter(_GoogleRestAdapter):
    """Schema-locked, exactly-once transaction writer for Google Sheets."""

    API = "https://sheets.googleapis.com/v4/spreadsheets"

    def __init__(self, spreadsheet_id, token_provider, **kwargs):
        super().__init__(token_provider, **kwargs)
        self.spreadsheet_id = str(spreadsheet_id or "").strip()
        if not self.spreadsheet_id:
            raise ValueError(f"{SPREADSHEET_ENV} is required")

    @classmethod
    def from_environment(cls, *, environ=None, session=None, token_provider=None):
        environment = os.environ if environ is None else environ
        provider = token_provider or BearerTokenProvider.from_environment(environment)
        return cls(
            _required_environment(environment, SPREADSHEET_ENV),
            provider,
            session=session,
        )

    def validate_schema(self):
        for title, expected in CANONICAL_SCHEMAS.items():
            response = self._session.get(
                f"{self.API}/{self.spreadsheet_id}/values/{urllib.parse.quote(title + '!1:1', safe='!')}",
                headers=self._headers(),
                timeout=self._timeout,
            )
            if response.status_code != 200:
                raise IncompatibleSchemaError(f"Cannot validate {title} schema")
            payload = _json_response(response, f"{title} schema")
            values = payload.get("values")
            actual = tuple(values[0]) if isinstance(values, list) and len(values) == 1 and isinstance(values[0], list) else ()
            if actual != expected:
                raise IncompatibleSchemaError(f"{title} schema is incompatible")
        return True

    def append_transaction(self, transaction, *, write_claim):
        row = self._transaction_row(transaction)
        transaction_id = row[0]
        if (
            write_claim is None
            or not getattr(write_claim, "active", False)
            or getattr(write_claim, "transaction_id", None) != transaction_id
        ):
            raise GoogleAdapterError("An active SQLite Sheets write claim is required")
        write_claim.assert_owner()
        self.validate_schema()
        recovered = self._find_row(transaction_id, required=False)
        if recovered is not None:
            return recovered

        sheet_id = self._transactions_sheet_id()
        requests_body = {
            "requests": [
                {"appendCells": {
                    "sheetId": sheet_id,
                    "rows": [{"values": [self._cell(value) for value in row]}],
                    "fields": "userEnteredValue",
                }},
            ]
        }
        write_claim.assert_owner(minimum_valid_seconds=self._timeout + 5)
        response = self._session.post(
            f"{self.API}/{self.spreadsheet_id}:batchUpdate",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=requests_body,
            timeout=self._timeout,
        )
        if response.status_code == 200:
            _json_response(response, "Sheets append")
            return self._find_row(transaction_id, required=True)

        # An ambiguous network result is recovered while this transaction's
        # durable lease still fences other workers from this write path.
        recovered = self._find_row(transaction_id, required=False)
        if recovered is not None:
            return recovered
        raise GoogleAdapterError("Sheets append failed")

    def _transactions_sheet_id(self):
        response = self._session.get(
            f"{self.API}/{self.spreadsheet_id}",
            headers=self._headers(),
            params={"fields": "sheets.properties(sheetId,title)"},
            timeout=self._timeout,
        )
        if response.status_code != 200:
            raise GoogleAdapterError("Sheets metadata lookup failed")
        payload = _json_response(response, "Sheets metadata lookup")
        matches = [
            sheet.get("properties", {}).get("sheetId")
            for sheet in payload.get("sheets", []) if isinstance(sheet, dict)
            if sheet.get("properties", {}).get("title") == "Transactions"
        ]
        if len(matches) != 1 or not isinstance(matches[0], int):
            raise IncompatibleSchemaError("Transactions sheet identity is incompatible")
        return matches[0]

    def _find_row(self, transaction_id, *, required):
        response = self._session.get(
            f"{self.API}/{self.spreadsheet_id}/values/{urllib.parse.quote('Transactions!A:A', safe='!')}",
            headers=self._headers(), timeout=self._timeout,
        )
        if response.status_code != 200:
            raise GoogleAdapterError("Sheets row recovery failed")
        payload = _json_response(response, "Sheets row recovery")
        values = payload.get("values")
        if not isinstance(values, list):
            raise GoogleAdapterError("Malformed Sheets row recovery response")
        rows = [index + 1 for index, value in enumerate(values) if isinstance(value, list) and value and value[0] == transaction_id]
        if len(rows) > 1:
            raise GoogleAdapterError("Sheets transaction row is duplicated")
        if not rows:
            if required:
                raise GoogleAdapterError("Sheets transaction row is missing")
            return None
        row_number = rows[0]
        return f"Transactions!A{row_number}:R{row_number}"

    @staticmethod
    def _cell(value):
        if isinstance(value, bool):
            return {"userEnteredValue": {"boolValue": value}}
        if isinstance(value, (int, float)):
            return {"userEnteredValue": {"numberValue": value}}
        return {"userEnteredValue": {"stringValue": "" if value is None else str(value)}}

    @staticmethod
    def _transaction_row(transaction):
        transaction_id = str(uuid.UUID(str(transaction.get("transaction_id"))))
        amount = transaction.get("ocr_fields", {}).get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise ValueError("Transaction amount must be numeric")
        confirmed_at = str(transaction.get("confirmed_at") or datetime.now(timezone.utc).isoformat())
        fields = transaction.get("ocr_fields") or {}
        values = {
            "transaction_id": transaction_id,
            "reference_no": transaction.get("reference_no", ""),
            "date": fields.get("date", ""), "payer": fields.get("payer", ""),
            "payee": fields.get("payee", ""), "project_id": transaction.get("project_id", ""),
            "project": transaction.get("project", ""), "type": transaction.get("transaction_type", ""),
            "category": transaction.get("category", ""), "amount": amount,
            "note": fields.get("note", ""), "confidence": transaction.get("confidence", ""),
            "submitted_by": transaction.get("selected_user_id") or transaction.get("telegram_user_id", ""),
            "drive_file_id": transaction.get("drive_file_id", ""), "slip_url": transaction.get("slip_url", ""),
            "status": "confirmed", "created_at": transaction.get("created_at", ""),
            "confirmed_at": confirmed_at,
        }
        return [values[column] for column in TRANSACTIONS_SCHEMA]


class ProductionSavePipeline:
    """Restart-safe coordinator over TransactionFlow's durable checkpoints."""

    def __init__(self, flow, drive, sheets, *, sheets_lease_seconds=120):
        self._flow = flow
        self._drive = drive
        self._sheets = sheets
        self._sheets_lease_seconds = float(sheets_lease_seconds)
        if self._sheets_lease_seconds <= 0:
            raise ValueError("Sheets lease duration must be positive")

    def save(self, transaction_id, *, platform, chat_id, telegram_user_id):
        actor = {"platform": platform, "chat_id": chat_id, "telegram_user_id": telegram_user_id}
        while True:
            record = self._flow.get_transaction(transaction_id, **actor)
            state = record["current_state"]
            if state == "confirmed":
                return record
            if state == "sheets_pending":
                try:
                    claim = self._flow.claim_sheets_write(
                        transaction_id,
                        lease_seconds=self._sheets_lease_seconds,
                        **actor,
                    )
                except Exception as exc:
                    if getattr(exc, "claim_busy", False):
                        time.sleep(min(max(exc.retry_after, 0.01), 0.1))
                        continue
                    latest = self._flow.get_transaction(transaction_id, **actor)
                    if latest["current_state"] == "confirmed":
                        return latest
                    raise
                try:
                    current = self._flow.get_transaction(transaction_id, **actor)
                    row_identity = self._sheets.append_transaction(
                        current, write_claim=claim
                    )
                    self._flow.complete_sheets_write(
                        claim, sheets_row_identity=row_identity, **actor
                    )
                except Exception:
                    self._flow.release_sheets_write(claim)
                    raise
                continue
            try:
                if state == "confirmed_intent":
                    self._flow.mark_drive_pending(transaction_id, expected_version=record["version"], **actor)
                elif state == "drive_pending":
                    if not record.get("drive_upload_id"):
                        reserved = self._drive.reserve_file_id()
                        self._flow.reserve_drive_upload(transaction_id, expected_version=record["version"], file_id=reserved, **actor)
                        continue
                    uploaded = self._drive.upload(transaction_id, record["source_image_path"], record["drive_upload_id"])
                    self._flow.mark_drive_uploaded(
                        transaction_id, expected_version=record["version"],
                        file_id=uploaded["file_id"], web_view_link=uploaded["webViewLink"], **actor,
                    )
                elif state == "drive_uploaded":
                    self._flow.mark_sheets_pending(transaction_id, expected_version=record["version"], **actor)
                else:
                    raise ValueError(f"Transaction cannot be saved from {state}")
            except Exception:
                # Another worker may have completed the same idempotent stage.
                # Converge only when durable state actually advanced; otherwise
                # preserve the original external/validation failure.
                latest = self._flow.get_transaction(transaction_id, **actor)
                if latest["version"] == record["version"]:
                    raise
