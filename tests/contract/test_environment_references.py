from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class EnvironmentReferenceTests(unittest.TestCase):
    def test_aksonocr_key_is_read_from_environment(self):
        sources = [
            ROOT / "plugins/accounting-slip-bridge/__init__.py",
            ROOT / "skills/accounting/process-slip-pipeline/scripts/process_slip.py",
        ]
        for source in sources:
            text = source.read_text(encoding="utf-8")
            self.assertIn('os.getenv("AKSONOCR_API_KEY")', text)
            self.assertNotIn("AKSONOCR_API_KEY =", text)
