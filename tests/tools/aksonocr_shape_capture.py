"""Emit metadata-only AksonOCR response-shape evidence.

Run this only where the raw designated-test response already exists. Raw scalar
values are never copied to the output.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


SCHEMA_VERSION = 1
REFERENCE_KEYS = {
    "reference",
    "referenceno",
    "referencenumber",
    "refno",
    "transactionreference",
    "transactionref",
    "bankreference",
    "slipreference",
}
TYPE_NAMES = {
    dict: "object",
    list: "array",
    str: "string",
    int: "number",
    float: "number",
    bool: "boolean",
    type(None): "null",
}
EVIDENCE_KEYS = {
    "schema_version",
    "top_level_keys",
    "response_shape",
    "pages_shapes",
    "reference_locations",
    "markdown_reference_shapes",
}
SENSITIVE_KEY = re.compile(
    r"(?i)(?:token|secret|password|credential|authorization|api.?key)"
)
DYNAMIC_KEY = re.compile(
    r"(?:\d{5,}|[A-Za-z0-9_-]{24,})"
)
URL_OR_AUTH = re.compile(r"(?i)(?:https?|file)://|bearer\s+")
NUMERIC_VALUE = re.compile(r"(?<!\d)\d{5,}(?!\d)")
MARKDOWN_REFERENCE = re.compile(
    r"(?im)"
    r"(?P<label_marker>\*{1,3}|_{1,3})?"
    r"(?P<label>หมายเลขอ้างอิง|รหัสอ้างอิง|เลขที่รายการ|"
    r"reference\s*(?:\*{1,3}|_{1,3})?\s*(?:no|number)|"
    r"ref\s*(?:\*{1,3}|_{1,3})?\s*(?:no|number)|reference|ref)"
    r"(?P<label_close>\*{1,3}|_{1,3})?"
    r"(?P<before_separator>\s*)"
    r"(?P<separator>[:：#-])"
    r"(?P<separator_close>\*{1,3}|_{1,3})?"
    r"(?P<after_separator>\s*)"
    r"(?P<value_marker>\*{1,3}|_{1,3})?"
    r"(?P<value>[A-Za-z0-9][A-Za-z0-9._/-]{2,127})"
    r"(?P<value_close>\*{1,3}|_{1,3})?"
)


def _safe_key(value):
    key = str(value)
    if SENSITIVE_KEY.search(key):
        return "<sensitive-key>"
    if len(key) > 64 or DYNAMIC_KEY.fullmatch(key):
        return "<dynamic-key>"
    if any(ord(character) < 32 for character in key):
        return "<invalid-key>"
    return key


def _path(parent, key):
    return f"{parent}.{_safe_key(key)}"


def _shape(value, depth=0):
    if depth >= 8:
        return {"type": "depth-limited"}
    if isinstance(value, dict):
        keys = sorted({_safe_key(key) for key in value})
        return {
            "type": "object",
            "keys": keys,
            "fields": {
                _safe_key(key): _shape(child, depth + 1)
                for key, child in value.items()
                if _safe_key(key)
                not in {"<sensitive-key>", "<dynamic-key>", "<invalid-key>"}
            },
        }
    if isinstance(value, list):
        return {
            "type": "array",
            "count": len(value),
            "item_shapes": [_shape(item, depth + 1) for item in value[:3]],
        }
    return {"type": TYPE_NAMES.get(type(value), "unknown")}


def _walk(value, path="$"):
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, _path(path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")


def _reference_locations(response):
    locations = []
    for path, value in _walk(response):
        if "." not in path:
            continue
        key = re.sub(r"[^a-z0-9]", "", path.rsplit(".", 1)[-1].lower())
        if key in REFERENCE_KEYS:
            locations.append(
                {"path": path, "value_type": TYPE_NAMES.get(type(value), "unknown")}
            )
    return locations


def _pages_shapes(response):
    pages = []
    for path, value in _walk(response):
        if path.rsplit(".", 1)[-1] != "pages":
            continue
        entry = {"path": path, "type": TYPE_NAMES.get(type(value), "unknown")}
        if isinstance(value, list):
            entry["count"] = len(value)
            entry["item_shapes"] = [_shape(item) for item in value[:3]]
        pages.append(entry)
    return pages


def _whitespace_shape(value):
    return {
        "has_newline": "\n" in value or "\r" in value,
        "newline_count": value.count("\n"),
        "space_count": sum(character in " \t" for character in value),
    }


def _markdown_reference_shapes(response):
    shapes = []
    for path, value in _walk(response):
        if not isinstance(value, str):
            continue
        for match in MARKDOWN_REFERENCE.finditer(value):
            label = match.group("label").lower()
            shapes.append(
                {
                    "path": path,
                    "label_language": (
                        "thai"
                        if any("ก" <= character <= "๙" for character in label)
                        else "english"
                    ),
                    "label_kind": "reference",
                    "label_emphasis": bool(
                        match.group("label_marker")
                        or match.group("label_close")
                        or match.group("separator_close")
                        or "*" in label
                        or "_" in label
                    ),
                    "separator": match.group("separator"),
                    "whitespace_before_separator": _whitespace_shape(
                        match.group("before_separator")
                    ),
                    "whitespace_after_separator": _whitespace_shape(
                        match.group("after_separator")
                    ),
                    "value_emphasis": bool(
                        match.group("value_marker") or match.group("value_close")
                    ),
                    "value": "<REFERENCE_NO>",
                }
            )
    return shapes


def capture_shape(response):
    if not isinstance(response, dict):
        raise ValueError("AksonOCR response must be a JSON object")
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "top_level_keys": sorted({_safe_key(key) for key in response}),
        "response_shape": _shape(response),
        "pages_shapes": _pages_shapes(response),
        "reference_locations": _reference_locations(response),
        "markdown_reference_shapes": _markdown_reference_shapes(response),
    }
    validate_evidence(evidence)
    return evidence


def validate_evidence(evidence):
    serialized = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
    if URL_OR_AUTH.search(serialized) or NUMERIC_VALUE.search(serialized):
        raise ValueError("Sanitized evidence contains a forbidden value")
    if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_KEYS:
        raise ValueError("Sanitized evidence does not match the expected schema")
    if evidence.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported sanitized evidence schema version")
    for item in evidence.get("markdown_reference_shapes", []):
        if item.get("value") != "<REFERENCE_NO>":
            raise ValueError("Markdown evidence must use the reference placeholder")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("input", nargs="?", default="-", help="raw JSON file or - for stdin")
    parser.add_argument("--output", help="sanitized evidence path; defaults to stdout")
    parser.add_argument(
        "--validate-evidence",
        action="store_true",
        help="validate an already-sanitized evidence file without printing it",
    )
    args = parser.parse_args(argv)
    raw = (
        sys.stdin.read()
        if args.input == "-"
        else Path(args.input).read_text(encoding="utf-8")
    )
    decoded = json.loads(raw)
    if args.validate_evidence:
        validate_evidence(decoded)
        sys.stdout.write("Sanitized evidence is valid.\n")
        return
    evidence = capture_shape(decoded)
    rendered = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)


if __name__ == "__main__":
    main()
