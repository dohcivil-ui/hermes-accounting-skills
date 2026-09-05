"""Durable pre-OCR duplicate protection for Lekza slip ingress.

The ledger owns image identity, claims, and sanitized OCR-result recovery.  It
does not know how to call AksonOCR; the bridge supplies that operation through
``ocr_reader`` so OCR ownership remains at the Telegram ingress seam.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
import unicodedata
import uuid

from PIL import Image, ImageOps


OCR_FIELDS = ("reference_no", "amount", "date", "payer", "payee", "note")


class OcrIngressOutcome:
    def __init__(
        self, *, status, ingress_id, tenant_id, message_identity,
        content_sha256, perceptual_hash, owner_id=None, ocr_result=None,
        transaction_id=None,
    ):
        self.status = status
        self.ingress_id = ingress_id
        self.tenant_id = tenant_id
        self.message_identity = message_identity
        self.content_sha256 = content_sha256
        self.perceptual_hash = perceptual_hash
        self.owner_id = owner_id
        self.ocr_result = ocr_result
        self.transaction_id = transaction_id


def _utc_now():
    return datetime.now(timezone.utc)


def _normalize_reference(value):
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(normalized.split()).upper()


def _normalize_text(value):
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.casefold().split())


def _normalize_amount(value):
    try:
        return Decimal(str(value).replace(",", "").strip()).normalize()
    except (InvalidOperation, ValueError):
        return None


def _edit_distance_at_most_one(left, right):
    if left == right or abs(len(left) - len(right)) > 1:
        return left == right
    if len(left) > len(right):
        left, right = right, left
    first = second = differences = 0
    while first < len(left) and second < len(right):
        if left[first] == right[second]:
            first += 1
            second += 1
            continue
        differences += 1
        if differences > 1:
            return False
        if len(left) == len(right):
            first += 1
        second += 1
    return differences + (len(right) - second) <= 1


def _hamming_distance(left, right):
    if not left or not right or len(left) != len(right):
        return None
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError:
        return None


def _image_identities(source_image_path):
    path = Path(source_image_path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    perceptual = None
    try:
        with Image.open(path) as image:
            normalized = ImageOps.exif_transpose(image).convert("L").resize(
                (16, 16), Image.Resampling.LANCZOS
            )
            pixels = list(normalized.get_flattened_data())
            average = sum(pixels) / len(pixels)
            bits = "".join("1" if value >= average else "0" for value in pixels)
            perceptual = f"{int(bits, 2):064x}"
    except (OSError, ValueError):
        # Exact-byte protection remains available for a decoder-specific image.
        pass
    return digest.hexdigest(), perceptual


def _sanitized_ocr_result(result, content_sha256, perceptual_hash):
    result = result if isinstance(result, dict) else {}
    parsed = result.get("parsed") if isinstance(result.get("parsed"), dict) else {}
    return {
        "akson_called": bool(result.get("akson_called", True)),
        "confidence": result.get("confidence"),
        "parsed": {
            field: parsed[field]
            for field in OCR_FIELDS
            if field in parsed and parsed[field] is not None
        },
        "source_image_sha256": content_sha256,
        "source_perceptual_hash": perceptual_hash,
    }


class OcrIngressLedger:
    """Deep module providing durable, tenant-scoped, exactly-once OCR claims."""

    def __init__(self, db_path, *, lease_seconds=120):
        path = Path(db_path).expanduser()
        if not path.is_absolute():
            raise ValueError("OCR ingress DB path must be absolute")
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = float(lease_seconds)
        if self.lease_seconds <= 0:
            raise ValueError("OCR ingress lease duration must be positive")
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            self._create_schema(connection)
        finally:
            connection.close()

    @classmethod
    def from_environment(cls, environment):
        db_path = str(environment.get("LEKZA_TRANSACTION_STATE_DB") or "").strip()
        if not db_path:
            raise ValueError("LEKZA_TRANSACTION_STATE_DB is required")
        return cls(
            db_path,
            lease_seconds=float(environment.get("LEKZA_OCR_LEASE_SECONDS", "120")),
        )

    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _create_schema(self, connection):
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS ocr_ingress (
                ingress_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                perceptual_hash TEXT,
                state TEXT NOT NULL CHECK (
                    state IN ('claimed', 'ready', 'completed', 'failed')
                ),
                claim_owner TEXT,
                claim_expires_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                ocr_result_json TEXT,
                transaction_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (tenant_id, content_sha256)
            );
            CREATE TABLE IF NOT EXISTS ocr_ingress_message (
                tenant_id TEXT NOT NULL,
                message_identity TEXT NOT NULL,
                ingress_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, message_identity),
                FOREIGN KEY (ingress_id) REFERENCES ocr_ingress(ingress_id)
            );
            CREATE INDEX IF NOT EXISTS idx_ocr_ingress_perceptual
            ON ocr_ingress(tenant_id, perceptual_hash);
            """
        )
        connection.commit()

    def lookup_message(self, *, tenant_id, message_identity):
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT ingress.* FROM ocr_ingress AS ingress
                JOIN ocr_ingress_message AS message
                  ON message.ingress_id = ingress.ingress_id
                WHERE message.tenant_id = ? AND message.message_identity = ?
                """,
                (str(tenant_id), str(message_identity)),
            ).fetchone()
        finally:
            connection.close()
        return self._existing_outcome(row, str(message_identity)) if row else None

    def obtain(
        self, *, tenant_id, message_identity, source_image_path, ocr_reader
    ):
        tenant_id = str(tenant_id)
        message_identity = str(message_identity)
        if not tenant_id or not message_identity:
            raise ValueError("Tenant and Telegram message identity are required")
        content_sha256, perceptual_hash = _image_identities(source_image_path)
        owner_id = uuid.uuid4().hex
        now = _utc_now()
        expires_at = (now + timedelta(seconds=self.lease_seconds)).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT ingress.* FROM ocr_ingress AS ingress
                JOIN ocr_ingress_message AS message
                  ON message.ingress_id = ingress.ingress_id
                WHERE message.tenant_id = ? AND message.message_identity = ?
                """,
                (tenant_id, message_identity),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    SELECT * FROM ocr_ingress
                    WHERE tenant_id = ? AND content_sha256 = ?
                    """,
                    (tenant_id, content_sha256),
                ).fetchone()
            if row is not None:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO ocr_ingress_message
                    (tenant_id, message_identity, ingress_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (tenant_id, message_identity, row["ingress_id"], now.isoformat()),
                )
                outcome = self._existing_outcome(row, message_identity)
                if outcome.status == "resume":
                    connection.execute(
                        """
                        UPDATE ocr_ingress
                        SET claim_owner = ?, claim_expires_at = ?, updated_at = ?
                        WHERE ingress_id = ? AND state = 'ready'
                        """,
                        (owner_id, expires_at, now.isoformat(), row["ingress_id"]),
                    )
                    outcome.owner_id = owner_id
                    outcome.status = "ready"
                connection.commit()
                return outcome

            ingress_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO ocr_ingress (
                    ingress_id, tenant_id, content_sha256, perceptual_hash,
                    state, claim_owner, claim_expires_at, attempt_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'claimed', ?, ?, 1, ?, ?)
                """,
                (
                    ingress_id, tenant_id, content_sha256, perceptual_hash,
                    owner_id, expires_at, now.isoformat(), now.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO ocr_ingress_message
                (tenant_id, message_identity, ingress_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (tenant_id, message_identity, ingress_id, now.isoformat()),
            )
            connection.commit()
        finally:
            connection.close()

        try:
            raw_result = ocr_reader()
            if not isinstance(raw_result, dict) or raw_result.get("error"):
                self._mark_failed(ingress_id, owner_id)
                return OcrIngressOutcome(
                    status="failed", ingress_id=ingress_id, tenant_id=tenant_id,
                    message_identity=message_identity,
                    content_sha256=content_sha256,
                    perceptual_hash=perceptual_hash, owner_id=owner_id,
                )
            result = _sanitized_ocr_result(
                raw_result, content_sha256, perceptual_hash
            )
            connection = self._connect()
            try:
                with connection:
                    cursor = connection.execute(
                        """
                        UPDATE ocr_ingress
                        SET state = 'ready', ocr_result_json = ?, updated_at = ?
                        WHERE ingress_id = ? AND state = 'claimed'
                          AND claim_owner = ?
                        """,
                        (
                            json.dumps(
                                result, ensure_ascii=False, separators=(",", ":")
                            ),
                            _utc_now().isoformat(), ingress_id, owner_id,
                        ),
                    )
            finally:
                connection.close()
            if cursor.rowcount != 1:
                raise RuntimeError("OCR ingress claim is no longer owned")
            return OcrIngressOutcome(
                status="ready", ingress_id=ingress_id, tenant_id=tenant_id,
                message_identity=message_identity, content_sha256=content_sha256,
                perceptual_hash=perceptual_hash, owner_id=owner_id,
                ocr_result=result,
            )
        except Exception:
            self._mark_failed(ingress_id, owner_id)
            raise

    def complete(self, outcome, *, transaction_id):
        if outcome.status != "ready" or not outcome.owner_id:
            raise ValueError("A ready OCR ingress outcome is required")
        transaction_id = str(transaction_id)
        connection = self._connect()
        try:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE ocr_ingress
                    SET state = 'completed', transaction_id = ?,
                        claim_owner = NULL, claim_expires_at = NULL,
                        updated_at = ?
                    WHERE ingress_id = ? AND state = 'ready'
                      AND claim_owner = ?
                    """,
                    (
                        transaction_id, _utc_now().isoformat(),
                        outcome.ingress_id, outcome.owner_id,
                    ),
                )
        finally:
            connection.close()
        if cursor.rowcount != 1:
            raise RuntimeError("OCR ingress completion is no longer owned")
        outcome.status = "completed"
        outcome.transaction_id = transaction_id
        outcome.owner_id = None
        return outcome

    def persist_result(self, outcome):
        if outcome.status != "ready" or not outcome.owner_id:
            raise ValueError("A ready OCR ingress outcome is required")
        result = _sanitized_ocr_result(
            outcome.ocr_result,
            outcome.content_sha256,
            outcome.perceptual_hash,
        )
        connection = self._connect()
        try:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE ocr_ingress
                    SET ocr_result_json = ?, updated_at = ?
                    WHERE ingress_id = ? AND state = 'ready'
                      AND claim_owner = ?
                    """,
                    (
                        json.dumps(
                            result, ensure_ascii=False, separators=(",", ":")
                        ),
                        _utc_now().isoformat(), outcome.ingress_id,
                        outcome.owner_id,
                    ),
                )
        finally:
            connection.close()
        if cursor.rowcount != 1:
            raise RuntimeError("OCR ingress result persistence is no longer owned")
        outcome.ocr_result = result
        return outcome

    def find_candidates(self, outcome):
        if not outcome.ocr_result:
            return []
        current = outcome.ocr_result.get("parsed") or {}
        current_reference = _normalize_reference(current.get("reference_no"))
        connection = self._connect()
        try:
            ingress_rows = connection.execute(
                """
                SELECT transaction_id, perceptual_hash, ocr_result_json
                FROM ocr_ingress
                WHERE tenant_id = ? AND ingress_id <> ?
                  AND transaction_id IS NOT NULL AND ocr_result_json IS NOT NULL
                ORDER BY created_at
                """,
                (outcome.tenant_id, outcome.ingress_id),
            ).fetchall()
            transaction_columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(transaction_state)"
                ).fetchall()
            }
            transaction_rows = []
            required = {
                "transaction_id", "tenant_id", "current_state",
                "ocr_fields_json", "created_at",
            }
            if required.issubset(transaction_columns):
                fingerprint_column = (
                    "source_perceptual_hash"
                    if "source_perceptual_hash" in transaction_columns else "NULL"
                )
                transaction_rows = connection.execute(
                    f"""
                    SELECT transaction_id,
                           {fingerprint_column} AS perceptual_hash,
                           ocr_fields_json AS ocr_result_json
                    FROM transaction_state
                    WHERE tenant_id = ? AND current_state <> 'cancelled'
                    ORDER BY created_at
                    """,
                    (outcome.tenant_id,),
                ).fetchall()
        finally:
            connection.close()
        rows_by_transaction = {
            row["transaction_id"]: row for row in ingress_rows
        }
        rows_by_transaction.update({
            row["transaction_id"]: row for row in transaction_rows
        })
        candidates = []
        for row in rows_by_transaction.values():
            stored = json.loads(row["ocr_result_json"])
            previous = stored.get("parsed", stored) or {}
            previous_reference = _normalize_reference(previous.get("reference_no"))
            reasons = []
            if (
                current_reference and previous_reference
                and current_reference == previous_reference
            ):
                reasons.append("exact_reference")
            if (
                current_reference and previous_reference
                and current_reference != previous_reference
                and _edit_distance_at_most_one(current_reference, previous_reference)
            ):
                reasons.append("near_reference")

            distance = _hamming_distance(
                outcome.perceptual_hash, row["perceptual_hash"]
            )
            business_matches = []
            if current_reference and current_reference == previous_reference:
                business_matches.append("reference")
            current_amount = _normalize_amount(current.get("amount"))
            previous_amount = _normalize_amount(previous.get("amount"))
            if current_amount is not None and current_amount == previous_amount:
                business_matches.append("amount")
            if current.get("date") == previous.get("date") and current.get("date"):
                business_matches.append("date")
            current_people = {
                _normalize_text(current.get("payer")),
                _normalize_text(current.get("payee")),
            } - {""}
            previous_people = {
                _normalize_text(previous.get("payer")),
                _normalize_text(previous.get("payee")),
            } - {""}
            if current_people & previous_people:
                business_matches.append("counterparty")
            if distance is not None and distance <= 64 and len(business_matches) >= 2:
                reasons.extend(["perceptual_image", *business_matches])
            if reasons:
                candidates.append({
                    "transaction_id": row["transaction_id"],
                    "reasons": tuple(dict.fromkeys(reasons)),
                })
        return candidates

    def _existing_outcome(self, row, message_identity):
        state = row["state"]
        status = {
            "completed": "duplicate",
            "failed": "failed",
            "ready": "processing",
            "claimed": "processing",
        }[state]
        owner_id = None
        ocr_result = None
        if state == "ready":
            ocr_result = json.loads(row["ocr_result_json"])
            try:
                expired = datetime.fromisoformat(row["claim_expires_at"]) <= _utc_now()
            except (TypeError, ValueError):
                expired = True
            if expired:
                status = "resume"
        elif state == "claimed":
            try:
                expired = datetime.fromisoformat(row["claim_expires_at"]) <= _utc_now()
            except (TypeError, ValueError):
                expired = True
            if expired:
                status = "ambiguous"
        return OcrIngressOutcome(
            status=status, ingress_id=row["ingress_id"],
            tenant_id=row["tenant_id"], message_identity=message_identity,
            content_sha256=row["content_sha256"],
            perceptual_hash=row["perceptual_hash"], owner_id=owner_id,
            ocr_result=ocr_result, transaction_id=row["transaction_id"],
        )

    def _mark_failed(self, ingress_id, owner_id):
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    UPDATE ocr_ingress
                    SET state = 'failed', claim_owner = NULL,
                        claim_expires_at = NULL, updated_at = ?
                    WHERE ingress_id = ? AND state = 'claimed'
                      AND claim_owner = ?
                    """,
                    (_utc_now().isoformat(), ingress_id, owner_id),
                )
        finally:
            connection.close()

    def close(self):
        return None
