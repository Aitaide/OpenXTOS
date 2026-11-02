"""XR Live tests build an owned source export and real CPython root."""
import hashlib
import importlib.util
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
SOURCE_FILES = (
    'scripts/install_eeg_loop.py', 'scripts/install_xr.py',
    'tools/xtos-xr/Makefile', 'tools/xtos-xr/include/xtos_xr/runtime_adapter.h',
    'tools/xtos-xr/src/runtime_adapter.c', 'tools/xtos-xr/src/xr_probe.c',
    'tools/xtos-xr/include/xtos_xr/eye_session.h',
    'tools/xtos-xr/src/eye_session.c', 'tools/xtos-xr/src/xr_eye_probe.c',
    'tools/xtos-xr/vendor/openxr/COPYING.adoc',
    'tools/xtos-xr/vendor/openxr/LICENSES/Apache-2.0.txt',
    'tools/xtos-xr/vendor/openxr/LICENSES/MIT.txt',
    'tools/xtos-xr/vendor/openxr/include/openxr/openxr.h',
    'tools/xtos-xr/vendor/openxr/include/openxr/openxr_platform.h',
    'tools/xtos-xr/vendor/openxr/include/openxr/openxr_platform_defines.h',
    'tools/xtos-xr/vendor/openxr/provenance.json',
    'tools/xtos-xr/xtos_xr/__init__.py', 'tools/xtos-xr/xtos_xr/__main__.py',
    'tools/xtos-xr/xtos_xr/runtime_events.py',
    'tools/xtos_action_gate/README.md', 'tools/xtos_action_gate/__init__.py',
    'tools/xtos_action_gate/__main__.py', 'tools/xtos_action_gate/schema.py',
    'tools/xtos_action_gate/store.py',
)
# Optional supplemental validation against a genuinely locked target rootfs.
BASE_OVERRIDE = os.environ.get('XR_LIVE_BASE_ROOTFS')


def binding(path):
    data = path.read_bytes()
    return dict(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))


def load_helper():
    path = REPO / 'scripts/live_xr.py'
    if not path.exists():
        raise AssertionError('XR Live install_and_verify helper is missing')
    spec = importlib.util.spec_from_file_location('live_xr_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class LiveXRTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        owned_parent = REPO/'.build/remaining-closure-v2'
        owned_parent.mkdir(parents=True, exist_ok=True)
        cls.owned = tempfile.TemporaryDirectory(prefix='xr-live-tests-', dir=owned_parent)
        try:
            cls.source = Path(cls.owned.name)/'source'
            cls.source.mkdir(mode=0o700)
            cls.export = {}
            for name in SOURCE_FILES:
                original = REPO/name
                if original.is_symlink() or not original.is_file():
                    raise AssertionError('XR source must be regular: ' + name)
                data = original.read_bytes()
                target = cls.source/name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                cls.export[name] = binding(target)
            cls.template = Path(cls.owned.name)/'target'
            cls.template.mkdir(mode=0o700)
            if os.geteuid() != 0:
                raise AssertionError('real target CPython/chroot gate requires root')
            base = Path(BASE_OVERRIDE).resolve(strict=True) if BASE_OVERRIDE else Path('/')
            if BASE_OVERRIDE:
                stdlib_candidates = sorted((base/'usr/lib').glob('python3.*'))
                if len(stdlib_candidates) != 1:
                    raise AssertionError('explicit target root must contain one Python stdlib')
                stdlib = stdlib_candidates[0]
                python_binary = base/'usr/bin/python3'
            else:
                import sysconfig
                stdlib = Path(sysconfig.get_path('stdlib')).resolve(strict=True)
                python_binary = Path(sys.executable).resolve(strict=True)
                if not stdlib.is_relative_to(Path(sys.base_prefix).resolve()):
                    raise AssertionError('stdlib escapes suite interpreter base')
            cls.stdlib_relative = stdlib.relative_to(base)
            target_stdlib = cls.template/cls.stdlib_relative
            shutil.copytree(stdlib, target_stdlib,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'test', 'tests',
                                                          'site-packages', 'dist-packages'))
            binaries = [python_binary, base/'bin/sh', *stdlib.rglob('*.so')]
            dependencies = set()
            for binary in binaries:
                result = subprocess.run(['ldd', str(binary)], capture_output=True, text=True, check=True)
                dependencies.update(re.findall(r'(/[^\s()]+)', result.stdout))
            cls.loader = next((p for p in sorted(dependencies) if 'ld-linux' in p or 'ld-musl' in p), None)
            if cls.loader is None:
                raise AssertionError('target ELF loader not found')
            cls.direct_deps = {}
            for name in sorted(dependencies | {'/usr/bin/python3', '/bin/sh'}):
                source = python_binary if name == '/usr/bin/python3' else base/name.lstrip('/')
                resolved = source.resolve(strict=True)
                if not resolved.is_relative_to(base):
                    raise AssertionError('target dependency escapes selected base: ' + name)
                target = cls.template/name.lstrip('/')
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(resolved, target)
                target.chmod(0o755)
                cls.direct_deps[name] = binding(target)
            (cls.template/'root').mkdir(mode=0o700)
            size = sum(p.stat().st_size for p in cls.template.rglob('*') if p.is_file())
            if size >= 100_000_000:
                raise AssertionError('target fixture exceeds 100 MB budget')
            cls.fixture_bytes = size
            cls.runtime_files = sum(p.is_file() for p in cls.template.rglob('*'))
        except BaseException:
            cls.owned.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            for name, expected in cls.export.items():
                if binding(cls.source/name) != expected or binding(REPO/name) != expected:
                    raise AssertionError('owned XR export or original source changed: ' + name)
        finally:
            cls.owned.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='root-', dir=self.owned.name)
        self.root = Path(self.tmp.name)
        shutil.copytree(self.template, self.root, dirs_exist_ok=True)
        self.addCleanup(self.tmp.cleanup)

    def test_default_fixture_is_owned_not_ignored_export_or_base(self):
        self.assertTrue(self.source.is_relative_to(Path(self.owned.name)))
        self.assertEqual(binding(self.template/'usr/bin/python3'),
                         binding(Path(sys.executable).resolve()))

    def test_optimized_mode_runs_real_target_python_optimized(self):
        report = load_helper().install_and_verify(self.root, self.source)
        self.assertEqual(report['runtime_smoke']['python']['optimization'], sys.flags.optimize)

    def test_headless_eye_binary_executes_target_missing_loader_boundary(self):
        report = load_helper().install_and_verify(self.root, self.source)
        native = report['runtime_smoke']['eye_missing_loader']
        self.assertEqual(native['argv'], ['/usr/bin/xr-eye-probe', '--loader',
            '/__xtos_live_xr_missing_loader__.so', '--accept-eye-data'])
        self.assertEqual(native['exit'], 1)
        self.assertEqual(native['result']['status'], 'runtime_unavailable')
        self.assertEqual(native['result']['safe_action'], 'no_action')
        self.assertIs(native['result']['physical_acceptance'], False)
        self.assertEqual(native['result']['samples'], [])

    def test_unmanifested_package_member_is_refused(self):
        helper = load_helper()
        helper.install_and_verify(self.root, self.source)
        (self.root/'usr/lib/xtos/xtos_xr/extra.py').write_text('raise RuntimeError()\n')
        with self.assertRaisesRegex(ValueError, 'XR package inventory mismatch'):
            helper.verify(self.root, self.source)

    def test_missing_target_stdlib_dependency_fails_runtime_gate(self):
        helper = load_helper()
        (self.root/self.stdlib_relative/'dataclasses.py').unlink()
        with self.assertRaisesRegex(ValueError, 'XR target runtime closure failed'):
            helper.install_and_verify(self.root, self.source)

    def test_launcher_interpreter_mismatch_refused_even_if_rehashed(self):
        helper = load_helper()
        helper.install_and_verify(self.root, self.source)
        path = self.root/'usr/bin/xr-feedback-demo'
        path.write_bytes(path.read_bytes().replace(b'/usr/bin/python3', b'/usr/bin/python9'))
        manifest = self.root/'usr/share/xtos/xr-sources.json'
        record = json.loads(manifest.read_text())
        record['payload_hashes']['usr/bin/xr-feedback-demo'] = binding(path)['sha256']
        manifest.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, 'XR launcher interpreter/argv mismatch'):
            helper.verify(self.root, self.source)

    def test_missing_target_python_fails_runtime_gate(self):
        helper = load_helper()
        (self.root/'usr/bin/python3').unlink()
        with self.assertRaisesRegex(ValueError, 'XR target runtime closure failed'):
            helper.install_and_verify(self.root, self.source)

    def test_missing_target_elf_loader_fails_runtime_gate(self):
        helper = load_helper()
        (self.root/self.loader.lstrip('/')).unlink()
        with self.assertRaisesRegex(ValueError, 'XR target runtime closure failed'):
            helper.install_and_verify(self.root, self.source)

    def test_installed_probe_must_be_regular_hash_bound_executable(self):
        helper = load_helper()
        helper.install_and_verify(self.root, self.source)
        probe = self.root/'usr/bin/xr-probe'
        data = probe.read_bytes()
        probe.write_bytes(data + b'corrupt')
        with self.assertRaisesRegex(ValueError, 'XR payload bytes/mode mismatch'):
            helper.verify(self.root, self.source)
        probe.write_bytes(data)
        probe.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'XR payload bytes/mode mismatch'):
            helper.verify(self.root, self.source)
        probe.unlink()
        probe.symlink_to('/usr/bin/true')
        with self.assertRaises((ValueError, OSError)):
            helper.verify(self.root, self.source)
        probe.unlink()
        os.mkfifo(probe)
        with self.assertRaisesRegex(ValueError, 'not a regular file'):
            helper.verify(self.root, self.source)

    def test_missing_installer_source_binding_is_refused(self):
        helper = load_helper()
        helper.install_and_verify(self.root, self.source)
        path = self.root/'usr/share/xtos/xr-sources.json'
        record = json.loads(path.read_text())
        del record['source_hashes']['tools/xtos-xr/src/runtime_adapter.c']
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, 'XR source inventory mismatch'):
            helper.verify(self.root, self.source)

    def test_rehashed_python_payload_cannot_borrow_original_source(self):
        helper = load_helper()
        helper.install_and_verify(self.root, self.source)
        member = self.root/'usr/lib/xtos/xtos_xr/runtime_events.py'
        member.write_bytes(member.read_bytes() + b'\n# tampered installed module\n')
        path = self.root/'usr/share/xtos/xr-sources.json'
        record = json.loads(path.read_text())
        record['payload_hashes']['usr/lib/xtos/xtos_xr/runtime_events.py'] = binding(member)['sha256']
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, 'XR Python source/payload mismatch'):
            helper.verify(self.root, self.source)

    def test_malformed_manifest_maps_fail_valueerror_before_chroot(self):
        helper = load_helper()
        helper.install_and_verify(self.root, self.source)
        path = self.root/'usr/share/xtos/xr-sources.json'
        original = json.loads(path.read_text())
        def no_chroot(*args, **kwargs):
            raise AssertionError('malformed manifest reached chroot')
        helper._target_run = no_chroot
        for field in ('source_hashes', 'payload_hashes'):
            for bad in ([], {}, None):
                with self.subTest(field=field, bad=bad):
                    record = dict(original)
                    record[field] = bad
                    path.write_text(json.dumps(record))
                    with self.assertRaises(ValueError):
                        helper.verify(self.root, self.source)
            with self.subTest(field=field, bad='omitted'):
                record = dict(original)
                del record[field]
                path.write_text(json.dumps(record))
                with self.assertRaises(ValueError):
                    helper.verify(self.root, self.source)
        path.write_text('[]')
        with self.assertRaises(ValueError):
            helper.verify(self.root, self.source)
        path.write_text(json.dumps(original))

    def test_manifest_omitted_payload_is_refused(self):
        helper = load_helper()
        helper.install_and_verify(self.root, self.source)
        path = self.root/'usr/share/xtos/xr-sources.json'
        record = json.loads(path.read_text())
        del record['payload_hashes']['usr/bin/xr-probe']
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, 'XR payload inventory mismatch'):
            helper.verify(self.root, self.source)

    def test_world_writable_owned_root_refused_before_install(self):
        helper = load_helper()
        self.root.chmod(0o777)
        with self.assertRaisesRegex(ValueError, 'unsafe XR staging root permissions'):
            helper.install_and_verify(self.root, self.source)
        self.assertFalse((self.root/'usr/bin/xr-probe').exists())

    def test_real_target_install_and_runtime_boundary(self):
        helper = load_helper()
        report = helper.install_and_verify(self.root, self.source)
        self.assertEqual(report['status'], 'installed_source_only')
        self.assertIs(report['physical_acceptance'], False)
        self.assertIs(report['vendor_runtime_bundled'], False)
        self.assertEqual(report['runtime_smoke']['python']['implementation'], 'cpython')
        self.assertEqual(report['runtime_smoke']['python']['executable'], '/usr/bin/python3')
        native = report['runtime_smoke']['native_missing_loader']
        self.assertEqual(native['exit'], 3)
        self.assertEqual(native['result']['status'], 'unavailable')
        self.assertEqual(native['result']['safe_action'], 'no_action')
        self.assertIs(native['result']['physical_acceptance'], False)
        demo = report['runtime_smoke']['demo']
        self.assertEqual(demo['exit'], 0)
        self.assertEqual(demo['result']['safe_action'], 'no_action')
        self.assertIs(demo['result']['physical_acceptance'], False)
        record = json.loads((self.root/'usr/share/xtos/xr-sources.json').read_text())
        self.assertEqual(set(report['source_inventory']), set(record['source_hashes']))
        self.assertEqual(set(report['source_inventory']), helper.SOURCE_NAMES)
        for payload, source in helper.PYTHON_SOURCE.items():
            self.assertEqual((self.root/payload).read_bytes(), (self.source/source).read_bytes())
        for name, row in report['source_inventory'].items():
            self.assertEqual(row, binding(self.source/name))
        self.assertEqual(set(report['payload_inventory']), set(record['payload_hashes']) | {'usr/share/xtos/xr-sources.json'})
        for name, row in report['payload_inventory'].items():
            self.assertEqual(row, dict(binding(self.root/name), mode=(self.root/name).stat().st_mode & 0o7777))
        self.assertLess(len(json.dumps(report)), 24000)
        guest = (REPO/'tests/fixtures/live_xr_guest_check.py').read_text()
        result = subprocess.run([shutil.which('chroot') or '/usr/sbin/chroot', str(self.root), '/usr/bin/python3',
                                 *(['-O'] if sys.flags.optimize else []), '-B', '-I', '-c', guest],
                                env={'PATH':'/usr/bin:/bin', 'HOME':'/root', 'LC_ALL':'C'},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(json.loads(result.stdout)['authority_store_created'], False)
        self.assertFalse((self.root/'var/lib/xtos/action-gate').exists())
        if evidence := os.environ.get('XR_LIVE_REPORT_PATH'):
            target = Path(evidence)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('x') as stream:
                json.dump(dict(report=report, guest=dict(argv=['/usr/bin/python3',
                    *(['-O'] if sys.flags.optimize else []), '-B', '-I', '-c',
                    'tests/fixtures/live_xr_guest_check.py'], exit=result.returncode,
                    stdout=result.stdout, stderr=result.stderr),
                    fixture=dict(bytes=self.fixture_bytes, files=self.runtime_files,
                                 direct_deps=self.direct_deps, stdlib=str(self.stdlib_relative)),
                    source_export=self.export, suite_optimize=sys.flags.optimize), stream, sort_keys=True, indent=2)


if __name__ == '__main__':
    unittest.main()
