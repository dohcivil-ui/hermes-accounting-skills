#!/usr/bin/env python3
"""Deterministic, read-only accounting queries for Hermes conversations."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import sys


ALLOWED_INTENTS = frozenset({
    "totals", "project_count", "party_summary", "follow_up", "explain",
})
ALLOWED_PERIODS = frozenset({"today", "week", "month", "all"})
ALLOWED_METRICS = frozenset({"expense", "income", "net", "all"})
ALLOWED_PARTY_ROLES = frozenset({"payee", "payer", "both"})
ALLOWED_REQUEST_FIELDS = frozenset({
    "intent", "period", "metric", "project", "party", "party_role",
    "previous_context",
})


class AccountingQueryError(RuntimeError):
    """A read-only accounting query cannot be answered safely."""


def _load_module(name, path):
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AccountingQueryError(f"Unable to load runtime module: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _accounting_root():
    return Path(__file__).resolve().parents[2]


def _source_root():
    return Path(__file__).resolve().parents[4]


def _reporting_module():
    return _load_module(
        "lekza_conversational_scheduled_reporting",
        _accounting_root()
        / "scheduled-project-report"
        / "scripts"
        / "scheduled_reporting.py",
    )


def build_reader(environment=None):
    """Build the existing schema-locked ReportingSheetsReader."""
    environment = os.environ if environment is None else environment
    runtime_mode = str(environment.get("LEKZA_RUNTIME_ENV") or "").strip()
    if runtime_mode not in {"production", "staging"}:
        raise ValueError("LEKZA_RUNTIME_ENV must be production or staging")
    spreadsheet_id = str(
        environment.get("LEKZA_ACCOUNTING_SPREADSHEET_ID") or ""
    ).strip()
    if not spreadsheet_id:
        raise ValueError("LEKZA_ACCOUNTING_SPREADSHEET_ID is required")
    google = _load_module(
        "lekza_conversational_google_adapters",
        _source_root() / "plugins" / "accounting-slip-bridge" / "google_adapters.py",
    )
    reporting = _reporting_module()
    token_provider = google.RefreshingTokenProvider.from_environment(environment)
    return reporting.ReportingSheetsReader(spreadsheet_id, token_provider)


def _text(value):
    return " ".join(str(value or "").strip().split())


def _key(value):
    return _text(value).casefold()


def _money(value):
    return f"{Decimal(value):.2f}"


def _period(period_name, now, reporting):
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    today = now.astimezone(reporting.BANGKOK).date()
    if period_name == "today":
        start = today
    elif period_name == "week":
        start = today - timedelta(days=6)
    elif period_name == "month":
        start = today.replace(day=1)
    else:
        start = date(1899, 12, 30)
    return reporting.ReportingPeriod(period_name, start, today, f"{start}_{today}")


def _resolve_project(project_rows, requested):
    if requested is None:
        return None
    needle = _key(requested)
    if not needle:
        return None
    exact = []
    partial = []
    for row in project_rows:
        project_id = str(row.get("project_id") or "").strip()
        project_name = str(row.get("project_name") or "").strip()
        if needle in {_key(project_id), _key(project_name)}:
            exact.append((project_id, project_name))
        elif needle in _key(project_name):
            partial.append((project_id, project_name))
    matches = exact or partial
    unique = list(dict.fromkeys(matches))
    if len(unique) == 1:
        return {"project_id": unique[0][0], "project_name": unique[0][1]}
    if not unique:
        raise AccountingQueryError(f"Unknown project: {_text(requested)}")
    names = ", ".join(item[1] for item in unique)
    raise AccountingQueryError(f"Ambiguous project: {names}")


def _effective_request(request, previous_context):
    if not isinstance(request, dict):
        raise ValueError("request must be an object")
    unknown = set(request) - ALLOWED_REQUEST_FIELDS
    if unknown:
        raise ValueError(f"Unsupported request fields: {', '.join(sorted(unknown))}")
    previous = previous_context
    if previous is None:
        previous = request.get("previous_context")
    if previous is None:
        previous = {}
    if not isinstance(previous, dict):
        raise ValueError("previous_context must be an object")

    requested_intent = _text(request.get("intent"))
    if requested_intent not in ALLOWED_INTENTS:
        raise AccountingQueryError("Only supported read-only accounting queries are allowed")
    previous_intent = _text(previous.get("intent"))
    if requested_intent in {"follow_up", "explain"} and not previous_intent:
        raise AccountingQueryError(f"{requested_intent} requires previous context")
    if requested_intent == "follow_up":
        intent = previous_intent
    elif requested_intent == "explain":
        intent = previous_intent if previous_intent in {"totals", "party_summary"} else "totals"
    else:
        intent = requested_intent
    if intent not in {"totals", "project_count", "party_summary"}:
        raise AccountingQueryError("Previous context has an unsupported intent")

    def inherited(name, default=None):
        return request[name] if name in request else previous.get(name, default)

    period_name = _text(inherited("period", "today"))
    metric = _text(inherited("metric", "all"))
    party_role = _text(inherited("party_role", "both"))
    if period_name not in ALLOWED_PERIODS:
        raise AccountingQueryError("Unsupported accounting period")
    if metric not in ALLOWED_METRICS:
        raise AccountingQueryError("Unsupported accounting metric")
    if party_role not in ALLOWED_PARTY_ROLES:
        raise AccountingQueryError("Unsupported party role")
    return {
        "requested_intent": requested_intent,
        "intent": intent,
        "period": period_name,
        "metric": metric,
        "project": inherited("project"),
        "party": inherited("party"),
        "party_role": party_role,
    }


def _project_reports(report, selected):
    if selected is None:
        return tuple(report.projects)
    return tuple(
        project for project in report.projects
        if (
            selected["project_id"]
            and project.project_id == selected["project_id"]
        ) or project.project_name == selected["project_name"]
    )


def _filtered_rows(transaction_rows, period, selected, reporting, projects):
    by_id = {project.project_id: project for project in projects if project.project_id}
    by_name = {project.project_name: project for project in projects}
    rows = []
    for row in transaction_rows:
        if _text(row.get("status")) != "confirmed":
            continue
        transaction_date = reporting._transaction_date(row.get("date"))
        if not period.start <= transaction_date <= period.end:
            continue
        # Match aggregate_report: a known ID wins; otherwise fall back to name.
        project_id = str(row.get("project_id") or "").strip()
        project_name = str(row.get("project") or "").strip()
        project = by_id.get(project_id) or by_name.get(project_name)
        if project is None:
            raise reporting.MalformedSheetRowError(
                "Confirmed transaction references an unknown project"
            )
        if selected is not None and (
            project.project_id != selected["project_id"]
            or project.project_name != selected["project_name"]
        ):
            continue
        copied = dict(row)
        copied["project_id"] = project.project_id
        copied["project"] = project.project_name
        copied["_date"] = transaction_date
        copied["_amount"] = reporting._amount(row.get("amount"))
        rows.append(copied)
    return rows


def _context(effective, selected):
    return {
        "intent": effective["intent"],
        "period": effective["period"],
        "metric": effective["metric"],
        "project": selected["project_name"] if selected else None,
        "party": _text(effective["party"]) or None,
        "party_role": effective["party_role"],
    }


def _totals(report, selected, metric):
    projects = _project_reports(report, selected)
    income = sum((project.income for project in projects), Decimal("0"))
    expense = sum((project.expense for project in projects), Decimal("0"))
    net = income - expense
    count = sum(project.count for project in projects)
    values = {"income": income, "expense": expense, "net": net}
    value = values.get(metric)
    return {
        "metric": metric,
        "value": _money(value) if value is not None else None,
        "currency": "THB",
        "income": _money(income),
        "expense": _money(expense),
        "net": _money(net),
        "transaction_count": count,
        "projects": [
            {
                "project_id": project.project_id,
                "project_name": project.project_name,
                "income": _money(project.income),
                "expense": _money(project.expense),
                "net": _money(project.net),
                "transaction_count": project.count,
            }
            for project in projects
        ],
    }


def _resolve_party(rows, requested, roles):
    if requested is None or not _text(requested):
        return None
    needle = _key(requested)
    names = {
        _text(row.get(role))
        for row in rows for role in roles if _text(row.get(role))
    }
    exact = sorted(name for name in names if _key(name) == needle)
    partial = sorted(name for name in names if needle in _key(name))
    matches = exact or partial
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise AccountingQueryError(f"Unknown payer/payee: {_text(requested)}")
    raise AccountingQueryError(f"Ambiguous payer/payee: {', '.join(matches)}")


def _party_summary(rows, selected_party, party_role, metric):
    roles = ("payee", "payer") if party_role == "both" else (party_role,)
    totals = defaultdict(lambda: {"amount": Decimal("0"), "count": 0})
    for row in rows:
        if metric in {"income", "expense"} and row["type"] != metric:
            continue
        amount = row["_amount"]
        if metric == "net" and row["type"] == "expense":
            amount = -amount
        for role in roles:
            name = _text(row.get(role))
            if not name or (selected_party and name != selected_party):
                continue
            key = (role, name)
            totals[key]["amount"] += amount
            totals[key]["count"] += 1
    summaries = [
        {
            "role": role,
            "name": name,
            "amount": _money(values["amount"]),
            "transaction_count": values["count"],
        }
        for (role, name), values in sorted(
            totals.items(), key=lambda item: (-item[1]["amount"], item[0])
        )
    ]
    return {
        "party": selected_party,
        "party_role": party_role,
        "currency": "THB",
        "summaries": summaries,
    }


def _ranked_group(rows, field):
    totals = defaultdict(lambda: {"amount": Decimal("0"), "count": 0})
    for row in rows:
        label = _text(row.get(field)) or "ไม่ระบุ"
        totals[label]["amount"] += row["_amount"]
        totals[label]["count"] += 1
    return [
        {
            "name": name,
            "amount": _money(values["amount"]),
            "transaction_count": values["count"],
        }
        for name, values in sorted(
            totals.items(), key=lambda item: (-item[1]["amount"], item[0])
        )
    ]


def _explanation(rows, metric):
    if metric == "expense":
        relevant = [row for row in rows if row.get("type") == "expense"]
    elif metric == "income":
        relevant = [row for row in rows if row.get("type") == "income"]
    else:
        relevant = list(rows)
    ordered = sorted(
        relevant,
        key=lambda row: (-row["_amount"], str(row.get("transaction_id") or "")),
    )
    return {
        "metric": metric,
        "currency": "THB",
        "by_category": _ranked_group(
            [row for row in relevant if row.get("type") == "expense"], "category"
        ),
        "by_payee": _ranked_group(
            [row for row in relevant if row.get("type") == "expense"], "payee"
        ),
        "by_payer": _ranked_group(
            [row for row in relevant if row.get("type") == "income"], "payer"
        ),
        "largest_transactions": [
            {
                "transaction_id": _text(row.get("transaction_id")),
                "date": row["_date"].isoformat(),
                "project": _text(row.get("project")),
                "type": _text(row.get("type")),
                "category": _text(row.get("category")) or None,
                "payer": _text(row.get("payer")) or None,
                "payee": _text(row.get("payee")) or None,
                "amount": _money(row["_amount"]),
            }
            for row in ordered[:5]
        ],
    }


class AccountingQueryEngine:
    """Deep read-only module behind one structured query interface."""

    def __init__(self, reader):
        self._reader = reader
        self._reporting = _reporting_module()

    def query(self, request, *, previous_context=None, now=None):
        effective = _effective_request(request, previous_context)
        now = datetime.now(timezone.utc) if now is None else now
        period = _period(effective["period"], now, self._reporting)
        project_rows, transaction_rows = self._reader.read()
        count_projects = effective["intent"] == "project_count"
        report = self._reporting.aggregate_report(
            "conversational", period, project_rows,
            () if count_projects else transaction_rows,
        )
        selected = _resolve_project(project_rows, effective["project"])
        if count_projects:
            return {
                "ok": True,
                "read_only": True,
                "intent": "project_count",
                "context": _context(effective, selected),
                "result": {
                    "total": len(report.projects),
                    "active": sum(
                        _key(row.get("status")) == "active" for row in project_rows
                    ),
                },
                "grounding": {"reader": "ReportingSheetsReader", "source": "Projects"},
            }
        rows = _filtered_rows(
            transaction_rows, period, selected, self._reporting, report.projects
        )
        roles = ("payee", "payer") if effective["party_role"] == "both" else (effective["party_role"],)
        party = _resolve_party(rows, effective["party"], roles)
        if party is not None:
            rows = [row for row in rows if any(_text(row.get(role)) == party for role in roles)]
            report = self._reporting.aggregate_report(
                "conversational", period, project_rows, rows
            )
        effective["party"] = party
        context = _context(effective, selected)

        if effective["requested_intent"] == "explain":
            intent = "explain"
            result = _explanation(rows, effective["metric"])
        elif effective["intent"] == "party_summary":
            intent = "party_summary"
            result = _party_summary(rows, party, effective["party_role"], effective["metric"])
        else:
            intent = "totals"
            result = _totals(report, selected, effective["metric"])

        return {
            "ok": True,
            "read_only": True,
            "intent": intent,
            "context": context,
            "result": result,
            "grounding": {
                "reader": "ReportingSheetsReader",
                "status": "confirmed",
                "period_start": period.start.isoformat(),
                "period_end": period.end.isoformat(),
                "matched_transaction_count": len(rows),
            },
        }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run a deterministic read-only Lekza accounting query"
    )
    parser.add_argument("--request-json", required=True)
    parser.add_argument("--now", help="Timezone-aware ISO datetime for controlled runs")
    arguments = parser.parse_args(argv)
    request = json.loads(arguments.request_json)
    now = datetime.fromisoformat(arguments.now) if arguments.now else None
    result = AccountingQueryEngine(build_reader()).query(request, now=now)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
