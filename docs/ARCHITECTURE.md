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
  -> accounting-transaction-buttons durable Telegram callbacks
  -> user confirmation
  -> Google Drive upload and Google Sheets append adapters
```

`accounting-slip-bridge` is the single OCR owner. `TransactionFlow` accepts an
existing OCR result and does not contain an OCR client or make network calls.

`telegram-clarify-pretty` patches the loaded Telegram adapter lazily. It preserves Hermes callback data (`cl:<id>:<index>`) and falls back to the stock `send_clarify` implementation if the patch cannot complete.

## Components

- `skills/accounting/process-slip-pipeline/scripts/process_slip.py`: command-line AksonOCR adapter used by the image trigger.
- `plugins/accounting-slip-bridge`: gateway hook that selects Telegram media, calls AksonOCR, and rewrites the agent input.
- `plugins/accounting-slip-bridge/transaction_flow.py`: SQLite-backed pending state, authorized state transitions, and durable Drive/Sheets checkpoints.
- `plugins/accounting-slip-bridge/google_adapters.py`: production Google REST adapters and the restart-safe save coordinator.
- `plugins/accounting-slip-bridge/telegram_wiring.py`: state-derived Telegram prompts and strict callback identities; it owns no OCR or session state.
- `plugins/accounting-transaction-buttons`: lazy Telegram adapter patch that routes Lekza callbacks and manual text to the durable controller while delegating all unrelated callbacks to Hermes.
- `plugins/telegram-clarify-pretty`: presentation layer for Telegram clarification buttons.
- Other accounting skills: confirmation, CRUD, multi-user context, reporting, and conversational policy.

## Trust boundaries

- `LEKZA_RUNTIME_ENV` is mandatory and accepts only `production` or `staging`.
  Unknown, empty, and misspelled modes fail before integration side effects.
- `AKSONOCR_API_KEY` and integration IDs come from the runtime environment.
- Real slips and transaction rows belong to runtime storage, not this repository.
- Writes require confirmation from the correct Telegram user.
- Staging OCR requires the incoming Telegram bot, chat, and user identities to
  match explicit staging allowlists before any media download or OCR request.

## Durable transaction state

Phase A stores transaction state in SQLite through
`LEKZA_TRANSACTION_STATE_DB`. The configured path must be an absolute runtime
data path outside source control. Approved upload/cache roots come from
`LEKZA_ALLOWED_UPLOAD_ROOTS`; `LEKZA_MAX_SLIP_BYTES` optionally sets the maximum
source size.

Phase D staging additionally requires an explicit `LEKZA_STAGING_DATA_ROOT`.
The SQLite DB and every upload/cache root must resolve beneath that external
staging root and outside the repository, `/data/plugins`, and
`/data/skills/accounting`.

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

If AksonOCR returns no labeled bank reference, the handoff still creates the
transaction using `transaction_id` as its durable identity. The row explicitly
sets `needs_reference` and Telegram requires authorized manual reference input
before project selection continues. Missing-reference rows are excluded from
the active tenant/reference uniqueness index until a validated reference is
stored atomically. Confirmation and the first external save checkpoint both
fail closed while `needs_reference` is set or the reference is empty.

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

## Telegram callback identity

Phase C callback data is `lk:<transaction-uuid-hex>:<base36-version>:<action>`;
project selections add a deterministic 12-character project token. Payloads are
strictly parsed and remain within Telegram's 64-byte limit. The full transaction
UUID is the callback identity and the durable row version rejects stale buttons.

The Telegram adapter is patched lazily after it is loaded. Every prompt is
rendered from SQLite, and manual input is located by actor and durable entry
mode, so callbacks and manual-entry steps survive process restarts. Confirm and
Retry call the production save coordinator, whose Drive reservation and Sheets
lease make external retries idempotent. OCR remains owned only by
`accounting-slip-bridge`.

After the existing AksonOCR success branch returns parsed fields,
`accounting-slip-bridge` hands that result to the button plugin. A hash of the
Telegram chat, actor, and stable inbound message ID is the durable handoff key,
so replay after restart recovers the same transaction even if the local media
cache path changes. An atomic SQLite
lease records `pending`/`delivering`/`delivered`, owner, expiry, attempt count,
and the delivered Telegram `message_id`. The lease transaction commits before
Telegram network I/O and an expired worker can be replaced after restart. A
delivered prompt is not normally sent again. Prompt delivery metadata does not
increment the callback version.

Telegram provides no true idempotency key. If Telegram accepts a prompt and the
worker crashes before persisting its `message_id`, lease takeover deliberately
uses at-least-once delivery and may rarely produce a duplicate. This favors
recovering a visible prompt over permanently suppressing it.

The button plugin validates that Hermes exposes callable
`_handle_callback_query` and `_handle_text_message` handlers before changing the
adapter class. An incompatible adapter is left untouched and emits an explicit
diagnostic. Callback data outside the `lk:` namespace always delegates to the
original Hermes callback handler.
