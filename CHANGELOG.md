# Changelog

## [0.2.0] - Unreleased

Previous stable release: `v0.1.0`.

### Added

- SQLite-backed durable Telegram transaction state with restart recovery,
  actor authorization, stale-button rejection, and duplicate reference checks
- Telegram project, user, type, category, Back, Cancel, Confirm, manual-entry,
  and Retry flows rendered from durable state
- Idempotent Google Drive upload and Google Sheets append adapters using durable
  upload identity and transaction-scoped Sheets leases
- Staging isolation guard, post-interaction verifier, and opt-in disposable
  Google integration test
- Production dependency pins in `requirements.txt`
- Durable pre-OCR duplicate-slip protection using Telegram message identity,
  tenant-scoped source SHA-256, OCR claim leases, restart recovery, and
  perceptual duplicate-candidate warnings without automatic merging
- Scheduled daily, rolling seven-day weekly, and month-end project reporting
  with Telegram summaries, UTF-8 HTML attachments, archived Thai-font PDFs,
  and durable per-chunk/per-attachment delivery suppression

### Fixed

- Reject scheduled-report ledger, archive, and font paths anywhere inside the
  resolved repository/runtime source tree, and fail closed on unknown
  transaction statuses while preserving known soft-delete filtering
- Hermes skill metadata compatibility while preserving the production
  `/data/skills/accounting` runtime path
- Enum-keyed Telegram adapter lookup and durable handling of remote Telegram
  media beneath approved upload roots
- Fail-closed staging dispatch, redacted exception diagnostics, and cleanup of
  downloaded slips that fail before durable handoff

### Operationally Breaking Changes

- `LEKZA_RUNTIME_ENV` is mandatory and must be exactly `production` or `staging`.
- Durable flow requires an external absolute `LEKZA_TRANSACTION_STATE_DB` and
  one or more absolute `LEKZA_ALLOWED_UPLOAD_ROOTS` paths.
- Google Sheets headers must exactly match the frozen schemas in
  `docs/ARCHITECTURE.md`; incompatible schemas fail closed.
- Scheduled reporting requires external absolute ledger/archive/font paths and
  an environment-configured Telegram destination. Cron remains disabled until
  a separately approved production deployment.

### Verification

- Focused scheduled-reporting tests: `30 passed`; zero failures and zero errors.
- Full synthetic suite excluding the prohibited Phase D smoke module:
  `175 passed / 1 skipped`; zero failures and zero errors.
- Intentionally skipped test:
  `live.test_google_adapters_live.GoogleAdaptersLiveSmokeTests.test_confirmed_transaction_reaches_designated_test_drive_and_sheet`.
  It requires explicitly acknowledged disposable Google resources and remains
  part of the staging evidence plan.
- Python compile, secret scan, and `git diff --check`: passed.

### Known Limitations

- Hostinger staging and production have not yet been deployed or verified for
  this release candidate.
- The edit path uses Back plus button/manual reselection; there is no separate
  callback named `Edit`.
- Telegram has no true prompt-delivery idempotency key. A crash after Telegram
  accepts a prompt but before its message ID is persisted can rarely cause a
  duplicate prompt; transaction callbacks and external writes remain durable.
- Google credentials and designated resource IDs must be provisioned in the
  runtime environment; they are not stored in this repository.
- Telegram Bot API has no server-side idempotency key. To prevent duplicates,
  a scheduled delivery interrupted after its external call starts remains in
  `delivering` and requires operator reconciliation instead of automatic replay.

### Rollback

- Production rollback target remains `v0.1.0` until `v0.2.0` is explicitly
  approved, tagged, released, deployed, and accepted.
- Follow `docs/RELEASE_POLICY.md` and the staging rollback procedure in
  `docs/PHASE_D_STAGING_RUNBOOK.md`; preserve the failed-run database and
  sanitized logs as evidence.

## [0.1.0] - 2026-08-30

### Added

- Hostinger Managed Hermes runtime baseline
- accounting skills
- accounting-slip-bridge
- telegram-clarify-pretty
- accounting-button-flow
- AksonOCR integration
- baseline Test Harness
- project foundation documentation

### Verified

- Telegram → accounting-slip-bridge
- AksonOCR HTTP 201
- OCR confidence
- Google Sheets read/write

### In Progress / Known Limitations

- Telegram Button Flow v2 end-to-end
- Google Drive upload
- `Transactions.slip_url`
- transactional save verification
- Google Drive/Sheets integration test ยัง skipped
