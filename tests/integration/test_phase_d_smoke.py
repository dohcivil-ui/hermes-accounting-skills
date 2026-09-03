import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
SMOKE_PATH = ROOT / "plugins/accounting-slip-bridge/phase_d_smoke.py"


class PhaseDSmokeTests(unittest.TestCase):
    def test_verifier_shares_one_refreshing_token_provider(self):
        provider = object()
        token_factory = Mock(return_value=provider)
        drive_adapter = Mock()
        drive_adapter.verify_upload.return_value = {"file_id": "drive-id"}
        sheets_adapter = Mock()
        sheets_adapter.find_transaction_row.return_value = "Transactions!A2"
        drive_factory = Mock(return_value=drive_adapter)
        sheets_factory = Mock(return_value=sheets_adapter)
        durable = {
            "transaction_id": "synthetic-transaction",
            "current_state": "confirmed",
            "retry_count": 1,
            "drive_file_id": "drive-id",
            "sheets_row_identity": "Transactions!A2",
        }
        pipeline = Mock()
        pipeline.save.side_effect = [dict(durable), dict(durable)]
        store = Mock()

        google_adapters = types.SimpleNamespace(
            RefreshingTokenProvider=types.SimpleNamespace(
                from_environment=token_factory
            ),
            GoogleDriveAdapter=types.SimpleNamespace(from_environment=drive_factory),
            GoogleSheetsAdapter=types.SimpleNamespace(from_environment=sheets_factory),
            ProductionSavePipeline=Mock(return_value=pipeline),
        )
        transaction_flow = types.SimpleNamespace(
            SQLiteStateStore=types.SimpleNamespace(
                from_environment=Mock(return_value=store)
            ),
            TransactionFlow=types.SimpleNamespace(
                from_environment=Mock(return_value=object())
            ),
        )
        staging_guard = types.SimpleNamespace(validate_staging_actor=Mock())

        with patch.dict(sys.modules, {
            "google_adapters": google_adapters,
            "staging_guard": staging_guard,
            "transaction_flow": transaction_flow,
        }):
            spec = importlib.util.spec_from_file_location(
                "lekza_phase_d_smoke_shared_provider", SMOKE_PATH
            )
            smoke = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(smoke)
            result = smoke.verify(
                "synthetic-transaction", "synthetic-chat", "synthetic-user", 1
            )

        self.assertTrue(result["ok"])
        token_factory.assert_called_once_with()
        drive_factory.assert_called_once_with(token_provider=provider)
        sheets_factory.assert_called_once_with(token_provider=provider)
        store.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
