"""Patch Hermes Telegram callbacks into the durable Lekza controller."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import threading


_PATCH_ATTR = "_lekza_transaction_buttons_v1"
_CONTROLLER = None
_CONTROLLER_LOCK = threading.Lock()
_MODULE_CACHE = {}
_LOG = logging.getLogger("lekza.accounting_transaction_buttons")


class AdapterCompatibilityError(RuntimeError):
    """The loaded Hermes Telegram adapter cannot be patched safely."""


def _load_runtime_module(name, path):
    cached = _MODULE_CACHE.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    _MODULE_CACHE[name] = module
    return module


def _controller_from_environment():
    global _CONTROLLER
    if _CONTROLLER is not None:
        return _CONTROLLER
    with _CONTROLLER_LOCK:
        if _CONTROLLER is not None:
            return _CONTROLLER
        bridge = Path(__file__).resolve().parents[1] / "accounting-slip-bridge"
        wiring = _load_runtime_module(
            "lekza_telegram_wiring", bridge / "telegram_wiring.py"
        )
        flow_module = _load_runtime_module(
            "lekza_transaction_flow", bridge / "transaction_flow.py"
        )
        google = _load_runtime_module(
            "lekza_google_adapters", bridge / "google_adapters.py"
        )
        guard = _load_runtime_module(
            "lekza_staging_guard", bridge / "staging_guard.py"
        )
        runtime_mode = guard.validate_runtime_environment()
        if runtime_mode == "staging":
            guard.validate_staging_environment()
        try:
            projects = json.loads(os.environ.get("LEKZA_ACTIVE_PROJECTS_JSON", "[]"))
        except json.JSONDecodeError as exc:
            raise ValueError("LEKZA_ACTIVE_PROJECTS_JSON must be JSON") from exc
        if not isinstance(projects, list) or any(
            not isinstance(item, str) for item in projects
        ):
            raise ValueError("LEKZA_ACTIVE_PROJECTS_JSON must be a JSON string list")
        store = flow_module.SQLiteStateStore.from_environment()
        flow = flow_module.TransactionFlow.from_environment(store, projects=projects)
        pipeline = google.ProductionSavePipeline(
            flow,
            google.GoogleDriveAdapter.from_environment(),
            google.GoogleSheetsAdapter.from_environment(),
        )
        _CONTROLLER = wiring.TelegramTransactionController(
            flow,
            pipeline,
            projects=projects,
            prompt_lease_seconds=float(
                os.environ.get("LEKZA_PROMPT_DELIVERY_LEASE_SECONDS", "120")
            ),
        )
    return _CONTROLLER


def _set_controller_for_tests(controller):
    global _CONTROLLER
    _CONTROLLER = controller


def _validate_staging_actor(chat_id, telegram_user_id):
    bridge = Path(__file__).resolve().parents[1] / "accounting-slip-bridge"
    guard = _load_runtime_module(
        "lekza_staging_guard", bridge / "staging_guard.py"
    )
    runtime_mode = guard.validate_runtime_environment()
    if runtime_mode == "staging":
        guard.validate_staging_actor(str(chat_id), str(telegram_user_id))


def _validate_adapter_class(adapter_cls):
    missing = [
        name
        for name in ("_handle_callback_query", "_handle_text_message")
        if not callable(getattr(adapter_cls, name, None))
    ]
    if missing:
        raise AdapterCompatibilityError(
            "Incompatible Hermes Telegram adapter; missing callable handler(s): "
            + ", ".join(missing)
        )


def _rows(mod, prompt):
    Button = getattr(mod, "InlineKeyboardButton")
    Markup = getattr(mod, "InlineKeyboardMarkup")
    buttons = [
        Button(item["label"], callback_data=item["callback_data"])
        for item in prompt.get("buttons", [])
    ]
    return Markup([[button] for button in buttons])


async def _show_prompt(mod, query, prompt):
    text = str(prompt.get("text") or "Lekza")
    markup = _rows(mod, prompt)
    edit = getattr(query, "edit_message_text", None)
    if edit is not None:
        await edit(text=text, reply_markup=markup)


def _patch_module(mod_name, *, strict=False):
    mod = sys.modules.get(mod_name)
    if mod is None:
        if strict:
            raise AdapterCompatibilityError(
                f"Hermes Telegram adapter module is not loaded: {mod_name}"
            )
        return False
    adapter_cls = getattr(mod, "TelegramAdapter", None)
    if adapter_cls is None:
        if strict:
            raise AdapterCompatibilityError(
                f"Hermes Telegram adapter module has no TelegramAdapter: {mod_name}"
            )
        return False
    _validate_adapter_class(adapter_cls)
    if getattr(adapter_cls, _PATCH_ATTR, False):
        return True
    original_callback = getattr(adapter_cls, "_handle_callback_query", None)
    original_text = getattr(adapter_cls, "_handle_text_message", None)

    async def handle_callback(self, update, context, _original=original_callback):
        query = getattr(update, "callback_query", None)
        data = str(getattr(query, "data", "") or "")
        if not data.startswith("lk:"):
            return await _original(self, update, context)
        answer = getattr(query, "answer", None)
        long_running = data.rsplit(":", 1)[-1] in {"cf", "rt"}
        answered = False
        try:
            if long_running and answer is not None:
                await answer(text="กำลังดำเนินการ")
                answered = True
            message = getattr(query, "message", None)
            chat_id = str(getattr(message, "chat_id", ""))
            user_id = str(getattr(getattr(query, "from_user", None), "id", ""))
            _validate_staging_actor(chat_id, user_id)
            result = await asyncio.to_thread(
                _controller_from_environment().handle_callback,
                data,
                platform="telegram",
                chat_id=chat_id,
                telegram_user_id=user_id,
            )
            if answer is not None and not answered:
                await answer(
                    text="สำเร็จ" if result.get("ok") else "ไม่สามารถดำเนินการได้"
                )
            if result.get("prompt"):
                await _show_prompt(mod, query, result["prompt"])
        except Exception:
            _LOG.exception("Lekza callback failed transaction_id=%s", data.split(":")[1] if ":" in data else "unknown")
            if answer is not None and not answered:
                await answer(text="ไม่สามารถดำเนินการได้")

    async def handle_text(self, update, context, _original=original_text):
        message = getattr(update, "message", None)
        text = getattr(message, "text", None)
        if text:
            try:
                chat_id = str(getattr(message, "chat_id", ""))
                user_id = str(getattr(getattr(message, "from_user", None), "id", ""))
                _validate_staging_actor(chat_id, user_id)
                result = await asyncio.to_thread(
                    _controller_from_environment().handle_manual_message,
                    text,
                    platform="telegram",
                    chat_id=chat_id,
                    telegram_user_id=user_id,
                )
                if result is not None:
                    if result.get("prompt"):
                        reply = getattr(message, "reply_text", None)
                        if reply is not None:
                            await reply(
                                result["prompt"]["text"],
                                reply_markup=_rows(mod, result["prompt"]),
                            )
                    return
            except Exception:
                pass
        return await _original(self, update, context)

    async def send_lekza_transaction_prompt(
        self, chat_id, transaction_id, telegram_user_id, thread_id=None
    ):
        prompt = await asyncio.to_thread(
            _controller_from_environment().render,
            transaction_id,
            platform="telegram",
            chat_id=str(chat_id),
            telegram_user_id=str(telegram_user_id),
        )
        thread_kwargs = (
            {"message_thread_id": int(thread_id)} if thread_id is not None else {}
        )
        return await self._bot.send_message(
            chat_id=int(chat_id),
            text=prompt["text"],
            reply_markup=_rows(mod, prompt),
            **thread_kwargs,
        )

    setattr(adapter_cls, "_handle_callback_query", handle_callback)
    setattr(adapter_cls, "_handle_text_message", handle_text)
    setattr(adapter_cls, "send_lekza_transaction_prompt", send_lekza_transaction_prompt)
    setattr(adapter_cls, _PATCH_ATTR, True)
    return True


def _patch_loaded_adapter():
    patched = False
    for name in (
        "hermes_plugins.telegram_platform.adapter",
        "plugins.platforms.telegram.adapter",
    ):
        try:
            patched = _patch_module(name) or patched
        except AdapterCompatibilityError as exc:
            _LOG.error("Lekza Telegram callback patch disabled: %s", exc)
    return patched


def _telegram_adapter(gateway):
    adapters = getattr(gateway, "adapters", {}) or {}
    for key, adapter in adapters.items():
        platform = getattr(key, "value", key)
        if str(platform).lower() == "telegram":
            module_name = adapter.__class__.__module__
            module = sys.modules.get(module_name)
            if module is None or getattr(module, "TelegramAdapter", None) is not adapter.__class__:
                raise AdapterCompatibilityError(
                    "Loaded Telegram adapter class cannot be resolved to its module"
                )
            _patch_module(module_name, strict=True)
            return adapter
    raise AdapterCompatibilityError("Gateway has no loaded Telegram adapter")


def _session_id(event, session_store):
    source = getattr(event, "source", None)
    if session_store is not None:
        get_or_create = getattr(session_store, "get_or_create_session", None)
        if callable(get_or_create):
            entry = get_or_create(source)
            value = getattr(entry, "session_id", None)
            if value:
                return str(value)
    message_id = str(getattr(event, "message_id", "") or "")
    if not message_id:
        raise ValueError("Telegram OCR handoff requires message_id or session store")
    return "telegram-message:" + message_id


async def _deliver_initial_prompt(controller, adapter, record, actor, claim):
    try:
        message = await adapter.send_lekza_transaction_prompt(
            actor["chat_id"],
            record["transaction_id"],
            actor["telegram_user_id"],
            record.get("thread_id"),
        )
        message_id = getattr(message, "message_id", None)
        controller.complete_initial_prompt(claim, message_id=message_id, **actor)
    finally:
        if claim.active:
            controller.release_initial_prompt_delivery(claim)


def _log_delivery_result(task):
    try:
        task.result()
    except Exception as exc:
        _LOG.error("Lekza initial Telegram prompt delivery failed: %s", exc)


def handoff_ocr_result(
    event, gateway, session_store, *, source_image_path, ocr_result
):
    """Durably create/recover an OCR transaction and claim its first prompt."""
    adapter = _telegram_adapter(gateway)
    source = getattr(event, "source", None)
    if source is None:
        raise ValueError("Telegram OCR handoff requires event.source")
    platform = getattr(source, "platform", None)
    if hasattr(platform, "value"):
        platform = platform.value
    actor = {
        "platform": str(platform),
        "chat_id": str(getattr(source, "chat_id", "") or ""),
        "telegram_user_id": str(getattr(source, "user_id", "") or ""),
    }
    if not actor["chat_id"] or not actor["telegram_user_id"]:
        raise ValueError("Telegram OCR handoff requires chat and user identity")
    _validate_staging_actor(actor["chat_id"], actor["telegram_user_id"])
    tenant_id = str(os.environ.get("LEKZA_TENANT_ID") or actor["chat_id"])
    controller = _controller_from_environment()
    record = controller.begin_from_ocr(
        tenant_id=tenant_id,
        chat_id=actor["chat_id"],
        thread_id=getattr(source, "thread_id", None),
        session_id=_session_id(event, session_store),
        handoff_id=str(getattr(event, "message_id", "") or ""),
        telegram_user_id=actor["telegram_user_id"],
        source_image_path=source_image_path,
        ocr_result=ocr_result,
    )
    _LOG.info(
        "Lekza OCR handoff transaction_id=%s", record["transaction_id"]
    )
    claim = controller.acquire_initial_prompt_delivery(
        record["transaction_id"], **actor
    )
    if claim is None:
        return {"transaction": record, "prompt_invoked": False, "task": None}
    delivery = _deliver_initial_prompt(controller, adapter, record, actor, claim)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(delivery)
        task = None
    else:
        task = loop.create_task(delivery)
        task.add_done_callback(_log_delivery_result)
    return {"transaction": record, "prompt_invoked": True, "task": task}


def register(ctx):
    sys.modules["lekza_accounting_transaction_buttons"] = sys.modules[__name__]
    def pre_gateway_dispatch(event, **kwargs):
        del kwargs
        source = getattr(event, "source", None)
        platform = getattr(source, "platform", None)
        if hasattr(platform, "value"):
            platform = platform.value
        if str(platform or "").lower() == "telegram":
            _patch_loaded_adapter()
        return None

    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)
    _patch_loaded_adapter()
