import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from enum import Enum
from unittest.mock import mock_open, patch


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "plugins/accounting-slip-bridge/__init__.py"
MARKDOWN_REFERENCE_FIXTURE = (
    ROOT / "tests/fixtures/aksonocr_markdown_reference.json"
)


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

    def test_local_media_outside_root_is_materialized_beneath_upload_root(self):
        image = b"\xff\xd8\xff\xe0synthetic-jpeg"
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            upload_root = base / "uploads"
            source = base / "hermes-cache" / "slip.jpg"
            source.parent.mkdir()
            source.write_bytes(image)
            with patch.dict(
                os.environ,
                {"LEKZA_ALLOWED_UPLOAD_ROOTS": str(upload_root)},
                clear=True,
            ):
                materialized = Path(self.module._materialize_media(source))

            self.assertEqual(materialized.parent, upload_root.resolve())
            self.assertNotEqual(materialized, source)
            self.assertEqual(materialized.read_bytes(), image)

    def test_local_media_inside_root_is_reused_without_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            upload_root = Path(temp) / "uploads"
            upload_root.mkdir()
            source = upload_root / "slip.png"
            source.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic-png")
            with patch.dict(
                os.environ,
                {"LEKZA_ALLOWED_UPLOAD_ROOTS": str(upload_root)},
                clear=True,
            ), patch.object(self.module.tempfile, "mkstemp") as mkstemp:
                materialized = self.module._materialize_media(source)

            self.assertEqual(Path(materialized), source.resolve())
            mkstemp.assert_not_called()

    def test_local_media_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            upload_root = base / "uploads"
            source = base / "slip.jpg"
            source.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
            traversal = upload_root / ".." / "slip.jpg"
            with patch.dict(
                os.environ,
                {"LEKZA_ALLOWED_UPLOAD_ROOTS": str(upload_root)},
                clear=True,
            ), self.assertRaisesRegex(ValueError, "traversal"):
                self.module._materialize_media(traversal)

    def test_local_media_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            upload_root = base / "uploads"
            target = base / "target.jpg"
            target.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
            link = base / "link.jpg"
            os.symlink(target, link)
            with patch.dict(
                os.environ,
                {"LEKZA_ALLOWED_UPLOAD_ROOTS": str(upload_root)},
                clear=True,
            ), self.assertRaisesRegex(ValueError, "symlink"):
                self.module._materialize_media(link)

    def test_local_media_oversize_and_unsupported_type_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            upload_root = base / "uploads"
            oversized = base / "oversized.jpg"
            oversized.write_bytes(b"\xff\xd8\xff" + b"x" * 32)
            unsupported = base / "unsupported.jpg"
            unsupported.write_bytes(b"text")
            environment = {
                "LEKZA_ALLOWED_UPLOAD_ROOTS": str(upload_root),
                "LEKZA_MAX_SLIP_BYTES": "16",
            }
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(ValueError, "size"):
                    self.module._materialize_media(oversized)
                with self.assertRaisesRegex(ValueError, "type"):
                    self.module._materialize_media(unsupported)

    def test_local_media_outside_root_handoff_uses_materialized_path(self):
        with tempfile.TemporaryDirectory() as temp:
            staging_root = Path(temp) / "staging"
            environment = self._staging_environment(staging_root)
            source = Path(temp) / "hermes-cache" / "slip.jpg"
            source.parent.mkdir()
            source.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
            event = types.SimpleNamespace(
                source=types.SimpleNamespace(
                    platform="telegram", chat_id="1001", user_id="2001"
                ),
                media_urls=[str(source)],
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
            with patch.dict(os.environ, environment, clear=True), patch.dict(
                "sys.modules", {"lekza_accounting_transaction_buttons": buttons}
            ), patch.object(
                self.module,
                "call_akson_ocr",
                return_value={
                    "akson_called": True,
                    "parsed": {"reference_no": "SYNTHETIC"},
                },
            ), patch.object(self.module.os, "makedirs"), patch(
                "builtins.open", mock_open()
            ):
                result = self._image_hook()(event, gateway=gateway)

            materialized = Path(handed_off[0])
            self.assertEqual(result, {"action": "skip"})
            self.assertEqual(
                materialized.parent, (staging_root / "uploads").resolve()
            )
            self.assertEqual(materialized.read_bytes(), source.read_bytes())

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

        self.assertEqual(result, {"action": "skip"})
        self.assertEqual(handed_off, [str(local_path)])

    def test_ocr_normalization_preserves_parsed_reference_no(self):
        ocr_result = {
            "parsed": {"reference_no": " SYNTHETIC-CANONICAL-001 ", "amount": 1}
        }

        normalized = self.module._normalize_ocr_result_for_handoff(ocr_result)

        self.assertEqual(
            normalized["parsed"]["reference_no"], "SYNTHETIC-CANONICAL-001"
        )
        self.assertEqual(
            ocr_result["parsed"]["reference_no"], " SYNTHETIC-CANONICAL-001 "
        )

    def test_ocr_normalization_uses_fallback_key_or_labeled_text(self):
        cases = (
            (
                {"parsed": {"ref_no": "SYNTHETIC-KEY-001"}},
                "SYNTHETIC-KEY-001",
            ),
            (
                {
                    "parsed": {"amount": 1},
                    "raw_ocr_text": "Amount 1.00\nReference No: SYNTHETIC-TEXT-001",
                },
                "SYNTHETIC-TEXT-001",
            ),
            (
                {
                    "parsed": {},
                    "raw_ocr_text": "รหัสอ้างอิง: SYNTHETIC-THAI-001",
                },
                "SYNTHETIC-THAI-001",
            ),
        )
        for ocr_result, expected in cases:
            with self.subTest(expected=expected):
                normalized = self.module._normalize_ocr_result_for_handoff(
                    ocr_result
                )
                self.assertEqual(normalized["parsed"]["reference_no"], expected)

    def test_ocr_normalization_handles_aksonocr_markdown_reference_fixture(self):
        fixture = json.loads(
            MARKDOWN_REFERENCE_FIXTURE.read_text(encoding="utf-8")
        )
        expected = fixture["reference_no"]
        for markdown in fixture["markdown_samples"]:
            with self.subTest(markdown=markdown):
                normalized = self.module._normalize_ocr_result_for_handoff({
                    "parsed": {"amount": 1},
                    "raw_ocr_text": markdown,
                })
                self.assertEqual(normalized["parsed"]["reference_no"], expected)

        for markdown in fixture["unlabeled_samples"]:
            with self.subTest(unlabeled=markdown):
                normalized = self.module._normalize_ocr_result_for_handoff({
                    "parsed": {"amount": 1},
                    "raw_ocr_text": markdown,
                })
                self.assertNotIn("reference_no", normalized["parsed"])

    def test_ocr_normalization_without_reference_does_not_fabricate_one(self):
        normalized = self.module._normalize_ocr_result_for_handoff({
            "parsed": {"amount": 1},
            "raw_ocr_text": "Amount: 1.00",
        })

        self.assertNotIn("reference_no", normalized["parsed"])

    def test_text_reference_is_normalized_before_durable_handoff(self):
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

            def require_reference(*args, **kwargs):
                reference_no = kwargs["ocr_result"].get("parsed", {}).get(
                    "reference_no"
                )
                if not reference_no:
                    raise ValueError("OCR result requires reference_no")
                handed_off.append(reference_no)
                return {"transaction": {"transaction_id": "synthetic-id"}}

            buttons = types.SimpleNamespace(handoff_ocr_result=require_reference)
            gateway = types.SimpleNamespace(adapters={
                "telegram": types.SimpleNamespace(
                    _bot=types.SimpleNamespace(id="3001")
                )
            })
            with patch.dict(os.environ, environment, clear=True), patch.dict(
                "sys.modules", {"lekza_accounting_transaction_buttons": buttons}
            ), patch.object(
                self.module, "_materialize_media", return_value=str(local_path)
            ), patch.object(
                self.module,
                "call_akson_ocr",
                return_value={
                    "akson_called": True,
                    "parsed": {"amount": 1},
                    "raw_ocr_text": "Reference No: SYNTHETIC-HANDOFF-001",
                },
            ), patch.object(self.module.os, "makedirs"), patch(
                "builtins.open", mock_open()
            ):
                result = self._image_hook()(event, gateway=gateway)

        self.assertEqual(result, {"action": "skip"})
        self.assertEqual(handed_off, ["SYNTHETIC-HANDOFF-001"])

    def test_handoff_failure_log_has_sanitized_truncated_error_message(self):
        secret_token = "synthetic-secret-token-value"
        file_url = "file:///synthetic/private/slip.jpg"
        google_id = "syntheticGoogleResourceId123456789"
        telegram_id = "987654321"
        raw_ocr = "synthetic raw OCR customer text"
        exception_message = (
            "handoff rejected: "
            f"token={secret_token} file={file_url} google_id={google_id} "
            f"telegram_id={telegram_id} raw_ocr_text={raw_ocr} "
            + "x" * 240
        )
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
            buttons = types.SimpleNamespace(
                handoff_ocr_result=lambda *args, **kwargs: (_ for _ in ()).throw(
                    ValueError(exception_message)
                )
            )
            gateway = types.SimpleNamespace(adapters={
                "telegram": types.SimpleNamespace(
                    _bot=types.SimpleNamespace(id="3001")
                )
            })
            opened = mock_open()
            with patch.dict(os.environ, environment, clear=True), patch.dict(
                "sys.modules", {"lekza_accounting_transaction_buttons": buttons}
            ), patch.object(
                self.module, "_materialize_media", return_value=str(local_path)
            ), patch.object(
                self.module,
                "call_akson_ocr",
                return_value={
                    "akson_called": True,
                    "parsed": {"reference_no": "SYNTHETIC-DIAGNOSTIC-001"},
                },
            ), patch.object(self.module.os, "makedirs"), patch(
                "builtins.open", opened
            ):
                self._image_hook()(event, gateway=gateway)

        entries = [
            json.loads(call.args[0])
            for call in opened().write.call_args_list
            if "telegram_transaction_handoff_failed" in call.args[0]
        ]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["error_type"], "ValueError")
        self.assertLessEqual(len(entries[0]["error_message"]), 160)
        self.assertIn("handoff rejected", entries[0]["error_message"])
        truncated = self.module._sanitized_error_message(
            ValueError("diagnostic " + "word " * 100)
        )
        self.assertEqual(len(truncated), 160)
        for sensitive_value in (
            secret_token,
            file_url,
            google_id,
            telegram_id,
            raw_ocr,
        ):
            self.assertNotIn(sensitive_value, entries[0]["error_message"])

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
