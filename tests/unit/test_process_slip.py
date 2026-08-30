import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "skills/accounting/process-slip-pipeline/scripts/process_slip.py"


def load_module():
    spec = importlib.util.spec_from_file_location("lekza_process_slip", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProcessSlipTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_missing_api_key_exits_without_network(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            self.module.requests, "post"
        ) as post, self.assertRaises(SystemExit) as raised:
            self.module.process_slip("synthetic.jpg")

        self.assertEqual(raised.exception.code, 1)
        post.assert_not_called()

    def test_success_outputs_akson_result(self):
        response = Mock(status_code=201)
        response.json.return_value = {
            "confidence": 0.98,
            "pages": [{"markdown": "SYNTHETIC OCR RESULT"}],
            "parsed": {"amount": 100.0},
            "usage": {"pages": 1},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "synthetic.jpg"
            image.write_bytes(b"synthetic image bytes")
            with patch.dict(
                os.environ, {"AKSONOCR_API_KEY": "synthetic-test-key"}, clear=True
            ), patch.object(
                self.module.requests, "post", return_value=response
            ), patch("builtins.print") as output:
                self.module.process_slip(str(image))

        payload = json.loads(output.call_args.args[0])
        self.assertTrue(payload["akson_called"])
        self.assertEqual(payload["confidence"], 0.98)
        self.assertEqual(payload["raw_ocr_text"], "SYNTHETIC OCR RESULT")
