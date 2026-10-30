"""Shared-runner and SBOM wiring for the fixture-only device plugin slice."""
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "tools/xtos-devices/xtos_devices.py"
README = ROOT / "tools/xtos-devices/README.md"


class DevicePluginIntegrationTests(unittest.TestCase):
    def test_shared_runner_executes_plugin_and_its_integration_guard_once(self):
        runner = (ROOT / "scripts/test.sh").read_text()
        self.assertEqual(runner.count("test_xtos_devices.py"), 1)
        self.assertEqual(runner.count("test_p4_device_plugin_integration.py"), 1)

    def test_sbom_records_plugin_as_excluded_and_binds_runtime_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "sbom.json"
            subprocess.run(["python3", "-B", str(ROOT / "tools/xtos-sbom/gen_sbom.py"),
                            "--out", str(output)], cwd=ROOT, check=True,
                           capture_output=True, text=True, timeout=30)
            bom = json.loads(output.read_text())
        components = {component["name"]: component for component in bom["components"]}
        self.assertIn("xtos-device-plugins", components)
        plugin = components["xtos-device-plugins"]
        self.assertEqual(plugin["scope"], "excluded")
        self.assertEqual(plugin["licenses"][0]["license"]["id"], "Apache-2.0")
        self.assertIn(hashlib.sha256(PLUGIN.read_bytes()).hexdigest(),
                      {entry["content"] for entry in plugin["hashes"]})
        system = next(component for component in bom["components"] if component["name"] == "xtos")
        system_ref = system["bom-ref"]
        dependencies = {entry["ref"]: entry["dependsOn"] for entry in bom["dependencies"]}
        self.assertNotIn(plugin["bom-ref"], dependencies[system_ref])

    def test_plugin_readme_reflects_current_focused_scope_and_limits(self):
        text = README.read_text()
        self.assertIn("11 unittest methods", text)
        self.assertIn("transport_cleanup_failed", text)
        self.assertIn("transport_connect_failed", text)
        self.assertIn("not a physical", text.lower())
        self.assertNotIn("run_suite.py final-normal", text)
        self.assertNotIn("run_suite.py final-optimized", text)
        self.assertIn("uuid.uuid4().hex", text)
        self.assertIn('"current-normal-$run_label"', text)
        self.assertIn('"current-optimized-$run_label"', text)


if __name__ == "__main__":
    unittest.main()