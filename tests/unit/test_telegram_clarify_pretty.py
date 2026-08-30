import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "plugins/telegram-clarify-pretty/__init__.py"


def load_module():
    spec = importlib.util.spec_from_file_location("lekza_clarify_pretty", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TelegramClarifyPrettyTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_compact_label_collapses_whitespace(self):
        self.assertEqual(self.module._compact_label("  งาน   A  "), "งาน A")

    def test_compact_label_uses_default_for_blank_value(self):
        self.assertEqual(self.module._compact_label("   "), "ตัวเลือก")

    def test_compact_label_truncates_with_ellipsis(self):
        label = self.module._compact_label("x" * 40, max_chars=10)
        self.assertEqual(label, "x" * 9 + "…")
