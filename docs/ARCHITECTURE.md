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
  -> Hermes rewrite with OCR text, parsed fields, and confidence
  -> user confirmation through native clarify contract
  -> Google Drive upload and Google Sheets append
```

`telegram-clarify-pretty` patches the loaded Telegram adapter lazily. It preserves Hermes callback data (`cl:<id>:<index>`) and falls back to the stock `send_clarify` implementation if the patch cannot complete.

## Components

- `skills/accounting/process-slip-pipeline/scripts/process_slip.py`: command-line AksonOCR adapter used by the image trigger.
- `plugins/accounting-slip-bridge`: gateway hook that selects Telegram media, calls AksonOCR, and rewrites the agent input.
- `plugins/telegram-clarify-pretty`: presentation layer for Telegram clarification buttons.
- Other accounting skills: confirmation, CRUD, multi-user context, reporting, and conversational policy.

## Trust boundaries

- `AKSONOCR_API_KEY` and integration IDs come from the runtime environment.
- Real slips and transaction rows belong to runtime storage, not this repository.
- Writes require confirmation from the correct Telegram user.
