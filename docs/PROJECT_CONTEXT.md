# Project Context

Lekza is a Telegram-first accounting assistant for construction-project income and expenses. Hostinger Managed Hermes runs the production agent, accounting skills, and Telegram plugins.

## Core workflow

1. A user submits a bank slip through Telegram.
2. `accounting-slip-bridge` sends the image to AksonOCR.
3. Hermes interprets the OCR response and asks the submitting user to confirm.
4. Only a valid confirmation may lead to Google Drive or Google Sheets writes.
5. Reports and summaries read confirmed transaction data by project and payee/payer.

## Domain terms

- Project: a construction job tracked independently.
- Transaction: one income or expense record.
- User: a Telegram user associated with submitted or confirmed work.
- Confirmation: the required user decision before a write.
- Slip: the source bank-transfer image; real slips are never test fixtures.

## Sync baseline

The source baseline for branch `sync/hostinger-runtime-20260830` is the Hostinger runtime snapshot created on 2026-08-30. Snapshot metadata and runtime configuration are audit inputs, not repository source.

## Product Direction

- Lekza is the first Reference Implementation for the broader contractor-accounting product direction.
- The next product target is a reusable SME Starter Template.
- Current delivery remains focused on stabilizing Lekza Production first.
- Future capabilities must not create MVP scope creep; Phase 9–10 work stays out of scope until the Lekza production baseline is stable.
