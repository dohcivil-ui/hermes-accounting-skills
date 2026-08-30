from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class RuntimeLayoutTests(unittest.TestCase):
    def test_required_runtime_sources_exist(self):
        required = [
            "skills/accounting/accounting-button-flow/SKILL.md",
            "skills/accounting/human-communication-style/SKILL.md",
            "skills/accounting/manage-transaction-data/SKILL.md",
            "skills/accounting/multiuser-memory/SKILL.md",
            "skills/accounting/process-slip-pipeline/SKILL.md",
            "skills/accounting/process-slip-pipeline/scripts/process_slip.py",
            "skills/accounting/query-payee-summary/SKILL.md",
            "skills/accounting/scheduled-project-report/SKILL.md",
            "plugins/accounting-slip-bridge/__init__.py",
            "plugins/accounting-slip-bridge/transaction_flow.py",
            "plugins/accounting-slip-bridge/telegram_wiring.py",
            "plugins/accounting-slip-bridge/plugin.yaml",
            "plugins/accounting-transaction-buttons/__init__.py",
            "plugins/accounting-transaction-buttons/plugin.yaml",
            "plugins/telegram-clarify-pretty/__init__.py",
            "plugins/telegram-clarify-pretty/plugin.yaml",
        ]

        missing = [path for path in required if not (ROOT / path).is_file()]
        self.assertEqual(missing, [])

    def test_old_accounting_layout_is_absent(self):
        self.assertFalse((ROOT / "accounting").exists())
