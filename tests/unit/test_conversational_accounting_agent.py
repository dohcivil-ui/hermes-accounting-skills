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
        self.assertEqual(follow_up["context"]["project"], "P-AIRPORT")
        self.assertEqual(
            explanation["result"]["by_category"][0],
            {"name": "materials", "amount": "1000.25", "transaction_count": 1},
        )

    def test_follow_up_keeps_exact_project_id_when_names_or_ids_overlap(self):
        for other_id, other_name in (
            ("P-B", "SYNTHETIC JOB"),
            ("P-B", "P-A"),
            ("p-a", "Other Job"),
        ):
            with self.subTest(other_id=other_id, other_name=other_name):
                projects = [
                    {"project_id": "P-A", "project_name": "Synthetic Job"},
                    {"project_id": other_id, "project_name": other_name},
                ]
                rows = [
                    self.transaction(
                        "T-SELECTED", "P-A", "Synthetic Job", "expense", "50.00",
                        payer="Company", payee="Vendor", category="materials",
                    ),
                    self.transaction(
                        "T-OTHER", other_id, other_name, "expense", "400.00",
                        payer="Company", payee="Vendor", category="materials",
                    ),
                ]
                engine = self.module.AccountingQueryEngine(FakeReader(projects, rows))
                first = engine.query(
                    {"intent": "totals", "project": "P-A", "metric": "expense"},
                    now=self.now,
                )
                follow_up = engine.query(
                    {"intent": "follow_up", "previous_context": first["context"]},
                    now=self.now,
                )
                explanation = engine.query(
                    {"intent": "explain", "previous_context": follow_up["context"]},
                    now=self.now,
                )
                self.assertEqual(first["result"]["value"], "50.00")
                self.assertEqual(follow_up["result"]["value"], "50.00")
                self.assertEqual(follow_up["context"]["project"], "P-A")
                self.assertEqual(
                    [row["transaction_id"] for row in explanation["result"]["largest_transactions"]],
                    ["T-SELECTED"],
                )

    def test_follow_up_survives_rename_and_allows_project_switch_or_clear(self):
        first = self.engine.query(
            {"intent": "totals", "project": "P-AIRPORT", "metric": "expense"},
            now=self.now,
        )
        self.projects[0]["project_name"] = "Renamed Airport"
        renamed = self.engine.query(
            {"intent": "follow_up", "previous_context": first["context"]},
            now=self.now,
        )
        self.assertEqual(renamed["result"]["value"], "1500.00")
        self.assertEqual(renamed["result"]["projects"][0]["project_name"], "Renamed Airport")
        switched = self.engine.query(
            {"intent": "follow_up", "project": "P-HOUSE",
             "previous_context": renamed["context"]}, now=self.now,
        )
        self.assertEqual(switched["result"]["value"], "700.00")
        cleared = self.engine.query(
            {"intent": "follow_up", "project": None,
             "previous_context": switched["context"]}, now=self.now,
        )
        self.assertEqual(cleared["result"]["value"], "2200.00")
        self.assertIsNone(cleared["context"]["project"])

    def test_legacy_name_context_and_projects_without_ids_remain_supported(self):
        for project_id in ("P-LEGACY", ""):
            with self.subTest(project_id=project_id):
                reader = FakeReader(
                    [{"project_id": project_id, "project_name": "Legacy Job"}],
                    [self.transaction(
                        "T-LEGACY", project_id, "Legacy Job", "expense", "75.00",
                        payer="Company", payee="Vendor", category="materials",
                    )],
                )
                engine = self.module.AccountingQueryEngine(reader)
                result = engine.query(
                    {"intent": "follow_up", "previous_context": {
                        "intent": "totals", "project": "Legacy Job",
                        "period": "today", "metric": "expense",
                    }}, now=self.now,
                )
                self.assertEqual(result["result"]["value"], "75.00")
                self.assertEqual(result["context"]["project"], project_id or "Legacy Job")

    def test_totals_count_includes_both_types_for_every_metric(self):
        for metric, value in (
            ("expense", "1500.00"), ("income", "5000.00"),
            ("net", "3500.00"), ("all", None),
        ):
            with self.subTest(metric=metric):
                result = self.engine.query(
                    {"intent": "totals", "project": "P-AIRPORT", "metric": metric},
                    now=self.now,
                )
                self.assertEqual(result["result"]["value"], value)
                # Two expenses plus one income; metric selects value, not count.
                self.assertEqual(result["result"]["transaction_count"], 3)
                self.assertEqual(result["result"]["projects"][0]["transaction_count"], 3)
                self.assertEqual(result["grounding"]["matched_transaction_count"], 3)

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

    def test_totals_apply_party_filter_to_amounts_counts_and_projects(self):
        result = self.engine.query(
            {"intent": "totals", "metric": "expense", "party": "ร้านเหล็ก",
             "party_role": "payee"}, now=self.now,
        )

        self.assertEqual(result["result"]["value"], "1700.25")
        self.assertEqual(result["result"]["income"], "0.00")
        self.assertEqual(result["result"]["net"], "-1700.25")
        self.assertEqual(result["result"]["transaction_count"], 2)
        self.assertEqual(result["grounding"]["matched_transaction_count"], 2)
        projects = {row["project_id"]: row for row in result["result"]["projects"]}
        self.assertEqual(projects["P-AIRPORT"]["expense"], "1000.25")
        self.assertEqual(projects["P-HOUSE"]["expense"], "700.00")
        self.assertEqual(projects["P-CLOSED"]["transaction_count"], 0)

    def test_party_summary_respects_each_metric(self):
        self.transactions.append(self.transaction(
            "T-PARTY-IN", "P-AIRPORT", "งานสนามบิน", "income", "300.00",
            payee="ร้านเหล็ก", payer="ลูกค้า B", category="installment",
        ))
        for metric, amount, count in (
            ("expense", "1700.25", 2), ("income", "300.00", 1),
            ("net", "-1400.25", 3), ("all", "2000.25", 3),
        ):
            with self.subTest(metric=metric):
                result = self.engine.query(
                    {"intent": "party_summary", "party": "ร้านเหล็ก",
                     "party_role": "payee", "metric": metric}, now=self.now,
                )
                self.assertEqual(result["result"]["summaries"], [{
                    "role": "payee", "name": "ร้านเหล็ก", "amount": amount,
                    "transaction_count": count,
                }])

    def test_explain_preserves_party_filter_from_previous_context(self):
        first = self.engine.query(
            {"intent": "party_summary", "party": "ร้านเหล็ก",
             "party_role": "payee", "metric": "expense"}, now=self.now,
        )
        result = self.engine.query(
            {"intent": "explain", "previous_context": first["context"]},
            now=self.now,
        )

        self.assertEqual(result["result"]["by_payee"], [{
            "name": "ร้านเหล็ก", "amount": "1700.25", "transaction_count": 2,
        }])
        self.assertEqual(
            {row["transaction_id"] for row in result["result"]["largest_transactions"]},
            {"T-1", "T-3"},
        )
        self.assertEqual(result["grounding"]["matched_transaction_count"], 2)

    def test_party_totals_support_payer_and_count_both_roles_once(self):
        self.transactions[:] = [self.transaction(
            "T-SELF", "P-AIRPORT", "งานสนามบิน", "expense", "100.00",
            payee="คนเดียวกัน", payer="คนเดียวกัน", category="materials",
        )]
        for role in ("payer", "payee", "both"):
            with self.subTest(role=role):
                result = self.engine.query(
                    {"intent": "totals", "metric": "expense",
                     "party": "คนเดียวกัน", "party_role": role}, now=self.now,
                )
                self.assertEqual(result["result"]["value"], "100.00")
                self.assertEqual(result["result"]["transaction_count"], 1)

    def test_unknown_or_ambiguous_party_fails_for_totals_and_explain(self):
        self.transactions.append(self.transaction(
            "T-OTHER-SHOP", "P-AIRPORT", "งานสนามบิน", "expense", "10.00",
            payee="ร้านไม้", payer="Lekza", category="materials",
        ))
        for intent in ("totals", "explain"):
            for party, message in (("ไม่มีชื่อนี้", "Unknown"), ("ร้าน", "Ambiguous")):
                with self.subTest(intent=intent, party=party):
                    with self.assertRaisesRegex(self.module.AccountingQueryError, message):
                        self.engine.query(
                            {"intent": intent, "party": party, "party_role": "payee",
                             "previous_context": {"intent": "totals"}}, now=self.now,
                        )

    def test_project_id_precedes_conflicting_name_for_totals_and_explain(self):
        self.transactions.append(self.transaction(
            "T-CONFLICT", "P-HOUSE", "งานสนามบิน", "expense", "300.00",
            payee="ร้านเหล็ก", payer="Lekza", category="materials",
        ))
        for project, amount, count, ids in (
            ("P-AIRPORT", "1500.00", 3, {"T-1", "T-2"}),
            ("P-HOUSE", "1000.00", 2, {"T-3", "T-CONFLICT"}),
        ):
            with self.subTest(project=project):
                totals = self.engine.query(
                    {"intent": "totals", "project": project, "metric": "expense"},
                    now=self.now,
                )
                explanation = self.engine.query(
                    {"intent": "explain", "previous_context": totals["context"]},
                    now=self.now,
                )
                self.assertEqual(totals["result"]["value"], amount)
                self.assertEqual(totals["grounding"]["matched_transaction_count"], count)
                self.assertEqual(
                    {row["transaction_id"] for row in explanation["result"]["largest_transactions"]},
                    ids,
                )
                if project == "P-HOUSE":
                    self.assertTrue(all(
                        row["project"] == "บ้านคุณสมชาย"
                        for row in explanation["result"]["largest_transactions"]
                    ))

    def test_project_name_fallback_matches_existing_aggregator(self):
        for project_id in ("", "P-UNKNOWN"):
            with self.subTest(project_id=project_id):
                self.transactions[:] = [self.transaction(
                    "T-FALLBACK", project_id, "งานสนามบิน", "expense", "50.00",
                    payee="ร้านเหล็ก", payer="Lekza", category="materials",
                )]
                result = self.engine.query(
                    {"intent": "totals", "project": "P-AIRPORT", "metric": "expense"},
                    now=self.now,
                )
                self.assertEqual(result["result"]["value"], "50.00")
                self.assertEqual(result["grounding"]["matched_transaction_count"], 1)

    def test_project_count_does_not_parse_transaction_values(self):
        for field, value in (("amount", "bad"), ("date", "not-a-date"), ("status", "unknown")):
            with self.subTest(field=field):
                original = self.transactions[0][field]
                self.transactions[0][field] = value
                try:
                    result = self.engine.query({"intent": "project_count"}, now=self.now)
                    self.assertEqual(result["result"], {"total": 3, "active": 2})
                    self.assertEqual(result["grounding"]["source"], "Projects")
                    self.assertNotIn("matched_transaction_count", result["grounding"])
                    self.assertNotIn("status", result["grounding"])
                finally:
                    self.transactions[0][field] = original

    def test_project_count_still_rejects_invalid_project_master(self):
        for invalid in (dict(self.projects[0]), {"project_id": "P-BAD", "project_name": ""}):
            with self.subTest(invalid=invalid):
                self.projects.append(invalid)
                try:
                    with self.assertRaises(self.module._reporting_module().MalformedSheetRowError):
                        self.engine.query({"intent": "project_count"}, now=self.now)
                finally:
                    self.projects.pop()


if __name__ == "__main__":
    unittest.main()
