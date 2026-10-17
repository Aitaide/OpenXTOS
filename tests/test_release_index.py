#!/usr/bin/env python3
"""release_index.py tests: create/verify round-trip, spec validation rejects."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'release_index.py'


def good_spec():
    return {
        'version': '1.0.0-beta.4', 'release_name': 'Beta 4', 'channel': 'prerelease',
        'published': '2026-10-16', 'commit': 'a' * 40,
        'notes_url': 'https://github.com/Aitaide/OpenXTOS/releases/tag/v1.0.0-beta.4',
        'artifacts': [
            {'name': 'iso', 'arch': 'x86_64', 'kind': 'installer-iso', 'size': 10,
             'sha256': 'b' * 64, 'url': 'https://example.com/iso'},
            {'name': 'sums', 'arch': 'all', 'kind': 'checksums', 'url': 'https://example.com/sums'},
        ],
    }


class ReleaseIndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='xtos-relidx-test-')
        cls.base = Path(cls.tmp.name)
        cls.key = cls.base / 'key.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'ed25519', '-out', str(cls.key)], check=True)
        cls.pub = cls.base / 'pub.pem'
        subprocess.run(['openssl', 'pkey', '-in', str(cls.key), '-pubout', '-out', str(cls.pub)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_script(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=120)

    def test_create_verify_roundtrip(self):
        spec_path = self.base / 'spec.json'
        spec_path.write_text(json.dumps(good_spec(), indent=2, sort_keys=True))
        out = self.base / 'idx' / 'release-index.json'
        r = self.run_script('create', '--spec', str(spec_path), '--out', str(out), '--key', str(self.key))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(Path(str(out) + '.sig').is_file())
        # canonical bytes
        index = json.loads(out.read_text())
        self.assertEqual(out.read_bytes(), (json.dumps(index, indent=2, sort_keys=True) + '\n').encode())
        r = self.run_script('verify', '--index', str(out), '--public-key', str(self.pub))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        verdict = json.loads(r.stdout)
        self.assertEqual(verdict['version'], '1.0.0-beta.4')

    def test_verify_rejects_resign_attempt(self):
        out = self.base / 'idx2' / 'release-index.json'
        spec_path = self.base / 'spec2.json'
        spec_path.write_text(json.dumps(good_spec(), indent=2, sort_keys=True))
        self.run_script('create', '--spec', str(spec_path), '--out', str(out), '--key', str(self.key))
        # flip a byte in the index: signature must fail
        raw = bytearray(out.read_bytes())
        raw[raw.index(b'"channel"') + 10] ^= 1
        out.write_bytes(bytes(raw))
        r = self.run_script('verify', '--index', str(out), '--public-key', str(self.pub))
        self.assertNotEqual(r.returncode, 0, 'tampered index accepted')

    def test_bad_specs_rejected(self):
        cases = [
            ({**good_spec(), 'version': 'not.a.version'}, 'version'),
            ({**good_spec(), 'commit': 'xyz'}, 'commit'),
            ({**good_spec(), 'notes_url': 'http://insecure'}, 'notes_url'),
            ({**good_spec(), 'artifacts': [{**good_spec()['artifacts'][0], 'url': 'http://x'}]}, 'url'),
            ({**good_spec(), 'artifacts': [{**good_spec()['artifacts'][0], 'sha256': 'zz'}]}, 'sha256'),
        ]
        for i, (spec, _) in enumerate(cases):
            spec_path = self.base / f'bad{i}.json'
            spec_path.write_text(json.dumps(spec, indent=2, sort_keys=True))
            r = self.run_script('create', '--spec', str(spec_path),
                                '--out', str(self.base / f'badout{i}.json'), '--key', str(self.key))
            self.assertNotEqual(r.returncode, 0, f'bad spec {i} accepted')


if __name__ == '__main__':
    unittest.main(verbosity=2)
