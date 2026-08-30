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
- Integration: reserved for mocked end-to-end Hermes flows; live external services are never called by tests.

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
