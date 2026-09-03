#!/usr/bin/env python3
"""Cron entrypoint for Lekza scheduled project reports."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys

from scheduled_reporting import (
    ArtifactBuilder,
    DeliveryLedger,
    ReportRunner,
    ReportingSheetsReader,
    ScheduleGateError,
    TelegramSender,
)


def _required(environment, name):
    value = str(environment.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _absolute_external_path(environment, name, source_roots, *, must_exist=False):
    path = Path(_required(environment, name))
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    resolved = path.resolve()
    if any(resolved == root or resolved.is_relative_to(root) for root in source_roots):
        raise ValueError(f"{name} must be outside runtime source directories")
    if must_exist and not resolved.is_file():
        raise ValueError(f"{name} must identify an existing file")
    return resolved


def _load_google_module(plugin_root):
    path = plugin_root / "accounting-slip-bridge" / "google_adapters.py"
    if not path.is_file():
        raise RuntimeError("accounting-slip-bridge/google_adapters.py is unavailable")
    name = "lekza_reporting_google_adapters"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def build_runner(environment=None):
    environment = os.environ if environment is None else environment
    runtime_mode = _required(environment, "LEKZA_RUNTIME_ENV")
    if runtime_mode not in {"production", "staging"}:
        raise ValueError("LEKZA_RUNTIME_ENV must be production or staging")

    data_root = Path(__file__).resolve().parents[4]
    skills_root = (data_root / "skills").resolve()
    plugin_root = (data_root / "plugins").resolve()
    source_roots = (skills_root, plugin_root)
    ledger_path = _absolute_external_path(
        environment, "LEKZA_REPORT_LEDGER_DB", source_roots
    )
    archive_root = _absolute_external_path(
        environment, "LEKZA_REPORT_ARCHIVE_ROOT", source_roots
    )
    font_path = _absolute_external_path(
        environment, "LEKZA_REPORT_THAI_FONT_PATH", source_roots,
        must_exist=True,
    )

    google = _load_google_module(plugin_root)
    token_provider = google.RefreshingTokenProvider.from_environment(environment)
    reader = ReportingSheetsReader(
        _required(environment, "LEKZA_ACCOUNTING_SPREADSHEET_ID"),
        token_provider,
    )
    sender = TelegramSender(
        _required(environment, "LEKZA_REPORT_TELEGRAM_BOT_TOKEN"),
        _required(environment, "LEKZA_REPORT_TELEGRAM_CHAT_ID"),
        thread_id=environment.get("LEKZA_REPORT_TELEGRAM_THREAD_ID"),
    )
    artifacts = ArtifactBuilder(archive_root, font_path)
    runner = ReportRunner(
        reader,
        sender,
        DeliveryLedger(
            ledger_path,
            lease_seconds=float(environment.get("LEKZA_REPORT_DELIVERY_LEASE_SECONDS", "120")),
        ),
        artifacts,
        destination=sender.destination,
    )
    return runner, artifacts


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1 or arguments[0] not in {"daily", "weekly", "monthly"}:
        print("usage: run_report.py {daily|weekly|monthly}", file=sys.stderr)
        return 2
    report_type = arguments[0]
    runner, artifacts = build_runner()
    try:
        try:
            result = runner.run(report_type, datetime.now(timezone.utc))
        except ScheduleGateError as exc:
            print(json.dumps({"report_type": report_type, "status": "not_due", "reason": str(exc)}))
            return 0
        print(json.dumps({
            "report_type": result.report_type,
            "period": result.period_key,
            "delivered": result.delivered,
            "skipped": result.skipped,
            "status": "complete",
        }))
        return 0
    finally:
        artifacts.close()


if __name__ == "__main__":
    raise SystemExit(main())
