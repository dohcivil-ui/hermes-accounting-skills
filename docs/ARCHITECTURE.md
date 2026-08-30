# Architecture

## Runtime layout

```text
/data/skills/accounting  <- repository skills/accounting
/data/plugins            <- repository plugins
```

## Slip flow

```text
Telegram image
  -> accounting-slip-bridge pre_gateway_dispatch hook
  -> AksonOCR /api/v2/upload
  -> durable TransactionFlow state created from the existing OCR result
  -> Hermes rewrite with parsed fields and confidence
  -> future Telegram callback wiring and user confirmation
  -> future Google Drive upload and Google Sheets append adapters
```

`accounting-slip-bridge` is the single OCR owner. `TransactionFlow` accepts an
existing OCR result and does not contain an OCR client or make network calls.

`telegram-clarify-pretty` patches the loaded Telegram adapter lazily. It preserves Hermes callback data (`cl:<id>:<index>`) and falls back to the stock `send_clarify` implementation if the patch cannot complete.

## Components

- `skills/accounting/process-slip-pipeline/scripts/process_slip.py`: command-line AksonOCR adapter used by the image trigger.
- `plugins/accounting-slip-bridge`: gateway hook that selects Telegram media, calls AksonOCR, and rewrites the agent input.
- `plugins/accounting-slip-bridge/transaction_flow.py`: SQLite-backed pending state, authorized state transitions, and durable Drive/Sheets checkpoints.
- `plugins/accounting-slip-bridge/google_adapters.py`: production Google REST adapters and the restart-safe save coordinator; Telegram callbacks do not instantiate these adapters yet.
- `plugins/telegram-clarify-pretty`: presentation layer for Telegram clarification buttons.
- Other accounting skills: confirmation, CRUD, multi-user context, reporting, and conversational policy.

## Trust boundaries

- `AKSONOCR_API_KEY` and integration IDs come from the runtime environment.
- Real slips and transaction rows belong to runtime storage, not this repository.
- Writes require confirmation from the correct Telegram user.

## Durable transaction state

Phase A stores transaction state in SQLite through
`LEKZA_TRANSACTION_STATE_DB`. The configured path must be an absolute runtime
data path outside source control. Approved upload/cache roots come from
`LEKZA_ALLOWED_UPLOAD_ROOTS`; `LEKZA_MAX_SLIP_BYTES` optionally sets the maximum
source size.

The operation identity is a UUID `transaction_id`. The tenant-scoped normalized
`reference_no` is the business duplicate key. Every transition checks
`transaction_id`, `platform`, `chat_id`, and `telegram_user_id`, then uses the
stored `version` for optimistic concurrency control.

Supported durable states are:

```text
waiting_project -> waiting_user -> waiting_type -> waiting_category
-> waiting_review -> confirmed_intent -> drive_pending -> drive_uploaded
-> sheets_pending -> confirmed

active external state -> failed -> retry state
interactive state -> cancelled
```

Only required parsed OCR fields and confidence are persisted. Raw OCR text,
provider responses, API credentials, and customer-sensitive diagnostic data are
not stored by this module. Source images must resolve beneath an approved root,
must not be symlinks, and must pass image type, extension, and size validation.

Phase B adds production Drive/Sheets adapters without registering Telegram
callbacks or changing OCR ownership. Drive pre-generates and durably reserves a
file ID before upload. Sheets validates all frozen headers and appends the row
with `batchUpdate` only while holding the SQLite single-writer claim.

## Frozen Google Sheets schemas

Column names and order are exact. Adapters fail closed when any header differs.

```text
Transactions: transaction_id, reference_no, date, payer, payee, project_id,
project, type, category, amount, note, confidence, submitted_by, drive_file_id,
slip_url, status, created_at, confirmed_at

Projects: project_id, project_name, customer, status, start_date, created_by,
created_at

Users: telegram_user_id, name, frequent_projects, frequent_keywords,
last_actions, created_at, updated_at
```

`transaction_id` is the external idempotency identity. A Drive retry reuses the
reserved file ID and a Sheets retry recovers the existing row from column A.

Production Python dependencies are pinned in repository-root `requirements.txt`.
Install them on Hostinger with `python3 -m pip install --requirement requirements.txt`.

Sheets duplicate prevention does not rely on developer metadata uniqueness.
The save coordinator atomically persists a transaction-scoped lease owner and
expiry, then commits before any Google request. Only the current owner may scan
or append, and the owner is revalidated immediately before append with enough
remaining lease time to exceed the HTTP timeout. A crashed worker leaves a
short-lived lease; after expiry, its replacement takes ownership and searches
column A for `transaction_id` before deciding whether to append. Leases are per
transaction, so unrelated transactions do not wait for each other's network I/O.
