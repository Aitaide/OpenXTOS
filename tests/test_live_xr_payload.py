"""Actual installed XR inventory is distinct from declared smoke success."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Any
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures/live_xr_inventory.py'


class LiveXRPayloadTests(unittest.TestCase):
    def fixture(self):
        self.assertTrue(FIXTURE.is_file(), 'installed XR payload inventory is missing')
        spec = importlib.util.spec_from_file_location('xr_installed_inventory', FIXTURE)
        if spec is None or spec.loader is None:
            self.fail('installed inventory fixture cannot load')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_guest_inventory_must_match_build_manifest_with_exact_types(self):
        spec = importlib.util.spec_from_file_location('xr_payload_verifier', ROOT / 'scripts/verify_live.py')
        if spec is None or spec.loader is None:
            self.fail('Live verifier unavailable')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(hasattr(module, 'validate_xr_installed_inventory'),
                        'guest inventory is not bound to the build manifest')
        payload = {'usr/bin/xr-probe': dict(sha256='a' * 64, bytes=10, mode=0o755),
                   'usr/share/xtos/xr-sources.json': dict(sha256='b' * 64, bytes=100, mode=0o644)}
        sources = {'tools/xtos-xr/src/xr_probe.c': dict(sha256='c' * 64, bytes=400)}
        build = dict(schema='xtos.xr.live-install.v1', payload_inventory=payload,
                     source_inventory=sources, physical_acceptance=False)
        report: dict[str, Any] = dict(payload_inventory=payload,
                      source_hashes={name: row['sha256'] for name, row in sources.items()},
                      physical_acceptance=False)
        self.assertEqual(module.validate_xr_installed_inventory(report, build), report)
        import copy
        mutations = []
        for key, value in [('bytes', 10.0), ('mode', float(0o755)),
                           ('sha256', 'd' * 64), ('bytes', False)]:
            bad = copy.deepcopy(report)
            bad['payload_inventory']['usr/bin/xr-probe'][key] = value
            mutations.append(bad)
        bad = copy.deepcopy(report)
        bad['source_hashes'] = {}
        mutations.append(bad)
        for bad in mutations:
            with self.subTest(inventory=bad), self.assertRaises(ValueError):
                module.validate_xr_installed_inventory(bad, build)

    def test_inventory_reads_every_installed_byte_and_manifest(self):
        module = self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = b'harmless fixture, not an executable\n'
            payload = root / 'usr/bin/xr-probe'
            payload.parent.mkdir(parents=True)
            payload.write_bytes(data)
            payload.chmod(0o755)
            manifest = root / 'usr/share/xtos/xr-sources.json'
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({'schema': 'xtos.xr.source-inventory.v1',
                'status': 'installed_source_only', 'physical_acceptance': False,
                'vendor_runtime_bundled': False,
                'source_hashes': {'tools/xtos-xr/src/xr_probe.c': 'a' * 64},
                'payload_hashes': {'usr/bin/xr-probe': hashlib.sha256(data).hexdigest()}}))
            manifest.chmod(0o644)
            result = module.inventory(root)
            for relative in ('usr/bin/xr-probe', 'usr/share/xtos/xr-sources.json'):
                path = root / relative
                self.assertEqual(result['payload_inventory'][relative], {
                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'bytes': len(path.read_bytes()), 'mode': path.stat().st_mode & 0o7777})
            self.assertEqual(result['source_hashes'], {'tools/xtos-xr/src/xr_probe.c': 'a' * 64})
            payload.write_bytes(data + b'changed')
            with self.assertRaisesRegex(ValueError, 'payload hash mismatch'):
                module.inventory(root)


if __name__ == '__main__':
    unittest.main()
