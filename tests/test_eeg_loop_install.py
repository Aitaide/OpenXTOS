"""Installed EEG process source inventory and fail-closed entrypoint."""
import hashlib
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/install_eeg_loop.py'
spec = importlib.util.spec_from_file_location('install_eeg_loop', SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def _install_child(stage, result):
    try:
        installer.install(Path(stage), ROOT)
    except Exception as exc:
        result.send(('rejected', type(exc).__name__))
    else:
        result.send(('installed', None))
    finally:
        result.close()


class EegLoopInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/root/.hermes/cache/scratch')
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.stage = self.work / 'rootfs'
        self.stage.mkdir()

    def test_installs_exact_runtime_sources_and_manifest(self):
        report = installer.install(self.stage, ROOT)
        self.assertEqual(report['schema'], 'xtos-eeg-loop-source-inventory-v1')
        self.assertEqual(report['status'], 'installed_source_only')
        self.assertTrue(report['source_hashes'])
        for rel, digest in report['source_hashes'].items():
            path = self.stage / rel
            self.assertTrue(path.is_file(), rel)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest, rel)
        self.assertTrue((self.stage / 'usr/bin/eeg-loop').stat().st_mode & 0o111)
        self.assertTrue((self.stage / 'usr/libexec/xtos/eeg-loop-entry.py').is_file())
        for rel, digest in report['generated_hashes'].items():
            self.assertEqual(hashlib.sha256((self.stage / rel).read_bytes()).hexdigest(), digest)
        self.assertFalse(any('fixtures' in p.parts or 'test_' in p.name
                             for p in (self.stage / 'usr/lib/xtos').rglob('*')))
        manifest = json.loads((self.stage / 'usr/share/xtos/eeg-loop-sources.json').read_text())
        self.assertEqual(manifest, report)
        self.assertNotIn('private_key', json.dumps(report).lower())
        self.assertNotIn('nonce', json.dumps(report).lower())

    def test_identical_existing_install_is_reused(self):
        first = installer.install(self.stage, ROOT)
        before = {str(path.relative_to(self.stage)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in self.stage.rglob('*') if path.is_file()}
        second = installer.install(self.stage, ROOT)
        after = {str(path.relative_to(self.stage)): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in self.stage.rglob('*') if path.is_file()}
        self.assertEqual(second, first)
        self.assertEqual(after, before)

    def test_entry_imports_installed_modules_under_isolated_python(self):
        installer.install(self.stage, ROOT)
        entry = self.stage / 'usr/libexec/xtos/eeg-loop-entry.py'
        result = subprocess.run(['/usr/bin/python3', '-I', '-B', str(entry), '--help'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        for option in ('--binding', '--socket', '--deadline-ms', '--candidate-ttl-ms'):
            self.assertIn(option, result.stdout)
        self.assertNotIn('fixture', result.stdout.lower())
        launcher = self.stage / 'usr/bin/eeg-loop'
        self.assertIn('/opt/xtos-ai/bin/xtos-ai-python', launcher.read_text())
        self.assertNotIn('/usr/bin/python3', launcher.read_text())
        # Execute the exact installed launcher in a minimal target root. Both
        # interpreter paths are observable sentinels; no host paths are rewritten.
        shell = self.stage / 'bin/sh'
        shell.parent.mkdir(parents=True)
        shutil.copy2(Path('/bin/sh').resolve(), shell)
        libraries = subprocess.run(['ldd', '/bin/sh'], check=True,
                                   capture_output=True, text=True)
        for token in libraries.stdout.split():
            if token.startswith('/'):
                source = Path(token)
                target = self.stage / source.relative_to('/')
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        system_python = self.stage / 'usr/bin/python3'
        system_python.write_text('#!/bin/sh\necho SYSTEM_FALLBACK\nexit 77\n')
        system_python.chmod(0o755)
        private = self.stage / 'opt/xtos-ai/bin/xtos-ai-python'
        private.parent.mkdir(parents=True)
        private.write_text('#!/bin/sh\nprintf "PRIVATE\\n"\nprintf "%s\\n" "$@"\n')
        private.chmod(0o755)
        command = ['chroot', str(self.stage), '/usr/bin/eeg-loop',
                   '--binding', 'path with spaces', '--help']
        selected = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertEqual(selected.stdout.splitlines(), [
            'PRIVATE', '/usr/libexec/xtos/eeg-loop-entry.py',
            '--binding', 'path with spaces', '--help'])
        private.unlink()
        absent = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(absent.returncode, 0)
        self.assertNotIn('SYSTEM_FALLBACK', absent.stdout + absent.stderr)

    def test_refuses_existing_destinations_without_overwriting(self):
        target = self.stage / 'usr/bin/eeg-loop'
        target.parent.mkdir(parents=True)
        target.write_text('caller-owned')
        with self.assertRaisesRegex(ValueError, 'generated destination mismatch|already exists'):
            installer.install(self.stage, ROOT)
        self.assertEqual(target.read_text(), 'caller-owned')

    def test_refuses_symlinked_existing_package_member(self):
        package = self.stage / 'usr/lib/xtos/xtos_action_gate'
        package.mkdir(parents=True)
        source = ROOT / 'tools/xtos_action_gate'
        for name in installer.PACKAGES['xtos_action_gate']:
            if name == 'store.py':
                continue
            (package / name).write_bytes((source / name).read_bytes())
        external = self.work / 'caller-store.py'
        external.write_bytes((source / 'store.py').read_bytes())
        member = package / 'store.py'
        member.symlink_to(external)
        with self.assertRaisesRegex(ValueError, 'regular|source mismatch'):
            installer.install(self.stage, ROOT)
        self.assertTrue(member.is_symlink())
        self.assertEqual(member.readlink(), external)
        self.assertEqual(external.read_bytes(), (source / 'store.py').read_bytes())
        self.assertFalse((self.stage / 'usr/bin/eeg-loop').exists())

    def _assert_peerless_fifo_rejected(self, target):
        parent, child = multiprocessing.get_context('fork').Pipe(duplex=False)
        process = multiprocessing.get_context('fork').Process(
            target=_install_child, args=(str(self.stage), child))
        process.start()
        child.close()
        process.join(3)
        if process.is_alive():
            process.terminate()
            process.join()
            parent.close()
            self.fail('installer blocked while inspecting peerless FIFO: ' + str(target))
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(parent.recv()[0], 'rejected')
        parent.close()

    def test_peerless_fifo_package_member_is_rejected_without_blocking(self):
        package = self.stage / 'usr/lib/xtos/xtos_action_gate'
        package.mkdir(parents=True)
        source = ROOT / 'tools/xtos_action_gate'
        for name in installer.PACKAGES['xtos_action_gate']:
            member = package / name
            if name == 'store.py':
                os.mkfifo(member)
            else:
                member.write_bytes((source / name).read_bytes())
        self._assert_peerless_fifo_rejected(package / 'store.py')
        self.assertTrue(stat.S_ISFIFO((package / 'store.py').lstat().st_mode))

    def test_peerless_fifo_generated_launcher_is_rejected_without_blocking(self):
        installer.install(self.stage, ROOT)
        target = self.stage / 'usr/bin/eeg-loop'
        target.unlink()
        os.mkfifo(target)
        self._assert_peerless_fifo_rejected(target)
        self.assertTrue(stat.S_ISFIFO(target.lstat().st_mode))

    def test_atomic_publish_preserves_racing_empty_directory(self):
        publish = installer._rename_noreplace
        target = self.stage / 'usr/lib/xtos/xtos_action_gate'
        injected = []

        def race(source, destination):
            if Path(destination) == target and not injected:
                target.mkdir(parents=True)
                injected.append(target.lstat())
            return publish(source, destination)

        with mock.patch.object(installer, '_rename_noreplace', side_effect=race):
            with self.assertRaises(FileExistsError):
                installer.install(self.stage, ROOT)
        self.assertEqual(len(injected), 1)
        current = target.lstat()
        self.assertEqual((current.st_dev, current.st_ino),
                         (injected[0].st_dev, injected[0].st_ino))
        self.assertFalse((self.stage / 'usr/bin/eeg-loop').exists())

    def test_failed_publish_never_recursively_removes_published_directories(self):
        target = self.stage / 'usr/lib/xtos/xtos_action_gate'
        real_rmtree = installer.shutil.rmtree
        cleanup_attempts = []
        failed = self.stage / 'usr/lib/xtos/xtos_eeg_loop'
        real_publish = installer._rename_noreplace

        def fail_second(source, destination):
            if Path(destination) == failed:
                raise OSError(5, 'injected second publish failure')
            return real_publish(source, destination)

        def record_rmtree(path, *args, **kwargs):
            cleanup_attempts.append(Path(path))
            return real_rmtree(path, *args, **kwargs)

        with mock.patch.object(installer, '_rename_noreplace', side_effect=fail_second):
            with mock.patch.object(installer.shutil, 'rmtree', side_effect=record_rmtree):
                with self.assertRaises(OSError):
                    installer.install(self.stage, ROOT)
        self.assertNotIn(target, cleanup_attempts)
        self.assertTrue(target.is_dir())
        self.assertEqual({p.name for p in target.iterdir()},
                         set(installer.PACKAGES['xtos_action_gate']))


if __name__ == '__main__':
    unittest.main()
