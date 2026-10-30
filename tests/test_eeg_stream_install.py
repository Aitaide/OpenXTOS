#!/usr/bin/env python3
"""Exercise installed native producer without opening a device or building media."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class EegStreamInstallTests(unittest.TestCase):
    def test_live_install_contains_static_producer_and_bound_runtime_sources(self):
        spec = importlib.util.spec_from_file_location('live_stream_install', ROOT / 'scripts/build_live.py')
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # A caller can select its own immutable source export while native writers run.
        source = Path(os.environ.get('XTOS_PACKAGING_SOURCE_ROOT', ROOT))
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            root = Path(temporary)
            stage, work = root / 'rootfs', root / 'work'
            (stage / 'usr/bin').mkdir(parents=True)
            work.mkdir()
            with patch.object(module, 'ROOT', source):
                inventory = module.install_sdk(stage, work)
            binary = stage / 'usr/bin/eeg-stream'
            self.assertTrue(binary.is_file(), 'native producer missing from Live SDK install')
            self.assertEqual(binary.read_bytes()[:4], b'\x7fELF')
            detail = subprocess.run(['readelf', '-l', str(binary)], check=True,
                                    capture_output=True, text=True)
            self.assertNotIn('INTERP', detail.stdout)
            before = {p.relative_to(stage).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in stage.rglob('*') if p.is_file()}
            result = subprocess.run([str(binary), '--packaging-invalid-option'],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('invalid', result.stderr.lower())
            after = {p.relative_to(stage).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in stage.rglob('*') if p.is_file()}
            self.assertEqual(before, after, 'read-only option validation changed installed files')
            record = json.loads((stage / 'usr/share/xtos/eeg-stream-sources.json').read_text())
            self.assertEqual(inventory, record)
            expected = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in (source / 'tools/xtos-eeg-stream').iterdir()
                        if p.suffix in ('.c', '.h') or p.name == 'Makefile'}
            self.assertEqual(record['source_hashes'], expected)
            self.assertEqual(record['binary_hashes']['usr/bin/eeg-stream'], before['usr/bin/eeg-stream'])
            cyton = stage / 'usr/share/xtos/eeg-pipeline'
            self.assertEqual({p.name for p in cyton.iterdir()},
                {'openbci_cyton.py', 'cyton_capture.py', 'cyton_replay.py', 'device_contract.py'})

    def test_system_profiles_install_native_producer_without_python_payload(self):
        for filename in ('build_system.sh', 'build_system_arm64.sh', 'build_system_riscv64.sh'):
            with self.subTest(builder=filename):
                text = (ROOT / 'scripts' / filename).read_text()
                self.assertIn('xtos-eeg-stream', text)
                self.assertIn('install_eeg_stream', text)
                self.assertNotIn('install_eeg_loop', text)
                self.assertNotIn('cyton_capture.py', text)
                self.assertNotIn('cyton_replay.py', text)

    def test_system_install_entry_reuses_work_and_honors_compiler_override(self):
        source = Path(os.environ.get('XTOS_PACKAGING_SOURCE_ROOT', ROOT))
        spec = importlib.util.spec_from_file_location('stream_toolchain', ROOT / 'scripts/build_live.py')
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            root = Path(temporary)
            stage, work = root / 'rootfs', root / 'work'
            (stage / 'usr/include').mkdir(parents=True)
            (stage / 'usr/lib').mkdir()
            work.mkdir()
            # Build real SDKs in scratch, never in the shared checkout.
            import shutil
            for library in ('bci', 'signal'):
                directory = root / ('libxtos-' + library)
                shutil.copytree(source / 'sdk' / directory.name, directory)
                subprocess.run(['make', '-C', str(directory), 'clean', 'all'],
                               check=True, capture_output=True)
                shutil.copy2(directory / (directory.name + '.a'), stage / 'usr/lib')
                shutil.copytree(directory / 'include/xtos', stage / 'usr/include/xtos', dirs_exist_ok=True)
            wrapper = root / 'selected-cc'
            marker = root / 'compiler-invocations'
            wrapper.write_text('#!/bin/sh\nprintf "selected-cc\\n" >> "' + str(marker) + '"\nexec gcc "$@"\n')
            wrapper.chmod(0o755)
            with patch.dict(os.environ, {'CC': str(wrapper), 'AR': 'ar'}):
                first = module.install_eeg_stream(stage, work, source)
                second = module.install_eeg_stream(stage, work, source)
            self.assertEqual(first, second)
            self.assertGreaterEqual(len(marker.read_text().splitlines()), 8)
            self.assertFalse(any(stage.rglob('*.py')))


if __name__ == '__main__':
    unittest.main()
