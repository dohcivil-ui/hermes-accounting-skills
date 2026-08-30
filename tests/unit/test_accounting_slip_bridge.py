import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch


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
