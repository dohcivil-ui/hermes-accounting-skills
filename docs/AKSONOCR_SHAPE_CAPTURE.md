# Sanitized AksonOCR Response-Shape Capture

Use this only in the designated Hostinger staging/test terminal. It does not
deploy code, restart Hermes, or authorize production access.

The capture tool reads a raw JSON response locally and emits metadata only:
key names, JSON types, `pages` shape, reference-field paths, and Markdown
format flags. It never copies scalar response values to its output.

## Hostinger terminal

Keep the raw response outside Git with restrictive permissions. From a checkout
containing the capture tool, run:

```text
python3 tests/tools/aksonocr_shape_capture.py \
  /path/outside-git/aksonocr-response.json \
  --output /path/outside-git/aksonocr-shape-evidence.json
```

Inspect the evidence before transferring it:

```text
python3 -m json.tool /path/outside-git/aksonocr-shape-evidence.json
```

Transfer only `aksonocr-shape-evidence.json`. Never transfer the raw response,
slip image, API key, request headers, URLs, IDs, parsed values, or OCR text.

## Repo intake

Place the received evidence outside Git first. Validate it locally without
printing source data:

```text
python tests/tools/aksonocr_shape_capture.py \
  --validate-evidence /path/outside-git/aksonocr-shape-evidence.json
```

Use the evidence to update a fully synthetic fixture: preserve only observed
keys, nesting, list shapes, reference paths, and Markdown formatting. Replace
the reference with `PHASED-SMOKE-20260901-01` and all other values with
synthetic data. Do not commit the received evidence unless it has been reviewed
and contains metadata only.

Run the focused regression and full suite before any staging deployment:

```text
python -m unittest tests.integration.test_phase_d_ocr_handoff_regression -v
python -m unittest discover -s tests -p "test_*.py" -v
```
