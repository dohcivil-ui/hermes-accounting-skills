import importlib.util
import os
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
GUARD_PATH = ROOT / "plugins/accounting-slip-bridge/staging_guard.py"


def load_guard():
    spec = importlib.util.spec_from_file_location("lekza_test_staging_guard", GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StagingGuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = load_guard()
        self.environment = {
            "LEKZA_RUNTIME_ENV": "staging",
            "LEKZA_STAGING_RESOURCE_ACK": "designated-staging-resources",
            "LEKZA_STAGING_TELEGRAM_BOT_ACK": "designated-test-bot",
            "AKSONOCR_API_KEY": "synthetic-key",
            "LEKZA_GOOGLE_ACCESS_TOKEN": "synthetic-token",
            "LEKZA_SLIP_FOLDER_ID": "staging-folder",
            "LEKZA_ACCOUNTING_SPREADSHEET_ID": "staging-sheet",
            "LEKZA_PRODUCTION_SLIP_FOLDER_ID": "production-folder",
            "LEKZA_PRODUCTION_ACCOUNTING_SPREADSHEET_ID": "production-sheet",
            "LEKZA_TRANSACTION_STATE_DB": str(ROOT / "synthetic-state.db"),
            "LEKZA_ALLOWED_UPLOAD_ROOTS": str(ROOT / "synthetic-uploads"),
            "LEKZA_ACTIVE_PROJECTS_JSON": '["Synthetic Project"]',
            "LEKZA_STAGING_TELEGRAM_CHAT_IDS": "1001,1002",
            "LEKZA_STAGING_TELEGRAM_USER_IDS": "2001",
        }

    def test_designated_staging_configuration_is_accepted(self):
        config = self.guard.validate_staging_environment(self.environment)
        self.assertEqual(config["runtime_env"], "staging")
        self.assertNotIn("synthetic-key", repr(config))
        self.assertNotIn("synthetic-token", repr(config))

    def test_production_google_resource_ids_are_rejected(self):
        self.environment["LEKZA_SLIP_FOLDER_ID"] = "production-folder"
        with self.assertRaisesRegex(ValueError, "must differ from production"):
            self.guard.validate_staging_environment(self.environment)

    def test_missing_production_ids_fail_closed(self):
        del self.environment["LEKZA_PRODUCTION_SLIP_FOLDER_ID"]
        with self.assertRaisesRegex(ValueError, "is required for staging"):
            self.guard.validate_staging_environment(self.environment)

    def test_wrong_chat_or_user_is_rejected(self):
        with self.assertRaisesRegex(PermissionError, "chat"):
            self.guard.validate_staging_actor("9999", "2001", self.environment)
        with self.assertRaisesRegex(PermissionError, "user"):
            self.guard.validate_staging_actor("1001", "9999", self.environment)

    def test_relative_db_and_upload_paths_are_rejected(self):
        self.environment["LEKZA_TRANSACTION_STATE_DB"] = "state.db"
        with self.assertRaisesRegex(ValueError, "must be absolute"):
            self.guard.validate_staging_environment(self.environment)
        self.environment["LEKZA_TRANSACTION_STATE_DB"] = str(ROOT / "state.db")
        self.environment["LEKZA_ALLOWED_UPLOAD_ROOTS"] = "uploads"
        with self.assertRaisesRegex(ValueError, "absolute paths"):
            self.guard.validate_staging_environment(self.environment)


if __name__ == "__main__":
    unittest.main()
