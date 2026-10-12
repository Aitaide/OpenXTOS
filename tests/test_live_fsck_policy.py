#!/usr/bin/env python3
"""Unit-test early fsck exit policy with explicit fake checker fixtures.

These tests validate control flow only; real ext4/QEMU acceptance is separate.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'system/live/xtos-persist-check'


class FsckPolicyTests(unittest.TestCase):
    def setUp(self):
        busybox = shutil.which('busybox')
        if not busybox:
            self.skipTest('host BusyBox needed for shell policy fixture')
        self.temp = tempfile.TemporaryDirectory(prefix='xtos-fsck-policy-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lower = self.root / 'lower'
        self.checker = self.lower / 'usr/sbin/e2fsck'
        self.checker.parent.mkdir(parents=True)
        self.checker.write_text('#!/bin/sh\nexit 99\n')
        self.checker.chmod(0o755)
        self.loader = self.lower / 'lib64/ld-linux-x86-64.so.2'
        self.loader.parent.mkdir(parents=True)
        self.loader.write_text(
            '#!/bin/sh\n'
            '[ "$1" = --library-path ] || exit 98\n'
            '[ "$3" = "$TEST_LOWER/usr/sbin/e2fsck" ] || exit 97\n'
            '[ "$4" = -p ] || exit 96\n'
            '[ "$5" = /dev/fixture ] || exit 95\n'
            'echo "fixture-checker: rc=$TEST_RC"\n'
            'exit "$TEST_RC"\n'
        )
        self.loader.chmod(0o755)
        self.log = self.root / 'fsck.log'
        # Replace fixed deployment paths only in this isolated policy harness.
        script = SCRIPT.read_text().replace('BB=/bin/busybox', f'BB={busybox}')
        script = script.replace('LOG=/run/persist-fsck.log', f'LOG={self.log}')
        self.script = self.root / 'policy.sh'
        self.script.write_text(script)
        self.env = dict(os.environ, TEST_LOWER=str(self.lower))

    def run_policy(self, rc):
        env = dict(self.env, TEST_RC=str(rc))
        return subprocess.run(['sh', str(self.script), '/dev/fixture', str(self.lower)],
                              capture_output=True, text=True, timeout=10, env=env)

    def test_only_clean_or_corrected_are_accepted(self):
        for rc in (0, 1, 2, 3, 4, 8, 16, 32, 128):
            with self.subTest(checker_rc=rc):
                result = self.run_policy(rc)
                accept = rc in (0, 1)
                self.assertEqual(result.returncode, 0 if accept else 1, result.stderr)
                self.assertIn(f'rc={rc} action={"accept" if accept else "refuse"}', result.stdout)
                self.assertEqual(self.log.read_text(), f'fixture-checker: rc={rc}\n')

    def test_missing_checker_refuses_instead_of_mounting_unchecked(self):
        self.checker.unlink()
        result = self.run_policy(0)
        self.assertEqual(result.returncode, 1)
        self.assertIn('rc=127 action=refuse', result.stdout)
        self.assertIn('missing', self.log.read_text())

    def test_missing_loader_refuses(self):
        self.loader.unlink()
        result = self.run_policy(0)
        self.assertEqual(result.returncode, 1)
        self.assertIn('rc=127 action=refuse', result.stdout)

    def test_invalid_argument_count(self):
        result = subprocess.run(['sh', str(self.script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
