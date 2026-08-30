# Roadmap

## 1. Runtime parity

- Validate this repository against the 2026-08-30 Hostinger baseline.
- Deploy only after review of the sync diff.

## 2. Transaction flow verification

- Verify Telegram Button Flow v2 end-to-end.
- Verify Google Drive upload and `Transactions.slip_url`.
- Verify duplicate prevention and exactly-one-row transaction writes.

## 3. Test depth

- Add contract fixtures for Hermes gateway events.
- Add mocked AksonOCR failure and timeout coverage.
- Add mocked Google Drive and Google Sheets integration tests.
- Add a deployment smoke-test checklist for Hostinger Managed Hermes.

## 4. Operational safety

- Add automated secret scanning.
- Define log retention and redaction requirements.
- Document rollback and runtime-version verification.
