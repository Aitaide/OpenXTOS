"""Optional BLE bundle: real offline wheels and actual chroot interpreter."""
import hashlib
import importlib.util
import copy
import zipfile
import stat
import warnings
from unittest.mock import patch
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/install_ble_runtime.py"
LOCK = REPO / "system/packages/ble-runtime-cp313-x86_64.json"
WHEEL_INPUT = os.environ.get('XTOS_BLE_WHEEL_DIR')
WHEELS = Path(WHEEL_INPUT) if WHEEL_INPUT else REPO / '__optional_ble_cache_not_selected__'
EVIDENCE = Path(os.environ.get('XTOS_BLE_TEST_EVIDENCE',
    str(REPO / '.build/remaining-closure-v2/ble-runtime-parent-tests')))


def copy_elf(source, root):
    source = Path(source)
    dest = root / str(source).lstrip("/")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    dest.chmod(0o755)
    out = subprocess.run(["ldd", str(source)], text=True, capture_output=True, check=True).stdout
    for name in re.findall(r"(?:=>\s*)?(/[^\s()]+)", out):
        dep = Path(name)
        target = root / name.lstrip("/")
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(dep, target)
            target.chmod(0o755)


class LockAndWheelTests(unittest.TestCase):
    def test_exact_lock_schema_rejects_malformed_or_unsafe_fields(self):
        spec = importlib.util.spec_from_file_location('ble_runtime', SCRIPT)
        if spec is None or spec.loader is None:
            raise RuntimeError('installer module unavailable')
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        baseline = json.loads(LOCK.read_text())
        with tempfile.TemporaryDirectory(prefix='ble-lock-') as tmp:
            path = Path(tmp) / 'lock.json'
            path.write_text(json.dumps(baseline))
            self.assertEqual(app.read_lock(path), baseline)
            mutations = []
            for field, value in [('schema', 'unknown'), ('unexpected', True), ('packages', []),
                                 ('target', dict(baseline['target'], abi='cp312'))]:
                value_lock = copy.deepcopy(baseline)
                value_lock[field] = value
                mutations.append(value_lock)
            for field, value in [('bytes', True), ('bytes', 1.0), ('sha256', 'z' * 64),
                                 ('wheel', '../escape.whl'), ('source_url', 'http://example.org/x.whl'),
                                 ('licenses', ['../LICENSE']), ('requires_dist', [None])]:
                value_lock = copy.deepcopy(baseline)
                value_lock['packages'][0][field] = value
                mutations.append(value_lock)
            for mutation in mutations:
                with self.subTest(mutation=mutation):
                    path.write_text(json.dumps(mutation))
                    with self.assertRaises(ValueError):
                        app.read_lock(path)
            path.write_text('{"schema":"a","schema":"b"}')
            with self.assertRaises(ValueError):
                app.read_lock(path)


    def test_malformed_field_types_are_cleanly_refused(self):
        spec = importlib.util.spec_from_file_location('ble_runtime', SCRIPT)
        if spec is None or spec.loader is None:
            raise RuntimeError('installer module unavailable')
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        baseline = json.loads(LOCK.read_text())
        with tempfile.TemporaryDirectory(prefix='ble-lock-types-') as tmp:
            path = Path(tmp) / 'lock.json'
            for key, value in [('name', []), ('source_url', 7), ('licenses', [[]]),
                               ('licenses', [None]), ('requires_python', []),
                               ('requires_dist', {}), ('version', False)]:
                with self.subTest(field=key, value=value):
                    changed = copy.deepcopy(baseline)
                    changed['packages'][0][key] = value
                    path.write_text(json.dumps(changed))
                    with self.assertRaises(ValueError):
                        app.read_lock(path)

    @unittest.skipUnless(WHEEL_INPUT, 'optional real offline wheels: set XTOS_BLE_WHEEL_DIR')
    def test_wheel_bytes_archive_members_and_record_must_match(self):
        spec = importlib.util.spec_from_file_location('ble_runtime', SCRIPT)
        if spec is None or spec.loader is None:
            raise RuntimeError('installer module unavailable')
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        lock = json.loads(LOCK.read_text())
        for package in lock['packages']:
            payload = app.read_wheel(WHEELS / package['wheel'], package)
            self.assertTrue(payload)
            self.assertIn(package['name'].replace('-', '_') + '/__init__.py', payload)
        package = lock['packages'][0]
        with tempfile.TemporaryDirectory(prefix='ble-wheel-') as tmp:
            wheel = Path(tmp) / package['wheel']
            raw = (WHEELS / package['wheel']).read_bytes()
            wheel.write_bytes(raw[:-1])
            with self.assertRaisesRegex(ValueError, 'hash/bytes'):
                app.read_wheel(wheel, package)
            for name in ('../escape.py', '/escape.py', 'bleak/../../escape.py',
                         'bleak\\escape.py', 'bleak/__init__.py', 'bleak/link.py'):
                with self.subTest(member=name):
                    wheel.write_bytes(raw)
                    with warnings.catch_warnings(record=True) as emitted:
                        warnings.simplefilter('always')
                        with zipfile.ZipFile(wheel, 'a') as z:
                            entry = zipfile.ZipInfo(name)
                            entry.external_attr = (stat.S_IFLNK | 0o777) << 16 if name.endswith('link.py') else (stat.S_IFREG | 0o644) << 16
                            z.writestr(entry, 'sentinel')
                    self.assertEqual(bool(emitted), name == 'bleak/__init__.py')
                    changed = dict(package, bytes=wheel.stat().st_size,
                                   sha256=hashlib.sha256(wheel.read_bytes()).hexdigest())
                    with self.assertRaises(ValueError):
                        app.read_wheel(wheel, changed)
            wheel.write_bytes(raw)
            changed = dict(package, requires_python='>=99')
            with self.assertRaisesRegex(ValueError, 'metadata'):
                app.read_wheel(wheel, changed)


    @unittest.skipUnless(WHEEL_INPUT, 'optional real offline wheels: set XTOS_BLE_WHEEL_DIR')
    def test_archive_metadata_record_and_resource_boundaries(self):
        spec = importlib.util.spec_from_file_location('ble_runtime', SCRIPT)
        if spec is None or spec.loader is None:
            raise RuntimeError('installer module unavailable')
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        package = json.loads(LOCK.read_text())['packages'][0]
        with zipfile.ZipFile(WHEELS / package['wheel']) as original:
            entries = [(item, original.read(item)) for item in original.infolist()]
        mutations = ['record-content', 'wheel-abi', 'license-missing', 'unrecorded-file',
                     'file-directory', 'special-mode', 'compression-limit', 'member-limit']
        with tempfile.TemporaryDirectory(prefix='ble-wheel-metadata-') as tmp:
            path = Path(tmp) / package['wheel']
            for mutation in mutations:
                with self.subTest(case=mutation):
                    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as z:
                        for original_info, original_data in entries:
                            info, data = copy.copy(original_info), original_data
                            if mutation == 'record-content' and info.filename == 'bleak/__init__.py':
                                data += b'\n# deliberately altered member'
                            if mutation == 'wheel-abi' and info.filename.endswith('.dist-info/WHEEL'):
                                data = data.replace(b'Tag: py3-none-any', b'Tag: cp312-cp312-linux_x86_64')
                            if mutation == 'license-missing' and info.filename.endswith('/licenses/LICENSE'):
                                continue
                            if mutation == 'special-mode' and info.filename == 'bleak/__init__.py':
                                info.external_attr = (stat.S_IFREG | 0o4755) << 16
                            z.writestr(info, data)
                        if mutation in ('unrecorded-file', 'file-directory'):
                            z.writestr('bleak/new.py' if mutation == 'unrecorded-file' else 'bleak/backends', b'x')
                        if mutation == 'compression-limit':
                            z.writestr('bleak/huge.py', b'x' * 2000000)
                        if mutation == 'member-limit':
                            for index in range(4100):
                                z.writestr('bleak/member-' + str(index), b'x')
                    changed = dict(package, bytes=path.stat().st_size,
                                   sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                    with self.assertRaises(ValueError):
                        app.read_wheel(path, changed)


@unittest.skipUnless(WHEEL_INPUT, 'optional real offline wheels: set XTOS_BLE_WHEEL_DIR')
class BLERuntimeInstall(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0 or not shutil.which("chroot"):
            raise RuntimeError("real chroot tests require root and chroot")
        if sys.version_info[:2] != (3, 13):
            raise RuntimeError("fixture requires actual CPython 3.13")
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        cls.work = Path(tempfile.mkdtemp(prefix='fixture-', dir=EVIDENCE))
        def cleanup_fixture():
            for name in ('commands.jsonl', 'runtime-copy-inventory.json'):
                if (cls.work / name).is_file():
                    shutil.copyfile(cls.work / name, EVIDENCE / (cls.work.name + '-' + name))
            shutil.rmtree(cls.work)
        cls.addClassCleanup(cleanup_fixture)
        cls.base = cls.work / "base"
        cls.base.mkdir()
        copy_elf("/usr/bin/python3.13", cls.base)
        stdlib = Path("/usr/lib/python3.13")
        shutil.copytree(stdlib, cls.base / "usr/lib/python3.13", ignore=shutil.ignore_patterns("__pycache__", "site-packages", "dist-packages", "test", "tests"))
        for so in (stdlib / "lib-dynload").glob("*.so"):
            copy_elf(so, cls.base)
        inventory = []
        for p in sorted(cls.base.rglob("*")):
            if p.is_file():
                data = p.read_bytes()
                inventory.append({"path": str(p.relative_to(cls.base)), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        (cls.work / "runtime-copy-inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="case-", dir=self.work)) / "root"
        shutil.copytree(self.base, self.root, copy_function=os.link)

    def install(self, *extra, wheel_dir=WHEELS, lock=LOCK):
        flags = ['-O'] if sys.flags.optimize else []
        command = [sys.executable, *flags, '-B', str(SCRIPT), '--root', str(self.root), '--wheel-dir', str(wheel_dir), '--lock', str(lock), *extra]
        p = subprocess.run(command, text=True, capture_output=True, timeout=40)
        log = {"command": command, "exit": p.returncode, "stdout": p.stdout, "stderr": p.stderr}
        with (self.work / "commands.jsonl").open("a") as f:
            f.write(json.dumps(log) + "\n")
        return p

    def test_installs_exact_offline_bundle_and_runs_real_target_import(self):
        # The optional extension has its own ELF closure, beyond CPython's.
        copy_elf('/lib/x86_64-linux-gnu/libpthread.so.0', self.root)
        p = self.install()
        self.assertEqual(p.returncode, 0, p.stderr)
        receipt = json.loads(p.stdout)
        self.assertEqual(receipt["probe"]["status"], "dependencies_ready")
        self.assertFalse(receipt["probe"]["physical_ble_verified"])
        self.assertFalse(receipt["probe"]["bluez_verified"])
        self.assertTrue(receipt['probe']['native_modules'])
        self.assertEqual(receipt['probe']['optimization'], sys.flags.optimize)
        self.assertEqual(receipt['probe']['versions'], {'bleak': '3.0.1', 'dbus-fast': '5.2.0'})
        self.assertEqual(receipt['probe']['linux_backend'], 'bleak.backends.bluezdbus.client')
        self.assertEqual(receipt['lock_sha256'], hashlib.sha256(LOCK.read_bytes()).hexdigest())
        bundle = self.root / "opt/xtos-ble"
        self.assertTrue((bundle / "site-packages/bleak-3.0.1.dist-info/licenses/LICENSE").is_file())
        self.assertTrue((bundle / "site-packages/dbus_fast-5.2.0.dist-info/licenses/LICENSE").is_file())
        flags = ['-O'] if sys.flags.optimize else []
        command = [shutil.which('chroot'), str(self.root), '/usr/bin/python3.13', *flags, '-B', '-I', '-S', '/opt/xtos-ble/probe.py']
        actual = subprocess.run(command, text=True, capture_output=True, timeout=15)
        with (self.work / 'commands.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(command=command, exit=actual.returncode,
                stdout=actual.stdout, stderr=actual.stderr)) + '\n')
        self.assertEqual(actual.returncode, 0, actual.stderr)
        self.assertEqual(json.loads(actual.stdout)["status"], "dependencies_ready")
        self.assertEqual(json.loads((bundle / 'manifest.json').read_text()), receipt)
        self.assertEqual({row['path'] for row in receipt['files']},
                         {str(p.relative_to(bundle)) for p in bundle.rglob('*')
                          if p.is_file() and p.name != 'manifest.json'})
        for row in receipt['files']:
            path = bundle / row['path']
            self.assertEqual(len(path.read_bytes()), row['bytes'])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), row['sha256'])
            self.assertEqual(path.stat().st_mode & 0o7777, row['mode'])
        manifest_before = (bundle / 'manifest.json').read_bytes()
        refused_retry = self.install()
        self.assertNotEqual(refused_retry.returncode, 0)
        self.assertIn('fresh-only', refused_retry.stderr)
        self.assertEqual((bundle / 'manifest.json').read_bytes(), manifest_before)
        self.assertFalse((self.root / 'usr/lib/python3.13/site-packages').exists())


    def test_missing_native_dso_refuses_without_publishing_and_can_retry(self):
        failed = self.install()
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('libpthread.so.0', failed.stderr)
        self.assertFalse((self.root / 'opt/xtos-ble').exists())
        self.assertEqual(list((self.root / 'opt').iterdir()), [])
        copy_elf('/lib/x86_64-linux-gnu/libpthread.so.0', self.root)
        recovered = self.install()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)

    def test_missing_or_corrupt_wheel_refuses_before_target_writes(self):
        with tempfile.TemporaryDirectory(prefix='ble-cache-') as tmp:
            cache = Path(tmp)
            for label in ('missing', 'corrupt'):
                with self.subTest(case=label):
                    if label == 'corrupt':
                        for p in WHEELS.glob('*.whl'):
                            shutil.copyfile(p, cache / p.name)
                        name = json.loads(LOCK.read_text())['packages'][0]['wheel']
                        with (cache / name).open('ab') as stream:
                            stream.write(b'corrupt')
                    failed = self.install(wheel_dir=cache)
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertIn('wheel hash/bytes', failed.stderr)
                    self.assertFalse((self.root / 'opt').exists())

    def test_symlink_or_existing_destination_is_preserved(self):
        (self.root / 'opt').mkdir()
        outside = self.root.parent / 'outside'
        outside.mkdir()
        (outside / 'sentinel').write_bytes(b'owned-by-caller')
        (self.root / 'opt/xtos-ble').symlink_to(outside, target_is_directory=True)
        copy_elf('/lib/x86_64-linux-gnu/libpthread.so.0', self.root)
        failed = self.install()
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('fresh-only', failed.stderr)
        self.assertEqual((outside / 'sentinel').read_bytes(), b'owned-by-caller')
        self.assertTrue((self.root / 'opt/xtos-ble').is_symlink())
        (self.root / 'opt/xtos-ble').unlink()
        (self.root / 'opt').rmdir()
        (self.root / 'opt').symlink_to(outside, target_is_directory=True)
        failed = self.install()
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('only directories', failed.stderr)
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ['sentinel'])

    def test_target_abi_mismatch_refuses_before_publication(self):
        # Break a test-owned copy, never the hardlinked base or host stdlib.
        data = self.root / 'usr/lib/python3.13/_sysconfigdata__linux_x86_64-linux-gnu.py'
        raw = data.read_bytes()
        self.assertEqual(raw.count(b"'SOABI': 'cpython-313-x86_64-linux-gnu'"), 1)
        data.unlink()
        data.write_bytes(raw.replace(b"'SOABI': 'cpython-313-x86_64-linux-gnu'",
                                     b"'SOABI': 'cpython-312-x86_64-linux-gnu'"))
        failed = self.install()
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('target ABI mismatch', failed.stderr)
        self.assertFalse((self.root / 'opt').exists())

    def test_lock_changed_during_probe_is_not_rebound_as_valid(self):
        spec = importlib.util.spec_from_file_location('ble_runtime', SCRIPT)
        if spec is None or spec.loader is None:
            raise RuntimeError('installer module unavailable')
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        copy_elf('/lib/x86_64-linux-gnu/libpthread.so.0', self.root)
        lock = self.root.parent / 'changing-lock.json'
        lock.write_bytes(LOCK.read_bytes())
        run = app.command
        def change_lock(argv):
            result = run(argv)
            if argv[-1].endswith('/probe.py'):
                lock.write_bytes(b'{"changed":true}')
            return result
        with patch.object(app, 'command', side_effect=change_lock):
            with self.assertRaisesRegex(ValueError, 'lock changed'):
                app.install(self.root, WHEELS, lock)
        self.assertEqual(list((self.root / 'opt').iterdir()), [])

    def test_probe_side_effect_cannot_publish_changed_payload(self):
        spec = importlib.util.spec_from_file_location('ble_runtime', SCRIPT)
        if spec is None or spec.loader is None:
            raise RuntimeError('installer module unavailable')
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        copy_elf('/lib/x86_64-linux-gnu/libpthread.so.0', self.root)
        run = app.command
        def change_payload(argv):
            result = run(argv)
            if argv[-1].endswith('/probe.py'):
                target = self.root / argv[-1].lstrip('/')
                target.write_bytes(b'changed after import')
            return result
        with patch.object(app, 'command', side_effect=change_payload):
            with self.assertRaisesRegex(ValueError, 'payload changed'):
                app.install(self.root, WHEELS, LOCK)
        self.assertEqual(list((self.root / 'opt').iterdir()), [])

    def test_publication_race_preserves_new_caller_directory(self):
        spec = importlib.util.spec_from_file_location('ble_runtime', SCRIPT)
        if spec is None or spec.loader is None:
            raise RuntimeError('installer module unavailable')
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        copy_elf('/lib/x86_64-linux-gnu/libpthread.so.0', self.root)
        publish = app._publish
        def racing_publish(stage, final):
            final.mkdir()
            (final / 'sentinel').write_bytes(b'racing-caller')
            publish(stage, final)
        with patch.object(app, '_publish', side_effect=racing_publish):
            with self.assertRaises(OSError):
                app.install(self.root, WHEELS, LOCK)
        final = self.root / 'opt/xtos-ble'
        self.assertEqual((final / 'sentinel').read_bytes(), b'racing-caller')
        self.assertEqual(sorted(p.name for p in (self.root / 'opt').iterdir()), ['xtos-ble'])


if __name__ == "__main__":
    unittest.main()
