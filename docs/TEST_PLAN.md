# Test Plan

## Baseline command

Run from the repository root:

```text
python -m unittest discover -s tests -p "test_*.py" -v
```

## Baseline scope

- Contract: required runtime paths, skill frontmatter, and plugin manifests.
- Unit: AksonOCR error/success behavior and Telegram button-label behavior.
- Safety: forbidden runtime artifacts and likely credential assignments.
- Integration: durable transaction state, mocked external checkpoints, restart recovery, authorization, and idempotency; live external services are never called by tests.

## Fixture policy

Fixtures must be synthetic or redacted. Never store real slips, Telegram identifiers, credentials, Google Sheet rows, or production OCR responses.

## Completion criteria

- All baseline tests pass locally.
- No test performs network access or writes to Google services.
- Production files synced from Hostinger retain byte-for-byte parity with the approved snapshot.

## Future acceptance gates

Baseline tests remain required. Add the following acceptance gates as their roadmap phases enter implementation:

- Telegram Button Flow: validate button rendering, callback identity, and correct-user handling.
- Pending/Confirm/Back/Cancel: validate state ownership, transitions, cancellation, and return navigation.
- Drive → `slip_url` → `Transactions`: validate upload, link assignment, duplicate prevention, and exactly-one-row transactional save.
- Project Summary: validate project totals for Revenue, Expense, Profit, and Margin.
- Close Job: validate close, reopen, archive, and deletion only when no history exists.
- Thai PDF render validation: embed a Thai font and inspect rendered pages before delivery.
- AI Audit safety: AI may suggest classifications or anomalies but must not invent accounting values or bypass human confirmation.
- Cost Meter: validate OCR requests/credits, AI tokens, cost per slip, cost per Job, and monthly totals.

## Scheduled reporting acceptance

Synthetic unit and integration tests cover Bangkok calendar boundaries, daily
and rolling seven-day weekly periods, Sunday and month-end gates including leap
years, confirmed-only aggregation, zero-activity projects, totals and counts,
weekly top payees, monthly category/payee breakdowns, deterministic Telegram
chunking, UTF-8 HTML, embedded Thai-font PDF generation, read-only frozen-schema
Sheets access, and durable suppression of duplicate messages and attachments
across retries and process restarts. These tests perform no live network calls
and do not invoke the Phase D staging smoke procedure.

On-demand monthly PDF coverage includes both English and Thai command routing,
authorized actor checks, Bangkok day-one-through-today periods, confirmed-only
aggregation with zero-activity projects, PDF delivery, replay suppression by
Telegram message identity, and separation from the scheduled month-end ledger.

Runtime-path regressions reject the local repository root and every descendant.
Hostinger-layout regressions reject `/data/skills`, `/data/plugins`, and their
descendants for ledger, archive, and font configuration while accepting
absolute persistent paths beneath `/data/lekza-production`. Reporting accepts
only known transaction statuses: `confirmed` is aggregated, `deleted` is
skipped, and empty, malformed, or unknown values fail closed.

## Phase A durable-state acceptance

The deterministic SQLite integration harness covers:

- restart after OCR state creation;
- restart after Confirm before Drive;
- restart after Drive metadata is persisted and before Sheets;
- restart after the Sheets row identity is persisted;
- callback replay and stale-button rejection through state versions;
- two-connection concurrent Confirm convergence;
- wrong-user transition rejection;
- tenant-scoped normalized `reference_no` uniqueness;
- failure/retry state and retry count persistence;
- allowed-root, traversal, symlink, image type, extension, and size checks;
- minimal OCR-field retention and numeric amount normalization.
- missing OCR reference handoff, authorized manual reference entry, restart
  recovery, duplicate-reference rejection, and fail-closed confirmation.

The Drive and Sheets steps in these tests are durable state checkpoints only.
Production adapters and real Telegram callback wiring remain outside Phase A.

## Phase B adapter acceptance

Mocked Google REST integration tests cover frozen schema validation, Drive
upload/retry/crash recovery, atomic Sheets append and duplicate prevention,
malformed responses, numeric amount preservation, and restart recovery through
the production save coordinator. No test performs a live Google API call.

### Opt-in Google smoke test

Use a disposable spreadsheet with the frozen schemas and a disposable Drive
folder. Never provide production resource IDs. Set all variables below, then run:

```text
LEKZA_RUN_GOOGLE_LIVE_TESTS=1
LEKZA_LIVE_TEST_RESOURCE_ACK=designated-test-resources
LEKZA_GOOGLE_ACCESS_TOKEN=<short-lived test credential>
LEKZA_TEST_SLIP_FOLDER_ID=<disposable test folder>
LEKZA_TEST_ACCOUNTING_SPREADSHEET_ID=<disposable test spreadsheet>
LEKZA_TEST_UPLOAD_ROOT=<absolute dedicated local test directory>
LEKZA_TEST_TRANSACTION_STATE_DB=<absolute DB path beneath test upload root>
python -m unittest tests.live.test_google_adapters_live -v
```

The smoke test is skipped unless explicitly enabled. It rejects test resource
IDs that equal configured production IDs, writes only a synthetic one-baht row
and synthetic image, and reuses its persistent test state on rerun.

## Phase C Telegram acceptance

Synthetic integration tests cover compact callback identity, project/user/type/
category selection, Back, Cancel, Confirm, Retry, duplicate Confirm delivery,
stale versions, wrong users, manual entry, process restart, malformed payloads,
the image/OCR-to-durable-state handoff, prompt delivery before/after-send crash
recovery, expired-lease restart takeover, delivered replay suppression,
incompatible Hermes adapter detection, and coexistence with non-Lekza Hermes
callbacks. Prompts are asserted from durable state; Telegram and Google network
APIs are not called.

## Duplicate slip protection acceptance

Synthetic integration tests verify that Telegram message replay, a new message
with byte-identical source bytes, concurrent delivery, and process restart each
produce one OCR call. They also cover tenant-scoped source SHA-256 uniqueness,
case/space reference normalization, one-character near-reference candidate
warnings without auto-merge, fail-closed missing references, duplicate-reference
responses that never fall back to a generic OCR rewrite, and suppression of a
second transaction/Sheets identity for the same exact source image. Perceptual
matches are candidate signals only and require corroborating transaction data.

## Phase D staging acceptance

Use `docs/PHASE_D_STAGING_RUNBOOK.md` only with designated Hostinger staging,
Telegram test, AksonOCR test-authorized, and disposable Google resources. The
staging guard must pass before service restart. Capture evidence for restart
recovery, duplicate Confirm convergence, a staging-only failure and Retry,
exactly one Sheets row, one reserved Drive identity, and terminal durable state.
The post-interaction verifier must run in a fresh process with a minimum retry
count of one. No Phase D command authorizes production deployment.
