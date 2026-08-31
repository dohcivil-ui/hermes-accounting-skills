from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
PLUGINS_ROOT = ROOT / "plugins"


class PluginManifestTests(unittest.TestCase):
    def test_manifests_match_plugin_directories(self):
        failures = []
        manifests = sorted(PLUGINS_ROOT.glob("*/plugin.yaml"))
        self.assertEqual(len(manifests), 3)

        for manifest in manifests:
            text = manifest.read_text(encoding="utf-8")
            name = re.search(r"(?m)^name:\s*[\"']?([^\s\"']+)", text)
            version = re.search(r"(?m)^version:\s*.+$", text)
            if not name or name.group(1) != manifest.parent.name:
                failures.append(f"{manifest}: name does not match directory")
            if not version:
                failures.append(f"{manifest}: version is missing")
            if not (manifest.parent / "__init__.py").is_file():
                failures.append(f"{manifest}: __init__.py is missing")

        self.assertEqual(failures, [])
