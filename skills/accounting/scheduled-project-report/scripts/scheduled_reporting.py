"""Scheduled project reporting for Lekza.

The public seam is :class:`ReportRunner`. Pure period, aggregation, and
rendering functions remain available so behavior can be tested without
network or runtime side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from html import escape
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import urllib.parse
import uuid
from zoneinfo import ZoneInfo


_HOSTINGER_DATA_ROOT = Path("/data")


def _production_report_vendor_path():
    try:
        production_root = (
            _HOSTINGER_DATA_ROOT / "lekza-production"
        ).resolve()
        vendor_root = (production_root / "vendor").resolve()
    except OSError:
        return None
    if (
        not vendor_root.is_relative_to(production_root)
        or not vendor_root.is_dir()
    ):
        return None
    candidates = []
    try:
        paths = vendor_root.iterdir()
        for path in paths:
            candidate = path.resolve()
            reportlab_path = (candidate / "reportlab").resolve()
            if (
                candidate.is_relative_to(vendor_root)
                and reportlab_path.is_relative_to(candidate)
                and reportlab_path.is_dir()
            ):
                candidates.append(candidate)
    except OSError:
        return None
    return candidates[0] if len(candidates) == 1 else None


def bootstrap_report_vendor_path(environment=None):
    """Expose durable report dependencies without relying on venv startup files."""
    environment = os.environ if environment is None else environment
    runtime_mode = str(environment.get("LEKZA_RUNTIME_ENV") or "").strip()
    if runtime_mode not in {"production", "staging"}:
        return None
    configured = str(environment.get("LEKZA_REPORT_VENDOR_PATH") or "").strip()
    if configured:
        vendor_path = Path(configured)
    elif runtime_mode == "production":
        vendor_path = _production_report_vendor_path()
        if vendor_path is None:
            return None
    else:
        return None
    if not vendor_path.is_absolute():
        raise ValueError("LEKZA_REPORT_VENDOR_PATH must be an absolute path")
    vendor_path = vendor_path.resolve()
    if not vendor_path.is_dir():
        return None
    if not any(
        entry and Path(entry).resolve() == vendor_path
        for entry in sys.path
    ):
        sys.path.insert(0, str(vendor_path))
    return vendor_path


bootstrap_report_vendor_path()

BANGKOK = ZoneInfo("Asia/Bangkok")
REPORT_TYPES = frozenset({"daily", "weekly", "monthly"})
KNOWN_TRANSACTION_STATUSES = frozenset({"confirmed", "deleted"})
CATEGORY_LABELS = {
    "labor": "ค่าแรง",
    "materials": "วัสดุ",
    "rent": "ค่าเช่า",
    "transport": "ค่าขนส่ง",
    "contractor": "ผู้รับเหมา",
    "other": "อื่นๆ",
}
MONTHLY_CATEGORY_ORDER = ("labor", "materials", "rent", "transport", "contractor", "other")
TRANSACTIONS_SCHEMA = (
    "transaction_id", "reference_no", "date", "payer", "payee", "project_id",
    "project", "type", "category", "amount", "note", "confidence",
    "submitted_by", "drive_file_id", "slip_url", "status", "created_at",
    "confirmed_at",
)
PROJECTS_SCHEMA = (
    "project_id", "project_name", "customer", "status", "start_date",
    "created_by", "created_at",
)


class ReportingError(RuntimeError):
    """Base error for fail-closed report generation."""


class ScheduleGateError(ReportingError):
    """The requested report is not eligible on the Bangkok calendar date."""


class MalformedSheetRowError(ReportingError):
    """A required reporting value in Google Sheets is malformed."""


class DefinitiveDeliveryError(ReportingError):
    """The remote destination definitively rejected a delivery attempt."""


class AmbiguousDeliveryError(ReportingError):
    """Delivery may have succeeded and must not be repeated automatically."""


@dataclass(frozen=True)
class ReportingPeriod:
    report_type: str
    start: date
    end: date
    key: str


@dataclass
class ProjectReport:
    project_id: str
    project_name: str
    income: Decimal = Decimal("0")
    expense: Decimal = Decimal("0")
    count: int = 0
    category_expenses: dict[str, Decimal] = field(default_factory=dict)
    payee_expenses: dict[str, Decimal] = field(default_factory=dict)

    @property
    def net(self):
        return self.income - self.expense

    @property
    def top_payees(self):
        return tuple(
            sorted(self.payee_expenses.items(), key=lambda item: (-item[1], item[0]))
        )


@dataclass(frozen=True)
class Report:
    report_type: str
    period: ReportingPeriod
    projects: tuple[ProjectReport, ...]

    @property
    def income(self):
        return sum((project.income for project in self.projects), Decimal("0"))

    @property
    def expense(self):
        return sum((project.expense for project in self.projects), Decimal("0"))

    @property
    def net(self):
        return self.income - self.expense

    @property
    def count(self):
        return sum(project.count for project in self.projects)


@dataclass(frozen=True)
class DeliveryClaim:
    key: str
    owner: str


@dataclass(frozen=True)
class RunResult:
    report_type: str
    period_key: str
    delivered: int
    skipped: int


def _bangkok_date(now):
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    return now.astimezone(BANGKOK).date()


def reporting_period(report_type, now):
    """Return the Bangkok reporting period, enforcing weekly/monthly date gates."""
    if report_type not in REPORT_TYPES:
        raise ValueError(f"Unsupported report type: {report_type}")
    today = _bangkok_date(now)
    if report_type == "daily":
        start = today
        key = today.isoformat()
    elif report_type == "weekly":
        if today.weekday() != 6:
            raise ScheduleGateError("Weekly reports run only on Sunday in Asia/Bangkok")
        start = today - timedelta(days=6)
        key = f"{start.isoformat()}_{today.isoformat()}"
    else:
        tomorrow = today + timedelta(days=1)
        if tomorrow.month == today.month:
            raise ScheduleGateError("Monthly reports run only on the last Bangkok calendar day")
        start = today.replace(day=1)
        key = today.strftime("%Y-%m")
    return ReportingPeriod(report_type, start, today, key)


def current_month_period(now):
    """Return day one through today in the Bangkok calendar month."""
    today = _bangkok_date(now)
    return ReportingPeriod("monthly", today.replace(day=1), today, today.strftime("%Y-%m"))


def ensure_schedule_due(report_type, now):
    """Fail closed before the configured Bangkok delivery time."""
    period = reporting_period(report_type, now)
    local = now.astimezone(BANGKOK)
    due = {"daily": (21, 0), "weekly": (21, 10), "monthly": (21, 20)}[report_type]
    if (local.hour, local.minute) < due:
        raise ScheduleGateError(
            f"{report_type} report is not due before {due[0]:02d}:{due[1]:02d} Asia/Bangkok"
        )
    return period


def _project_key(row):
    project_id = str(row.get("project_id") or "").strip()
    project_name = str(row.get("project_name") or "").strip()
    if not project_name:
        raise MalformedSheetRowError("Projects.project_name is required")
    return project_id, project_name


def _amount(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise MalformedSheetRowError("Confirmed transaction amount must be numeric")
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise MalformedSheetRowError("Confirmed transaction amount must be finite and non-negative")
    return amount


def _transaction_date(value):
    if isinstance(value, bool):
        raise MalformedSheetRowError(
            "Confirmed transaction date must be YYYY-MM-DD or a Google Sheets date serial"
        )
    if isinstance(value, (int, float, Decimal)):
        serial = Decimal(str(value))
        if not serial.is_finite() or serial != serial.to_integral_value():
            raise MalformedSheetRowError(
                "Confirmed transaction date must be YYYY-MM-DD or a Google Sheets date serial"
            )
        try:
            return date(1899, 12, 30) + timedelta(days=int(serial))
        except (OverflowError, ValueError) as exc:
            raise MalformedSheetRowError(
                "Confirmed transaction date must be YYYY-MM-DD or a Google Sheets date serial"
            ) from exc
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise MalformedSheetRowError(
            "Confirmed transaction date must be YYYY-MM-DD or a Google Sheets date serial"
        ) from exc
    if parsed.isoformat() != text:
        raise MalformedSheetRowError(
            "Confirmed transaction date must be YYYY-MM-DD or a Google Sheets date serial"
        )
    return parsed


def aggregate_report(report_type, period, project_rows, transaction_rows):
    """Aggregate confirmed transactions while preserving every Projects row."""
    projects = []
    by_id = {}
    by_name = {}
    for row in project_rows:
        project_id, project_name = _project_key(row)
        if (project_id and project_id in by_id) or project_name in by_name:
            raise MalformedSheetRowError("Projects contains a duplicate project identity")
        project = ProjectReport(project_id, project_name)
        projects.append(project)
        if project_id:
            by_id[project_id] = project
        by_name[project_name] = project

    for row in transaction_rows:
        status = row.get("status")
        if not isinstance(status, str) or not status.strip():
            raise MalformedSheetRowError("Transactions.status is required")
        status = status.strip()
        if status not in KNOWN_TRANSACTION_STATUSES:
            raise MalformedSheetRowError("Transactions.status is unknown")
        if status != "confirmed":
            continue
        transaction_date = _transaction_date(row.get("date"))
        if not period.start <= transaction_date <= period.end:
            continue
        transaction_type = row.get("type")
        if transaction_type not in {"income", "expense"}:
            raise MalformedSheetRowError("Confirmed transaction type must be income or expense")
        amount = _amount(row.get("amount"))
        project_id = str(row.get("project_id") or "").strip()
        project_name = str(row.get("project") or "").strip()
        project = by_id.get(project_id) if project_id else None
        project = project or by_name.get(project_name)
        if project is None:
            raise MalformedSheetRowError("Confirmed transaction references an unknown project")

        project.count += 1
        if transaction_type == "income":
            project.income += amount
            continue
        project.expense += amount
        category = str(row.get("category") or "อื่นๆ").strip() or "อื่นๆ"
        project.category_expenses[category] = project.category_expenses.get(category, Decimal("0")) + amount
        payee = str(row.get("payee") or "ไม่ระบุ").strip() or "ไม่ระบุ"
        project.payee_expenses[payee] = project.payee_expenses.get(payee, Decimal("0")) + amount

    return Report(report_type, period, tuple(projects))


def _money(value):
    return f"{Decimal(value):,.2f}"


def _category_label(value):
    return CATEGORY_LABELS.get(value, value)


def _monthly_categories(project):
    keys = list(MONTHLY_CATEGORY_ORDER)
    keys.extend(sorted(set(project.category_expenses) - set(keys)))
    return tuple((key, project.category_expenses.get(key, Decimal("0"))) for key in keys)


def _title(report):
    if report.report_type == "daily":
        return f"📊 รายงานประจำวัน {report.period.end.strftime('%d/%m/%Y')}"
    if report.report_type == "weekly":
        return (
            "📊 รายงานสัปดาห์ "
            f"{report.period.start.strftime('%d/%m')} — "
            f"{report.period.end.strftime('%d/%m/%Y')}"
        )
    return f"📊 รายงานประจำเดือน {report.period.end.strftime('%m/%Y')}"


def _plain_title(report):
    if report.report_type == "daily":
        return f"รายงานประจำวัน {report.period.end.strftime('%d/%m/%Y')}"
    if report.report_type == "weekly":
        return (
            "รายงานสัปดาห์ "
            f"{report.period.start.strftime('%d/%m')} - "
            f"{report.period.end.strftime('%d/%m/%Y')}"
        )
    return f"รายงานประจำเดือน {report.period.end.strftime('%m/%Y')}"


def render_telegram(report):
    """Render the established Thai summary format before deterministic chunking."""
    sections = [_title(report)]
    if report.count == 0:
        sections.append("วันนี้ไม่มีรายการครับ 😴" if report.report_type == "daily" else "ช่วงนี้ไม่มีรายการครับ")
    for project in report.projects:
        lines = [
            f"📁 งาน: {project.project_name}",
            f"  💚 เงินเข้า: {_money(project.income)} บาท",
            f"  ❤️ เงินออก: {_money(project.expense)} บาท",
            f"  📌 เหลือสุทธิ: {_money(project.net)} บาท",
            f"  🧾 จำนวนรายการ: {project.count}",
        ]
        if report.report_type == "weekly":
            if project.top_payees:
                name, amount = project.top_payees[0]
                lines.append(f"  👥 คนรับเงินสูงสุด: {name} ({_money(amount)} บาท)")
            else:
                lines.append("  👥 คนรับเงินสูงสุด: ไม่มี")
        if report.report_type == "monthly":
            lines.append("\n  📂 แยกตามประเภทค่าใช้จ่าย:")
            for category, amount in _monthly_categories(project):
                lines.append(f"  • {_category_label(category)}: {_money(amount)} บาท")
            lines.append("\n  👥 Top คนรับเงิน:")
            if project.top_payees:
                for index, (name, amount) in enumerate(project.top_payees[:2], 1):
                    lines.append(f"  {index}. {name}: {_money(amount)} บาท")
            else:
                lines.append("  ไม่มี")
        sections.append("\n".join(lines))
    sections.append(
        "──────────────────\n"
        "🏦 รวมทุกงาน\n"
        f"  💚 เงินเข้า: {_money(report.income)} บาท\n"
        f"  ❤️ เงินออก: {_money(report.expense)} บาท\n"
        f"  📌 เหลือสุทธิ: {_money(report.net)} บาท\n"
        f"  🧾 จำนวนรายการรวม: {report.count}"
    )
    return "\n\n".join(sections)


def chunk_telegram_text(value, *, limit=4096):
    """Split text deterministically without losing or reordering characters."""
    if not isinstance(value, str) or not value:
        raise ValueError("Telegram text must be a non-empty string")
    if not isinstance(limit, int) or limit < 1:
        raise ValueError("Telegram limit must be a positive integer")
    chunks = []
    remaining = value
    while len(remaining) > limit:
        split = remaining.rfind("\n\n", 0, limit + 1)
        width = split + 2 if split >= 0 else 0
        if width <= 0:
            split = remaining.rfind("\n", 0, limit + 1)
            width = split + 1 if split >= 0 else limit
        chunks.append(remaining[:width])
        remaining = remaining[width:]
    if remaining:
        chunks.append(remaining)
    return tuple(chunks)


def render_html(report):
    """Render a self-contained UTF-8 HTML report."""
    project_sections = []
    for project in report.projects:
        details = ""
        if report.report_type == "weekly":
            top = "ไม่มี"
            if project.top_payees:
                name, amount = project.top_payees[0]
                top = f"{escape(name)} ({_money(amount)} บาท)"
            details = f"<p><strong>คนรับเงินสูงสุด:</strong> {top}</p>"
        elif report.report_type == "monthly":
            categories = "".join(
                f"<li>{escape(_category_label(category))}: {_money(amount)} บาท</li>"
                for category, amount in _monthly_categories(project)
            )
            payees = "".join(
                f"<li>{escape(name)}: {_money(amount)} บาท</li>"
                for name, amount in project.top_payees[:2]
            ) or "<li>ไม่มี</li>"
            details = (
                "<h3>แยกตามประเภทค่าใช้จ่าย</h3><ul>" + categories + "</ul>"
                "<h3>Top คนรับเงิน</h3><ol>" + payees + "</ol>"
            )
        project_sections.append(
            f"<section><h2>{escape(project.project_name)}</h2>"
            "<table><thead><tr><th>รายรับ</th><th>รายจ่าย</th><th>สุทธิ</th><th>จำนวนรายการ</th></tr></thead>"
            f"<tbody><tr><td>{_money(project.income)}</td><td>{_money(project.expense)}</td>"
            f"<td>{_money(project.net)}</td><td>{project.count}</td></tr></tbody></table>{details}</section>"
        )
    body = "".join(project_sections)
    total = (
        "<section class=\"total\"><h2>รวมทุกโครงการ</h2><table><tbody><tr>"
        f"<td>รายรับรวม {_money(report.income)} บาท</td>"
        f"<td>รายจ่ายรวม {_money(report.expense)} บาท</td>"
        f"<td>สุทธิรวม {_money(report.net)} บาท</td>"
        f"<td>จำนวนรายการรวม {report.count}</td></tr></tbody></table></section>"
    )
    document = f"""<!doctype html>
<html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{escape(_title(report))}</title>
<style>body{{font-family:'Noto Sans Thai',Tahoma,sans-serif;margin:32px;color:#17212b}}h1{{color:#176b52}}section{{margin:24px 0}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccd5db;padding:9px;text-align:right}}th:first-child,td:first-child{{text-align:left}}.total{{background:#edf7f2;padding:16px}}</style>
</head><body><h1>{escape(_title(report))}</h1>{body}{total}</body></html>"""
    return document.encode("utf-8")


class ReportingSheetsReader:
    """Read-only, schema-locked adapter for Projects and Transactions."""

    API = "https://sheets.googleapis.com/v4/spreadsheets"

    def __init__(self, spreadsheet_id, token_provider, *, session=None, timeout=30):
        self.spreadsheet_id = str(spreadsheet_id or "").strip()
        if not self.spreadsheet_id:
            raise ValueError("Spreadsheet ID is required")
        self._token_provider = token_provider
        if session is None:
            import requests
            self._session = requests.Session()
            self._request_errors = (requests.RequestException,)
        else:
            self._session = session
            self._request_errors = (OSError,)
        self._timeout = timeout

    def _get(self, range_name):
        token = self._token_provider()
        def request(active_token):
            return self._session.get(
                f"{self.API}/{urllib.parse.quote(self.spreadsheet_id, safe='')}/values/"
                f"{urllib.parse.quote(range_name, safe='!')}",
                headers={"Authorization": f"Bearer {active_token}"},
                params={"majorDimension": "ROWS", "valueRenderOption": "UNFORMATTED_VALUE"},
                timeout=self._timeout,
            )
        try:
            response = request(token)
            refresh = getattr(self._token_provider, "refresh_if_stale", None)
            if response.status_code == 401 and callable(refresh):
                response = request(refresh(token))
        except self._request_errors as exc:
            raise ReportingError("Google Sheets read failed") from exc
        if response.status_code != 200:
            raise ReportingError("Google Sheets read failed")
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise ReportingError("Google Sheets returned malformed JSON") from exc
        values = payload.get("values")
        if not isinstance(values, list):
            raise ReportingError("Google Sheets returned malformed rows")
        return values

    @staticmethod
    def _decode_rows(values, schema, title):
        if not values or tuple(values[0]) != schema:
            raise MalformedSheetRowError(f"{title} schema is incompatible")
        decoded = []
        for number, values_row in enumerate(values[1:], 2):
            if not isinstance(values_row, list):
                raise MalformedSheetRowError(f"{title} row {number} is malformed")
            if not values_row or not any(value not in (None, "") for value in values_row):
                continue
            if len(values_row) > len(schema):
                raise MalformedSheetRowError(f"{title} row {number} has extra columns")
            padded = values_row + [""] * (len(schema) - len(values_row))
            decoded.append(dict(zip(schema, padded)))
        return decoded

    def read(self):
        # Validate both frozen headers before retrieving any report data.
        for title, schema in (("Transactions", TRANSACTIONS_SCHEMA), ("Projects", PROJECTS_SCHEMA)):
            header = self._get(f"{title}!1:1")
            if len(header) != 1 or tuple(header[0]) != schema:
                raise MalformedSheetRowError(f"{title} schema is incompatible")
        transactions = self._decode_rows(
            self._get("Transactions!A:R"), TRANSACTIONS_SCHEMA, "Transactions"
        )
        projects = self._decode_rows(
            self._get("Projects!A:G"), PROJECTS_SCHEMA, "Projects"
        )
        return projects, transactions


class ArtifactBuilder:
    """Create transient UTF-8 HTML and archive monthly Thai PDFs."""

    def __init__(self, archive_root, thai_font_path):
        self.archive_root = Path(archive_root)
        self.thai_font_path = Path(thai_font_path)
        if not self.archive_root.is_absolute():
            raise ValueError("Report archive root must be absolute")
        if not self.thai_font_path.is_absolute() or not self.thai_font_path.is_file():
            raise ValueError("Thai font path must be an absolute existing file")
        self.archive_root.mkdir(parents=True, exist_ok=True)
        self._transient = tempfile.TemporaryDirectory(prefix="lekza-report-")

    def build_html(self, report):
        path = Path(self._transient.name) / f"project-report-{report.period.key}.html"
        path.write_bytes(render_html(report))
        return path

    def build_monthly_pdf(self, report):
        if report.report_type != "monthly":
            raise ValueError("Only monthly reports are archived as PDF")
        target_dir = (
            self.archive_root / "monthly" /
            report.period.end.strftime("%Y") / report.period.end.strftime("%m")
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"project-report-{report.period.key}.pdf"
        if target.is_file():
            return target
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".project-report-", suffix=".pdf", dir=target_dir
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            self._write_pdf(report, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def build_manual_monthly_pdf(self, report, request_identity):
        """Build a transient monthly PDF without touching the scheduled archive."""
        if report.report_type != "monthly":
            raise ValueError("Only monthly reports can be rendered as PDF")
        identity = str(request_identity or "").strip()
        if not identity:
            raise ValueError("Manual report request identity is required")
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        target = Path(self._transient.name) / (
            f"manual-project-report-{report.period.key}-{digest}.pdf"
        )
        if target.is_file():
            return target
        with tempfile.NamedTemporaryFile(
            prefix=".manual-project-report-",
            suffix=".pdf",
            dir=self._transient.name,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        try:
            self._write_pdf(report, temporary)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return target

    def _write_pdf(self, report, output_path):
        try:
            from reportlab.lib import colors
            from reportlab.lib.enums import TA_CENTER, TA_RIGHT
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
            from reportlab.lib.units import mm
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        except ImportError as exc:
            raise ReportingError("reportlab is required for monthly PDF reports") from exc

        font_name = "LekzaThai"
        font = TTFont(font_name, str(self.thai_font_path))
        pdf_text = " ".join(
            [_plain_title(report), "รวมทุกโครงการ", "รายรับ รายจ่าย สุทธิ จำนวนรายการ"]
            + [project.project_name for project in report.projects]
            + [_category_label(name) for project in report.projects for name, _ in _monthly_categories(project)]
            + [name for project in report.projects for name in project.payee_expenses]
        )
        required_characters = {ord(character) for character in pdf_text if not character.isspace()}
        supported = set(font.face.charToGlyph)
        missing = sorted(required_characters - supported)
        if missing:
            raise ReportingError("Configured PDF font does not cover report characters")
        pdfmetrics.registerFont(font)

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "LekzaTitle", parent=styles["Title"], fontName=font_name,
            fontSize=18, leading=25, textColor=colors.HexColor("#176B52"),
        )
        heading_style = ParagraphStyle(
            "LekzaHeading", parent=styles["Heading2"], fontName=font_name,
            fontSize=13, leading=19, spaceBefore=8,
        )
        body_style = ParagraphStyle(
            "LekzaBody", parent=styles["BodyText"], fontName=font_name,
            fontSize=10, leading=15,
        )
        right_style = ParagraphStyle(
            "LekzaRight", parent=body_style, alignment=TA_RIGHT,
        )
        center_style = ParagraphStyle(
            "LekzaCenter", parent=body_style, alignment=TA_CENTER,
        )
        document = SimpleDocTemplate(
            str(output_path), pagesize=A4, rightMargin=16 * mm,
            leftMargin=16 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
            title=_plain_title(report), author="Lekza Hermes Accounting",
        )
        story = [Paragraph(escape(_plain_title(report)), title_style), Spacer(1, 5 * mm)]
        table_style = TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E4F3ED")),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#AAB8BE")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ])
        for project in report.projects:
            story.append(Paragraph(escape(project.project_name), heading_style))
            data = [
                [Paragraph("รายรับ", center_style), Paragraph("รายจ่าย", center_style), Paragraph("สุทธิ", center_style), Paragraph("จำนวนรายการ", center_style)],
                [Paragraph(_money(project.income), right_style), Paragraph(_money(project.expense), right_style), Paragraph(_money(project.net), right_style), Paragraph(str(project.count), right_style)],
            ]
            table = Table(data, colWidths=[42 * mm] * 4)
            table.setStyle(table_style)
            story.extend([table, Spacer(1, 2 * mm)])
            categories = ", ".join(
                f"{_category_label(name)}: {_money(amount)} บาท"
                for name, amount in _monthly_categories(project)
            )
            payees = ", ".join(
                f"{index}. {name}: {_money(amount)} บาท"
                for index, (name, amount) in enumerate(project.top_payees[:2], 1)
            ) or "ไม่มี"
            story.append(Paragraph("แยกตามประเภทค่าใช้จ่าย: " + escape(categories), body_style))
            story.append(Paragraph("Top คนรับเงิน: " + escape(payees), body_style))
            story.append(Spacer(1, 3 * mm))
        story.append(Paragraph("รวมทุกโครงการ", heading_style))
        total_data = [[
            Paragraph(f"รายรับรวม {_money(report.income)} บาท", body_style),
            Paragraph(f"รายจ่ายรวม {_money(report.expense)} บาท", body_style),
            Paragraph(f"สุทธิรวม {_money(report.net)} บาท", body_style),
            Paragraph(f"จำนวนรายการรวม {report.count}", body_style),
        ]]
        total_table = Table(total_data, colWidths=[42 * mm] * 4)
        total_table.setStyle(table_style)
        story.append(total_table)
        document.build(story)

    def close(self):
        self._transient.cleanup()


class TelegramSender:
    """Telegram Bot adapter for text and document deliveries."""

    API = "https://api.telegram.org"

    def __init__(self, bot_token, chat_id, *, thread_id=None, session=None, timeout=30):
        self._bot_token = str(bot_token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.thread_id = None if thread_id in (None, "") else str(thread_id)
        if not self._bot_token or not self.chat_id:
            raise ValueError("Telegram bot token and report chat ID are required")
        if session is None:
            import requests
            self._session = requests.Session()
            self._request_errors = (requests.RequestException,)
        else:
            self._session = session
            self._request_errors = (OSError,)
        self._timeout = timeout
        self.destination = f"telegram:{self.chat_id}"
        if self.thread_id is not None:
            self.destination += f":thread:{self.thread_id}"

    def _post(self, method, *, data, files=None):
        try:
            response = self._session.post(
                f"{self.API}/bot{self._bot_token}/{method}",
                data=data,
                files=files,
                timeout=self._timeout,
            )
        except self._request_errors as exc:
            raise AmbiguousDeliveryError("Telegram delivery outcome is uncertain") from exc
        if response.status_code != 200:
            raise DefinitiveDeliveryError(f"Telegram rejected {method} with HTTP {response.status_code}")
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise AmbiguousDeliveryError("Telegram returned malformed success data") from exc
        if payload.get("ok") is not True:
            raise DefinitiveDeliveryError(f"Telegram rejected {method}")
        result = payload.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is None:
            raise AmbiguousDeliveryError("Telegram success did not include message_id")
        return str(message_id)

    def _base_data(self):
        data = {"chat_id": self.chat_id}
        if self.thread_id is not None:
            data["message_thread_id"] = self.thread_id
        return data

    def _check_destination(self, destination):
        if destination != self.destination:
            raise ValueError("Telegram destination does not match configured sender")

    def send_text(self, destination, text):
        self._check_destination(destination)
        if len(text) > 4096:
            raise ValueError("Telegram text exceeds the 4096-character limit")
        data = self._base_data()
        data["text"] = text
        return self._post("sendMessage", data=data)

    def send_document(self, destination, path, caption):
        self._check_destination(destination)
        path = Path(path)
        if not path.is_file():
            raise ValueError("Telegram document path does not exist")
        data = self._base_data()
        data["caption"] = str(caption)
        with path.open("rb") as stream:
            return self._post(
                "sendDocument", data=data,
                files={"document": (path.name, stream, "application/octet-stream")},
            )


class DeliveryLedger:
    """SQLite state machine for at-most-once scheduled Telegram delivery.

    An item whose external call began but did not return remains ``delivering``.
    It is intentionally not reclaimed automatically because Telegram has no
    server-side idempotency key and replay could duplicate a message.
    """

    def __init__(self, path, *, lease_seconds=120, clock=time.time):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("Delivery ledger path must be absolute")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = float(lease_seconds)
        if self.lease_seconds <= 0:
            raise ValueError("Delivery lease must be positive")
        self._clock = clock
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        with self._connection() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS report_deliveries (
                    delivery_key TEXT PRIMARY KEY,
                    report_type TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','delivering','delivered')),
                    lease_owner TEXT,
                    lease_expires REAL,
                    external_attempted INTEGER NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    external_id TEXT,
                    updated_at REAL NOT NULL
                )"""
            )

    @staticmethod
    def delivery_key(report_type, period_key, destination, artifact_type, chunk_index):
        return "\x1f".join(
            (report_type, period_key, destination, artifact_type, str(int(chunk_index)))
        )

    def ensure(self, report_type, period_key, destination, artifact_type, chunk_index):
        key = self.delivery_key(report_type, period_key, destination, artifact_type, chunk_index)
        now = self._clock()
        with self._connection() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO report_deliveries
                   (delivery_key,report_type,period_key,destination,artifact_type,chunk_index,state,updated_at)
                   VALUES (?,?,?,?,?,?,'pending',?)""",
                (key, report_type, period_key, destination, artifact_type, int(chunk_index), now),
            )
        return key

    def claim(self, key):
        now = self._clock()
        owner = uuid.uuid4().hex
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state,lease_expires,external_attempted FROM report_deliveries WHERE delivery_key=?",
                (key,),
            ).fetchone()
            if row is None:
                raise KeyError(key)
            if row["state"] == "delivered":
                return None
            if row["state"] == "delivering":
                if row["external_attempted"] or (row["lease_expires"] or 0) > now:
                    return None
            connection.execute(
                """UPDATE report_deliveries SET state='delivering',lease_owner=?,lease_expires=?,
                   external_attempted=0,attempt_count=attempt_count+1,updated_at=? WHERE delivery_key=?""",
                (owner, now + self.lease_seconds, now, key),
            )
        return DeliveryClaim(key, owner)

    def mark_external_attempt(self, claim):
        with self._connection() as connection:
            changed = connection.execute(
                """UPDATE report_deliveries SET external_attempted=1,updated_at=?
                   WHERE delivery_key=? AND state='delivering' AND lease_owner=?""",
                (self._clock(), claim.key, claim.owner),
            ).rowcount
        if changed != 1:
            raise ReportingError("Delivery claim is no longer owned")

    def mark_delivered(self, claim, external_id):
        with self._connection() as connection:
            changed = connection.execute(
                """UPDATE report_deliveries SET state='delivered',external_id=?,lease_owner=NULL,
                   lease_expires=NULL,updated_at=? WHERE delivery_key=? AND state='delivering' AND lease_owner=?""",
                (str(external_id), self._clock(), claim.key, claim.owner),
            ).rowcount
        if changed != 1:
            raise ReportingError("Delivery claim is no longer owned")

    def mark_definitive_failure(self, claim):
        with self._connection() as connection:
            changed = connection.execute(
                """UPDATE report_deliveries SET state='pending',lease_owner=NULL,lease_expires=NULL,
                   external_attempted=0,updated_at=? WHERE delivery_key=? AND state='delivering' AND lease_owner=?""",
                (self._clock(), claim.key, claim.owner),
            ).rowcount
        if changed != 1:
            raise ReportingError("Delivery claim is no longer owned")


class ReportRunner:
    """Deep module coordinating one scheduled report through a small interface."""

    def __init__(self, reader, sender, ledger, artifacts, *, destination, telegram_limit=4096):
        self._reader = reader
        self._sender = sender
        self._ledger = ledger
        self._artifacts = artifacts
        self._destination = str(destination)
        self._telegram_limit = int(telegram_limit)

    def _deliver(
        self, report, artifact_type, chunk_index, callback, *,
        identity_report_type=None, identity_period_key=None,
    ):
        key = self._ledger.ensure(
            identity_report_type or report.report_type,
            identity_period_key or report.period.key,
            self._destination,
            artifact_type, chunk_index,
        )
        claim = self._ledger.claim(key)
        if claim is None:
            return False
        self._ledger.mark_external_attempt(claim)
        try:
            external_id = callback()
        except DefinitiveDeliveryError:
            self._ledger.mark_definitive_failure(claim)
            raise
        except Exception as exc:
            # Preserve delivering/attempted so cron restart cannot duplicate an
            # external side effect whose outcome is unknown.
            raise AmbiguousDeliveryError("Delivery outcome is uncertain; automatic retry suppressed") from exc
        self._ledger.mark_delivered(claim, external_id)
        return True

    def run_current_month_pdf(self, now, request_identity):
        """Generate and deliver only the current-month PDF for one Telegram request."""
        identity = str(request_identity or "").strip()
        if not identity:
            raise ValueError("Manual report request identity is required")
        period = current_month_period(now)
        projects, transactions = self._reader.read()
        report = aggregate_report("monthly", period, projects, transactions)
        pdf_path = self._artifacts.build_manual_monthly_pdf(report, identity)
        sent = self._deliver(
            report,
            "pdf_attachment",
            0,
            lambda: self._sender.send_document(
                self._destination, pdf_path, f"PDF report {period.key}"
            ),
            identity_report_type="manual_monthly_pdf",
            identity_period_key=identity,
        )
        return RunResult("manual_monthly_pdf", period.key, int(sent), int(not sent))

    def run(self, report_type, now):
        period = ensure_schedule_due(report_type, now)
        projects, transactions = self._reader.read()
        report = aggregate_report(report_type, period, projects, transactions)
        delivered = 0
        skipped = 0
        for index, chunk in enumerate(chunk_telegram_text(render_telegram(report), limit=self._telegram_limit)):
            sent = self._deliver(
                report, "telegram_text", index,
                lambda chunk=chunk: self._sender.send_text(self._destination, chunk),
            )
            delivered += int(sent)
            skipped += int(not sent)
        if report_type in {"weekly", "monthly"}:
            html_path = self._artifacts.build_html(report)
            sent = self._deliver(
                report, "html_attachment", 0,
                lambda: self._sender.send_document(
                    self._destination, html_path, f"HTML report {period.key}"
                ),
            )
            delivered += int(sent)
            skipped += int(not sent)
        if report_type == "monthly":
            pdf_path = self._artifacts.build_monthly_pdf(report)
            sent = self._deliver(
                report, "pdf_attachment", 0,
                lambda: self._sender.send_document(
                    self._destination, pdf_path, f"PDF report {period.key}"
                ),
            )
            delivered += int(sent)
            skipped += int(not sent)
        return RunResult(report_type, period.key, delivered, skipped)
