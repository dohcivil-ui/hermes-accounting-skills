# Staging labeled OCR fallback

Fill only missing amount/date/payer/payee from explicitly labeled OCR text.
Support plain or emphasized labels with same-line/next-line values, positive
amounts with at most two decimals, and valid ISO dates. Conflicting or malformed
candidates remain missing. Unsupported layouts require manual input. No new
network requests, persisted-state repair, or old-slip replay.

The staging bridge is older than the branch. Do not replace it with the full
repository __init__.py. `prepare_g2_labeled_ocr.py BASE SOURCE OUTPUT` checks the
approved runtime SHA256 and copies only the pure helper plus its normalization
call from SOURCE. OUTPUT must not exist. It neither imports the plugin nor
installs the result. Inspect the generated diff before installation.

Approved baseline SHA256:
32888c5c7713b79abe17f89a2179532b955b748de519a385641083a0ca262eac
Candidate SHA256:
03adb9327f3f9acab0a9171e7a889bb2088a47f07c4956aac6b2c325a11c5b80

Validation: 298 tests run, 296 passed, 2 skipped. Seven synthetic labeled-field
tests also passed against the generated staging candidate. No live OCR,
Telegram, Google, deployment, or restart performed. Existing pending records
are not backfilled. The review-only synthetic slip remains unconfirmed.
