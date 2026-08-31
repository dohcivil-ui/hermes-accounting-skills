# Phase D Hostinger Staging Smoke Test

This runbook is for designated staging/test resources only. It does not
authorize a production deployment, merge, tag, or release.

## Safety prerequisites

- Use a disposable Google Drive folder and spreadsheet with the frozen schemas.
- Use a dedicated Telegram test bot, chat, and user.
- Use an isolated absolute SQLite path and upload root under staging runtime data.
- Keep all credentials and real identifiers in Hostinger environment variables.
- Record the previous staging artifact or commit before changing staging.
- Record the current staging service configuration without copying secrets into
  Git, and create a recoverable backup outside the checkout of the staging
  SQLite database and any persistent upload/cache files. Use SQLite's online
  backup facility or stop the staging writer while copying the database.
- Record backup paths, timestamps, checksums, ownership, and restore commands.
  Verify that the staging service account can read the checkout and write the
  staging data root, database directory, upload roots, and sanitized log path.

The runtime fails closed in staging unless all variables below are present:

```text
LEKZA_RUNTIME_ENV=staging
LEKZA_STAGING_RESOURCE_ACK=designated-staging-resources
LEKZA_STAGING_TELEGRAM_BOT_ACK=designated-test-bot
LEKZA_STAGING_TELEGRAM_BOT_IDS=<comma-separated test bot IDs>
LEKZA_STAGING_TELEGRAM_CHAT_IDS=<comma-separated test chat IDs>
LEKZA_STAGING_TELEGRAM_USER_IDS=<comma-separated test user IDs>
LEKZA_PRODUCTION_SLIP_FOLDER_ID=<production ID used only for inequality guard>
LEKZA_PRODUCTION_ACCOUNTING_SPREADSHEET_ID=<production ID used only for inequality guard>
AKSONOCR_API_KEY=<staging-authorized secret>
LEKZA_GOOGLE_ACCESS_TOKEN=<short-lived test credential>
LEKZA_SLIP_FOLDER_ID=<disposable staging folder ID>
LEKZA_ACCOUNTING_SPREADSHEET_ID=<disposable staging spreadsheet ID>
LEKZA_STAGING_DATA_ROOT=<absolute external staging data root>
LEKZA_TRANSACTION_STATE_DB=<absolute staging SQLite path>
LEKZA_ALLOWED_UPLOAD_ROOTS=<absolute staging upload/cache root(s)>
LEKZA_ACTIVE_PROJECTS_JSON=["Phase D Test Project"]
LEKZA_TENANT_ID=<isolated staging tenant>
LEKZA_MAX_SLIP_BYTES=10485760
LEKZA_PROMPT_DELIVERY_LEASE_SECONDS=120
```

The production IDs are guard inputs only and must not be selected as the active
`LEKZA_SLIP_FOLDER_ID` or `LEKZA_ACCOUNTING_SPREADSHEET_ID`.
The state DB and all allowed upload roots must resolve beneath
`LEKZA_STAGING_DATA_ROOT`; repository paths, `/data/plugins`, and
`/data/skills/accounting` are rejected. Runtime mode is mandatory: production
must explicitly use `LEKZA_RUNTIME_ENV=production`, while this runbook requires
`staging`. Missing or unknown modes fail before OCR or other integration work.

## Runtime prerequisites

- Use the existing Hostinger Hermes staging service and record its Hermes and
  `python3` versions before deployment. The selected Python runtime must install
  the pinned root `requirements.txt` and compile the deployed Python sources.
- Keep staging skill/plugin paths separate from production `/data/skills/accounting`
  and `/data/plugins`; verify the restart command targets staging only.
- Allow outbound HTTPS from staging to AksonOCR, Google Drive API, Google Sheets
  API, and Telegram. Verify DNS/TLS connectivity without logging credentials.
- Prepare a disposable Drive folder and spreadsheet. The `Transactions`,
  `Projects`, and `Users` tabs must use the exact frozen headers in
  `docs/ARCHITECTURE.md`.
- Provision the dedicated Telegram test bot and allowlisted chat/user, an
  AksonOCR-authorized staging key, and a short-lived Google token with access
  only to the disposable resources.
- Ensure the external staging data root and its database/upload directories are
  persistent across a Hermes service restart and have sufficient free space.

## Staging deployment procedure (after Review Gate D1 approval)

Replace placeholders with reviewed Hostinger staging paths. Do not run these
against `/data/skills/accounting` or `/data/plugins` when those paths serve
production.

```text
git fetch origin main
git worktree add --detach <staging-checkout> <approved-commit-sha>
python3 -m pip install --requirement <staging-checkout>/requirements.txt
rsync -a --delete <staging-checkout>/skills/accounting/ <staging-skills-path>/accounting/
rsync -a --delete <staging-checkout>/plugins/ <staging-plugins-path>/
python3 <staging-plugins-path>/accounting-slip-bridge/staging_guard.py
<reviewed-staging-restart-command>
```

The first three commands are preparation. The two `rsync --delete` operations
and service restart are destructive staging actions and require the approved
Gate D1 plus verified path substitutions immediately before execution.

## Smoke sequence

1. Send one synthetic, uniquely referenced one-baht image to the allowlisted test bot/chat.
2. Verify the real AksonOCR request succeeds, creates one durable transaction,
   and renders the expected inline buttons.
3. Exercise the edit path: advance to review, use Back, change one field by
   button or manual entry, return to review, and verify the durable value and
   callback version changed without creating another transaction.
4. Send a second uniquely referenced synthetic slip, press Cancel, and verify
   its terminal state is `cancelled` with no Drive file and no Sheets row.
5. Restart only the staging Hermes service before confirming the first
   transaction, then recover the same transaction and current buttons/state.
6. Confirm once. Record `transaction_id`, Drive file ID, Sheets row identity,
   `slip_url`, and terminal state.
7. Deliver the same Confirm callback again; it must converge without a second
   Drive object or Sheets row.
8. For the network/API failure case, use a separately reviewed staging-only
   fault (for example, temporarily revoke the test spreadsheet permission),
   Confirm, observe `failed`, restore permission, and press Retry. Never alter
   production permissions.
9. Restart staging again and run the verifier in a fresh process:

```text
python3 <staging-plugins-path>/accounting-slip-bridge/phase_d_smoke.py \
  <transaction-id> --chat-id <test-chat-id> --user-id <test-user-id> \
  --minimum-retry-count 1
```

The verifier replays the save twice, checks terminal durable state, verifies the
reserved Drive identity, and fails if the transaction is absent or duplicated
in Sheets column A.

10. Rehearse rollback using the recorded previous staging artifact and
    configuration. Restore the pre-deploy SQLite backup only if the previous
    artifact cannot read the upgraded staging state; retain a separate copy of
    the failed-run database and logs first. Restart staging, run the previous
    smoke check, and record the result. Re-deploying the candidate afterward is
    a separate destructive staging action requiring approval.

## Evidence to capture

- Approved commit SHA and staging path substitutions.
- Previous staging artifact/configuration identifiers and backup/restore record.
- Sanitized logs containing `transaction_id` and stage, never secrets or image URLs.
- State before/after each restart and `retry_count` after the simulated failure.
- Drive file ID and exactly one matching Sheets row.
- Full test output and verifier JSON output.

## Rollback

Stop the staging service, preserve a diagnostic copy of the Phase D SQLite DB
and sanitized logs, restore the recorded previous staging artifact to the same
verified staging paths, and restore the prior staging environment. Restore the
pre-deploy SQLite backup only when required for schema compatibility. Verify
ownership and permissions, restart the staging service, and run its previous
smoke check. Do not delete the failed-run evidence. Production is not part of
this rollback.
