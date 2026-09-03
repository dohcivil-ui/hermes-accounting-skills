import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "skills/accounting/scheduled-project-report/scripts/scheduled_reporting.py"


def load_module():
    name = "lekza_scheduled_reporting_delivery"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class ReadOnlySheetsSession:
    def __init__(self, reporting):
        self.reporting = reporting
        self.calls = []
        self.schemas = {
            "Transactions": list(reporting.TRANSACTIONS_SCHEMA),
            "Projects": list(reporting.PROJECTS_SCHEMA),
        }
        self.transactions = [
            list(reporting.TRANSACTIONS_SCHEMA),
            ["tx-1", "ref", "2026-09-03", "ผู้จ่าย", "ผู้รับ", "p1", "บ้าน", "income", "", 500, "", 0.9, "u1", "", "", "confirmed", "", ""],
        ]
        self.projects = [
            list(reporting.PROJECTS_SCHEMA),
            ["p1", "บ้าน", "ลูกค้า", "active", "2026-01-01", "u1", ""],
            ["p2", "โกดัง", "ลูกค้า", "active", "2026-02-01", "u1", ""],
        ]

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        encoded_range = url.split("/values/", 1)[1]
        if encoded_range.startswith("Transactions!1%3A1"):
            return FakeResponse(200, {"values": [self.schemas["Transactions"]]})
        if encoded_range.startswith("Projects!1%3A1"):
            return FakeResponse(200, {"values": [self.schemas["Projects"]]})
        if encoded_range.startswith("Transactions!A%3AR"):
            return FakeResponse(200, {"values": self.transactions})
        if encoded_range.startswith("Projects!A%3AG"):
            return FakeResponse(200, {"values": self.projects})
        raise AssertionError(url)

    def post(self, *args, **kwargs):
        raise AssertionError("reporting path must never write Google Sheets")


class FakeSender:
    def __init__(self):
        self.sent = []
        self.fail_once = False
        self.ambiguous_once = False

    def send_text(self, destination, text):
        self.sent.append(("text", destination, text))
        if self.fail_once:
            self.fail_once = False
            raise reporting.DefinitiveDeliveryError("synthetic rejected request")
        if self.ambiguous_once:
            self.ambiguous_once = False
            raise OSError("synthetic connection loss after send")
        return f"message-{len(self.sent)}"

    def send_document(self, destination, path, caption):
        self.sent.append(("document", destination, Path(path).suffix, caption))
        return f"document-{len(self.sent)}"


class FakeArtifacts:
    def __init__(self, root):
        self.root = Path(root)

    def build_html(self, report):
        path = self.root / f"{report.report_type}-{report.period.key}.html"
        path.write_bytes(reporting.render_html(report))
        return path

    def build_monthly_pdf(self, report):
        path = self.root / f"{report.period.key}.pdf"
        path.write_bytes(b"%PDF-1.4 synthetic")
        return path


reporting = load_module()


class ReportingSheetsReaderTests(unittest.TestCase):
    def test_validates_frozen_schemas_then_reads_without_writes(self):
        session = ReadOnlySheetsSession(reporting)
        reader = reporting.ReportingSheetsReader(
            "sheet-1", lambda: "token", session=session
        )
        projects, transactions = reader.read()
        self.assertEqual([row["project_name"] for row in projects], ["บ้าน", "โกดัง"])
        self.assertEqual(transactions[0]["amount"], 500)
        self.assertTrue(all(method == "get" for method, _, _ in session.calls))

    def test_incompatible_schema_fails_before_data_read(self):
        session = ReadOnlySheetsSession(reporting)
        session.schemas["Transactions"][9] = "wrong_amount"
        reader = reporting.ReportingSheetsReader("sheet-1", lambda: "token", session=session)
        with self.assertRaises(reporting.MalformedSheetRowError):
            reader.read()
        self.assertEqual(len(session.calls), 1)

    def test_malformed_confirmed_sheet_row_fails_before_delivery(self):
        session = ReadOnlySheetsSession(reporting)
        session.transactions[1][9] = "500"
        reader = reporting.ReportingSheetsReader("sheet-1", lambda: "token", session=session)
        sender = FakeSender()
        with tempfile.TemporaryDirectory() as directory:
            runner = reporting.ReportRunner(
                reader,
                sender,
                reporting.DeliveryLedger(Path(directory) / "ledger.sqlite3"),
                FakeArtifacts(directory),
                destination="telegram:synthetic-chat",
            )
            with self.assertRaises(reporting.MalformedSheetRowError):
                runner.run("daily", datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc))
        self.assertEqual(sender.sent, [])


class TelegramSenderTests(unittest.TestCase):
    def test_sends_text_and_document_to_configured_destination(self):
        class Session:
            def __init__(self):
                self.calls = []

            def post(inner_self, url, **kwargs):
                inner_self.calls.append((url, kwargs))
                return FakeResponse(200, {"ok": True, "result": {"message_id": len(inner_self.calls)}})

        session = Session()
        sender = reporting.TelegramSender(
            "synthetic-token", "-100123", thread_id="7", session=session
        )
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "report.html"
            document.write_text("รายงาน", encoding="utf-8")
            sender.send_text(sender.destination, "สรุป")
            sender.send_document(sender.destination, document, "รายงาน HTML")
        self.assertTrue(session.calls[0][0].endswith("/sendMessage"))
        self.assertTrue(session.calls[1][0].endswith("/sendDocument"))
        self.assertEqual(session.calls[0][1]["data"]["message_thread_id"], "7")

    def test_http_rejection_is_a_definitive_failure(self):
        class Session:
            def post(self, url, **kwargs):
                return FakeResponse(400, {"ok": False})

        sender = reporting.TelegramSender("synthetic-token", "123", session=Session())
        with self.assertRaises(reporting.DefinitiveDeliveryError):
            sender.send_text(sender.destination, "สรุป")


class DeliveryLedgerLeaseTests(unittest.TestCase):
    def test_expired_unattempted_lease_is_reclaimed_but_attempted_item_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            ticks = [100.0]
            ledger = reporting.DeliveryLedger(
                Path(directory) / "ledger.sqlite3",
                lease_seconds=10,
                clock=lambda: ticks[0],
            )
            key = ledger.ensure("daily", "2026-09-03", "telegram:1", "telegram_text", 0)
            first = ledger.claim(key)
            self.assertIsNotNone(first)
            ticks[0] = 111.0
            second = ledger.claim(key)
            self.assertIsNotNone(second)
            ledger.mark_external_attempt(second)
            ticks[0] = 122.0
            self.assertIsNone(ledger.claim(key))

    def test_delivered_item_is_never_reclaimed(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = reporting.DeliveryLedger(Path(directory) / "ledger.sqlite3")
            key = ledger.ensure("monthly", "2026-09", "telegram:1", "pdf_attachment", 0)
            claim = ledger.claim(key)
            ledger.mark_external_attempt(claim)
            ledger.mark_delivered(claim, "message-1")
            self.assertIsNone(ledger.claim(key))


class ReportRunnerDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger_path = Path(self.temp.name) / "delivery.sqlite3"
        self.sender = FakeSender()
        session = ReadOnlySheetsSession(reporting)
        self.reader = reporting.ReportingSheetsReader("sheet-1", lambda: "token", session=session)
        self.artifacts = FakeArtifacts(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, *, telegram_limit=4096):
        return reporting.ReportRunner(
            self.reader,
            self.sender,
            reporting.DeliveryLedger(self.ledger_path, lease_seconds=30),
            self.artifacts,
            destination="telegram:synthetic-chat",
            telegram_limit=telegram_limit,
        )

    def test_daily_restart_suppresses_duplicate_text_chunks(self):
        now = datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc)
        first = self.runner().run("daily", now)
        second = self.runner().run("daily", now)
        self.assertEqual(first.delivered, 1)
        self.assertEqual(second.delivered, 0)
        self.assertEqual([item[0] for item in self.sender.sent], ["text"])

    def test_weekly_sends_summary_and_html_once(self):
        now = datetime(2026, 9, 6, 14, 10, tzinfo=timezone.utc)
        self.runner().run("weekly", now)
        self.runner().run("weekly", now)
        self.assertEqual([item[0] for item in self.sender.sent], ["text", "document"])
        self.assertEqual(self.sender.sent[1][2], ".html")

    def test_monthly_sends_html_and_pdf_once(self):
        now = datetime(2028, 2, 29, 14, 20, tzinfo=timezone.utc)
        self.runner().run("monthly", now)
        self.runner().run("monthly", now)
        self.assertEqual([item[0] for item in self.sender.sent], ["text", "document", "document"])
        self.assertEqual([item[2] for item in self.sender.sent[1:]], [".html", ".pdf"])

    def test_definitive_failure_returns_item_to_pending_for_retry(self):
        now = datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc)
        self.sender.fail_once = True
        with self.assertRaises(reporting.DefinitiveDeliveryError):
            self.runner().run("daily", now)
        self.runner().run("daily", now)
        self.assertEqual([item[0] for item in self.sender.sent], ["text", "text"])

    def test_ambiguous_attempt_is_not_replayed_after_restart(self):
        now = datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc)
        self.sender.ambiguous_once = True
        with self.assertRaises(reporting.AmbiguousDeliveryError):
            self.runner().run("daily", now)
        result = self.runner().run("daily", now)
        self.assertEqual(result.delivered, 0)
        self.assertEqual(len(self.sender.sent), 1)

    def test_each_text_chunk_has_its_own_durable_identity(self):
        now = datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc)
        first = self.runner(telegram_limit=100).run("daily", now)
        second = self.runner(telegram_limit=100).run("daily", now)
        text_calls = [item for item in self.sender.sent if item[0] == "text"]
        self.assertGreater(first.delivered, 1)
        self.assertEqual(second.delivered, 0)
        self.assertEqual(len(text_calls), first.delivered)


if __name__ == "__main__":
    unittest.main()
