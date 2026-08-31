import importlib.util
import os
from pathlib import Path
import tempfile
import types
import unittest
from enum import Enum
from unittest.mock import mock_open, patch


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "plugins/accounting-slip-bridge/__init__.py"


def load_module():
    spec = importlib.util.spec_from_file_location("lekza_accounting_bridge", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AccountingSlipBridgeTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_missing_api_key_returns_error_without_network(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            self.module.requests, "post"
        ) as post:
            result = self.module.call_akson_ocr("synthetic.jpg")

        self.assertIn("AKSONOCR_API_KEY", result["error"])
        post.assert_not_called()

    def test_missing_local_image_returns_error_without_network(self):
        with patch.dict(
            os.environ, {"AKSONOCR_API_KEY": "synthetic-test-key"}, clear=True
        ), patch.object(self.module.requests, "post") as post:
            result = self.module.call_akson_ocr("definitely-missing-synthetic.jpg")

        self.assertIn("Image file not found", result["error"])
        post.assert_not_called()

    def _staging_environment(self, root):
        return {
            "LEKZA_RUNTIME_ENV": "staging",
            "LEKZA_STAGING_RESOURCE_ACK": "designated-staging-resources",
            "LEKZA_STAGING_TELEGRAM_BOT_ACK": "designated-test-bot",
            "LEKZA_STAGING_TELEGRAM_CHAT_IDS": "1001",
            "LEKZA_STAGING_TELEGRAM_USER_IDS": "2001",
            "LEKZA_STAGING_TELEGRAM_BOT_IDS": "3001",
            "LEKZA_PRODUCTION_SLIP_FOLDER_ID": "production-folder",
            "LEKZA_PRODUCTION_ACCOUNTING_SPREADSHEET_ID": "production-sheet",
            "AKSONOCR_API_KEY": "synthetic-key",
            "LEKZA_GOOGLE_ACCESS_TOKEN": "synthetic-token",
            "LEKZA_SLIP_FOLDER_ID": "staging-folder",
            "LEKZA_ACCOUNTING_SPREADSHEET_ID": "staging-sheet",
            "LEKZA_STAGING_DATA_ROOT": str(root),
            "LEKZA_TRANSACTION_STATE_DB": str(root / "state" / "transactions.db"),
            "LEKZA_ALLOWED_UPLOAD_ROOTS": str(root / "uploads"),
            "LEKZA_ACTIVE_PROJECTS_JSON": '["Synthetic Project"]',
        }

    def _image_hook(self):
        context = types.SimpleNamespace(hooks={})
        context.register_hook = lambda name, callback: context.hooks.__setitem__(
            name, callback
        )
        self.module.register(context)
        return context.hooks["pre_gateway_dispatch"]

    def test_unauthorized_staging_actor_is_rejected_before_ocr_or_network(self):
        with tempfile.TemporaryDirectory() as temp:
            environment = self._staging_environment(Path(temp) / "staging")
            event = types.SimpleNamespace(
                source=types.SimpleNamespace(
                    platform="telegram", chat_id="unauthorized", user_id="2001"
                ),
                media_urls=["https://example.invalid/slip.jpg?token=secret"],
                media_types=["image/jpeg"],
                message_id="synthetic-message",
            )
            with patch.dict(os.environ, environment, clear=True), patch.object(
                self.module, "call_akson_ocr"
            ) as ocr, patch.object(self.module.requests, "get") as get, patch.object(
                self.module.requests, "post"
            ) as post, patch.object(self.module.os, "makedirs"):
                result = self._image_hook()(event, gateway=types.SimpleNamespace(
                    adapters={"telegram": types.SimpleNamespace(
                        _bot=types.SimpleNamespace(id="3001")
                    )}
                ))

        self.assertEqual(result, {"action": "skip"})
        ocr.assert_not_called()
        get.assert_not_called()
        post.assert_not_called()

    def test_unknown_runtime_mode_is_rejected_before_ocr(self):
        event = types.SimpleNamespace(
            source=types.SimpleNamespace(
                platform="telegram", chat_id="1001", user_id="2001"
            ),
            media_urls=["https://example.invalid/slip.jpg"],
            media_types=["image/jpeg"],
            message_id="synthetic-message",
        )
        for value in (None, "", "stagng"):
            environment = {} if value is None else {"LEKZA_RUNTIME_ENV": value}
            with self.subTest(value=value), patch.dict(
                os.environ, environment, clear=True
            ), patch.object(self.module, "call_akson_ocr") as ocr:
                self.assertEqual(self._image_hook()(event), {"action": "skip"})
                ocr.assert_not_called()

    def test_staging_bot_lookup_accepts_enum_adapter_keys(self):
        class Platform(Enum):
            TELEGRAM = "telegram"

        gateway = types.SimpleNamespace(adapters={
            Platform.TELEGRAM: types.SimpleNamespace(
                _bot=types.SimpleNamespace(id="3001")
            )
        })

        self.assertEqual(self.module._telegram_bot_id(gateway), "3001")

    def test_remote_media_is_materialized_beneath_upload_root(self):
        image = b"\xff\xd8\xff\xe0synthetic-jpeg"
        response = types.SimpleNamespace(status_code=200, content=image)
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LEKZA_ALLOWED_UPLOAD_ROOTS": temp}, clear=True
        ), patch.object(self.module.requests, "get", return_value=response):
            local_path = Path(
                self.module._materialize_media("https://example.invalid/slip")
            )
            try:
                self.assertEqual(local_path.parent, Path(temp).resolve())
                self.assertEqual(local_path.read_bytes(), image)
            finally:
                local_path.unlink(missing_ok=True)

    def test_remote_media_handoff_receives_materialized_local_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "staging"
            environment = self._staging_environment(root)
            local_path = root / "uploads" / "telegram-slip.jpg"
            event = types.SimpleNamespace(
                source=types.SimpleNamespace(
                    platform="telegram", chat_id="1001", user_id="2001"
                ),
                media_urls=["https://example.invalid/slip.jpg"],
                media_types=["image/jpeg"],
                message_id="synthetic-message",
            )
            handed_off = []
            buttons = types.SimpleNamespace(
                handoff_ocr_result=lambda *args, **kwargs: handed_off.append(
                    kwargs["source_image_path"]
                ) or {"transaction": {"transaction_id": "synthetic-id"}}
            )
            gateway = types.SimpleNamespace(adapters={
                "telegram": types.SimpleNamespace(
                    _bot=types.SimpleNamespace(id="3001")
                )
            })
            ocr_result = {
                "akson_called": True,
                "parsed": {"reference_no": "SYNTHETIC"},
            }
            with patch.dict(os.environ, environment, clear=True), patch.dict(
                "sys.modules", {"lekza_accounting_transaction_buttons": buttons}
            ), patch.object(
                self.module, "_materialize_media", return_value=str(local_path)
            ), patch.object(
                self.module, "call_akson_ocr", return_value=ocr_result
            ), patch.object(self.module.os, "makedirs"), patch(
                "builtins.open", mock_open()
            ):
                result = self._image_hook()(event, gateway=gateway)

        self.assertEqual(result["action"], "rewrite")
        self.assertEqual(handed_off, [str(local_path)])

    def test_remote_download_exception_is_redacted_and_fails_closed(self):
        secret_url = "https://example.invalid/slip.jpg?token=secret-value"
        with tempfile.TemporaryDirectory() as temp:
            environment = self._staging_environment(Path(temp) / "staging")
            event = types.SimpleNamespace(
                source=types.SimpleNamespace(
                    platform="telegram", chat_id="1001", user_id="2001"
                ),
                media_urls=[secret_url],
                media_types=["image/jpeg"],
                message_id="synthetic-message",
            )
            gateway = types.SimpleNamespace(adapters={
                "telegram": types.SimpleNamespace(
                    _bot=types.SimpleNamespace(id="3001")
                )
            })
            opened = mock_open()
            with patch.dict(os.environ, environment, clear=True), patch.object(
                self.module.requests,
                "get",
                side_effect=RuntimeError(f"download failed: {secret_url}"),
            ), patch.object(self.module.os, "makedirs"), patch(
                "builtins.open", opened
            ):
                result = self._image_hook()(event, gateway=gateway)

        logged = "".join(
            str(call.args[0]) for call in opened().write.call_args_list
        )
        self.assertEqual(result, {"action": "skip"})
        self.assertNotIn(secret_url, logged)
        self.assertNotIn("secret-value", logged)
        self.assertIn('"error_type": "RuntimeError"', logged)

    def test_remote_file_is_removed_when_ocr_raises(self):
        secret_url = "https://example.invalid/slip.jpg?token=secret-value"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "staging"
            environment = self._staging_environment(root)
            local_path = root / "uploads" / "telegram-slip.jpg"
            local_path.parent.mkdir(parents=True)
            local_path.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
            event = types.SimpleNamespace(
                source=types.SimpleNamespace(
                    platform="telegram", chat_id="1001", user_id="2001"
                ),
                media_urls=[secret_url],
                media_types=["image/jpeg"],
                message_id="synthetic-message",
            )
            gateway = types.SimpleNamespace(adapters={
                "telegram": types.SimpleNamespace(
                    _bot=types.SimpleNamespace(id="3001")
                )
            })
            with patch.dict(os.environ, environment, clear=True), patch.object(
                self.module, "_materialize_media", return_value=str(local_path)
            ), patch.object(
                self.module, "call_akson_ocr", side_effect=RuntimeError("synthetic")
            ), patch.object(self.module.os, "makedirs"), patch(
                "builtins.open", mock_open()
            ):
                result = self._image_hook()(event, gateway=gateway)

            self.assertEqual(result, {"action": "skip"})
            self.assertFalse(local_path.exists())

    def test_remote_media_url_is_not_written_to_diagnostic_log(self):
        secret_url = "https://example.invalid/slip.jpg?credential=secret"
        with tempfile.TemporaryDirectory() as temp:
            environment = self._staging_environment(Path(temp) / "staging")
            event = types.SimpleNamespace(
                source=types.SimpleNamespace(
                    platform="telegram", chat_id="1001", user_id="2001"
                ),
                media_urls=[secret_url],
                media_types=["image/jpeg"],
                message_id="synthetic-message",
            )
            opened = mock_open()
            with patch.dict(os.environ, environment, clear=True), patch.object(
                self.module, "_materialize_media", return_value=secret_url
            ) as materialize, patch.object(
                self.module, "call_akson_ocr", return_value={"error": "synthetic"}
            ), patch.object(self.module.os, "makedirs"), patch(
                "builtins.open", opened
            ):
                self._image_hook()(event, gateway=types.SimpleNamespace(
                    adapters={"telegram": types.SimpleNamespace(
                        _bot=types.SimpleNamespace(id="3001")
                    )}
                ))

        materialize.assert_called_once_with(secret_url)

        logged = "".join(
            str(call.args[0]) for call in opened().write.call_args_list
        )
        self.assertNotIn(secret_url, logged)
        self.assertNotIn("credential=secret", logged)
        self.assertIn('"media_is_remote": true', logged)
