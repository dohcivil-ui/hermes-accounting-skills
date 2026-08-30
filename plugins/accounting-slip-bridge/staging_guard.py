"""Fail-closed configuration guard for Lekza staging smoke tests."""

from __future__ import annotations

import json
import os
from pathlib import Path


STAGING_ACK = "designated-staging-resources"
TEST_BOT_ACK = "designated-test-bot"
VALID_RUNTIME_MODES = {"production", "staging"}
STAGING_DATA_ROOT_ENV = "LEKZA_STAGING_DATA_ROOT"
FORBIDDEN_APPLICATION_ROOTS = (
    Path("/data/plugins"),
    Path("/data/skills/accounting"),
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


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


def validate_runtime_environment(environ=None):
    """Require an explicit, known runtime mode before integration work."""
    environment = os.environ if environ is None else environ
    mode = str(environment.get("LEKZA_RUNTIME_ENV") or "").strip().lower()
    if mode not in VALID_RUNTIME_MODES:
        raise ValueError("LEKZA_RUNTIME_ENV must be production or staging")
    return mode


def _resolved_absolute(environment, name):
    path = Path(_required(environment, name)).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be absolute")
    return path.resolve()


def _is_within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_external_staging_path(path, staging_root, name):
    if path == REPOSITORY_ROOT or _is_within(path, REPOSITORY_ROOT):
        raise ValueError(f"{name} must be outside the repository checkout")
    if path == staging_root or not _is_within(path, staging_root):
        raise ValueError(f"{name} must be beneath {STAGING_DATA_ROOT_ENV}")
    for application_root in FORBIDDEN_APPLICATION_ROOTS:
        resolved_root = application_root.resolve()
        if path == resolved_root or _is_within(path, resolved_root):
            raise ValueError(f"{name} must be outside runtime application paths")


def validate_staging_environment(environ=None):
    """Validate designated staging resources without returning secrets."""
    environment = os.environ if environ is None else environ
    if validate_runtime_environment(environment) != "staging":
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

    staging_root = _resolved_absolute(environment, STAGING_DATA_ROOT_ENV)
    db_path = _resolved_absolute(environment, "LEKZA_TRANSACTION_STATE_DB")
    _validate_external_staging_path(
        db_path, staging_root, "LEKZA_TRANSACTION_STATE_DB"
    )
    roots = [
        Path(item.strip()).expanduser()
        for item in _required(environment, "LEKZA_ALLOWED_UPLOAD_ROOTS").split(os.pathsep)
        if item.strip()
    ]
    if not roots or any(not root.is_absolute() for root in roots):
        raise ValueError("LEKZA_ALLOWED_UPLOAD_ROOTS must contain absolute paths")
    roots = [root.resolve() for root in roots]
    for root in roots:
        _validate_external_staging_path(
            root, staging_root, "LEKZA_ALLOWED_UPLOAD_ROOTS"
        )

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
    bot_ids = _csv_set(environment, "LEKZA_STAGING_TELEGRAM_BOT_IDS")
    return {
        "runtime_env": "staging",
        "staging_data_root": str(staging_root),
        "drive_folder_id": folder_id,
        "spreadsheet_id": spreadsheet_id,
        "db_path": str(db_path),
        "upload_roots": [str(root) for root in roots],
        "telegram_chat_ids": chat_ids,
        "telegram_user_ids": user_ids,
        "telegram_bot_ids": bot_ids,
        "projects": projects,
    }


def validate_staging_actor(chat_id, telegram_user_id, environ=None):
    config = validate_staging_environment(environ)
    if str(chat_id) not in config["telegram_chat_ids"]:
        raise PermissionError("Telegram chat is not approved for staging")
    if str(telegram_user_id) not in config["telegram_user_ids"]:
        raise PermissionError("Telegram user is not approved for staging")
    return True


def validate_staging_ocr_actor(bot_id, chat_id, telegram_user_id, environ=None):
    config = validate_staging_environment(environ)
    if str(bot_id) not in config["telegram_bot_ids"]:
        raise PermissionError("Telegram bot is not approved for staging")
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
        "telegram_bot_count": len(config["telegram_bot_ids"]),
        "project_count": len(config["projects"]),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
