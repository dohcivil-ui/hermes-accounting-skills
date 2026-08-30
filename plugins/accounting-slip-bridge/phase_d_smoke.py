"""Phase D post-interaction verifier for designated staging resources."""

from __future__ import annotations

import argparse
import json

import google_adapters
import staging_guard
import transaction_flow


def verify(transaction_id, chat_id, telegram_user_id, minimum_retry_count=0):
    staging_guard.validate_staging_actor(chat_id, telegram_user_id)
    actor = {
        "platform": "telegram",
        "chat_id": str(chat_id),
        "telegram_user_id": str(telegram_user_id),
    }
    store = transaction_flow.SQLiteStateStore.from_environment()
    try:
        flow = transaction_flow.TransactionFlow.from_environment(store)
        drive_adapter = google_adapters.GoogleDriveAdapter.from_environment()
        sheets_adapter = google_adapters.GoogleSheetsAdapter.from_environment()
        pipeline = google_adapters.ProductionSavePipeline(
            flow, drive_adapter, sheets_adapter
        )
        # A fresh process plus two save replays proves restart recovery and
        # duplicate Confirm convergence against the durable terminal state.
        first = pipeline.save(transaction_id, **actor)
        second = pipeline.save(transaction_id, **actor)
        if first["current_state"] != "confirmed" or second["current_state"] != "confirmed":
            raise RuntimeError("transaction is not confirmed")
        if int(second.get("retry_count") or 0) < int(minimum_retry_count):
            raise RuntimeError("transaction did not record the expected retry")
        if not second.get("drive_file_id") or not second.get("sheets_row_identity"):
            raise RuntimeError("external durable identities are incomplete")

        drive = drive_adapter.verify_upload(transaction_id, second["drive_file_id"])
        row = sheets_adapter.find_transaction_row(transaction_id)
        if row != second["sheets_row_identity"]:
            raise RuntimeError("durable Sheets row identity does not match Google")
        return {
            "ok": True,
            "transaction_id": second["transaction_id"],
            "state": second["current_state"],
            "retry_count": second["retry_count"],
            "drive_file_id": drive["file_id"],
            "sheets_row_identity": row,
        }
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("transaction_id")
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--minimum-retry-count", type=int, default=0)
    args = parser.parse_args()
    print(json.dumps(verify(
        args.transaction_id,
        args.chat_id,
        args.user_id,
        args.minimum_retry_count,
    ), sort_keys=True))


if __name__ == "__main__":
    main()
