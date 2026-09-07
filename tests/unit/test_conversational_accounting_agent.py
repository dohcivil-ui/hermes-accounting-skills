from datetime import datetime
import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = (
    ROOT
    / "skills/accounting/conversational-accounting-agent/scripts/query_accounting.py"
)


def load_module():
    name = "lekza_test_conversational_accounting_agent"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeReader:
    def __init__(self, projects, transactions):
        self.projects = projects
        self.transactions = transactions
        self.calls = 0

    def read(self):
        self.calls += 1
        return list(self.projects), list(self.transactions)


class ConversationalAccountingAgentTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.now = datetime.fromisoformat("2026-09-07T12:00:00+07:00")
        self.projects = [
            {"project_id": "P-AIRPORT", "project_name": "งานสนามบิน", "status": "active"},
            {"project_id": "P-HOUSE", "project_name": "บ้านคุณสมชาย", "status": "active"},
            {"project_id": "P-CLOSED", "project_name": "งานปิดแล้ว", "status": "closed"},
        ]
        self.transactions = [
            self.transaction(
                "T-1", "P-AIRPORT", "งานสนามบิน", "expense", "1000.25",
                payee="ร้านเหล็ก", payer="Lekza", category="materials",
            ),
            self.transaction(
                "T-2", "P-AIRPORT", "งานสนามบิน", "expense", 499.75,
                payee="ช่างเอ", payer="Lekza", category="labor",
            ),
            self.transaction(
                "T-3", "P-HOUSE", "บ้านคุณสมชาย", "expense", 700,
                payee="ร้านเหล็ก", payer="Lekza", category="materials",
            ),
            self.transaction(
                "T-4", "P-AIRPORT", "งานสนามบิน", "income", 5000,
                payee="Lekza", payer="ลูกค้า A", category="installment",
            ),
            self.transaction(
                "T-DELETED", "P-AIRPORT", "งานสนามบิน", "expense", 99999,
                payee="ร้านเหล็ก", payer="Lekza", category="materials", status="deleted",
            ),
            self.transaction(
                "T-OLD", "P-AIRPORT", "งานสนามบิน", "expense", 8000,
                payee="ร้านเหล็ก", payer="Lekza", category="materials",
                transaction_date="2026-08-01",
            ),
        ]
        self.reader = FakeReader(self.projects, self.transactions)
        self.engine = self.module.AccountingQueryEngine(self.reader)

    @staticmethod
    def transaction(
        transaction_id, project_id, project, transaction_type, amount, *,
        payee, payer, category, status="confirmed", transaction_date="2026-09-07",
    ):
        return {
            "transaction_id": transaction_id,
            "reference_no": f"REF-{transaction_id}",
            "date": transaction_date,
            "payer": payer,
            "payee": payee,
            "project_id": project_id,
            "project": project,
            "type": transaction_type,
            "category": category,
            "amount": amount,
            "status": status,
        }

    def test_natural_accounting_question_uses_grounded_today_expense_plan(self):
        # Hermes maps "วันนี้จ่ายเท่าไหร่" to this structured plan.
        result = self.engine.query(
            {"intent": "totals", "period": "today", "metric": "expense"},
            now=self.now,
        )

        self.assertEqual(result["result"]["value"], "2200.00")
        self.assertEqual(result["result"]["transaction_count"], 4)
        self.assertEqual(result["grounding"]["matched_transaction_count"], 4)
        self.assertTrue(result["read_only"])

    def test_conversational_follow_up_inherits_period_and_metric(self):
        first = self.engine.query(
            {"intent": "totals", "period": "today", "metric": "expense"},
            now=self.now,
        )
        follow_up = self.engine.query(
            {
                "intent": "follow_up",
                "project": "สนามบิน",
                "previous_context": first["context"],
            },
            now=self.now,
        )
        explanation = self.engine.query(
            {"intent": "explain", "previous_context": follow_up["context"]},
            now=self.now,
        )

        self.assertEqual(follow_up["result"]["value"], "1500.00")
        self.assertEqual(follow_up["context"]["period"], "today")
        self.assertEqual(follow_up["context"]["metric"], "expense")
        self.assertEqual(follow_up["context"]["project"], "งานสนามบิน")
        self.assertEqual(
            explanation["result"]["by_category"][0],
            {"name": "materials", "amount": "1000.25", "transaction_count": 1},
        )

    def test_grounded_party_summary_excludes_deleted_and_out_of_period_rows(self):
        result = self.engine.query(
            {
                "intent": "party_summary",
                "period": "month",
                "party_role": "payee",
                "party": "ร้านเหล็ก",
            },
            now=self.now,
        )

        self.assertEqual(
            result["result"]["summaries"],
            [{
                "role": "payee",
                "name": "ร้านเหล็ก",
                "amount": "1700.25",
                "transaction_count": 2,
            }],
        )
        self.assertEqual(result["grounding"]["status"], "confirmed")
        self.assertEqual(self.reader.calls, 1)

    def test_ai_cannot_request_transaction_mutation(self):
        for intent in ("create", "update", "delete", "confirm"):
            with self.subTest(intent=intent):
                with self.assertRaisesRegex(self.module.AccountingQueryError, "read-only"):
                    self.engine.query({"intent": intent}, now=self.now)
        self.assertEqual(self.reader.calls, 0)

    def test_project_count_is_read_only_and_deterministic(self):
        result = self.engine.query({"intent": "project_count"}, now=self.now)

        self.assertEqual(result["result"], {"total": 3, "active": 2})
        self.assertTrue(result["read_only"])


if __name__ == "__main__":
    unittest.main()
