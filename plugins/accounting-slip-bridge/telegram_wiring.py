"""Durable Telegram button controller for Lekza transaction flow.

This module owns callback identity, action routing, and state-derived rendering.
It does not own OCR and stores no Telegram session state in memory.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import uuid


CALLBACK_PREFIX = "lk"
CALLBACK_LIMIT_BYTES = 64
_PAYLOAD_RE = re.compile(
    r"^lk:([0-9a-f]{32}):([0-9a-z]+):(p|np|mp|us|in|ex|ma|la|tr|co|ot|iw|ar|mc|bk|ca|cf|rt)(?::([0-9a-f]{12}))?$"
)

ACTION_CODES = {
    "new_project": "np",
    "manual_project": "mp",
    "use_sender": "us",
    "income": "in",
    "expense": "ex",
    "materials": "ma",
    "labor": "la",
    "transport": "tr",
    "contractor": "co",
    "other": "ot",
    "installment": "iw",
    "advance_refund": "ar",
    "manual_category": "mc",
    "back": "bk",
    "cancel": "ca",
    "confirm": "cf",
    "retry": "rt",
}
CODE_ACTIONS = {value: key for key, value in ACTION_CODES.items()}


class CallbackPayloadError(ValueError):
    """Callback data is malformed or exceeds Telegram's contract."""


def _base36(number):
    number = int(number)
    if number < 0:
        raise ValueError("version must not be negative")
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if number == 0:
        return "0"
    result = ""
    while number:
        number, remainder = divmod(number, 36)
        result = alphabet[remainder] + result
    return result


def _project_token(project):
    return hashlib.sha256(str(project).encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class CallbackIdentity:
    transaction_id: str
    expected_version: int
    action: str
    value_token: str | None = None


def encode_callback(transaction_id, expected_version, action, value_token=None):
    transaction_hex = uuid.UUID(str(transaction_id)).hex
    code = "p" if action == "select_project" else ACTION_CODES.get(action)
    if code is None:
        raise CallbackPayloadError("Unknown callback action")
    parts = [CALLBACK_PREFIX, transaction_hex, _base36(expected_version), code]
    if value_token is not None:
        token = str(value_token)
        if code != "p" or not re.fullmatch(r"[0-9a-f]{12}", token):
            raise CallbackPayloadError("Invalid callback value token")
        parts.append(token)
    elif code == "p":
        raise CallbackPayloadError("Project callback requires a value token")
    payload = ":".join(parts)
    if len(payload.encode("utf-8")) > CALLBACK_LIMIT_BYTES:
        raise CallbackPayloadError("Callback payload exceeds Telegram limit")
    return payload


def decode_callback(payload):
    raw = str(payload or "")
    if len(raw.encode("utf-8")) > CALLBACK_LIMIT_BYTES:
        raise CallbackPayloadError("Callback payload exceeds Telegram limit")
    match = _PAYLOAD_RE.fullmatch(raw)
    if match is None:
        raise CallbackPayloadError("Malformed callback payload")
    transaction_hex, version_text, code, token = match.groups()
    try:
        transaction_id = str(uuid.UUID(hex=transaction_hex))
        version = int(version_text, 36)
    except (ValueError, OverflowError) as exc:
        raise CallbackPayloadError("Malformed callback payload") from exc
    action = "select_project" if code == "p" else CODE_ACTIONS[code]
    if (action == "select_project") != (token is not None):
        raise CallbackPayloadError("Malformed callback payload")
    return CallbackIdentity(transaction_id, version, action, token)


class TelegramTransactionController:
    """Routes Telegram actions exclusively through durable flow methods."""

    def __init__(
        self, flow, save_pipeline, *, projects, prompt_lease_seconds=120
    ):
        self._flow = flow
        self._save_pipeline = save_pipeline
        self._prompt_lease_seconds = float(prompt_lease_seconds)
        if self._prompt_lease_seconds <= 0:
            raise ValueError("Prompt delivery lease duration must be positive")
        self._projects = tuple(str(project) for project in projects)
        self._project_tokens = {}
        for project in self._projects:
            token = _project_token(project)
            if token in self._project_tokens:
                raise ValueError("Project callback token collision")
            self._project_tokens[token] = project

    def begin_from_ocr(self, **handoff):
        handoff["platform"] = "telegram"
        return self._flow.begin_or_recover(**handoff)

    def acquire_initial_prompt_delivery(
        self, transaction_id, *, platform, chat_id, telegram_user_id
    ):
        return self._flow.acquire_initial_prompt_delivery(
            transaction_id,
            platform=platform,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            lease_seconds=self._prompt_lease_seconds,
        )

    def release_initial_prompt_delivery(self, claim):
        self._flow.release_initial_prompt_delivery(claim)

    def complete_initial_prompt(
        self,
        claim,
        *,
        platform,
        chat_id,
        telegram_user_id,
        message_id,
    ):
        return self._flow.complete_initial_prompt(
            claim,
            platform=platform,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            message_id=message_id,
        )

    def handle_callback(
        self, payload, *, platform, chat_id, telegram_user_id
    ):
        try:
            identity = decode_callback(payload)
        except CallbackPayloadError:
            return self._error("malformed_callback")
        actor = self._actor(platform, chat_id, telegram_user_id)
        try:
            record = self._flow.get_transaction(identity.transaction_id, **actor)
            if identity.action == "confirm":
                return self._handle_confirm(record, identity, actor)
            if identity.action == "retry":
                updated = self._flow.retry(
                    identity.transaction_id,
                    expected_version=identity.expected_version,
                    **actor,
                )
                return self._run_save(updated["transaction_id"], actor)
            if identity.action == "back":
                self._flow.back(
                    identity.transaction_id,
                    expected_version=identity.expected_version,
                    **actor,
                )
            elif identity.action == "cancel":
                self._flow.cancel(
                    identity.transaction_id,
                    expected_version=identity.expected_version,
                    **actor,
                )
            else:
                action, value = self._choice(identity)
                self._flow.choose(
                    identity.transaction_id,
                    expected_version=identity.expected_version,
                    action=action,
                    value=value,
                    **actor,
                )
            return self._success(identity.transaction_id, actor)
        except Exception as exc:
            return self._flow_error(exc)

    def handle_manual_input(
        self,
        transaction_id,
        expected_version,
        text,
        *,
        platform,
        chat_id,
        telegram_user_id,
    ):
        actor = self._actor(platform, chat_id, telegram_user_id)
        try:
            self._flow.submit_manual(
                transaction_id,
                expected_version=expected_version,
                value=text,
                **actor,
            )
            return self._success(transaction_id, actor)
        except Exception as exc:
            return self._flow_error(exc)

    def handle_manual_message(
        self, text, *, platform, chat_id, telegram_user_id
    ):
        actor = self._actor(platform, chat_id, telegram_user_id)
        try:
            record = self._flow.get_manual_pending(**actor)
        except Exception as exc:
            return self._flow_error(exc)
        if record is None:
            return None
        return self.handle_manual_input(
            record["transaction_id"],
            record["version"],
            text,
            **actor,
        )

    def render(self, transaction_id, *, platform, chat_id, telegram_user_id):
        actor = self._actor(platform, chat_id, telegram_user_id)
        record = self._flow.get_transaction(transaction_id, **actor)
        state = record["current_state"]
        version = record["version"]
        buttons = []

        def button(label, action, value_token=None):
            buttons.append({
                "label": label,
                "callback_data": encode_callback(
                    transaction_id, version, action, value_token
                ),
            })

        if record.get("needs_reference") or record.get("needs_amount"):
            button("❌ ยกเลิก", "cancel")
        elif state == "waiting_project":
            if record.get("entry_mode") in {"new_project", "manual_entry"}:
                button("⬅️ กลับ", "back")
                button("❌ ยกเลิก", "cancel")
            else:
                for project in self._projects:
                    button(project, "select_project", _project_token(project))
                button("➕ สร้างโครงการใหม่", "new_project")
                button("✏️ บันทึกเอง", "manual_project")
                button("❌ ยกเลิก", "cancel")
        elif state == "waiting_user":
            button("✅ ใช้ผู้ส่งรายการ", "use_sender")
            button("⬅️ กลับ", "back")
            button("❌ ยกเลิก", "cancel")
        elif state == "waiting_type":
            button("🟢 รายรับ", "income")
            button("🔴 รายจ่าย", "expense")
            button("⬅️ กลับ", "back")
            button("❌ ยกเลิก", "cancel")
        elif state == "waiting_category":
            if record.get("entry_mode") == "category":
                button("⬅️ กลับ", "back")
                button("❌ ยกเลิก", "cancel")
            else:
                categories = (
                    (("🧱 ค่าวัสดุ", "materials"), ("👷 ค่าแรง", "labor"),
                     ("🚚 ค่าขนส่ง", "transport"), ("🧾 ผู้รับเหมา", "contractor"),
                     ("📦 อื่นๆ", "other"))
                    if record.get("transaction_type") == "expense"
                    else (("💰 รับงวดงาน", "installment"),
                          ("💵 เงินทดรอง/คืนเงิน", "advance_refund"),
                          ("📦 อื่นๆ", "other"))
                )
                for label, action in categories:
                    button(label, action)
                button("✏️ บันทึกเอง", "manual_category")
                button("⬅️ กลับ", "back")
                button("❌ ยกเลิก", "cancel")
        elif state == "waiting_review":
            button("✅ คอนเฟิร์มบันทึก", "confirm")
            button("⬅️ กลับ", "back")
            button("❌ ยกเลิก", "cancel")
        elif state == "failed":
            button("🔄 ลองใหม่", "retry")

        return {
            "transaction_id": transaction_id,
            "current_state": state,
            "version": version,
            "text": self._prompt_text(record),
            "buttons": buttons,
            "manual_input_required": record.get("entry_mode") is not None,
        }

    def _choice(self, identity):
        if identity.action == "select_project":
            project = self._project_tokens.get(identity.value_token)
            if project is None:
                raise ValueError("Project callback is no longer available")
            return "select_project", project
        mapping = {
            "new_project": "new_project",
            "manual_project": "manual_entry",
            "use_sender": "use_sender",
            "income": "income",
            "expense": "expense",
            "materials": "materials",
            "labor": "labor",
            "transport": "transport",
            "contractor": "contractor",
            "other": "other",
            "installment": "installment",
            "advance_refund": "advance_refund",
            "manual_category": "manual_entry",
        }
        if identity.action not in mapping:
            raise ValueError("Callback action is not a durable transition")
        return mapping[identity.action], None

    def _handle_confirm(self, record, identity, actor):
        if record["current_state"] == "confirmed":
            return self._success(identity.transaction_id, actor)
        self._flow.confirm(
            identity.transaction_id,
            expected_version=identity.expected_version,
            **actor,
        )
        return self._run_save(identity.transaction_id, actor)

    def _run_save(self, transaction_id, actor):
        try:
            self._save_pipeline.save(transaction_id, **actor)
            return self._success(transaction_id, actor)
        except (ValueError,) as exc:
            result = self._flow_error(exc)
            result["error_code"] = "validation_error"
            result["message"] = str(exc)
            result["prompt"] = self.render(transaction_id, **actor)
            return result
        except Exception:
            record = self._flow.get_transaction(transaction_id, **actor)
            if record["current_state"] in {
                "confirmed_intent", "drive_pending", "drive_uploaded", "sheets_pending"
            }:
                error_code = (
                    "SHEETS_TRANSIENT"
                    if record["current_state"] in {"drive_uploaded", "sheets_pending"}
                    else "DRIVE_TRANSIENT"
                )
                try:
                    self._flow.mark_failed(
                        transaction_id,
                        expected_version=record["version"],
                        error_code=error_code,
                        **actor,
                    )
                except Exception:
                    pass
            result = self._error("external_save_failed")
            try:
                result["prompt"] = self.render(transaction_id, **actor)
            except Exception:
                pass
            return result

    def _success(self, transaction_id, actor):
        return {"ok": True, "prompt": self.render(transaction_id, **actor)}

    @staticmethod
    def _error(code):
        return {"ok": False, "error_code": code}

    def _flow_error(self, exc):
        name = exc.__class__.__name__
        if name == "ValueError":
            return {
                "ok": False,
                "error_code": "validation_error",
                "message": str(exc),
            }
        codes = {
            "AuthorizationError": "unauthorized",
            "StaleStateError": "stale_callback",
            "InvalidTransitionError": "invalid_transition",
            "KeyError": "not_found",
        }
        return self._error(codes.get(name, "invalid_callback"))

    @staticmethod
    def _actor(platform, chat_id, telegram_user_id):
        return {
            "platform": str(platform),
            "chat_id": str(chat_id),
            "telegram_user_id": str(telegram_user_id),
        }

    @staticmethod
    def _prompt_text(record):
        state = record["current_state"]
        if record.get("needs_reference"):
            return "พิมพ์หมายเลขอ้างอิงจากสลิปก่อนดำเนินการต่อ"
        if record.get("needs_amount"):
            return "กรุณาพิมพ์ยอดเงินที่มากกว่า 0 (รองรับทศนิยม)"
        if state == "waiting_project" and record.get("entry_mode") == "new_project":
            return "พิมพ์ชื่อโครงการใหม่"
        if state == "waiting_project" and record.get("entry_mode") == "manual_entry":
            return "พิมพ์ชื่อโครงการสำหรับรายการนี้"
        if state == "waiting_category" and record.get("entry_mode") == "category":
            return "พิมพ์หมวดรายการ"
        labels = {
            "waiting_project": "เลือกโครงการสำหรับรายการนี้",
            "waiting_user": "เลือกผู้ส่งรายการ",
            "waiting_type": "เลือกประเภทรายการ",
            "waiting_category": "เลือกหมวดรายการ",
            "waiting_review": "ตรวจสอบข้อมูลและยืนยันการบันทึก",
            "confirmed_intent": "กำลังเตรียมบันทึก",
            "drive_pending": "กำลังอัปโหลดสลิป",
            "drive_uploaded": "อัปโหลดสลิปแล้ว",
            "sheets_pending": "กำลังบันทึกรายการ",
            "confirmed": "บันทึกรายการสำเร็จ",
            "cancelled": "ยกเลิกรายการแล้ว",
            "failed": "บันทึกไม่สำเร็จ สามารถลองใหม่ได้",
        }
        return labels.get(state, state)
