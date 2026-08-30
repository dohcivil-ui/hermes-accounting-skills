"""Pretty Telegram clarify buttons for Hermes.

This plugin does NOT implement its own callback-query protocol.
It reuses Hermes' native clarify callback_data contract (cl:<id>:<idx>),
so button taps continue through the built-in clarify resolver.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path("/data/logs/telegram-clarify-pretty.log")
_PATCH_ATTR = "_lekza_pretty_clarify_v1"


def _log(event: str, **data) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **data,
        }
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _compact_label(value, max_chars: int = 30) -> str:
    label = " ".join(str(value).split()).strip() or "ตัวเลือก"
    if len(label) > max_chars:
        label = label[: max_chars - 1].rstrip() + "…"
    return label


def _patch_module(mod_name: str) -> bool:
    mod = sys.modules.get(mod_name)
    if mod is None:
        return False

    adapter_cls = getattr(mod, "TelegramAdapter", None)
    if adapter_cls is None:
        return False

    if getattr(adapter_cls, _PATCH_ATTR, False):
        return True

    original = getattr(adapter_cls, "send_clarify", None)
    if original is None:
        return False

    async def send_clarify_pretty(
        self,
        chat_id,
        question,
        choices,
        clarify_id,
        session_key,
        metadata=None,
        _mod=mod,
        _original=original,
    ):
        # Preserve Hermes' native behavior for open-ended clarify prompts.
        if not choices:
            return await _original(
                self,
                chat_id,
                question,
                choices,
                clarify_id,
                session_key,
                metadata,
            )

        try:
            html_mod = getattr(_mod, "_html")
            InlineKeyboardButton = getattr(_mod, "InlineKeyboardButton")
            InlineKeyboardMarkup = getattr(_mod, "InlineKeyboardMarkup")
            ParseMode = getattr(_mod, "ParseMode")
            SendResult = getattr(_mod, "SendResult")

            if not getattr(self, "_bot", None):
                return SendResult(success=False, error="Not connected")

            full_choices = [str(c) for c in choices]
            text = f"❓ {html_mod.escape(str(question))}"
            option_lines = "\n".join(
                f"{i + 1}. {html_mod.escape(choice)}"
                for i, choice in enumerate(full_choices)
            )
            text += f"\n\n{option_lines}"

            labels = [_compact_label(c) for c in full_choices]
            buttons = [
                InlineKeyboardButton(
                    label,
                    callback_data=f"cl:{clarify_id}:{idx}",
                )
                for idx, label in enumerate(labels)
            ]

            # Two buttons per row when labels are short; otherwise one per row.
            if labels and max(len(x) for x in labels) <= 18 and len(labels) <= 8:
                rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
            else:
                rows = [[b] for b in buttons]

            # Native "other" path becomes the user's requested manual-entry button.
            rows.append([
                InlineKeyboardButton(
                    "✏️ บันทึกเอง",
                    callback_data=f"cl:{clarify_id}:other",
                )
            ])

            kwargs = {
                "chat_id": int(chat_id),
                "text": text,
                "parse_mode": ParseMode.HTML,
                "reply_markup": InlineKeyboardMarkup(rows),
                **self._link_preview_kwargs(),
            }

            thread_id = self._metadata_thread_id(metadata)
            reply_to_id = self._reply_to_message_id_for_send(None, metadata)
            kwargs["reply_to_message_id"] = reply_to_id
            kwargs.update(
                self._thread_kwargs_for_send(
                    chat_id,
                    thread_id,
                    metadata,
                    reply_to_message_id=reply_to_id,
                )
            )

            msg = await self._send_message_with_thread_fallback(**kwargs)
            self._clarify_state[clarify_id] = session_key

            _log(
                "clarify_sent",
                chat_id=str(chat_id),
                clarify_id=str(clarify_id),
                choices=full_choices,
            )
            return SendResult(success=True, message_id=str(msg.message_id))

        except Exception as exc:
            # Fail safe: fall back to Hermes' stock clarify implementation.
            _log("clarify_patch_fallback", error=str(exc))
            return await _original(
                self,
                chat_id,
                question,
                choices,
                clarify_id,
                session_key,
                metadata,
            )

    setattr(adapter_cls, "send_clarify", send_clarify_pretty)
    setattr(adapter_cls, _PATCH_ATTR, True)
    _log("adapter_patched", module=mod_name)
    return True


def _patch_loaded_telegram_adapter() -> bool:
    # Managed Hermes loads the Telegram platform under hermes_plugins.*.
    # The package path is included too because both names can coexist.
    names = (
        "hermes_plugins.telegram_platform.adapter",
        "plugins.platforms.telegram.adapter",
    )
    patched = False
    for name in names:
        patched = _patch_module(name) or patched
    return patched


def register(ctx):
    # Patch lazily on each inbound gateway event. By this point the Telegram
    # adapter has already been imported, while the clarify prompt happens later
    # in the same agent turn.
    def pre_gateway_dispatch(event, gateway=None, session_store=None, **kwargs):
        del gateway, session_store, kwargs
        platform = None
        try:
            source = getattr(event, "source", None)
            platform = getattr(source, "platform", None)
            if hasattr(platform, "value"):
                platform = platform.value
        except Exception:
            platform = None

        if str(platform or "").lower() == "telegram":
            ok = _patch_loaded_telegram_adapter()
            if not ok:
                _log("adapter_not_found")
        return None

    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)
    _log("plugin_registered")
