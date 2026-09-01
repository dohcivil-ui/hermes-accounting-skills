import importlib.util
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = ROOT / "tests/tools/aksonocr_shape_capture.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("lekza_shape_capture", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AksonOcrShapeCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tool = load_tool()

    def test_capture_emits_shape_and_markdown_metadata_without_values(self):
        sensitive_values = (
            "synthetic-api-secret-value",
            "https://example.invalid/private-slip.jpg",
            "PHASED-SMOKE-20260901-01",
            "987654321",
            "synthetic customer OCR text",
        )
        response = {
            "request_id": sensitive_values[3],
            "token": sensitive_values[0],
            "source_url": sensitive_values[1],
            "pages": [
                {
                    "markdown": (
                        "synthetic customer OCR text\n"
                        "**หมายเลขอ้างอิง:**\n  **PHASED-SMOKE-20260901-01**"
                    ),
                    "width": 1080,
                },
                {
                    "markdown": (
                        "Reference **Number:** **PHASED-SMOKE-20260901-01**"
                    )
                },
            ],
            "parsed": {"reference_no": sensitive_values[2], "amount": 1.0},
        }

        evidence = self.tool.capture_shape(response)
        rendered = json.dumps(evidence, ensure_ascii=False)

        for value in sensitive_values:
            self.assertNotIn(value, rendered)
        self.assertIn("<sensitive-key>", evidence["top_level_keys"])
        self.assertEqual(evidence["pages_shapes"][0]["path"], "$.pages")
        self.assertEqual(
            evidence["reference_locations"],
            [{"path": "$.parsed.reference_no", "value_type": "string"}],
        )
        markdown = evidence["markdown_reference_shapes"][0]
        self.assertEqual(markdown["label_language"], "thai")
        self.assertTrue(markdown["label_emphasis"])
        self.assertTrue(markdown["value_emphasis"])
        self.assertTrue(markdown["whitespace_after_separator"]["has_newline"])
        self.assertEqual(markdown["value"], "<REFERENCE_NO>")
        self.assertEqual(
            {item["label_language"] for item in evidence["markdown_reference_shapes"]},
            {"thai", "english"},
        )

    def test_unlabeled_numbers_do_not_create_reference_metadata(self):
        evidence = self.tool.capture_shape({
            "pages": [{"markdown": "วันที่: 2026-09-01\nจำนวนเงิน: 1.00"}],
            "parsed": {"amount": 1.0},
        })

        self.assertEqual(evidence["reference_locations"], [])
        self.assertEqual(evidence["markdown_reference_shapes"], [])

    def test_validator_rejects_value_bearing_evidence(self):
        with self.assertRaisesRegex(ValueError, "forbidden value"):
            self.tool.validate_evidence({
                "markdown_reference_shapes": [],
                "leak": "https://example.invalid/private",
            })


if __name__ == "__main__":
    unittest.main()
