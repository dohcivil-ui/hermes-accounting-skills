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
