import importlib.util
import os
from pathlib import Path
import tempfile
import types
import unittest
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

        self.assertIsNone(result)
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
                self.assertIsNone(self._image_hook()(event))
                ocr.assert_not_called()

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
                self.module, "call_akson_ocr", return_value={"error": "synthetic"}
            ), patch.object(self.module.os, "makedirs"), patch(
                "builtins.open", opened
            ):
                self._image_hook()(event, gateway=types.SimpleNamespace(
                    adapters={"telegram": types.SimpleNamespace(
                        _bot=types.SimpleNamespace(id="3001")
                    )}
                ))

        logged = "".join(
            str(call.args[0]) for call in opened().write.call_args_list
        )
        self.assertNotIn(secret_url, logged)
        self.assertNotIn("credential=secret", logged)
        self.assertIn('"media_is_remote": true', logged)
