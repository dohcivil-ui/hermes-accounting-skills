"""Build a staging-only candidate; never import or replace the running plugin."""
import argparse
import ast
import hashlib
from pathlib import Path

EXPECTED_BASE_SHA256 = "32888c5c7713b79abe17f89a2179532b955b748de519a385641083a0ca262eac"


def prepare(base, source):
    if hashlib.sha256(base).hexdigest() != EXPECTED_BASE_SHA256:
        raise ValueError("Runtime baseline differs; stop and review the diff")
    tree = ast.parse(source)
    helper = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name == "_add_labeled_text_fields")
    lines = source.splitlines(keepends=True)
    text = base.decode("utf-8")
    text = text.replace("def _normalize_ocr_result_for_handoff(ocr_result):",
                        "".join(lines[helper.lineno-1:helper.end_lineno]) + "\n\n\n"
                        + "def _normalize_ocr_result_for_handoff(ocr_result):", 1)
    anchor = '    normalized["parsed"] = parsed\n    return normalized'
    if text.count(anchor) != 1:
        raise ValueError("Unexpected normalization boundary")
    text = text.replace(anchor, '    _add_labeled_text_fields(parsed, ocr_result)\n' + anchor)
    compile(text, "staging-candidate", "exec")
    return text.encode("utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    candidate = prepare(args.base.read_bytes(), args.source.read_text(encoding="utf-8"))
    with args.output.open("xb") as stream:
        stream.write(candidate)
    print("CANDIDATE_OK", hashlib.sha256(candidate).hexdigest(), args.output)
