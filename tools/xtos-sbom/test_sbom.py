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
        [sys.executable, *(['-O'] if sys.flags.optimize else []),
         str(HERE / "gen_sbom.py"), "--out", str(out)],
        capture_output=True, text=True, check=True)
    assert "sbom written" in result.stdout
    return json.loads(out.read_text())


class SbomTests(unittest.TestCase):
    def test_kernel_version_matches_verified_source_pin(self):
        pin = json.loads((ROOT / 'kernel/source-pin.json').read_text())
        kernel = next(c for c in self.bom['components'] if c['name'] == 'linux')
        self.assertEqual(kernel['version'], pin['version'])
        values = dict(line.split(' = ', 1) for line in
                      (ROOT / 'kernel/linux/Makefile').read_text().splitlines()
                      if line.startswith(('VERSION = ', 'PATCHLEVEL = ', 'SUBLEVEL = ')))
        self.assertEqual(kernel['version'], '.'.join(values[key] for key in
                                                   ('VERSION', 'PATCHLEVEL', 'SUBLEVEL')))
        self.assertEqual(kernel['externalReferences'][0]['url'], pin['upstream'])
        props = {p['name']: p['value'] for p in self.bom['metadata']['properties']}
        self.assertEqual(props['xtos:kernel-release'], pin['version'])

    def test_kernel_archive_and_source_pin_are_hash_bound(self):
        pin_path = ROOT / 'kernel/source-pin.json'
        pin = json.loads(pin_path.read_text())
        kernel = next(c for c in self.bom['components'] if c['name'] == 'linux')
        props = {p['name']: p['value'] for p in kernel['properties']}
        self.assertEqual(props.get('xtos:upstream-archive-sha256'), pin['archive_sha256'])
        self.assertEqual(props.get('xtos:upstream-archive-bytes'), str(pin['archive_bytes']))
        self.assertEqual(props.get('xtos:source-pin-sha256'), hashlib.sha256(pin_path.read_bytes()).hexdigest())
        for name in ('Makefile', 'COPYING'):
            self.assertIn(hashlib.sha256((ROOT / 'kernel/linux' / name).read_bytes()).hexdigest(),
                          {h['content'] for h in kernel['hashes']})

    def test_current_integration_patch_matches_source_pin(self):
        pin = json.loads((ROOT / 'kernel/source-pin.json').read_text())
        kernel = next(c for c in self.bom['components'] if c['name'] == 'xtos-linux-integration')
        props = {p['name']: p['value'] for p in kernel['properties']}
        self.assertEqual(props['xtos:source-path'], pin['xtos_patch'])
        digest = hashlib.sha256((ROOT / pin['xtos_patch']).read_bytes()).hexdigest()
        self.assertEqual(digest, pin['xtos_patch_sha256'])
        self.assertIn(digest, {h['content'] for h in kernel['hashes']})

    def test_mainline_rt_has_no_external_patch_dependency(self):
        components = {c['name']: c for c in self.bom['components']}
        historical = components['preempt-rt']
        self.assertEqual(historical['scope'], 'excluded')
        self.assertEqual(historical['version'], '6.6-rt15')
        self.assertEqual(historical['licenses'][0]['license']['id'], 'GPL-2.0-only')
        old_patch = ROOT / 'kernel/patches/preempt-rt-6.6-rt15.patch'
        self.assertIn(hashlib.sha256(old_patch.read_bytes()).hexdigest(),
                      {h['content'] for h in historical['hashes']})
        props = {p['name']: p['value'] for p in components['linux']['properties']}
        self.assertEqual(props.get('xtos:rt-mode'), 'mainline')
        self.assertEqual(props.get('xtos:rt-config'), 'CONFIG_PREEMPT_RT=y')
        self.assertEqual(props.get('xtos:rt-runtime-evidence'), 'not-validated')
        deps = {row['ref']: row['dependsOn'] for row in self.bom['dependencies']}
        for component in components.values():
            if component['scope'] == 'required':
                self.assertNotIn(historical['bom-ref'], deps[component['bom-ref']])
        self.assertIn(components['linux']['bom-ref'], deps[components['xtos-linux-integration']['bom-ref']])

    def test_optional_ble_source_is_declared_in_runtime_hash_inventory(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('sbom_ble_inventory', HERE / 'gen_sbom.py')
        if spec is None or spec.loader is None:
            self.fail('SBOM generator loader unavailable')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        plugin = next(entry for entry in module.COMPONENTS
                      if entry['name'] == 'xtos-device-plugins')
        self.assertIn('ganglion_ble.py', plugin['hash_globs'],
                      'installed optional BLE source is not bound by the SBOM')

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
                     "cyton_serial_plugin.py", "ganglion_protocol.py", "ganglion_plugin.py",
                     "ganglion_ble.py"):
            self.assertIn(hashlib.sha256((ROOT / "tools/xtos-devices" / name).read_bytes()).hexdigest(),
                          plugin_hashes)
        kernel = next(c for c in self.bom["components"] if c["name"] == "xtos-linux-integration")
        props = {p["name"]: p["value"] for p in kernel["properties"]}
        self.assertEqual(props["openbci-cyton-packet.h"], hashlib.sha256(
            (ROOT / 'kernel/linux/drivers/xtos/bci/xtos_cyton_packet.h').read_bytes()).hexdigest())
        required_comps = [c for c in self.bom["components"]
                          if c.get("scope") == "required"]
        self.assertEqual(len(required_comps), 19)  # system + RT diagnostics; mainline RT belongs to Linux

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

    def test_kernel_compiled_runtime_inputs_are_completely_bound(self):
        kernel = next(c for c in self.bom['components'] if c['name'] == 'xtos-linux-integration')
        props = {p['name']: p['value'] for p in kernel['properties']}
        base = ROOT / 'kernel/linux'
        sources = [p for directory in ('drivers/xtos', 'kernel/sched/xtos')
                   for p in (base / directory).rglob('*') if p.is_file()
                   and not p.name.startswith('test_')]
        for pattern in ('include/linux/xtos*.h', 'include/uapi/linux/xtos*.h',
                        'kernel/sched/xtos*.c', 'kernel/sched/xtos*.h'):
            sources.extend(base.glob(pattern))
        sources.extend(base / p for p in ('drivers/Kconfig', 'drivers/Makefile',
                                          'init/Kconfig', 'kernel/sched/Makefile'))
        for source in sources:
            with self.subTest(source=source.relative_to(base)):
                self.assertEqual(props.get('xtos:source:' + source.relative_to(base).as_posix()),
                                 hashlib.sha256(source.read_bytes()).hexdigest())

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

    def test_kernel_source_pin_patch_and_runtime_mutations_in_replica(self):
        import importlib.util
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location('sbom_kernel_mutation', HERE / 'gen_sbom.py')
        if spec is None or spec.loader is None:
            self.fail('SBOM generator loader unavailable')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        entries = {e['name']: e for e in module.COMPONENTS}
        integration = next(c for c in self.bom['components'] if c['name'] == 'xtos-linux-integration')
        sources = ['kernel/linux/' + p['name'].removeprefix('xtos:source:')
                   for p in integration['properties'] if p['name'].startswith('xtos:source:')]
        pin = json.loads((ROOT / 'kernel/source-pin.json').read_text())
        paths = ['kernel/source-pin.json', pin['xtos_patch'],
                 'kernel/linux/Makefile', 'kernel/linux/COPYING', *sources]
        with tempfile.TemporaryDirectory() as temporary:
            replica = Path(temporary)
            for name in paths:
                target = replica / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / name).read_bytes())
            with patch.object(module, 'ROOT', replica):
                for name in paths:
                    component = 'linux' if name in paths[:1] + paths[2:4] else 'xtos-linux-integration'
                    before = module.build_component(entries[component])
                    target = replica / name
                    original = target.read_bytes()
                    with self.subTest(source=name):
                        target.write_bytes(original + b'\n')
                        try:
                            self.assertNotEqual(before, module.build_component(entries[component]))
                        finally:
                            target.write_bytes(original)

    def test_mainline_rt_diagnostic_inputs_are_hash_bound(self):
        component = next(c for c in self.bom['components']
                         if c['name'] == 'xtos-rt-diagnostics')
        self.assertEqual(component['scope'], 'required')
        contents = {row['content'] for row in component['hashes']}
        for name in ('tools/xtos-native/info/info.c', 'tools/xtos-native/rt/rt.c',
                     'tools/xtos-native/common/xtos_rt_state.h',
                     'tools/xtos-native/common/xtos_ui.h',
                     'tools/xtos-native/rt/rt_irq.h',
                     'system/rootfs/usr/bin/xtos-rt-status'):
            with self.subTest(source=name):
                self.assertIn(hashlib.sha256((ROOT/name).read_bytes()).hexdigest(), contents)

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

    def test_optional_provider_experiment_is_excluded_and_hash_bound(self):
        component = next(item for item in self.bom['components']
                         if item['name'] == 'xtos-ai-provider-experiment')
        self.assertEqual(component['scope'], 'excluded')
        self.assertIn(hashlib.sha256((ROOT / 'tools/xtos-ai-provider-experiment/provider_experiment.py').read_bytes()).hexdigest(),
                      {row['content'] for row in component['hashes']})
        system_ref = self.bom['metadata']['component']['bom-ref']
        self.assertNotIn(component['bom-ref'], next(row for row in self.bom['dependencies']
                                                   if row['ref'] == system_ref)['dependsOn'])

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
