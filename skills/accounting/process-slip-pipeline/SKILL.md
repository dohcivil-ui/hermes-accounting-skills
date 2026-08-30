---
name: process-slip-pipeline
description: Use when processing bank slip images from Telegram via AksonOCR script.
platforms: [telegram]
triggers:
  - type: image
    handler: python3 /data/skills/accounting/process-slip-pipeline/scripts/process_slip.py
---

# Process Slip Pipeline (AksonOCR-First)

## Mandatory Execution & Implementation
1. **Script Path**: `/data/skills/accounting/process-slip-pipeline/scripts/process_slip.py`
2. **HTTP Endpoint**: `https://backend.aksonocr.com/api/v2/upload`
3. **Environment**: Uses `AKSONOCR_API_KEY` from container environment.
4. **Execution Flow**:
   - When a Telegram image is received, the gateway maps the image attachment path and passes it to `process_slip.py`.
   - `process_slip.py` performs the HTTP POST request to AksonOCR.
   - Hermes receives the raw OCR text and confidence, parses the fields, and presents:
     - OCR source: AksonOCR
     - confidence: [score]
     - raw_ocr_text: [3-5 lines snippet]
     - parsed fields (date, amount, payer, payee, reference_no, note)
     - status: waiting_for_confirm
### 6. Master Data Management (Users & Projects)
- **Users Table**: Before saving any transaction, inspect the `Users` sheet using the Telegram `user_id`. If not present, create a new user entry automatically (1 time). If already present, do not duplicate.
- **Projects Table**: Check the project name/ID against the `Projects` sheet. If it exists, use the existing `project_id`. If it does not exist, ask the user if it's a new project. Only add to `Projects` after explicit user confirmation. Prevent duplicate projects.
- **Transactions Appending**: Upon user confirmation:
  1. Upload slip image to Google Drive folder (`LEKZA_SLIP_FOLDER_ID`) with name `YYYY-MM-DD_reference_no.jpg`.
  2. Get `webViewLink` and assign to `Transactions.slip_url`.
  3. Verify `reference_no` in `Transactions` to prevent duplicate transaction entries.
  4. Append exactly 1 row to `Transactions`.
  5. Master data (Users and Projects) are maintained independently and not recreated per transaction.
