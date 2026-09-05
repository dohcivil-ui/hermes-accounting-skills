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

    @staticmethod
    def _buttons_with_ingress(handoff_ocr_result):
        def obtain(*args, **kwargs):
            return types.SimpleNamespace(
                status="ready",
                ocr_result=kwargs["ocr_reader"](),
                transaction_id=None,
            )

        def complete(outcome, transaction_id):
            outcome.status = "completed"
            outcome.transaction_id = transaction_id
            return outcome

        return types.SimpleNamespace(
            lookup_ocr_ingress=lambda *args: None,
            obtain_ocr_ingress=obtain,
            find_ocr_duplicate_candidates=lambda outcome: [],
            complete_ocr_ingress=complete,
            handoff_ocr_result=handoff_ocr_result,
        )

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

    def test_amount_key_extraction_uses_bounded_amount_only_request(self):
        response = types.SimpleNamespace(
            status_code=200,
            json=lambda: {
                "success": True,
                "data": {"amount": "1250.50"},
            },
        )
        with tempfile.TemporaryDirectory() as temp:
            image_path = Path(temp) / "synthetic-slip.jpg"
            image_path.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
            with patch.dict(
                os.environ, {"AKSONOCR_API_KEY": "synthetic-test-key"}, clear=True
            ), patch.object(
                self.module.requests, "post", return_value=response
            ) as post:
                amount = self.module.call_akson_amount_extraction(image_path)

        self.assertEqual(amount, "1250.50")
        _, kwargs = post.call_args
        self.assertEqual(
            post.call_args.args[0],
            "https://backend.aksonocr.com/api/v1/key-extract",
        )
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(
            json.loads(kwargs["data"]["customFields"]),
            [{
                "key": "amount",
                "description": (
                    "ยอดเงินที่โอนหรือชำระในสลิป "
                    "ส่งคืนเฉพาะตัวเลข ไม่รวม THB, ฿ หรือ บาท"
                ),
            }],
        )
        self.assertIn("ตัวเลขเท่านั้น", kwargs["data"]["additionalInstructions"])
        self.assertEqual(kwargs["data"]["model"], "AksonOCR-preview")

    def test_amount_key_extraction_failure_returns_none_without_response_leak(self):
        secret_response = "payer=Private Person account=999 amount=9876.54"
        response = types.SimpleNamespace(
            status_code=500,
            text=secret_response,
            json=lambda: {"raw": secret_response},
        )
        with tempfile.TemporaryDirectory() as temp:
            image_path = Path(temp) / "synthetic-slip.jpg"
            image_path.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
            with patch.dict(
                os.environ, {"AKSONOCR_API_KEY": "synthetic-test-key"}, clear=True
            ), patch.object(
                self.module.requests, "post", return_value=response
            ), patch("builtins.print") as printed:
                amount = self.module.call_akson_amount_extraction(image_path)

        self.assertIsNone(amount)
        printed.assert_not_called()

    def test_date_key_extraction_uses_bounded_date_only_request(self):
        response = types.SimpleNamespace(
            status_code=200,
            json=lambda: {
                "success": True,
                "data": {"date": "05/09/2569"},
            },
        )
        with tempfile.TemporaryDirectory() as temp:
            image_path = Path(temp) / "synthetic-slip.jpg"
            image_path.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
            with patch.dict(
                os.environ, {"AKSONOCR_API_KEY": "synthetic-test-key"}, clear=True
            ), patch.object(
                self.module.requests, "post", return_value=response
            ) as post:
                extracted = self.module.call_akson_date_extraction(image_path)

        self.assertEqual(extracted, "05/09/2569")
        _, kwargs = post.call_args
        self.assertEqual(
            json.loads(kwargs["data"]["customFields"]),
            [{
                "key": "date",
                "description": "วันที่ทำรายการบนสลิป วัน/เดือน/ปี พ.ศ.",
            }],
        )
        self.assertIn("หลายวันที่", kwargs["data"]["additionalInstructions"])
        self.assertIn("ห้ามเดา", kwargs["data"]["additionalInstructions"])
        self.assertEqual(kwargs["timeout"], 30)

    def _add_party_note_fields(self, parsed, extracted):
        ocr_result = self.module._normalize_ocr_result_for_handoff({
            "parsed": dict(parsed)
        })
        with patch.object(
            self.module,
            "call_akson_party_note_extraction",
            return_value=extracted,
        ) as extraction:
            self.module._add_extracted_party_note(
                ocr_result, "synthetic-slip.jpg"
            )
        return ocr_result["parsed"], extraction

    def _call_party_note_extraction(self, response=None, *, side_effect=None):
        with tempfile.TemporaryDirectory() as temp:
            image_path = Path(temp) / "synthetic-slip.jpg"
            image_path.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
            with patch.dict(
                os.environ,
                {"AKSONOCR_API_KEY": "synthetic-test-key"},
                clear=True,
            ), patch.object(
                self.module.requests,
                "post",
                return_value=response,
                side_effect=side_effect,
            ) as post:
                result = self.module.call_akson_party_note_extraction(
                    image_path, ("payer", "payee", "note")
                )
        return result, post

    def test_structured_payer_is_preserved(self):
        parsed, extraction = self._add_party_note_fields({
            "payer": "  Existing   Payer  ",
            "payee": "Existing Payee",
            "note": "Existing note",
        }, {})
        self.assertEqual(parsed["payer"], "Existing Payer")
        extraction.assert_not_called()

    def test_structured_payee_is_preserved(self):
        parsed, extraction = self._add_party_note_fields({
            "payer": "Existing Payer",
            "payee": "  Existing   Payee  ",
            "note": "Existing note",
        }, {})
        self.assertEqual(parsed["payee"], "Existing Payee")
        extraction.assert_not_called()

    def test_structured_note_is_preserved(self):
        parsed, extraction = self._add_party_note_fields({
            "payer": "Existing Payer",
            "payee": "Existing Payee",
            "note": "  เบิกสำรองค่าแรงชุดปูบล็อค   สนามบิน  ",
        }, {})
        self.assertEqual(
            parsed["note"], "เบิกสำรองค่าแรงชุดปูบล็อค สนามบิน"
        )
        extraction.assert_not_called()

    def test_missing_payer_is_extracted(self):
        parsed, extraction = self._add_party_note_fields(
            {"payee": "Existing Payee", "note": "Existing note"},
            {"payer": "Extracted Payer"},
        )
        self.assertEqual(parsed["payer"], "Extracted Payer")
        extraction.assert_called_once_with("synthetic-slip.jpg", ("payer",))

    def test_missing_payee_is_extracted(self):
        parsed, extraction = self._add_party_note_fields(
            {"payer": "Existing Payer", "note": "Existing note"},
            {"payee": "Extracted Payee"},
        )
        self.assertEqual(parsed["payee"], "Extracted Payee")
        extraction.assert_called_once_with("synthetic-slip.jpg", ("payee",))

    def test_missing_note_is_extracted_verbatim_with_whitespace_normalized(self):
        parsed, extraction = self._add_party_note_fields(
            {"payer": "Existing Payer", "payee": "Existing Payee"},
            {"note": "  เบิกสำรองค่าแรงชุดปูบล็อค\n  สนามบิน  "},
        )
        self.assertEqual(
            parsed["note"], "เบิกสำรองค่าแรงชุดปูบล็อค สนามบิน"
        )
        extraction.assert_called_once_with("synthetic-slip.jpg", ("note",))

    def test_partial_missing_fields_request_only_missing_custom_fields(self):
        response = types.SimpleNamespace(
            status_code=200,
            json=lambda: {
                "success": True,
                "data": {"payee": "Extracted Payee", "note": "Exact note"},
            },
        )
        with tempfile.TemporaryDirectory() as temp:
            image_path = Path(temp) / "synthetic-slip.jpg"
            image_path.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
            with patch.dict(
                os.environ,
                {"AKSONOCR_API_KEY": "synthetic-test-key"},
                clear=True,
            ), patch.object(
                self.module.requests, "post", return_value=response
            ) as post:
                result = self.module.call_akson_party_note_extraction(
                    image_path, ("payee", "note")
                )

        custom_fields = json.loads(post.call_args.kwargs["data"]["customFields"])
        self.assertEqual([field["key"] for field in custom_fields], [
            "payee", "note",
        ])
        self.assertEqual(result, {
            "payee": "Extracted Payee", "note": "Exact note",
        })

    def test_empty_party_note_response_is_blank_and_fail_open(self):
        response = types.SimpleNamespace(
            status_code=200,
            json=lambda: {"success": True, "data": {}},
        )
        result, _ = self._call_party_note_extraction(response)
        self.assertEqual(result, {"payer": "", "payee": "", "note": ""})

    def test_ambiguous_party_note_response_is_blank_and_fail_open(self):
        response = types.SimpleNamespace(
            status_code=200,
            json=lambda: {
                "success": True,
                "data": {
                    "payer": ["First", "Second"],
                    "payee": {"ambiguous": True},
                    "note": None,
                },
            },
        )
        result, _ = self._call_party_note_extraction(response)
        self.assertEqual(result, {"payer": "", "payee": "", "note": ""})

    def test_malformed_party_note_response_is_blank_and_fail_open(self):
        response = types.SimpleNamespace(status_code=200, json=lambda: [])
        result, _ = self._call_party_note_extraction(response)
        self.assertEqual(result, {"payer": "", "payee": "", "note": ""})

    def test_non_2xx_party_note_response_is_blank_and_fail_open(self):
        response = types.SimpleNamespace(
            status_code=500,
            text="synthetic private provider payload",
        )
        result, _ = self._call_party_note_extraction(response)
        self.assertEqual(result, {"payer": "", "payee": "", "note": ""})

    def test_party_note_exception_is_blank_and_fail_open(self):
        result, _ = self._call_party_note_extraction(
            side_effect=RuntimeError("synthetic private provider payload")
        )
        self.assertEqual(result, {"payer": "", "payee": "", "note": ""})

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
            buttons = self._buttons_with_ingress(
                lambda *args, **kwargs: handed_off.append(
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
            buttons = self._buttons_with_ingress(
                lambda *args, **kwargs: handed_off.append(
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

    def test_missing_normalized_amount_uses_key_extraction_before_handoff(self):
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
            buttons = self._buttons_with_ingress(
                lambda *args, **kwargs: handed_off.append(
                    kwargs["ocr_result"]
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
                self.module, "_materialize_media", return_value=str(local_path)
            ), patch.object(
                self.module,
                "call_akson_ocr",
                return_value={
                    "akson_called": True,
                    "parsed": {"reference_no": "SYNTHETIC-FALLBACK-001"},
                },
            ), patch.object(
                self.module,
                "call_akson_amount_extraction",
                return_value="1250.50",
            ) as fallback, patch.object(self.module.os, "makedirs"), patch(
                "builtins.open", mock_open()
            ):
                result = self._image_hook()(event, gateway=gateway)

        self.assertEqual(result, {"action": "skip"})
        fallback.assert_called_once_with(str(local_path))
        self.assertEqual(handed_off[0]["parsed"]["amount"], "1250.50")

    def test_duplicate_reference_after_ocr_returns_fail_closed_duplicate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "staging"
            environment = self._staging_environment(root)
            local_path = root / "uploads" / "synthetic.jpg"
            local_path.parent.mkdir(parents=True)
            local_path.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
            event = types.SimpleNamespace(
                source=types.SimpleNamespace(
                    platform="telegram", chat_id="1001", user_id="2001"
                ),
                media_urls=[str(local_path)],
                media_types=["image/jpeg"],
                message_id="duplicate-reference-message",
            )

            class DuplicateReferenceError(RuntimeError):
                duplicate_kind = "reference"

                def __init__(self):
                    super().__init__("duplicate")
                    self.existing_transaction_id = "existing-transaction"

            completed = []

            def obtain(*args, **kwargs):
                return types.SimpleNamespace(
                    status="ready",
                    ocr_result=kwargs["ocr_reader"](),
                    transaction_id=None,
                )

            buttons = types.SimpleNamespace(
                lookup_ocr_ingress=lambda *args: None,
                obtain_ocr_ingress=obtain,
                find_ocr_duplicate_candidates=lambda outcome: [],
                complete_ocr_ingress=lambda outcome, transaction_id: completed.append(
                    transaction_id
                ),
                handoff_ocr_result=lambda *args, **kwargs: (_ for _ in ()).throw(
                    DuplicateReferenceError()
                ),
            )
            gateway = types.SimpleNamespace(adapters={
                "telegram": types.SimpleNamespace(_bot=types.SimpleNamespace(id="3001"))
            })
            with patch.dict(os.environ, environment, clear=True), patch.dict(
                "sys.modules", {"lekza_accounting_transaction_buttons": buttons}
            ), patch.object(
                self.module,
                "call_akson_ocr",
                return_value={
                    "akson_called": True,
                    "confidence": 0.9,
                    "parsed": {
                        "reference_no": "SYNTHETIC-DUPLICATE",
                        "amount": 100,
                        "date": "2026-09-05",
                    },
                },
            ), patch.object(self.module.os, "makedirs"), patch(
                "builtins.open", mock_open()
            ):
                result = self._image_hook()(event, gateway=gateway)

        self.assertEqual(completed, ["existing-transaction"])
        self.assertEqual(result["action"], "rewrite")
        self.assertIn("Duplicate Slip", result["text"])
        self.assertNotIn("AksonOCR Slip Result", result["text"])

    def test_unavailable_ingress_guard_fails_closed_before_ocr(self):
        event = types.SimpleNamespace(
            source=types.SimpleNamespace(
                platform="telegram", chat_id="1001", user_id="2001"
            ),
            media_urls=["https://example.invalid/slip.jpg"],
            media_types=["image/jpeg"],
            message_id="guard-unavailable",
        )
        incomplete_buttons = types.SimpleNamespace(handoff_ocr_result=lambda: None)
        gateway = types.SimpleNamespace(adapters={
            "telegram": types.SimpleNamespace(_bot=types.SimpleNamespace(id="3001"))
        })
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, self._staging_environment(Path(temp)), clear=True
        ), patch.dict(
            "sys.modules", {"lekza_accounting_transaction_buttons": incomplete_buttons}
        ), patch.object(self.module, "call_akson_ocr") as ocr, patch.object(
            self.module.os, "makedirs"
        ), patch("builtins.open", mock_open()):
            result = self._image_hook()(event, gateway=gateway)

        self.assertEqual(result, {"action": "skip"})
        ocr.assert_not_called()

    def test_duplicate_replay_returns_before_ocr_or_date_extraction(self):
        event = types.SimpleNamespace(
            source=types.SimpleNamespace(
                platform="telegram", chat_id="1001", user_id="2001"
            ),
            media_urls=["https://example.invalid/slip.jpg"],
            media_types=["image/jpeg"],
            message_id="duplicate-replay",
        )
        duplicate = types.SimpleNamespace(
            status="duplicate", transaction_id="existing-transaction"
        )
        buttons = types.SimpleNamespace(
            lookup_ocr_ingress=lambda *args: duplicate,
            obtain_ocr_ingress=lambda *args, **kwargs: self.fail(
                "duplicate replay must not obtain OCR"
            ),
            find_ocr_duplicate_candidates=lambda outcome: [],
            complete_ocr_ingress=lambda outcome, transaction_id: None,
        )
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, self._staging_environment(Path(temp)), clear=True
        ), patch.dict(
            "sys.modules", {"lekza_accounting_transaction_buttons": buttons}
        ), patch.object(self.module, "_materialize_media") as materialize, \
                patch.object(self.module, "call_akson_ocr") as ocr, \
                patch.object(
                    self.module, "call_akson_party_note_extraction"
                ) as party_note_extraction, \
                patch.object(
                    self.module, "call_akson_date_extraction"
                ) as date_extraction:
            result = self._image_hook()(
                event,
                gateway=types.SimpleNamespace(adapters={
                    "telegram": types.SimpleNamespace(
                        _bot=types.SimpleNamespace(id="3001")
                    )
                }),
            )

        self.assertEqual(result["action"], "rewrite")
        self.assertIn("Duplicate Slip", result["text"])
        materialize.assert_not_called()
        ocr.assert_not_called()
        party_note_extraction.assert_not_called()
        date_extraction.assert_not_called()

    def test_content_duplicate_returns_before_party_note_extraction(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "staging"
            image_path = root / "uploads" / "synthetic.jpg"
            duplicate = types.SimpleNamespace(
                status="duplicate", transaction_id="existing-transaction"
            )
            buttons = types.SimpleNamespace(
                lookup_ocr_ingress=lambda *args: None,
                obtain_ocr_ingress=lambda *args, **kwargs: duplicate,
                find_ocr_duplicate_candidates=lambda outcome: self.fail(
                    "content duplicate must return before candidate search"
                ),
                complete_ocr_ingress=lambda outcome, transaction_id: None,
            )
            event = types.SimpleNamespace(
                source=types.SimpleNamespace(
                    platform="telegram", chat_id="1001", user_id="2001"
                ),
                media_urls=[str(image_path)], media_types=["image/jpeg"],
                message_id="content-duplicate",
            )
            gateway = types.SimpleNamespace(adapters={
                "telegram": types.SimpleNamespace(
                    _bot=types.SimpleNamespace(id="3001")
                )
            })
            with patch.dict(
                os.environ, self._staging_environment(root), clear=True
            ), patch.dict(
                "sys.modules", {"lekza_accounting_transaction_buttons": buttons}
            ), patch.object(
                self.module, "_materialize_media", return_value=str(image_path)
            ), patch.object(
                self.module, "call_akson_ocr"
            ) as ocr, patch.object(
                self.module, "call_akson_party_note_extraction"
            ) as party_note_extraction, patch.object(
                self.module.os, "makedirs"
            ), patch("builtins.open", mock_open()):
                result = self._image_hook()(event, gateway=gateway)

        self.assertEqual(result["action"], "rewrite")
        self.assertIn("Duplicate Slip", result["text"])
        ocr.assert_not_called()
        party_note_extraction.assert_not_called()

    def test_party_note_fallback_runs_after_candidate_check_before_handoff(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "staging"
            image_path = root / "uploads" / "synthetic.jpg"
            events = []

            def obtain(*args, **kwargs):
                return types.SimpleNamespace(
                    status="ready",
                    transaction_id=None,
                    ocr_result=kwargs["ocr_reader"](),
                )

            def find_candidates(outcome):
                events.append("candidate_check")
                return []

            def extract_party_note(path, fields):
                events.append("party_note_extraction")
                self.assertEqual(fields, ("payer", "payee", "note"))
                return {
                    "payer": "Extracted Payer",
                    "payee": "Extracted Payee",
                    "note": "Exact note",
                }

            def handoff(*args, **kwargs):
                events.append("handoff")
                self.assertEqual(kwargs["ocr_result"]["parsed"], {
                    "reference_no": "SYNTHETIC-REF",
                    "amount": 100,
                    "date": "2026-09-05",
                    "payer": "Extracted Payer",
                    "payee": "Extracted Payee",
                    "note": "Exact note",
                })
                return {"transaction": {"transaction_id": "synthetic-id"}}

            buttons = types.SimpleNamespace(
                lookup_ocr_ingress=lambda *args: None,
                obtain_ocr_ingress=obtain,
                find_ocr_duplicate_candidates=find_candidates,
                complete_ocr_ingress=lambda outcome, transaction_id: None,
                handoff_ocr_result=handoff,
            )
            event = types.SimpleNamespace(
                source=types.SimpleNamespace(
                    platform="telegram", chat_id="1001", user_id="2001"
                ),
                media_urls=[str(image_path)], media_types=["image/jpeg"],
                message_id="party-note-order",
            )
            gateway = types.SimpleNamespace(adapters={
                "telegram": types.SimpleNamespace(
                    _bot=types.SimpleNamespace(id="3001")
                )
            })
            with patch.dict(
                os.environ, self._staging_environment(root), clear=True
            ), patch.dict(
                "sys.modules", {"lekza_accounting_transaction_buttons": buttons}
            ), patch.object(
                self.module, "_materialize_media", return_value=str(image_path)
            ), patch.object(
                self.module,
                "call_akson_ocr",
                side_effect=lambda path: events.append("ocr") or {
                    "akson_called": True,
                    "parsed": {
                        "reference_no": "SYNTHETIC-REF",
                        "amount": 100,
                        "date": "2026-09-05",
                    },
                },
            ), patch.object(
                self.module,
                "call_akson_party_note_extraction",
                side_effect=extract_party_note,
            ), patch.object(
                self.module.os, "makedirs"
            ), patch("builtins.open", mock_open()):
                result = self._image_hook()(event, gateway=gateway)

        self.assertEqual(result, {"action": "skip"})
        self.assertEqual(events, [
            "ocr", "candidate_check", "party_note_extraction", "handoff",
        ])

    def test_resumed_ingress_does_not_repeat_party_note_extraction(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "staging"
            image_path = root / "uploads" / "synthetic.jpg"
            resume = types.SimpleNamespace(status="resume")
            outcome = types.SimpleNamespace(
                status="ready",
                transaction_id=None,
                ocr_result={
                    "akson_called": True,
                    "parsed": {
                        "reference_no": "SYNTHETIC-REF",
                        "amount": 100,
                        "date": "2026-09-05",
                    },
                },
            )
            handed_off = []
            buttons = types.SimpleNamespace(
                lookup_ocr_ingress=lambda *args: resume,
                obtain_ocr_ingress=lambda *args, **kwargs: outcome,
                find_ocr_duplicate_candidates=lambda value: [],
                complete_ocr_ingress=lambda value, transaction_id: None,
                handoff_ocr_result=lambda *args, **kwargs: (
                    handed_off.append(kwargs["ocr_result"])
                    or {"transaction": {"transaction_id": "synthetic-id"}}
                ),
            )
            event = types.SimpleNamespace(
                source=types.SimpleNamespace(
                    platform="telegram", chat_id="1001", user_id="2001"
                ),
                media_urls=[str(image_path)], media_types=["image/jpeg"],
                message_id="resumed-ingress",
            )
            gateway = types.SimpleNamespace(adapters={
                "telegram": types.SimpleNamespace(
                    _bot=types.SimpleNamespace(id="3001")
                )
            })
            with patch.dict(
                os.environ, self._staging_environment(root), clear=True
            ), patch.dict(
                "sys.modules", {"lekza_accounting_transaction_buttons": buttons}
            ), patch.object(
                self.module, "_materialize_media", return_value=str(image_path)
            ), patch.object(
                self.module, "call_akson_ocr"
            ) as ocr, patch.object(
                self.module, "call_akson_party_note_extraction"
            ) as party_note_extraction, patch.object(
                self.module.os, "makedirs"
            ), patch("builtins.open", mock_open()):
                result = self._image_hook()(event, gateway=gateway)

        self.assertEqual(result, {"action": "skip"})
        self.assertEqual(len(handed_off), 1)
        ocr.assert_not_called()
        party_note_extraction.assert_not_called()

    def test_exact_reference_candidate_returns_duplicate_without_handoff(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "staging"
            image_path = root / "uploads" / "synthetic.jpg"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
            completed = []

            def obtain(*args, **kwargs):
                return types.SimpleNamespace(
                    status="ready",
                    transaction_id=None,
                    ocr_result=kwargs["ocr_reader"](),
                )

            buttons = types.SimpleNamespace(
                lookup_ocr_ingress=lambda *args: None,
                obtain_ocr_ingress=obtain,
                find_ocr_duplicate_candidates=lambda value: [{
                    "transaction_id": "existing-transaction",
                    "reasons": ("exact_reference",),
                }],
                complete_ocr_ingress=lambda value, transaction_id: completed.append(
                    transaction_id
                ),
                handoff_ocr_result=lambda *args, **kwargs: self.fail(
                    "duplicate reference must not reach handoff"
                ),
            )
            event = types.SimpleNamespace(
                source=types.SimpleNamespace(
                    platform="telegram", chat_id="1001", user_id="2001"
                ),
                media_urls=[str(image_path)], media_types=["image/jpeg"],
                message_id="exact-reference",
            )
            gateway = types.SimpleNamespace(adapters={
                "telegram": types.SimpleNamespace(_bot=types.SimpleNamespace(id="3001"))
            })
            with patch.dict(
                os.environ, self._staging_environment(root), clear=True
            ), patch.dict(
                "sys.modules", {"lekza_accounting_transaction_buttons": buttons}
            ), patch.object(
                self.module,
                "call_akson_ocr",
                return_value={
                    "akson_called": True,
                    "parsed": {
                        "reference_no": "abc123",
                        "amount": 100,
                        "date": "2026-09-05",
                    },
                },
            ), patch.object(
                self.module, "call_akson_party_note_extraction"
            ) as party_note_extraction, patch.object(
                self.module, "call_akson_amount_extraction"
            ) as amount_extraction, patch.object(
                self.module, "call_akson_date_extraction"
            ) as date_extraction, patch.object(
                self.module.os, "makedirs"
            ), patch("builtins.open", mock_open()):
                result = self._image_hook()(event, gateway=gateway)

        self.assertEqual(completed, ["existing-transaction"])
        self.assertEqual(result["action"], "rewrite")
        self.assertIn("Duplicate Slip", result["text"])
        party_note_extraction.assert_not_called()
        amount_extraction.assert_not_called()
        date_extraction.assert_not_called()

    def test_key_extraction_exception_keeps_manual_amount_without_secret_log(self):
        secret_response = "payer=Private Person account=999 amount=9876.54"
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
            buttons = self._buttons_with_ingress(
                lambda *args, **kwargs: handed_off.append(
                    kwargs["ocr_result"]
                ) or {"transaction": {"transaction_id": "synthetic-id"}}
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
                    "parsed": {"reference_no": "SYNTHETIC-FALLBACK-FAIL"},
                },
            ), patch.object(
                self.module,
                "call_akson_amount_extraction",
                side_effect=RuntimeError(secret_response),
            ), patch.object(self.module.os, "makedirs"), patch(
                "builtins.open", opened
            ):
                result = self._image_hook()(event, gateway=gateway)

        self.assertEqual(result, {"action": "skip"})
        self.assertNotIn("amount", handed_off[0]["parsed"])
        logged = "".join(str(call.args[0]) for call in opened().write.call_args_list)
        self.assertNotIn(secret_response, logged)
        self.assertNotIn("9876.54", logged)

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

    def test_date_normalization_accepts_be_and_gregorian_four_digit_dates(self):
        cases = (
            ("05/09/2026", "2026-09-05"),
            ("2026-09-05", "2026-09-05"),
            ("2500-01-01", "2500-01-01"),
            ("2569-09-05", "2569-09-05"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                normalized = self.module._normalize_ocr_result_for_handoff({
                    "parsed": {"date": value}
                })
                self.assertEqual(normalized["parsed"]["date"], expected)

        self.assertEqual(
            self.module._normalize_slip_date(
                "05/09/2569", buddhist_era=True
            ),
            "2026-09-05",
        )
        self.assertEqual(
            self.module._normalize_slip_date(
                "2500-01-01", buddhist_era=True
            ),
            "2500-01-01",
        )

    def test_date_normalization_uses_raw_response_and_rejects_unsafe_dates(self):
        normalized = self.module._normalize_ocr_result_for_handoff({
            "parsed": {},
            "raw_response": {"data": {"date": "05/09/2569"}},
        })
        self.assertEqual(normalized["parsed"]["date"], "2569-09-05")

        invalid_values = (
            "05/09/69",
            "05/09",
            "31/02/2569",
            "2026-02-29",
            "09/05/2026/extra",
            "5/9/2026",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                rejected = self.module._normalize_ocr_result_for_handoff({
                    "parsed": {"date": value}
                })
                self.assertNotIn("date", rejected["parsed"])

        ambiguous = self.module._normalize_ocr_result_for_handoff({
            "parsed": {"date": "05/09/2026"},
            "raw_response": {"data": {"date": "06/09/2026"}},
        })
        self.assertNotIn("date", ambiguous["parsed"])

    def test_ambiguous_structured_dates_do_not_call_key_extraction(self):
        raw_result = {
            "parsed": {"date": "05/09/2026"},
            "raw_response": {"data": {"date": "06/09/2026"}},
        }
        _, ambiguous = self.module._structured_date(raw_result)
        ocr_result = self.module._normalize_ocr_result_for_handoff(raw_result)
        with patch.object(
            self.module, "call_akson_date_extraction"
        ) as extraction:
            self.module._add_extracted_date(
                ocr_result, "synthetic.jpg", ambiguous=ambiguous
            )

        extraction.assert_not_called()
        self.assertNotIn("date", ocr_result["parsed"])

    def test_date_key_extraction_is_normalized_before_handoff(self):
        ocr_result = self.module._normalize_ocr_result_for_handoff({
            "parsed": {"reference_no": "SYNTHETIC-DATE-001", "amount": 1}
        })
        with patch.object(
            self.module,
            "call_akson_date_extraction",
            return_value="05/09/2569",
        ) as extraction:
            self.module._add_extracted_date(ocr_result, "synthetic.jpg")

        extraction.assert_called_once_with("synthetic.jpg")
        self.assertEqual(ocr_result["parsed"]["date"], "2026-09-05")

    def test_valid_structured_date_prevents_date_key_extraction_in_hook(self):
        cases = (
            {
                "akson_called": True,
                "parsed": {
                    "reference_no": "DATE-PARSED",
                    "amount": 1,
                    "date": "2026-09-05",
                },
            },
            {
                "akson_called": True,
                "parsed": {"reference_no": "DATE-RAW", "amount": 1},
                "raw_response": {"data": {"date": "2026-09-05"}},
            },
        )
        for index, ocr_result in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "staging"
                local_path = root / "uploads" / "synthetic.jpg"
                event = types.SimpleNamespace(
                    source=types.SimpleNamespace(
                        platform="telegram", chat_id="1001", user_id="2001"
                    ),
                    media_urls=[str(local_path)],
                    media_types=["image/jpeg"],
                    message_id=f"valid-date-{index}",
                )
                handed_off = []
                buttons = self._buttons_with_ingress(
                    lambda *args, **kwargs: handed_off.append(
                        kwargs["ocr_result"]
                    ) or {"transaction": {"transaction_id": "synthetic-id"}}
                )
                gateway = types.SimpleNamespace(adapters={
                    "telegram": types.SimpleNamespace(
                        _bot=types.SimpleNamespace(id="3001")
                    )
                })
                with patch.dict(
                    os.environ, self._staging_environment(root), clear=True
                ), patch.dict(
                    "sys.modules",
                    {"lekza_accounting_transaction_buttons": buttons},
                ), patch.object(
                    self.module, "_materialize_media", return_value=str(local_path)
                ), patch.object(
                    self.module, "call_akson_ocr", return_value=ocr_result
                ), patch.object(
                    self.module, "call_akson_date_extraction"
                ) as extraction, patch.object(
                    self.module.os, "makedirs"
                ), patch(
                    "builtins.open", mock_open()
                ):
                    result = self._image_hook()(event, gateway=gateway)

                self.assertEqual(result, {"action": "skip"})
                extraction.assert_not_called()
                self.assertEqual(
                    handed_off[0]["parsed"]["date"], "2026-09-05"
                )

    def test_reference_normalization_ignores_case_and_internal_space(self):
        spaced = self.module._normalize_ocr_result_for_handoff({
            "parsed": {"reference_no": " AbC 123 "}
        })
        compact = self.module._normalize_ocr_result_for_handoff({
            "parsed": {"reference_no": "abc123"}
        })
        self.assertEqual(
            spaced["parsed"]["reference_no"].upper(),
            compact["parsed"]["reference_no"].upper(),
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

            buttons = self._buttons_with_ingress(require_reference)
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
            ), patch.object(
                self.module, "call_akson_amount_extraction"
            ) as fallback, patch.object(self.module.os, "makedirs"), patch(
                "builtins.open", mock_open()
            ):
                result = self._image_hook()(event, gateway=gateway)

        self.assertEqual(result, {"action": "skip"})
        self.assertEqual(handed_off, ["SYNTHETIC-HANDOFF-001"])
        fallback.assert_not_called()

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
            buttons = self._buttons_with_ingress(
                lambda *args, **kwargs: (_ for _ in ()).throw(
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
            buttons = self._buttons_with_ingress(
                lambda *args, **kwargs: self.fail("handoff must not run")
            )
            with patch.dict(os.environ, environment, clear=True), patch.dict(
                "sys.modules", {"lekza_accounting_transaction_buttons": buttons}
            ), patch.object(
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
            staging_root = Path(temp) / "staging"
            environment = self._staging_environment(staging_root)
            materialized_path = staging_root / "uploads" / "downloaded.jpg"
            materialized_path.parent.mkdir(parents=True)
            materialized_path.write_bytes(b"\xff\xd8\xff\xe0synthetic-jpeg")
            event = types.SimpleNamespace(
                source=types.SimpleNamespace(
                    platform="telegram", chat_id="1001", user_id="2001"
                ),
                media_urls=[secret_url],
                media_types=["image/jpeg"],
                message_id="synthetic-message",
            )
            opened = mock_open()
            buttons = self._buttons_with_ingress(
                lambda *args, **kwargs: self.fail("handoff must not run")
            )
            with patch.dict(os.environ, environment, clear=True), patch.dict(
                "sys.modules", {"lekza_accounting_transaction_buttons": buttons}
            ), patch.object(
                self.module, "_materialize_media", return_value=str(materialized_path)
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
