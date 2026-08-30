# Current State

Last verified from the Hostinger Managed Hermes snapshot created on 2026-08-30.

## Working in the runtime

- Telegram dispatch reaches `accounting-slip-bridge`.
- AksonOCR responds successfully over HTTP.
- OCR confidence is returned to Hermes.
- Google Sheets read/write is available.
- `accounting-button-flow` is installed.
- `telegram-clarify-pretty` is installed.

## In progress or not yet verified end-to-end

- Telegram Button Flow v2.
- Google Drive slip upload.
- Population of `Transactions.slip_url`.
- Transactional save verification.

## Repository baseline

- Production skills live under `skills/accounting/`.
- Production plugins live under `plugins/`.
- Runtime exports, logs, credentials, and real accounting data are excluded from Git.

## Technical debt

- `plugins/accounting-slip-bridge/__init__.py` retains trailing whitespace from the approved 2026-08-30 Hostinger snapshot. Keep it unchanged while source parity is required; clean it only in a separately reviewed maintenance change.
