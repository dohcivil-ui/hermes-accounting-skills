"""Synthetic labeled OCR regressions; no external services."""
import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "labeled_bridge", ROOT / "plugins/accounting-slip-bridge/__init__.py")
BRIDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BRIDGE)


class LabeledOCRTests(unittest.TestCase):
    def extract(self, text, parsed=None):
        original = {"raw_ocr_text": text, "parsed": parsed or {}}
        before = copy.deepcopy(original)
        result = dict(original["parsed"])
        with patch.object(BRIDGE.requests, "post", side_effect=AssertionError("network")):
            BRIDGE._add_labeled_text_fields(result, original)
        self.assertEqual(original, before)
        return result

    def test_synthetic_multiline_receipt(self):
        result = self.extract("Transfer amount\n7.25 THB\nDate\n2026-09-18\n"
                              "Payer\nTest Sender\nPayee\nTest Receiver")
        self.assertEqual(result, dict(amount="7.25", date="2026-09-18",
                                      payer="Test Sender", payee="Test Receiver"))

    def test_thai_markdown_labels(self):
        self.assertEqual(self.extract("**จำนวนเงิน:** 1,250.50 บาท\n"
                                     "วันที่: 2026-09-18\nผู้โอน: ผู้ทดสอบ\nผู้รับเงิน: ร้านทดสอบ"),
                         dict(amount="1250.50", date="2026-09-18",
                              payer="ผู้ทดสอบ", payee="ร้านทดสอบ"))

    def test_preserve_structured_values(self):
        fields = dict(amount=9, date="invalid", payer="Original", payee="Original")
        self.assertEqual(self.extract("Amount: 7.25\nDate: 2026-09-18\n"
                                      "Payer: Other\nPayee: Other", fields), fields)

    def test_conflicting_values_remain_missing(self):
        self.assertEqual(self.extract("Amount: 1\nAmount: 2\nDate: 2026-09-18\n"
                                      "Date: 2026-09-19\nPayee: One\nPayee: Two"), {})

    def test_invalid_unlabeled_and_unsupported_layouts_remain_missing(self):
        for text in ("7.25\n2026-09-18\nTest Receiver", "Date: 2026-02-30",
                     "Amount: -2", "Amount: 1.234", "Payee\nDate\n2026-09-18",
                     "| Payee | Receiver |", "Payee: Date: 2026-09-18"):
            with self.subTest(text=text):
                result = self.extract(text)
                self.assertNotIn("payee", result)
                if not text.startswith("Payee\nDate"):
                    self.assertEqual(result, {})

    def test_malformed_alternative_is_not_ignored(self):
        self.assertEqual(self.extract("Date: 2026-09-18\nDate: unclear"), {})
        self.assertEqual(self.extract("Amount: 7.25\nAmount: unclear"), {})
        self.assertEqual(self.extract("Payee\nTime\n22:00"), {})

    def test_raw_pages_and_handoff(self):
        response = {"raw_response": {"pages": [{"markdown":
                    "Payer: Sender\nPayee: Receiver\nDate: 2026-09-18"}]}}
        result = BRIDGE._normalize_ocr_result_for_handoff(response)
        self.assertEqual(result["parsed"]["payee"], "Receiver")
        self.assertEqual(result["parsed"]["date"], "2026-09-18")
