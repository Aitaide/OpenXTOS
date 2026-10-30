#!/usr/bin/env python3
"""Verify complete private AI inputs before copying into a Live root."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/live_ai.py'
sys.path.insert(0, str(ROOT / 'scripts'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LiveAiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.source = self.work / 'source'
        package = self.source / 'tools/xtos-ai-runtime/xtos_ai_runtime'
        package.mkdir(parents=True)
        maintained = ROOT / 'tools/xtos-ai-runtime/xtos_ai_runtime'
        for source in maintained.glob('*.py'):
            (package / source.name).write_bytes(source.read_bytes())
        self.runtime = self.work / 'runtime'
        self.runtime.mkdir()
        (self.runtime / 'bin').mkdir()
        spec = importlib.util.spec_from_file_location('runtime_provision', ROOT / 'scripts/provision_ai_runtime.py')
        provision = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(provision)
        (self.runtime / 'bin/xtos-ai-python').write_bytes(provision.WRAPPER)
        (self.runtime / 'bin/xtos-ai-python').chmod(0o755)
        (self.runtime / 'clean').mkdir()
        payload = self.runtime / 'clean/payload'
        payload.write_bytes(b'pinned payload')
        self.descriptor = self.work / 'descriptor.json'
        self.descriptor.write_text(json.dumps({
            'schema': 'xtos-ai-runtime-bundle-v1',
            'runtime': {'CPython': '3.11.15', 'onnxruntime': '1.23.2', 'numpy': '1.26.4'},
            'architecture': 'x86_64', 'lock': {'sha256': 'a' * 64},
            'bundle': {'sha256': 'b' * 64, 'bytes': 12},
            'members': [{'path': '.', 'type': 'dir', 'mode': 0o755},
                        {'path': 'payload', 'type': 'file', 'mode': 0o644,
                         'size': payload.stat().st_size, 'sha256': sha(payload)}],
        }))
        self.manifest = {
            'schema': 'xtos-ai-private-runtime-manifest-v1',
            'status': 'installed_verified_artifact', 'architecture': 'x86_64',
            'runtime': json.loads(self.descriptor.read_text())['runtime'],
            'lock': {'sha256': 'a' * 64},
            'bundle': {'sha256': 'b' * 64, 'bytes': 12},
            'descriptor_sha256': sha(self.descriptor),
            'files': [
                {'path': 'bin', 'type': 'dir'},
                {'path': 'bin/xtos-ai-python', 'type': 'file',
                 'bytes': (self.runtime / 'bin/xtos-ai-python').stat().st_size,
                 'sha256': sha(self.runtime / 'bin/xtos-ai-python')},
                {'path': 'clean', 'type': 'dir'},
                {'path': 'clean/payload', 'type': 'file',
                 'bytes': payload.stat().st_size, 'sha256': sha(payload)},
            ],
        }
        self.write_manifest()
        spec = importlib.util.spec_from_file_location('live_ai', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def write_manifest(self):
        (self.runtime / 'provision-manifest.json').write_text(json.dumps(self.manifest))

    def validate(self):
        return self.module.validate_runtime(self.runtime, self.descriptor, 'a' * 64)

    def test_complete_verified_runtime_is_copied_with_sources_and_launcher(self):
        result = self.validate()
        stage = self.work / 'stage'
        stage.mkdir()
        report = self.module.install_runtime(stage, self.runtime, self.descriptor, ROOT, 'a' * 64)
        self.assertEqual(report['provision_manifest_sha256'], result['provision_manifest_sha256'])
        self.assertEqual((stage / 'opt/xtos-ai/bin/xtos-ai-python').read_bytes(),
                         (self.runtime / 'bin/xtos-ai-python').read_bytes())
        for name in ['__init__.py', '__main__.py', 'client.py', 'server.py', 'worker.py', 'trust.py']:
            self.assertEqual((stage / 'usr/lib/xtos/xtos_ai_runtime' / name).read_bytes(),
                             (self.source / 'tools/xtos-ai-runtime/xtos_ai_runtime' / name).read_bytes())
            self.assertEqual(report['service_sources_sha256'][name],
                             sha(self.source / 'tools/xtos-ai-runtime/xtos_ai_runtime' / name))
        launcher = stage / 'usr/libexec/xtos/ai-service'
        self.assertTrue(os.access(launcher, os.X_OK))
        syntax = subprocess.run(['/bin/sh', '-n', str(launcher)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        text = launcher.read_text()
        self.assertIn('/opt/xtos-ai/bin/xtos-ai-python', text)
        self.assertNotIn('PYTHONPATH=', text)
        self.assertNotIn('unsigned', text)
        self.assertEqual(json.loads((stage / 'usr/share/xtos/ai-runtime.json').read_text()), report)

    def test_tampered_missing_and_extra_files_are_refused(self):
        entry = self.runtime / 'bin/xtos-ai-python'
        original = entry.read_bytes()
        entry.write_bytes(original + b'#tamper\n')
        with self.assertRaises(ValueError): self.validate()
        entry.write_bytes(original)
        entry.unlink()
        with self.assertRaises(ValueError): self.validate()
        entry.write_bytes(original)
        extra = self.runtime / 'extra'
        extra.write_bytes(b'unlisted')
        with self.assertRaises(ValueError): self.validate()

    def test_manifest_and_descriptor_binding_fail_closed(self):
        self.manifest['descriptor_sha256'] = '0' * 64
        self.write_manifest()
        with self.assertRaises(ValueError): self.validate()
        self.manifest['descriptor_sha256'] = sha(self.descriptor)
        self.manifest['runtime']['onnxruntime'] = '0.0.0'
        self.write_manifest()
        with self.assertRaises(ValueError): self.validate()

    def test_descriptor_member_inventory_is_bound_to_installed_clean_tree(self):
        descriptor = json.loads(self.descriptor.read_text())
        descriptor['members'][1]['sha256'] = '0' * 64
        self.descriptor.write_text(json.dumps(descriptor))
        self.manifest['descriptor_sha256'] = sha(self.descriptor)
        self.write_manifest()
        with self.assertRaises(ValueError):
            self.validate()

    def test_generated_private_launcher_cannot_be_reauthorized_by_manifest_edit(self):
        launcher = self.runtime / 'bin/xtos-ai-python'
        launcher.write_bytes(b'#!/bin/sh\nexec /usr/bin/python3 "$@"\n')
        launcher_row = next(row for row in self.manifest['files']
                            if row['path'] == 'bin/xtos-ai-python')
        launcher_row.update(bytes=launcher.stat().st_size, sha256=sha(launcher))
        self.write_manifest()
        with self.assertRaises(ValueError):
            self.validate()

    def test_special_path_types_and_duplicate_members_are_refused(self):
        self.manifest['files'].append(dict(self.manifest['files'][0]))
        self.write_manifest()
        with self.assertRaises(ValueError): self.validate()
        self.manifest['files'].pop()
        self.manifest['files'][0]['path'] = '../escape'
        self.write_manifest()
        with self.assertRaises(ValueError): self.validate()

    def test_symlink_escape_is_refused_and_private_internal_link_works(self):
        link = self.runtime / 'bin/private-python'
        link.symlink_to('xtos-ai-python')
        self.manifest['files'].append({'path': 'bin/private-python', 'type': 'symlink',
                                       'target': 'xtos-ai-python'})
        self.write_manifest()
        self.validate()
        link.unlink()
        outside = self.work / 'outside'
        outside.write_bytes(b'caller-owned')
        link.symlink_to(outside)
        self.manifest['files'][-1]['target'] = str(outside)
        self.write_manifest()
        with self.assertRaises(ValueError): self.validate()
        self.assertEqual(outside.read_bytes(), b'caller-owned')

    def test_live_builder_exposes_private_runtime_inputs(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/build_live.py'), '--help'],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--ai-runtime-root', result.stdout)
        self.assertIn('--ai-runtime-descriptor', result.stdout)

    def test_live_builder_copies_verified_private_runtime(self):
        import ast
        tree = ast.parse((ROOT / 'scripts/build_live.py').read_text())
        build = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'build')
        calls = [node for node in ast.walk(build) if isinstance(node, ast.Call)]
        installers = [node for node in calls if isinstance(node.func, ast.Name) and node.func.id == 'install_runtime']
        self.assertEqual(len(installers), 1)
        self.assertIn('ai_runtime', (ROOT / 'scripts/build_live.py').read_text())

    def test_full_suite_keeps_all_new_security_modules(self):
        text = (ROOT / 'scripts/test.sh').read_text()
        for name in ['test_ai_model_trust.py', 'test_ai_model_revocation.py',
                     'test_ai_signed_lifecycle.py', 'test_ai_runtime_assembly.py',
                     'test_action_gate.py', 'test_action_executor.py',
                     'test_action_executor_resources.py', 'test_native_modelverify.py',
                     'test_ai_runtime_bundle.py', 'test_live_ai.py']:
            self.assertIn(name, text)

    def test_preinstalled_verified_runtime_sources_are_reused_not_overwritten(self):
        spec = importlib.util.spec_from_file_location('install_eeg_loop', ROOT / 'scripts/install_eeg_loop.py')
        install_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(install_module)
        install_eeg = install_module.install
        stage = self.work / 'stage'
        stage.mkdir()
        install_eeg(stage, ROOT)
        before = {path.relative_to(stage / 'usr/lib/xtos').as_posix(): sha(path)
                  for path in (stage / 'usr/lib/xtos/xtos_ai_runtime').glob('*.py')}
        report = self.module.install_runtime(stage, self.runtime, self.descriptor, ROOT, 'a' * 64)
        self.assertEqual(set(report['service_sources_sha256']),
                         {Path(path).name for path in before})
        after = {path.name: sha(path)
                 for path in (stage / 'usr/lib/xtos/xtos_ai_runtime').glob('*.py')}
        self.assertEqual(report['service_sources_sha256'], after)
        self.assertEqual({path.relative_to(stage / 'usr/lib/xtos').as_posix(): sha(path)
                          for path in (stage / 'usr/lib/xtos/xtos_ai_runtime').glob('*.py')}, before)

    def test_preinstalled_tampered_runtime_source_is_refused_without_overwrite(self):
        spec = importlib.util.spec_from_file_location('install_eeg_loop', ROOT / 'scripts/install_eeg_loop.py')
        install_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(install_module)
        install_eeg = install_module.install
        stage = self.work / 'stage'
        stage.mkdir()
        install_eeg(stage, ROOT)
        target = stage / 'usr/lib/xtos/xtos_ai_runtime/client.py'
        target.write_bytes(b'caller-owned tampering')
        with self.assertRaisesRegex(ValueError, 'AI staging destination already exists'):
            self.module.install_runtime(stage, self.runtime, self.descriptor, ROOT, 'a' * 64)
        self.assertEqual(target.read_bytes(), b'caller-owned tampering')

    def test_p3_suites_remain_in_shared_maintenance_entry(self):
        text = (ROOT / 'scripts/test.sh').read_text()
        for name in ('test_eeg_stream_process.py', 'test_eeg_controller.py',
                     'test_eeg_feature_binding.py', 'test_ai_model_binding.py'):
            self.assertIn(name, text)

    def test_postcopy_validation_failure_removes_only_owned_runtime(self):
        stage = self.work / 'stage'
        sentinels = []
        for directory in ['opt', 'usr/lib/xtos', 'usr/libexec/xtos', 'usr/share/xtos']:
            parent = stage / directory
            parent.mkdir(parents=True, exist_ok=True)
            sentinel = parent / 'caller-sentinel'
            sentinel.write_bytes(b'caller-owned')
            sentinels.append(sentinel)
        before = set(stage.rglob('*'))
        destination = stage / 'opt/xtos-ai'
        original_payload = (self.runtime / 'clean/payload').read_bytes()
        copytree = self.module.shutil.copytree
        copied_roots = []

        def corrupt_copy(source, target, *args, **kwargs):
            result = copytree(source, target, *args, **kwargs)
            if Path(source) == self.runtime:
                copied_roots.append(Path(target))
                (Path(target) / 'clean/payload').write_bytes(b'changed after copy')
            return result

        with mock.patch.object(self.module.shutil, 'copytree', side_effect=corrupt_copy):
            with self.assertRaisesRegex(ValueError, 'AI file content or mode mismatch'):
                self.module.install_runtime(stage, self.runtime, self.descriptor, self.source, 'a' * 64)
        self.assertEqual(len(copied_roots), 1)
        self.assertFalse(os.path.lexists(destination), 'rejected runtime must not remain installed')
        self.assertEqual(set(stage.rglob('*')), before, 'only operation-owned paths may be removed')
        for sentinel in sentinels:
            self.assertEqual(sentinel.read_bytes(), b'caller-owned')
        self.assertEqual((self.runtime / 'clean/payload').read_bytes(), original_payload)
        self.validate()

    def test_invalid_runtime_creates_no_destination(self):
        stage = self.work / 'stage'
        stage.mkdir()
        (self.runtime / 'bin/xtos-ai-python').write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            self.module.install_runtime(stage, self.runtime, self.descriptor, self.source, 'a' * 64)
        self.assertEqual(list(stage.iterdir()), [])

    def test_postcopy_report_mismatch_removes_owned_runtime(self):
        stage = self.work / 'stage'
        stage.mkdir()
        copytree = self.module.shutil.copytree
        copied_reports = []

        def change_manifest(source, target, *args, **kwargs):
            result = copytree(source, target, *args, **kwargs)
            if Path(source) == self.runtime:
                manifest = Path(target) / 'provision-manifest.json'
                manifest.write_bytes(manifest.read_bytes() + b'\n')
                copied_reports.append(self.module.validate_runtime(target, self.descriptor, 'a' * 64))
            return result

        with mock.patch.object(self.module.shutil, 'copytree', side_effect=change_manifest):
            with self.assertRaisesRegex(ValueError, 'AI input changed while copying'):
                self.module.install_runtime(stage, self.runtime, self.descriptor, self.source, 'a' * 64)
        self.assertEqual(len(copied_reports), 1, 'changed copy must pass its own full validation')
        self.assertNotEqual(copied_reports[0], self.validate())
        self.assertFalse(os.path.lexists(stage / 'opt/xtos-ai'))
        self.assertFalse(os.path.lexists(stage / 'opt'), 'failed validation must not leave a new parent')
        self.assertFalse(os.path.lexists(stage / 'usr'))

    def test_preexisting_destinations_are_preserved(self):
        targets = ['opt/xtos-ai', 'usr/lib/xtos/xtos_ai_runtime',
                   'usr/libexec/xtos/ai-service', 'usr/libexec/xtos/ai-entry.py',
                   'usr/share/xtos/ai-runtime.json']
        for index, relative in enumerate(targets):
            for kind in ['directory', 'file', 'symlink', 'dangling-symlink']:
                with self.subTest(target=relative, kind=kind):
                    stage = self.work / f'stage-{index}-{kind}'
                    target = stage / relative
                    target.parent.mkdir(parents=True)
                    outside = self.work / f'caller-{index}-{kind}'
                    outside.write_bytes(b'caller-owned')
                    if kind == 'directory':
                        target.mkdir()
                        (target / 'caller-sentinel').write_bytes(b'caller-owned')
                    elif kind == 'file':
                        target.write_bytes(b'caller-owned')
                    else:
                        target.symlink_to(outside if kind == 'symlink' else outside.with_suffix('.absent'))
                    identity = target.lstat()
                    before = set(stage.rglob('*'))
                    with self.assertRaisesRegex(ValueError, 'AI staging destination already exists'):
                        self.module.install_runtime(stage, self.runtime, self.descriptor, self.source, 'a' * 64)
                    self.assertEqual(set(stage.rglob('*')), before)
                    current = target.lstat()
                    for attribute in ['st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns']:
                        self.assertEqual(getattr(current, attribute), getattr(identity, attribute))
                    self.assertEqual(outside.read_bytes(), b'caller-owned')
                    if kind == 'directory':
                        self.assertEqual((target / 'caller-sentinel').read_bytes(), b'caller-owned')
                    elif kind == 'file':
                        self.assertEqual(target.read_bytes(), b'caller-owned')
                    else:
                        self.assertEqual(os.readlink(target), str(outside if kind == 'symlink'
                                                                  else outside.with_suffix('.absent')))

    def test_postcopy_failure_preserves_racing_destination(self):
        stage = self.work / 'stage'
        (stage / 'opt').mkdir(parents=True)
        destination = stage / 'opt/xtos-ai'
        copytree = self.module.shutil.copytree
        injected = []

        def race_copy(source, target, *args, **kwargs):
            result = copytree(source, target, *args, **kwargs)
            if Path(source) == self.runtime:
                destination.mkdir()
                sentinel = destination / 'caller-sentinel'
                sentinel.write_bytes(b'caller-owned')
                injected.append(sentinel)
                (Path(target) / 'clean/payload').write_bytes(b'changed after copy')
            return result

        with mock.patch.object(self.module.shutil, 'copytree', side_effect=race_copy):
            with self.assertRaisesRegex(ValueError, 'AI file content or mode mismatch'):
                self.module.install_runtime(stage, self.runtime, self.descriptor, self.source, 'a' * 64)
        self.assertEqual(len(injected), 1)
        self.assertEqual(injected[0].read_bytes(), b'caller-owned')
        self.assertEqual(set(stage.rglob('*')), {stage / 'opt', destination, injected[0]})

    def test_publication_preserves_racing_empty_destination(self):
        stage = self.work / 'stage'
        (stage / 'opt').mkdir(parents=True)
        destination = stage / 'opt/xtos-ai'
        copytree = self.module.shutil.copytree
        injected = []

        def race_copy(source, target, *args, **kwargs):
            result = copytree(source, target, *args, **kwargs)
            if Path(source) == self.runtime:
                destination.mkdir()
                injected.append(destination.stat())
            return result

        with mock.patch.object(self.module.shutil, 'copytree', side_effect=race_copy):
            with self.assertRaises(FileExistsError):
                self.module.install_runtime(stage, self.runtime, self.descriptor, self.source, 'a' * 64)
        self.assertEqual(len(injected), 1)
        current = destination.stat()
        for attribute in ['st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns']:
            self.assertEqual(getattr(current, attribute), getattr(injected[0], attribute))
        self.assertEqual(set(stage.rglob('*')), {stage / 'opt', destination})

    def test_service_source_hash_revalidation_refuses_copy_tampering(self):
        stage = self.work / 'stage'
        stage.mkdir()
        copyfile = self.module.shutil.copyfile
        tampered = []

        def corrupt_service(source, target, *args, **kwargs):
            result = copyfile(source, target, *args, **kwargs)
            if Path(source).is_relative_to(self.source):
                Path(target).write_bytes(b'# changed after copy\n')
                tampered.append(Path(target))
            return result

        with mock.patch.object(self.module.shutil, 'copyfile', side_effect=corrupt_service):
            with self.assertRaisesRegex(ValueError, 'AI service source changed while copying'):
                self.module.install_runtime(stage, self.runtime, self.descriptor, self.source, 'a' * 64)
        self.assertEqual(len(tampered), 1)
        self.assertEqual((self.source / 'tools/xtos-ai-runtime/xtos_ai_runtime/__init__.py').read_bytes(),
                         (ROOT / 'tools/xtos-ai-runtime/xtos_ai_runtime/__init__.py').read_bytes())
        for relative in ['usr/libexec/xtos/ai-service', 'usr/libexec/xtos/ai-entry.py',
                         'usr/share/xtos/ai-runtime.json']:
            self.assertFalse(os.path.lexists(stage / relative))


if __name__ == '__main__':
    unittest.main()
