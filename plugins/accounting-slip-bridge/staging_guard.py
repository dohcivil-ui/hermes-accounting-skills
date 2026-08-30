"""Fail-closed configuration guard for Lekza staging smoke tests."""

from __future__ import annotations

import json
import os
from pathlib import Path


STAGING_ACK = "designated-staging-resources"
TEST_BOT_ACK = "designated-test-bot"


def _required(environment, name):
    value = str(environment.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is required for staging")
    return value


def _csv_set(environment, name):
    values = {item.strip() for item in _required(environment, name).split(",")}
    values.discard("")
    if not values:
        raise ValueError(f"{name} must contain at least one identifier")
    return values


def validate_staging_environment(environ=None):
    """Validate designated staging resources without returning secrets."""
    environment = os.environ if environ is None else environ
    if _required(environment, "LEKZA_RUNTIME_ENV").lower() != "staging":
        raise ValueError("LEKZA_RUNTIME_ENV must be staging for Phase D")
    if _required(environment, "LEKZA_STAGING_RESOURCE_ACK") != STAGING_ACK:
        raise ValueError(
            f"LEKZA_STAGING_RESOURCE_ACK must be {STAGING_ACK}"
        )
    if _required(environment, "LEKZA_STAGING_TELEGRAM_BOT_ACK") != TEST_BOT_ACK:
        raise ValueError(
            f"LEKZA_STAGING_TELEGRAM_BOT_ACK must be {TEST_BOT_ACK}"
        )

    for name in ("AKSONOCR_API_KEY", "LEKZA_GOOGLE_ACCESS_TOKEN"):
        _required(environment, name)

    folder_id = _required(environment, "LEKZA_SLIP_FOLDER_ID")
    spreadsheet_id = _required(environment, "LEKZA_ACCOUNTING_SPREADSHEET_ID")
    production_folder_id = _required(
        environment, "LEKZA_PRODUCTION_SLIP_FOLDER_ID"
    )
    production_spreadsheet_id = _required(
        environment, "LEKZA_PRODUCTION_ACCOUNTING_SPREADSHEET_ID"
    )
    if folder_id == production_folder_id:
        raise ValueError("staging Drive folder must differ from production")
    if spreadsheet_id == production_spreadsheet_id:
        raise ValueError("staging spreadsheet must differ from production")

    db_path = Path(_required(environment, "LEKZA_TRANSACTION_STATE_DB")).expanduser()
    if not db_path.is_absolute():
        raise ValueError("LEKZA_TRANSACTION_STATE_DB must be absolute")
    roots = [
        Path(item.strip()).expanduser()
        for item in _required(environment, "LEKZA_ALLOWED_UPLOAD_ROOTS").split(os.pathsep)
        if item.strip()
    ]
    if not roots or any(not root.is_absolute() for root in roots):
        raise ValueError("LEKZA_ALLOWED_UPLOAD_ROOTS must contain absolute paths")

    try:
        projects = json.loads(_required(environment, "LEKZA_ACTIVE_PROJECTS_JSON"))
    except json.JSONDecodeError as exc:
        raise ValueError("LEKZA_ACTIVE_PROJECTS_JSON must be JSON") from exc
    if not isinstance(projects, list) or not projects or any(
        not isinstance(item, str) or not item.strip() for item in projects
    ):
        raise ValueError("LEKZA_ACTIVE_PROJECTS_JSON must be a non-empty string list")

    chat_ids = _csv_set(environment, "LEKZA_STAGING_TELEGRAM_CHAT_IDS")
    user_ids = _csv_set(environment, "LEKZA_STAGING_TELEGRAM_USER_IDS")
    return {
        "runtime_env": "staging",
        "drive_folder_id": folder_id,
        "spreadsheet_id": spreadsheet_id,
        "db_path": str(db_path),
        "upload_roots": [str(root) for root in roots],
        "telegram_chat_ids": chat_ids,
        "telegram_user_ids": user_ids,
        "projects": projects,
    }


def validate_staging_actor(chat_id, telegram_user_id, environ=None):
    config = validate_staging_environment(environ)
    if str(chat_id) not in config["telegram_chat_ids"]:
        raise PermissionError("Telegram chat is not approved for staging")
    if str(telegram_user_id) not in config["telegram_user_ids"]:
        raise PermissionError("Telegram user is not approved for staging")
    return True


def main():
    config = validate_staging_environment()
    print(json.dumps({
        "ok": True,
        "runtime_env": config["runtime_env"],
        "upload_root_count": len(config["upload_roots"]),
        "telegram_chat_count": len(config["telegram_chat_ids"]),
        "telegram_user_count": len(config["telegram_user_ids"]),
        "project_count": len(config["projects"]),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
