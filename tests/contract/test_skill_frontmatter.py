from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = ROOT / "skills/accounting"


class SkillFrontmatterTests(unittest.TestCase):
    def test_every_skill_has_named_frontmatter(self):
        skill_files = sorted(SKILLS_ROOT.glob("*/SKILL.md"))
        self.assertEqual(len(skill_files), 7)

        failures = []
        for skill_file in skill_files:
            text = skill_file.read_text(encoding="utf-8")
            if not text.startswith("---\n") or "\n---\n" not in text[4:]:
                failures.append(f"{skill_file}: missing frontmatter delimiters")
                continue
            frontmatter = text.split("---", 2)[1]
            match = re.search(r"(?m)^name:\s*([^\s]+)\s*$", frontmatter)
            if not match or match.group(1) != skill_file.parent.name:
                failures.append(f"{skill_file}: name does not match directory")

        self.assertEqual(failures, [])
