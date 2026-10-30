#!/usr/bin/env python3
"""Tests for the XTOS SBOM generator (tools/xtos-sbom).

Validates the generated CycloneDX document: required fields, real source
hashes, upstream archive digest, complete dependency graph, and license
coverage for every shipped component.
"""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def generate(out: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(HERE / "gen_sbom.py"), "--out", str(out)],
        capture_output=True, text=True, check=True)
    assert "sbom written" in result.stdout
    return json.loads(out.read_text())


class SbomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.bom = generate(Path(cls.tmp.name) / "sbom.json")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_format_and_metadata(self):
        self.assertEqual(self.bom["bomFormat"], "CycloneDX")
        self.assertEqual(self.bom["specVersion"], "1.5")
        self.assertIn("serialNumber", self.bom)
        meta = self.bom["metadata"]
        self.assertIn("timestamp", meta)
        self.assertEqual(meta["component"]["name"], "xtos")
        self.assertTrue(meta["properties"])

    def test_component_inventory(self):
        names = {c["name"] for c in self.bom["components"]}
        for required in ("linux", "preempt-rt", "xtos-linux-integration",
                         "busybox", "libxtos-rt", "libxtos-signal",
                         "libxtos-bci", "xtos-eeg-pipeline"):
            self.assertIn(required, names, f"{required} missing from SBOM")
        plugin = next(c for c in self.bom["components"]
                      if c["name"] == "xtos-device-plugins")
        self.assertEqual(plugin["scope"], "required")
        plugin_hashes = {h["content"] for h in plugin["hashes"]}
        for name in ("xtos_devices.py", "serial_transport.py", "cyton_protocol.py",
                     "cyton_serial_plugin.py"):
            self.assertIn(hashlib.sha256((ROOT / "tools/xtos-devices" / name).read_bytes()).hexdigest(),
                          plugin_hashes)
        kernel = next(c for c in self.bom["components"] if c["name"] == "xtos-linux-integration")
        props = {p["name"]: p["value"] for p in kernel["properties"]}
        self.assertEqual(props["openbci-cyton-packet.h"], hashlib.sha256(
            (ROOT / 'kernel/linux/drivers/xtos/bci/xtos_cyton_packet.h').read_bytes()).hexdigest())
        required_comps = [c for c in self.bom["components"]
                          if c.get("scope") == "required"]
        self.assertEqual(len(required_comps), 17)  # system + 16 shipped

    def test_runtime_build_inputs_are_completely_bound(self):
        inventories = {}
        for component, directory in (('bci', 'xtos-cyton'),
                                     ('xtos-eeg-pipeline', 'xtos-eeg-pipeline'),
                                     ('xtos-eeg-stream', 'xtos-eeg-stream')):
            base = ROOT / 'tools' / directory
            inventories[component] = [p for p in base.iterdir()
                if p.name == 'Makefile' or (p.suffix in ('.c', '.h', '.py')
                and not p.name.startswith('test_') and not p.stem.endswith('_fixture'))]
        components = {c['name']: c for c in self.bom['components']}
        for component, sources in inventories.items():
            with self.subTest(component=component):
                self.assertIn(component, components)
                hashes = {h['content'] for h in components[component]['hashes']}
                for source in sources:
                    self.assertIn(hashlib.sha256(source.read_bytes()).hexdigest(), hashes,
                                  f'{source.relative_to(ROOT)} missing from runtime inventory')

    def test_every_runtime_input_mutation_invalidates_component(self):
        import importlib.util
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location('sbom_mutation', HERE / 'gen_sbom.py')
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name in ('bci', 'xtos-eeg-pipeline', 'xtos-eeg-stream'):
            entries = [entry for entry in module.COMPONENTS if entry['name'] == name]
            self.assertTrue(entries, name + ' component missing')
            entry = entries[0]
            base = ROOT / entry['path']
            sources = [p for p in base.iterdir() if p.name == 'Makefile' or
                (p.suffix in ('.c', '.h', '.py') and not p.name.startswith('test_')
                 and not p.stem.endswith('_fixture'))]
            with tempfile.TemporaryDirectory() as temporary:
                scratch = Path(temporary)
                for source in sources:
                    target = scratch / entry['path'] / source.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
                with patch.object(module, 'ROOT', scratch):
                    before = module.build_component(entry)
                    for source in sources:
                        with self.subTest(component=name, source=source.name):
                            target = scratch / entry['path'] / source.name
                            original = target.read_bytes()
                            target.write_bytes(original + b'\n/* sbom mutation */\n')
                            try:
                                self.assertNotEqual(before, module.build_component(entry),
                                                    source.name + ' mutation escaped binding')
                            finally:
                                target.write_bytes(original)

    def test_native_bci_component_declares_source_hashes(self):
        native = next((c for c in self.bom['components'] if c['name'] == 'bci'), None)
        self.assertIsNotNone(native)
        self.assertEqual(native['scope'], 'required')
        expected = {hashlib.sha256((ROOT / 'tools/xtos-cyton' / name).read_bytes()).hexdigest()
                    for name in ('main.c', 'bci_stream.c', 'cyton_serial.c', 'bci_decode.h',
                                 'bci_config_cli.c', 'bci_config_cli.h',
                                 'bci_diagnostic_cli.c', 'bci_diagnostic_cli.h',
                                 'bci_diagnostic.c', 'bci_diagnostic.h',
                                 'bci_diagnostic_options.h', 'cyton_diagnostic_serial.c',
                                 'cyton_diagnostic_serial.h', 'bci_calibration.c',
                                 'bci_calibration.h', 'bci_calibration_cli.c',
                                 'bci_calibration_cli.h', 'bci_sha256.h')}
        self.assertTrue(expected.issubset({h['content'] for h in native['hashes']}))

    def test_frame_sources_and_public_SDK_headers_are_bound(self):
        kernel = next(c for c in self.bom['components'] if c['name'] == 'xtos-linux-integration')
        props = {p['name']: p['value'] for p in kernel['properties']}
        for path in ('drivers/xtos/bci/xtos_bci_frame.c',
                     'drivers/xtos/bci/xtos_bci_frame.h',
                     'drivers/xtos/bci/xtos_bci_frame_snapshot.c',
                     'drivers/xtos/bci/xtos_bci_frame_snapshot.h',
                     'include/uapi/linux/xtos_bci_frame.h'):
            with self.subTest(source=path):
                self.assertEqual(props.get('xtos:source:'+path), hashlib.sha256(
                    (ROOT/'kernel/linux'/path).read_bytes()).hexdigest())
        sdk = next(c for c in self.bom['components'] if c['name'] == 'libxtos-bci')
        hashes = {item['content'] for item in sdk['hashes']}
        for path in (ROOT/'sdk/libxtos-bci/include/xtos').glob('*.h'):
            with self.subTest(header=path.name):
                self.assertIn(hashlib.sha256(path.read_bytes()).hexdigest(), hashes)

    def test_eeg_stream_components_hash_complete_installed_inventory(self):
        names = {c['name']: c for c in self.bom['components']}
        for component in ('xtos-eeg-loop', 'xtos-ai-runtime', 'xtos-action-gate'):
            self.assertIn(component, names)
        for component, (package, source_names) in {
            'xtos-eeg-loop': ('xtos_eeg_loop', ('stream.py', 'controller.py', 'feature_binding.py', 'inference.py', 'lifecycle.py', 'simulation.py', '__init__.py')),
            'xtos-action-gate': ('xtos_action_gate', ('__init__.py', '__main__.py', 'schema.py', 'store.py')),
            'xtos-ai-runtime': ('xtos-ai-runtime/xtos_ai_runtime', ('__init__.py', '__main__.py', 'client.py', 'protocol.py', 'server.py', 'trust.py', 'worker.py')),
        }.items():
            self.assertEqual(names[component]['scope'], 'required')
            hashes = {item['content'] for item in names[component]['hashes']}
            for name in source_names:
                source = ROOT / 'tools' / package / name
                self.assertIn(hashlib.sha256(source.read_bytes()).hexdigest(), hashes,
                              f'{package}/{name} missing from shipped-source component hashes')

    def test_every_component_declares_license(self):
        for comp in self.bom["components"]:
            licenses = comp.get("licenses", [])
            self.assertTrue(licenses, f"{comp['name']} has no license")
            self.assertIn("license", licenses[0])

    def test_kernel_hash_is_real(self):
        kernel = next(c for c in self.bom["components"]
                      if c["name"] == "linux")
        real = hashlib.sha256(
            (ROOT / "kernel/linux/Makefile").read_bytes()).hexdigest()
        contents = {h["content"] for h in kernel["hashes"]}
        self.assertIn(real, contents)

    def test_busybox_upstream_digest_recorded(self):
        busybox = next(c for c in self.bom["components"]
                       if c["name"] == "busybox")
        props = {p["name"]: p["value"] for p in busybox["properties"]}
        self.assertEqual(props["xtos:upstream-archive-sha256"],
                         "3311dff32e746499f4df0d5df04d7eb396382d7e108bb9250e7b519b837043a4")

    def test_dependency_graph_covers_all_components(self):
        deps = {d["ref"]: d["dependsOn"] for d in self.bom["dependencies"]}
        refs = {c["bom-ref"] for c in self.bom["components"]}
        self.assertTrue(refs.issubset(deps.keys()),
                        "components without dependency nodes")
        system_ref = self.bom["metadata"]["component"]["bom-ref"]
        required_refs = {c["bom-ref"] for c in self.bom["components"]
                         if c.get("scope") == "required"
                         and c["bom-ref"] != system_ref}
        self.assertEqual(set(deps[system_ref]), required_refs)

    def test_optional_ml_deps_are_excluded_scope(self):
        excluded = [c for c in self.bom["components"]
                    if c.get("scope") == "excluded"]
        names = {c["name"] for c in excluded}
        for optional in ("onnxruntime", "numpy", "torch", "tensorflow"):
            self.assertIn(optional, names)
        for comp in excluded:
            self.assertNotIn(comp["bom-ref"],
                             self.bom["dependencies"][0]["dependsOn"])


if __name__ == "__main__":
    unittest.main()
