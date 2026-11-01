#!/usr/bin/env python3
"""Self-hosted userspace repository tests: create/sign/verify/populate round-trip.

Runs fully offline: fixture archives come from --cache-dir, never from the
network. Tamper, stray-file, signature, and lock-mismatch cases must fail.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import hashlib
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'userspace_repo.py'


def run_repo(*args, key=None):
    # Execute the unchanged production CLI in a child, replacing only its
    # network seam. Invalid/missing fixture cache must never contact Debian.
    code = ('import sys\n'
            'sys.path.insert(0,sys.argv[1])\nimport userspace_repo as repo\n'
            'def offline(*args, **kwargs):\n'
            '    raise RuntimeError("offline fixture forbids network")\n'
            'repo.fetch_into=offline\n'
            'sys.argv=[repo.__file__]+sys.argv[2:]\n'
            'raise SystemExit(repo.main())\n')
    cmd = [sys.executable, '-B', '-c', code, str(SCRIPT.parent), *args]
    env = dict(os.environ)
    if key:
        env['XTOS_USERSPACE_REPO_KEY'] = str(key)
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_lock(path, packages):
    lock = {
        'schema_version': 1, 'profile': 'fixture', 'architecture': 'x86_64',
        'repository_suite': 'trixie', 'requested_packages': [p['name'] for p in packages],
        'required_commands': [], 'defaults': {}, 'package_format': 'deb',
        'packages': sorted(packages, key=lambda p: p['name']),
        'provenance': {'authentication': 'fixture'},
    }
    Path(path).write_text(json.dumps(lock, indent=2, sort_keys=True) + '\n')


def make_fixture_packages():
    packages = []
    for name, version, arch, blob in [('zlib1g', '1.3.dfsg-2', 'amd64', b'A' * 4096),
                                      ('busybox', '1.37.0-6', 'amd64', b'B' * 8192),
                                      ('adduser', '3.152', 'all', b'C' * 512)]:
        filename = f'{name}_{version}_{arch}.deb'
        packages.append({'name': name, 'version': version, 'architecture': arch,
                         'filename': filename, 'sha256': hashlib.sha256(blob).hexdigest(),
                         'size': len(blob),
                         'source_url': f'https://deb.debian.org/debian/pool/main/{name[0]}/{name}/{filename}'})
    return packages


class UserspaceRepoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='xtos-repo-test-')
        cls.base = Path(cls.tmp.name)
        cls.key = cls.base / 'keys' / 'repo.pem'
        cls.cache = cls.base / 'cache'
        cls.cache.mkdir(parents=True)
        cls.packages = make_fixture_packages()
        blobs = {'zlib1g': b'A' * 4096, 'busybox': b'B' * 8192, 'adduser': b'C' * 512}
        for package in cls.packages:
            (cls.cache / package['filename']).write_bytes(blobs[package['name']])
        cls.lock = cls.base / 'fixture-lock.json'
        write_lock(cls.lock, cls.packages)
        cls.repo = cls.base / 'repo'

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def repo_run(self, *args, expect=0):
        result = run_repo(*args, key=self.key)
        self.assertEqual(result.returncode, expect, f'{args}\nstdout={result.stdout}\nstderr={result.stderr}')
        return result

    def test_01_create_and_verify(self):
        result = self.repo_run('create', '--lock', str(self.lock), '--repo', str(self.repo),
                               '--cache-dir', str(self.cache), '--key', str(self.key))
        summary = json.loads(result.stdout)
        self.assertEqual(summary['packages'], 3)
        self.assertEqual(summary['stats']['copied'], 3)
        self.assertTrue((self.repo / 'public.pem').is_file())
        self.assertTrue((self.repo / 'SHA256SUMS.sig').is_file())
        verified = json.loads(self.repo_run('verify', '--repo', str(self.repo), '--lock', str(self.lock)).stdout)
        self.assertEqual(verified['packages'], 3)

    def test_02_create_resumes_existing_pool(self):
        result = self.repo_run('create', '--lock', str(self.lock), '--repo', str(self.repo),
                               '--cache-dir', str(self.cache), '--key', str(self.key))
        self.assertEqual(json.loads(result.stdout)['stats']['reused'], 3)

    def test_03_populate_roundtrip(self):
        target = self.base / 'packages-out'
        result = self.repo_run('populate', '--repo', str(self.repo), '--packages-dir', str(target),
                               '--lock', str(self.lock))
        summary = json.loads(result.stdout)
        self.assertEqual(summary['packages'], 3)
        self.assertEqual(summary['bytes'], sum(p['size'] for p in self.packages))
        for package in self.packages:
            self.assertEqual(sha(target / package['filename']), package['sha256'])
        self.assertEqual(len(list(target.glob('*.deb'))), 3)

    def test_04_tampered_pool_fails_verify(self):
        victim = self.repo / 'pool' / self.packages[0]['filename']
        backup = victim.read_bytes()
        try:
            victim.write_bytes(backup + b'X')
            result = run_repo('verify', '--repo', str(self.repo))
            self.assertNotEqual(result.returncode, 0, 'tampered pool archive accepted')
        finally:
            victim.write_bytes(backup)
        self.repo_run('verify', '--repo', str(self.repo))

    def test_05_stray_file_fails_verify(self):
        stray = self.repo / 'pool' / 'rogue_1.0_amd64.deb'
        stray.write_bytes(b'rogue')
        try:
            result = run_repo('verify', '--repo', str(self.repo))
            self.assertNotEqual(result.returncode, 0, 'unlisted pool file accepted')
        finally:
            stray.unlink()
        self.repo_run('verify', '--repo', str(self.repo))

    def test_06_broken_signature_fails(self):
        sig = self.repo / 'SHA256SUMS.sig'
        backup = sig.read_bytes()
        try:
            sig.write_bytes(backup[:-1] + bytes([backup[-1] ^ 0xFF]))
            result = run_repo('verify', '--repo', str(self.repo))
            self.assertNotEqual(result.returncode, 0, 'broken signature accepted')
        finally:
            sig.write_bytes(backup)
        self.repo_run('verify', '--repo', str(self.repo))

    def test_07_missing_archive_fails(self):
        victim = self.repo / 'pool' / self.packages[1]['filename']
        backup = victim.read_bytes()
        try:
            victim.unlink()
            result = run_repo('verify', '--repo', str(self.repo))
            self.assertNotEqual(result.returncode, 0, 'missing archive accepted')
        finally:
            victim.write_bytes(backup)
        self.repo_run('verify', '--repo', str(self.repo))

    def test_08_different_lock_rejected(self):
        other_packages = [dict(p) for p in self.packages]
        other_packages[2]['version'] = '3.153'
        other_lock = self.base / 'other-lock.json'
        write_lock(other_lock, other_packages)
        result = run_repo('verify', '--repo', str(self.repo), '--lock', str(other_lock))
        self.assertNotEqual(result.returncode, 0, 'repository verified against a different lock')

    def test_09_populate_refuses_dirty_dir(self):
        dirty = self.base / 'packages-dirty'
        dirty.mkdir()
        (dirty / 'stale.deb').write_bytes(b'stale')
        result = run_repo('populate', '--repo', str(self.repo), '--packages-dir', str(dirty))
        self.assertNotEqual(result.returncode, 0, 'populate accepted a dirty packages dir')

    def test_10_unsafe_lock_rejected(self):
        bad = [dict(self.packages[0])]
        bad[0]['filename'] = '../escape.deb'
        bad_lock = self.base / 'bad-lock.json'
        write_lock(bad_lock, bad)
        result = run_repo('create', '--lock', str(bad_lock), '--repo', str(self.base / 'repo-bad'))
        self.assertNotEqual(result.returncode, 0, 'unsafe filename accepted')

    def test_11_foreign_host_rejected(self):
        bad = [dict(self.packages[0])]
        bad[0]['source_url'] = 'http://deb.debian.org/debian/pool/main/z/zlib1g/' + bad[0]['filename']
        bad_lock = self.base / 'http-lock.json'
        write_lock(bad_lock, bad)
        result = run_repo('create', '--lock', str(bad_lock), '--repo', str(self.base / 'repo-http'))
        self.assertNotEqual(result.returncode, 0, 'non-https source accepted')

    def test_12_bad_cache_bytes_rejected(self):
        poisoned = self.base / 'cache-poison'
        poisoned.mkdir()
        for package in self.packages:
            shutil.copyfile(self.cache / package['filename'], poisoned / package['filename'])
        (poisoned / self.packages[0]['filename']).write_bytes(b'WRONG')
        repo2 = self.base / 'repo-poison'
        result = run_repo('create', '--lock', str(self.lock), '--repo', str(repo2),
                          '--cache-dir', str(poisoned), '--key', str(self.key))
        self.assertNotEqual(result.returncode, 0, 'cache bytes with wrong hash were accepted')

    def test_13_fixture_subprocess_blocks_network_before_transport(self):
        hook = self.base / 'network-observer'
        hook.mkdir()
        marker = hook / 'transport-entered'
        (hook / 'sitecustomize.py').write_text(
            'import urllib.request\nfrom pathlib import Path\n'
            'def observe(*args, **kwargs):\n'
            f'    Path({str(marker)!r}).write_text("unexpected transport entry")\n'
            '    raise RuntimeError("outer fixture network observer")\n'
            'urllib.request.urlopen = observe\n')
        offline_lock = self.base / 'offline-lock.json'
        write_lock(offline_lock, [self.packages[0]])
        with patch.dict(os.environ, {'PYTHONPATH': str(hook)}):
            result = run_repo('create', '--lock', str(offline_lock),
                              '--repo', str(self.base / 'repo-offline'),
                              '--key', str(self.key))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists(), 'fixture reached the URL transport')
        self.assertIn('offline fixture forbids network', result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
