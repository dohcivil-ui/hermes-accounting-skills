# Scheduled Project Reporting Runbook

This runbook documents a future Hostinger cron installation. Do not install or
enable these entries until the normal release and production deployment gates
have been approved.

## Runtime command

The production source paths are:

```text
/data/skills/accounting/scheduled-project-report/scripts/run_report.py
/data/plugins/accounting-slip-bridge/google_adapters.py
```

The runner accepts exactly one argument: `daily`, `weekly`, or `monthly`.
Weekly runs are accepted only on Sunday in `Asia/Bangkok`. Monthly runs are
accepted only on the actual last Bangkok calendar day, including February in a
leap year.

## Required external configuration

Provide these values through Hostinger runtime environment configuration, never
in Git:

- `LEKZA_RUNTIME_ENV`
- Existing Google OAuth variables used by `accounting-slip-bridge`
- `LEKZA_ACCOUNTING_SPREADSHEET_ID`
- `LEKZA_REPORT_TELEGRAM_BOT_TOKEN`
- `LEKZA_REPORT_TELEGRAM_CHAT_ID`
- Optional `LEKZA_REPORT_TELEGRAM_THREAD_ID`
- `LEKZA_REPORT_LEDGER_DB`: absolute SQLite path outside `/data/skills` and `/data/plugins`
- `LEKZA_REPORT_ARCHIVE_ROOT`: absolute monthly-PDF archive root outside source directories
- `LEKZA_REPORT_THAI_FONT_PATH`: absolute path to a Thai-capable TTF such as Noto Sans Thai or TH Sarabun New
- Optional `LEKZA_REPORT_DELIVERY_LEASE_SECONDS`

The archive, ledger, font, credentials, and generated artifacts must remain
outside Git. Restrict the ledger and archive directories to the runtime user.

## Cron schedule

Preferred configuration when the Hostinger cron implementation supports
`CRON_TZ`:

```cron
CRON_TZ=Asia/Bangkok
0 21 * * * /usr/bin/python3 /data/skills/accounting/scheduled-project-report/scripts/run_report.py daily
10 21 * * 0 /usr/bin/python3 /data/skills/accounting/scheduled-project-report/scripts/run_report.py weekly
20 21 28-31 * * /usr/bin/python3 /data/skills/accounting/scheduled-project-report/scripts/run_report.py monthly
```

The monthly command is intentionally scheduled on days 28-31; the runner exits
without delivery unless the current Bangkok date is the month's final day.

If `CRON_TZ` is not supported, first verify that the host cron clock is UTC and
use the explicit UTC equivalents below. Bangkok is UTC+07:00 and has no daylight
saving transition:

```cron
0 14 * * * /usr/bin/python3 /data/skills/accounting/scheduled-project-report/scripts/run_report.py daily
10 14 * * 0 /usr/bin/python3 /data/skills/accounting/scheduled-project-report/scripts/run_report.py weekly
20 14 28-31 * * /usr/bin/python3 /data/skills/accounting/scheduled-project-report/scripts/run_report.py monthly
```

Do not use the UTC entries unless the host cron timezone has been positively
verified. Do not enable both schedule variants.

## Delivery recovery

Normal cron retries skip every `delivered` ledger item. A definitively rejected
Telegram request returns to `pending` and can retry. An interrupted or ambiguous
external call remains `delivering` because Telegram Bot API has no idempotency
key; do not reset it blindly, since doing so can duplicate a message or
attachment. Reconcile the destination and ledger before any manual recovery.
