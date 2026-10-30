"""Shared-runner, source installer and SBOM wiring for the serial plugin slice."""
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
        for name in ("test_xtos_devices.py", "test_xtos_serial_transport.py",
                     "test_xtos_cyton_protocol.py", "test_xtos_cyton_serial_plugin.py",
                     "test_xtos_ganglion_protocol.py", "test_xtos_ganglion_plugin.py",
                     "test_xtos_ganglion_stream.py",
                     "test_device_serial_install.py", "test_p4_device_plugin_integration.py"):
            self.assertEqual(runner.count(name), 1, name)
        self.assertIn("device_plugins=install_devices(runtime, ROOT)",
                      (ROOT/'scripts/build_live.py').read_text())
        self.assertIn("build_manifest['device_plugins']=device_plugins",
                      (ROOT/'scripts/build_live.py').read_text())

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
        self.assertEqual(plugin["scope"], "required")
        self.assertEqual(plugin["licenses"][0]["license"]["id"], "Apache-2.0")
        expected_hashes = {
            hashlib.sha256((ROOT / "tools/xtos-devices" / name).read_bytes()).hexdigest()
            for name in ("xtos_devices.py", "serial_transport.py", "cyton_protocol.py",
                         "cyton_serial_plugin.py")
        }
        self.assertTrue(expected_hashes.issubset(
            {entry["content"] for entry in plugin["hashes"]}))
        system = next(component for component in bom["components"] if component["name"] == "xtos")
        system_ref = system["bom-ref"]
        dependencies = {entry["ref"]: entry["dependsOn"] for entry in bom["dependencies"]}
        self.assertIn(plugin["bom-ref"], dependencies[system_ref])
        parser_component = next(component for component in bom["components"]
                                if component["name"] == "xtos-eeg-pipeline")
        self.assertIn(parser_component["bom-ref"], dependencies[plugin["bom-ref"]],
                      'device plugin canonical parser dependency missing')

    def test_plugin_readme_reflects_current_focused_scope_and_limits(self):
        text = README.read_text()
        for name in ("serial_transport.py", "cyton_protocol.py", "cyton_serial_plugin.py",
                     "ganglion_protocol.py", "ganglion_plugin.py",
                     "openbci_cyton.CytonPacketParser"):
            self.assertIn(name, text)
        self.assertIn("fixture_replay", text)
        self.assertIn("Authentication.NOT_CHECKED", text)
        self.assertIn("physical_device_evidence=False", text)
        self.assertIn("reset_defaults=True", text)
        self.assertIn("not phase acceptance", text)
        for suite in ("test_xtos_devices.py", "test_xtos_serial_transport.py",
                      "test_xtos_cyton_protocol.py", "test_xtos_cyton_serial_plugin.py",
                      "test_xtos_ganglion_protocol.py", "test_xtos_ganglion_plugin.py",
                      "test_xtos_ganglion_stream.py"):
            self.assertIn(suite, text)
        self.assertNotIn("run_suite.py final-normal", text)
        self.assertNotIn("run_suite.py final-optimized", text)


if __name__ == "__main__":
    unittest.main()