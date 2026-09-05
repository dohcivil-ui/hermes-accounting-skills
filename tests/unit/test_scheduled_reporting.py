import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest import mock
import tempfile


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = (
    ROOT / "skills/accounting/scheduled-project-report/scripts/scheduled_reporting.py"
)
RUNNER_PATH = ROOT / "skills/accounting/scheduled-project-report/scripts/run_report.py"


def load_module():
    name = "lekza_scheduled_reporting"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_runner_module():
    name = "lekza_scheduled_report_runner"
    scripts = str(RUNNER_PATH.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location(name, RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PeriodAndAggregationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reporting = load_module()

    def test_periods_use_bangkok_calendar_boundaries(self):
        # 2026-09-05 18:30 UTC is Sunday 2026-09-06 01:30 in Bangkok.
        instant = datetime(2026, 9, 5, 18, 30, tzinfo=timezone.utc)

        daily = self.reporting.reporting_period("daily", instant)
        weekly = self.reporting.reporting_period("weekly", instant)

        self.assertEqual((str(daily.start), str(daily.end)), ("2026-09-06", "2026-09-06"))
        self.assertEqual((str(weekly.start), str(weekly.end)), ("2026-08-31", "2026-09-06"))
        self.assertEqual(weekly.key, "2026-08-31_2026-09-06")

    def test_monthly_period_supports_leap_year_and_requires_month_end(self):
        cases = (
            (datetime(2027, 2, 28, 14, 20, tzinfo=timezone.utc), "2027-02-01", "2027-02-28"),
            (datetime(2028, 2, 29, 14, 20, tzinfo=timezone.utc), "2028-02-01", "2028-02-29"),
            (datetime(2026, 4, 30, 14, 20, tzinfo=timezone.utc), "2026-04-01", "2026-04-30"),
            (datetime(2026, 1, 31, 14, 20, tzinfo=timezone.utc), "2026-01-01", "2026-01-31"),
        )
        for instant, start, end in cases:
            with self.subTest(end=end):
                period = self.reporting.reporting_period("monthly", instant)
                self.assertEqual((str(period.start), str(period.end)), (start, end))

        with self.assertRaises(self.reporting.ScheduleGateError):
            self.reporting.reporting_period(
                "monthly", datetime(2028, 2, 28, 14, 20, tzinfo=timezone.utc)
            )

    def test_manual_current_month_period_uses_bangkok_day_one_through_today(self):
        period = self.reporting.current_month_period(
            datetime(2026, 9, 4, 20, 30, tzinfo=timezone.utc)
        )
        self.assertEqual(period.report_type, "monthly")
        self.assertEqual((str(period.start), str(period.end)), ("2026-09-01", "2026-09-05"))
        self.assertEqual(period.key, "2026-09")

    def test_weekly_requires_sunday_in_bangkok(self):
        with self.assertRaises(self.reporting.ScheduleGateError):
            self.reporting.reporting_period(
                "weekly", datetime(2026, 9, 5, 14, 10, tzinfo=timezone.utc)
            )

    def test_application_schedule_uses_bangkok_times(self):
        cases = (
            ("daily", datetime(2026, 9, 3, 13, 59, tzinfo=timezone.utc)),
            ("weekly", datetime(2026, 9, 6, 14, 9, tzinfo=timezone.utc)),
            ("monthly", datetime(2026, 9, 30, 14, 19, tzinfo=timezone.utc)),
        )
        for report_type, instant in cases:
            with self.subTest(report_type=report_type), self.assertRaises(self.reporting.ScheduleGateError):
                self.reporting.ensure_schedule_due(report_type, instant)
        self.reporting.ensure_schedule_due(
            "monthly", datetime(2026, 9, 30, 14, 20, tzinfo=timezone.utc)
        )

    def test_aggregate_includes_zero_projects_and_confirmed_rows_only(self):
        period = self.reporting.reporting_period(
            "daily", datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc)
        )
        projects = [
            {"project_id": "p1", "project_name": "บ้านสุขุมวิท"},
            {"project_id": "p2", "project_name": "โกดังบางนา"},
        ]
        transactions = [
            {"date": "2026-09-03", "project_id": "p1", "project": "บ้านสุขุมวิท", "type": "income", "category": "", "amount": 10000, "payee": "ลูกค้า", "status": "confirmed"},
            {"date": "2026-09-03", "project_id": "p1", "project": "บ้านสุขุมวิท", "type": "expense", "category": "materials", "amount": 2500, "payee": "ร้านวัสดุ", "status": "confirmed"},
            {"date": "2026-09-03", "project_id": "p1", "project": "บ้านสุขุมวิท", "type": "expense", "category": "labor", "amount": 999, "payee": "ช่าง", "status": "deleted"},
        ]

        report = self.reporting.aggregate_report("daily", period, projects, transactions)

        self.assertEqual(
            (report.projects[0].income, report.projects[0].expense, report.projects[0].net, report.projects[0].count),
            (10000, 2500, 7500, 2),
        )
        self.assertEqual(
            (report.projects[1].income, report.projects[1].expense, report.projects[1].net, report.projects[1].count),
            (0, 0, 0, 0),
        )
        self.assertEqual((report.income, report.expense, report.net, report.count), (10000, 2500, 7500, 2))

    def test_weekly_and_monthly_breakdowns_are_deterministic(self):
        period = self.reporting.reporting_period(
            "weekly", datetime(2026, 9, 6, 14, 10, tzinfo=timezone.utc)
        )
        projects = [{"project_id": "p1", "project_name": "บ้านสุขุมวิท"}]
        transactions = [
            {"date": "2026-08-31", "project_id": "p1", "project": "บ้านสุขุมวิท", "type": "expense", "category": "materials", "amount": 3000, "payee": "ร้าน ก", "status": "confirmed"},
            {"date": "2026-09-06", "project_id": "p1", "project": "บ้านสุขุมวิท", "type": "expense", "category": "labor", "amount": 4500, "payee": "ช่าง ข", "status": "confirmed"},
            {"date": "2026-08-30", "project_id": "p1", "project": "บ้านสุขุมวิท", "type": "expense", "category": "labor", "amount": 9000, "payee": "นอกช่วง", "status": "confirmed"},
        ]

        report = self.reporting.aggregate_report("weekly", period, projects, transactions)

        self.assertEqual(report.projects[0].top_payees[0], ("ช่าง ข", 4500))
        self.assertEqual(
            report.projects[0].category_expenses,
            {"materials": 3000, "labor": 4500},
        )

    def test_confirmed_transactions_accept_iso_and_google_serial_dates(self):
        period = self.reporting.current_month_period(
            datetime(2026, 9, 4, 20, 30, tzinfo=timezone.utc)
        )
        report = self.reporting.aggregate_report(
            "monthly",
            period,
            [{"project_id": "p1", "project_name": "บ้านตัวอย่าง"}],
            [
                {"date": "2026-09-01", "project_id": "p1", "project": "บ้านตัวอย่าง", "type": "income", "category": "", "amount": 100, "payee": "ลูกค้า", "status": "confirmed"},
                {"date": 46266, "project_id": "p1", "project": "บ้านตัวอย่าง", "type": "expense", "category": "materials", "amount": 363, "payee": "ร้านวัสดุ", "status": "confirmed"},
            ],
        )

        self.assertEqual(report.count, 2)
        self.assertEqual((report.income, report.expense, report.net), (100, 363, -263))

    def test_report_money_always_has_grouping_and_two_decimal_places(self):
        period = self.reporting.current_month_period(
            datetime(2026, 9, 4, 20, 30, tzinfo=timezone.utc)
        )
        report = self.reporting.aggregate_report(
            "monthly",
            period,
            [{"project_id": "p1", "project_name": "บ้านตัวอย่าง"}],
            [
                {"date": "2026-09-01", "project_id": "p1", "project": "บ้านตัวอย่าง", "type": "income", "category": "", "amount": 10000, "payee": "ลูกค้า", "status": "confirmed"},
                {"date": "2026-09-02", "project_id": "p1", "project": "บ้านตัวอย่าง", "type": "expense", "category": "labor", "amount": 10000, "payee": "ผู้รับเหมา", "status": "confirmed"},
                {"date": "2026-09-03", "project_id": "p1", "project": "บ้านตัวอย่าง", "type": "expense", "category": "materials", "amount": 363, "payee": "ร้านวัสดุ", "status": "confirmed"},
            ],
        )

        telegram = self.reporting.render_telegram(report)
        html = self.reporting.render_html(report).decode("utf-8")
        for rendered in (telegram, html):
            self.assertIn("10,000.00", rendered)
            self.assertIn("363.00", rendered)
            self.assertIn("-363.00", rendered)
        self.assertIn("จำนวนรายการ: 3", telegram)
        self.assertNotIn("จำนวนรายการ: 3.00", telegram)

    def test_no_data_report_still_renders_all_projects_and_zero_totals(self):
        period = self.reporting.reporting_period(
            "daily", datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc)
        )
        report = self.reporting.aggregate_report(
            "daily",
            period,
            [{"project_id": "p1", "project_name": "บ้านตัวอย่าง"}],
            [],
        )
        message = self.reporting.render_telegram(report)
        self.assertIn("บ้านตัวอย่าง", message)
        self.assertIn("วันนี้ไม่มีรายการครับ", message)
        self.assertIn("จำนวนรายการรวม: 0", message)

    def test_monthly_no_data_keeps_zero_category_rows(self):
        period = self.reporting.reporting_period(
            "monthly", datetime(2026, 9, 30, 14, 20, tzinfo=timezone.utc)
        )
        report = self.reporting.aggregate_report(
            "monthly", period,
            [{"project_id": "p1", "project_name": "โครงการไม่มีรายการ"}],
            [],
        )
        message = self.reporting.render_telegram(report)
        for label in (
            "ค่าแรง: 0.00 บาท", "วัสดุ: 0.00 บาท",
            "ค่าเช่า: 0.00 บาท", "อื่นๆ: 0.00 บาท",
        ):
            self.assertIn(label, message)

    def test_confirmed_malformed_rows_fail_closed(self):
        period = self.reporting.reporting_period(
            "daily", datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc)
        )
        projects = [{"project_id": "p1", "project_name": "บ้านตัวอย่าง"}]
        base = {"date": "2026-09-03", "project_id": "p1", "project": "บ้านตัวอย่าง", "type": "income", "category": "", "amount": 100, "payee": "ผู้จ่าย", "status": "confirmed"}
        for field, value in (
            ("amount", "100"),
            ("type", "other"),
            ("status", None),
            ("status", ""),
            ("status", "confirmd"),
        ):
            row = dict(base)
            row[field] = value
            with self.subTest(field=field), self.assertRaises(self.reporting.MalformedSheetRowError):
                self.reporting.aggregate_report("daily", period, projects, [row])

    def test_telegram_chunking_is_deterministic_and_within_limit(self):
        text = "หัวข้อ\n\n" + "\n\n".join(f"โครงการ {index} " + ("ก" * 1100) for index in range(6))
        first = self.reporting.chunk_telegram_text(text, limit=4096)
        second = self.reporting.chunk_telegram_text(text, limit=4096)
        self.assertEqual(first, second)
        self.assertTrue(all(len(chunk) <= 4096 for chunk in first))
        self.assertEqual("".join(first), text)

    def test_html_is_utf8_and_contains_monthly_details(self):
        period = self.reporting.reporting_period(
            "monthly", datetime(2028, 2, 29, 14, 20, tzinfo=timezone.utc)
        )
        report = self.reporting.aggregate_report(
            "monthly",
            period,
            [{"project_id": "p1", "project_name": "บ้านภาษาไทย"}],
            [{"date": "2028-02-10", "project_id": "p1", "project": "บ้านภาษาไทย", "type": "expense", "category": "materials", "amount": 1250, "payee": "ร้านวัสดุ", "status": "confirmed"}],
        )
        html = self.reporting.render_html(report)
        decoded = html.decode("utf-8")
        self.assertIn('<meta charset="utf-8">', decoded.lower())
        self.assertIn("บ้านภาษาไทย", decoded)
        self.assertIn("แยกตามประเภทค่าใช้จ่าย", decoded)
        self.assertIn("ร้านวัสดุ", decoded)

    def test_monthly_pdf_embeds_configured_thai_font_and_is_archived(self):
        candidates = [
            Path("C:/Windows/Fonts/tahoma.ttf"),
            Path("/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf"),
            Path("/usr/share/fonts/truetype/tlwg/Garuda.ttf"),
        ]
        font_path = next((path for path in candidates if path.is_file()), None)
        if font_path is None:
            self.skipTest("No Thai test font is installed")
        period = self.reporting.reporting_period(
            "monthly", datetime(2028, 2, 29, 14, 20, tzinfo=timezone.utc)
        )
        report = self.reporting.aggregate_report(
            "monthly",
            period,
            [{"project_id": "p1", "project_name": "บ้านภาษาไทย"}],
            [{"date": "2028-02-10", "project_id": "p1", "project": "บ้านภาษาไทย", "type": "expense", "category": "วัสดุ", "amount": 1250, "payee": "ร้านวัสดุ", "status": "confirmed"}],
        )
        with tempfile.TemporaryDirectory() as directory:
            builder = self.reporting.ArtifactBuilder(
                Path(directory) / "archive", font_path
            )
            try:
                pdf_path = builder.build_monthly_pdf(report)
                pdf_bytes = pdf_path.read_bytes()
                self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
                self.assertTrue(
                    any(marker in pdf_bytes for marker in (b"/FontFile ", b"/FontFile2", b"/FontFile3"))
                )
                self.assertTrue(pdf_path.is_relative_to(Path(directory) / "archive"))
                os.utime(pdf_path, (1, 1))
                self.assertEqual(builder.build_monthly_pdf(report).stat().st_mtime, 1)
                manual_pdf = builder.build_manual_monthly_pdf(
                    report, "telegram-message:77"
                )
                self.assertTrue(manual_pdf.read_bytes().startswith(b"%PDF-"))
                self.assertFalse(manual_pdf.is_relative_to(Path(directory) / "archive"))
                self.assertEqual(pdf_path.stat().st_mtime, 1)
            finally:
                builder.close()


class RuntimePathSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reporting = load_module()
        cls.runner = load_runner_module()

    def environment(self, external_root):
        font = external_root / "thai-font.ttf"
        font.write_bytes(b"synthetic font placeholder")
        return {
            "LEKZA_REPORT_LEDGER_DB": str(external_root / "state" / "reports.sqlite3"),
            "LEKZA_REPORT_ARCHIVE_ROOT": str(external_root / "archive"),
            "LEKZA_REPORT_THAI_FONT_PATH": str(font),
        }

    def test_vendor_bootstrap_uses_configured_path_once_without_corrupting_sys_path(self):
        with tempfile.TemporaryDirectory() as directory:
            vendor = Path(directory).resolve() / "vendor"
            vendor.mkdir()
            (vendor / "lekza_vendor_probe.py").write_text(
                "AVAILABLE = True\n", encoding="utf-8"
            )
            original = list(sys.path)
            try:
                selected = self.reporting.bootstrap_report_vendor_path(
                    {
                        "LEKZA_RUNTIME_ENV": "production",
                        "LEKZA_REPORT_VENDOR_PATH": str(vendor),
                    }
                )
                self.assertEqual(selected, vendor)
                self.assertEqual(sys.path, [str(vendor), *original])

                selected_again = self.reporting.bootstrap_report_vendor_path(
                    {
                        "LEKZA_RUNTIME_ENV": "production",
                        "LEKZA_REPORT_VENDOR_PATH": str(vendor),
                    }
                )
                self.assertEqual(selected_again, vendor)
                self.assertEqual(sys.path, [str(vendor), *original])

                probe = importlib.import_module("lekza_vendor_probe")
                self.assertTrue(probe.AVAILABLE)
            finally:
                sys.modules.pop("lekza_vendor_probe", None)
                sys.path[:] = original

    def test_reporting_module_bootstraps_vendor_path_during_import(self):
        with tempfile.TemporaryDirectory() as directory:
            vendor = Path(directory).resolve() / "vendor"
            vendor.mkdir()
            original = list(sys.path)
            module_name = "lekza_scheduled_reporting_bootstrap_test"
            try:
                with mock.patch.dict(
                    os.environ,
                    {
                        "LEKZA_RUNTIME_ENV": "production",
                        "LEKZA_REPORT_VENDOR_PATH": str(vendor),
                    },
                ):
                    spec = importlib.util.spec_from_file_location(
                        module_name, MODULE_PATH
                    )
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = module
                    spec.loader.exec_module(module)
                self.assertEqual(sys.path, [str(vendor), *original])
            finally:
                sys.modules.pop(module_name, None)
                sys.path[:] = original

    def test_explicit_vendor_path_is_available_in_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            vendor = Path(directory).resolve() / "vendor"
            vendor.mkdir()
            original = list(sys.path)
            try:
                selected = self.reporting.bootstrap_report_vendor_path(
                    {
                        "LEKZA_RUNTIME_ENV": "staging",
                        "LEKZA_REPORT_VENDOR_PATH": str(vendor),
                    }
                )
                self.assertEqual(selected, vendor)
                self.assertEqual(sys.path, [str(vendor), *original])
            finally:
                sys.path[:] = original

    def test_explicit_vendor_path_requires_known_runtime_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            vendor = Path(directory).resolve() / "vendor"
            vendor.mkdir()
            for runtime_mode in (None, "preview"):
                environment = {"LEKZA_REPORT_VENDOR_PATH": str(vendor)}
                if runtime_mode is not None:
                    environment["LEKZA_RUNTIME_ENV"] = runtime_mode
                original = list(sys.path)
                try:
                    with self.subTest(runtime_mode=runtime_mode):
                        selected = self.reporting.bootstrap_report_vendor_path(
                            environment
                        )
                        self.assertIsNone(selected)
                        self.assertEqual(sys.path, original)
                finally:
                    sys.path[:] = original

    def test_vendor_bootstrap_discovers_single_production_vendor_release(self):
        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory).resolve() / "data"
            vendor = data_root / "lekza-production" / "vendor" / "release-a"
            (vendor / "reportlab").mkdir(parents=True)
            original = list(sys.path)
            try:
                with mock.patch.object(
                    self.reporting, "_HOSTINGER_DATA_ROOT", data_root
                ):
                    selected = self.reporting.bootstrap_report_vendor_path(
                        {"LEKZA_RUNTIME_ENV": "production"}
                    )
                self.assertEqual(selected, vendor)
                self.assertEqual(sys.path, [str(vendor), *original])
            finally:
                sys.path[:] = original

    def test_staging_does_not_fall_back_to_production_vendor_path(self):
        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory).resolve() / "data"
            vendor = data_root / "lekza-production" / "vendor" / "release-a"
            (vendor / "reportlab").mkdir(parents=True)
            original = list(sys.path)
            with mock.patch.object(
                self.reporting, "_HOSTINGER_DATA_ROOT", data_root
            ):
                selected = self.reporting.bootstrap_report_vendor_path(
                    {"LEKZA_RUNTIME_ENV": "staging"}
                )
            self.assertIsNone(selected)
            self.assertEqual(sys.path, original)

    def test_production_fallback_rejects_vendor_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            data_root = root / "data"
            vendor_root = data_root / "lekza-production" / "vendor"
            vendor_root.mkdir(parents=True)
            outside = root / "outside-release"
            (outside / "reportlab").mkdir(parents=True)
            try:
                (vendor_root / "release-link").symlink_to(
                    outside, target_is_directory=True
                )
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")
            original = list(sys.path)
            with mock.patch.object(
                self.reporting, "_HOSTINGER_DATA_ROOT", data_root
            ):
                selected = self.reporting.bootstrap_report_vendor_path(
                    {"LEKZA_RUNTIME_ENV": "production"}
                )
            self.assertIsNone(selected)
            self.assertEqual(sys.path, original)

    def test_production_fallback_rejects_vendor_root_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            data_root = root / "data"
            production_root = data_root / "lekza-production"
            production_root.mkdir(parents=True)
            outside = root / "outside-vendor"
            (outside / "release-a" / "reportlab").mkdir(parents=True)
            try:
                (production_root / "vendor").symlink_to(
                    outside, target_is_directory=True
                )
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")
            original = list(sys.path)
            with mock.patch.object(
                self.reporting, "_HOSTINGER_DATA_ROOT", data_root
            ):
                selected = self.reporting.bootstrap_report_vendor_path(
                    {"LEKZA_RUNTIME_ENV": "production"}
                )
            self.assertIsNone(selected)
            self.assertEqual(sys.path, original)

    def test_vendor_bootstrap_ignores_missing_path_and_preserves_sys_path(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory).resolve() / "missing"
            original = list(sys.path)
            selected = self.reporting.bootstrap_report_vendor_path(
                {
                    "LEKZA_RUNTIME_ENV": "production",
                    "LEKZA_REPORT_VENDOR_PATH": str(missing),
                }
            )
            self.assertIsNone(selected)
            self.assertEqual(sys.path, original)

    def test_rejects_repo_root_and_every_repo_subdirectory_for_each_runtime_path(self):
        forbidden = (ROOT, ROOT / "docs", ROOT / "tests", ROOT / "skills", ROOT / "plugins", ROOT / "docs" / "nested")
        names = (
            "LEKZA_REPORT_LEDGER_DB",
            "LEKZA_REPORT_ARCHIVE_ROOT",
            "LEKZA_REPORT_THAI_FONT_PATH",
        )
        with tempfile.TemporaryDirectory() as directory:
            environment = self.environment(Path(directory))
            for name in names:
                for path in forbidden:
                    candidate = dict(environment)
                    candidate[name] = str(path)
                    with self.subTest(name=name, path=path), self.assertRaises(ValueError):
                        self.runner.resolve_runtime_paths(candidate)

    def test_accepts_absolute_runtime_paths_outside_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            external_root = Path(directory).resolve()
            paths = self.runner.resolve_runtime_paths(self.environment(external_root))
            self.assertEqual(paths["ledger"], external_root / "state" / "reports.sqlite3")
            self.assertEqual(paths["archive"], external_root / "archive")
            self.assertEqual(paths["font"], external_root / "thai-font.ttf")

    def test_hostinger_layout_accepts_persistent_production_data(self):
        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory).resolve() / "data"
            production_root = data_root / "lekza-production"
            production_root.mkdir(parents=True)
            script_path = (
                data_root
                / "skills/accounting/scheduled-project-report/scripts/run_report.py"
            )

            with (
                mock.patch.object(self.runner, "__file__", str(script_path)),
                mock.patch.object(self.runner, "_HOSTINGER_DATA_ROOT", data_root),
            ):
                paths = self.runner.resolve_runtime_paths(
                    self.environment(production_root)
                )

            self.assertEqual(
                paths["ledger"], production_root / "state" / "reports.sqlite3"
            )
            self.assertEqual(paths["archive"], production_root / "archive")
            self.assertEqual(paths["font"], production_root / "thai-font.ttf")

    def test_hostinger_layout_rejects_skills_and_plugins_source_trees(self):
        names = (
            "LEKZA_REPORT_LEDGER_DB",
            "LEKZA_REPORT_ARCHIVE_ROOT",
            "LEKZA_REPORT_THAI_FONT_PATH",
        )
        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory).resolve() / "data"
            production_root = data_root / "lekza-production"
            production_root.mkdir(parents=True)
            script_path = (
                data_root
                / "skills/accounting/scheduled-project-report/scripts/run_report.py"
            )
            environment = self.environment(production_root)
            for name in names:
                for source_path in (
                    data_root / "skills",
                    data_root / "skills" / "accounting" / "state",
                    data_root / "plugins",
                    data_root / "plugins" / "report-state",
                ):
                    candidate = dict(environment)
                    candidate[name] = str(source_path)
                    with self.subTest(name=name, source_path=source_path):
                        with self.assertRaises(ValueError):
                            with (
                                mock.patch.object(
                                    self.runner, "__file__", str(script_path)
                                ),
                                mock.patch.object(
                                    self.runner, "_HOSTINGER_DATA_ROOT", data_root
                                ),
                            ):
                                self.runner.resolve_runtime_paths(candidate)


if __name__ == "__main__":
    unittest.main()
