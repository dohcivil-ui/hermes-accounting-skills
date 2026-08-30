from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = [ROOT / "skills", ROOT / "plugins", ROOT / "docs", ROOT / "tests"]
SECRET_ASSIGNMENT = re.compile(
    r"(?i)(api[_-]?key|password|passwd|client[_-]?secret|private[_-]?key)"
    r"\s*[:=]\s*[\"'][^\"']{8,}[\"']"
)


class SecretSafetyTests(unittest.TestCase):
    def test_no_likely_literal_credentials(self):
        findings = []
        for scan_root in SCAN_ROOTS:
            for path in scan_root.rglob("*"):
                if not path.is_file() or path.suffix not in {".py", ".md", ".yaml", ".json"}:
                    continue
                text = path.read_text(encoding="utf-8")
                if SECRET_ASSIGNMENT.search(text):
                    findings.append(str(path.relative_to(ROOT)))

        self.assertEqual(findings, [])
