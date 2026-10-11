"""Offline userspace acceptance tests use real Debian and ELF fixtures."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
BUILD = REPO / 'scripts/build_userspace.sh'
VERIFY = REPO / 'scripts/verify_userspace.py'


class UserspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='xtos-userspace-', dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.packages = self.base / 'packages'
        self.packages.mkdir()
        self.lock = self.base / 'lock.json'
        self.output = self.base / 'rootfs'

    def package(self, name='tiny', arch='amd64', files=None):
        tree = self.base / ('source-' + name)
        (tree / 'DEBIAN').mkdir(parents=True)
        (tree / 'DEBIAN/control').write_text(
            f'Package: {name}\nVersion: 1.0-1\nArchitecture: {arch}\n'
            'Maintainer: XTOS Tests <test@example.invalid>\nDescription: local fixture\n')
        for path, value in (files or {'usr/share/doc/tiny/copyright': b'Test license\n'}).items():
            target = tree / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(value, tuple):
                target.symlink_to(value[0])
            else:
                target.write_bytes(value)
        deb = self.packages / f'{name}_1.0-1_{arch}.deb'
        subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(tree), str(deb)],
                       check=True, capture_output=True)
        return self.record(deb, name, arch)

    def record(self, deb, name='tiny', arch='amd64'):
        return {'name': name, 'version': '1.0-1', 'architecture': arch,
                'filename': deb.name, 'sha256': hashlib.sha256(deb.read_bytes()).hexdigest(),
                'size': deb.stat().st_size, 'source_url': 'https://example.invalid/' + deb.name}

    def write_lock(self, records, architecture='x86_64'):
        self.lock.write_text(json.dumps({'schema_version': 1, 'architecture': architecture,
                                        'package_format': 'deb', 'packages': records}))

    def build(self, *extra):
        return subprocess.run(['bash', str(BUILD), '--lock', str(self.lock),
                               '--packages-dir', str(self.packages), '--output-dir',
                               str(self.output), *extra], capture_output=True, text=True)

    def malicious_package(self, members):
        control = b'Package: evil\nVersion: 1.0-1\nArchitecture: amd64\nMaintainer: Test <t@example.invalid>\nDescription: fixture\n'
        def archive(entries):
            out = io.BytesIO()
            with tarfile.open(fileobj=out, mode='w:gz') as tar:
                for info, content in entries:
                    tar.addfile(info, io.BytesIO(content) if content is not None else None)
            return out.getvalue()
        info = tarfile.TarInfo('./control'); info.size = len(control)
        data = {'debian-binary': b'2.0\n', 'control.tar.gz': archive([(info, control)]),
                'data.tar.gz': archive(members)}
        deb = self.packages / 'evil.deb'
        with deb.open('wb') as stream:
            stream.write(b'!<arch>\n')
            for name, content in data.items():
                header = f'{name + "/":<16}{0:<12}{0:<6}{0:<6}{"100644":<8}{len(content):<10}`\n'
                stream.write(header.encode()); stream.write(content)
                if len(content) % 2: stream.write(b'\n')
        return self.record(deb, 'evil')

    def verify(self, rootfs, lock=None, policy=None):
        command = ['python3', str(VERIFY), '--rootfs', str(rootfs)]
        if lock:
            command += ['--lock', str(lock)]
        if policy:
            command += ['--elf-data-policy', str(policy)]
        return subprocess.run(command, capture_output=True, text=True)

    def go_data_fixture(self):
        source = self.base / 'go-object.c'
        source.write_text('int fixture;\n')
        obj = self.base / 'go-object.o'
        subprocess.run(['gcc', '-c', '-o', str(obj), str(source)],
                       check=True, capture_output=True)
        data = bytearray(obj.read_bytes())
        struct.pack_into('<H', data, 18, 183)
        path = '/usr/share/go-1.24/src/debug/elf/testdata/foreign.syso'
        self.write_lock([self.package('golang-1.24-src', arch='all',
                                     files={path.lstrip('/'): bytes(data)})])
        self.assertEqual(self.build().returncode, 0)
        record = {'path': path, 'sha256': hashlib.sha256(data).hexdigest(),
                  'package': 'golang-1.24-src', 'purpose': 'go-cross-compile-object'}
        policy = self.base / 'elf-data-policy.json'
        policy.write_text(json.dumps({'schema_version': 1, 'architecture': 'x86_64',
                                      'data_files': [record]}))
        return policy, record

    def test_hash_bound_go_data_is_explicitly_classified(self):
        policy, record = self.go_data_fixture()
        strict = self.verify(self.output)
        self.assertEqual(strict.returncode, 1)
        self.assertIn('wrong ELF machine', strict.stdout)
        result = self.verify(self.output, self.lock, policy)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['elf_count'], 1)
        self.assertEqual(report['runtime_elf_count'], 0)
        self.assertEqual(report['elf_data_files'], [{**record, 'classification': 'nonruntime_data'}])
        self.assertEqual(report['verification_level'],
                         'ELF closure paths and interpreter; no ABI symbol-version proof')

    def test_policy_classification_preserves_other_runtime_elf_checks(self):
        policy, record = self.go_data_fixture()
        runtime = self.output / 'bin/runtime'
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime_data = bytearray((self.base / 'go-object.o').read_bytes())
        struct.pack_into('<H', runtime_data, 18, 183)
        runtime.write_bytes(runtime_data)
        result = self.verify(self.output, policy=policy)
        report = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report['elf_count'], 2)
        self.assertEqual(report['runtime_elf_count'], 1)
        self.assertEqual(report['elf_data_files'], [{**record, 'classification': 'nonruntime_data'}])
        self.assertTrue(any(error['path'] == '/bin/runtime' and 'wrong ELF machine' in error['reason']
                            for error in report['errors']))

    def test_build_verify_accepts_elf_data_policy(self):
        policy, _ = self.go_data_fixture()
        self.output = self.base / 'verified-rootfs'
        result = self.build('--verify', '--elf-data-policy', str(policy))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.output.is_dir())

    def test_declared_go_data_runtime_alias_is_rejected(self):
        policy, record = self.go_data_fixture()
        alias = self.output / 'usr/bin/go-data-alias'
        alias.parent.mkdir(parents=True, exist_ok=True)
        alias.symlink_to(record['path'])
        result = self.verify(self.output, policy=policy)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(any(error['path'] == '/usr/bin/go-data-alias' and
                            'runtime alias' in error['reason']
                            for error in report['errors']))
        self.assertEqual(report['runtime_elf_count'], 0)
        self.assertEqual(report['elf_data_files'], [{**record, 'classification': 'nonruntime_data'}])

    def test_declared_go_data_runtime_directory_alias_is_rejected(self):
        policy, _ = self.go_data_fixture()
        target_dir = self.output / 'usr/bin/go-sources'
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        target_dir.symlink_to('/usr/share/go-1.24/src', target_is_directory=True)
        result = self.verify(self.output, policy=policy)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        alias_path = '/usr/bin/go-sources/debug/elf/testdata/foreign.syso'
        self.assertTrue(any(error['path'] == alias_path and 'runtime alias' in error['reason']
                            for error in report['errors']))

    def test_nested_runtime_directory_alias_into_go_data_is_rejected(self):
        policy, _ = self.go_data_fixture()
        alias = self.output / 'usr/bin/source-alias'
        alias.parent.mkdir(parents=True, exist_ok=True)
        indirect = self.output / 'usr/share/source-links'
        indirect.mkdir(parents=True)
        (indirect / 'go').symlink_to('/usr/share/go-1.24/src', target_is_directory=True)
        alias.symlink_to('/usr/share/source-links', target_is_directory=True)
        result = self.verify(self.output, policy=policy)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        expected = '/usr/bin/source-alias/go/debug/elf/testdata/foreign.syso'
        self.assertTrue(any(error['path'] == expected and 'runtime alias' in error['reason']
                            for error in json.loads(result.stdout)['errors']))

    def test_runtime_needed_library_cannot_resolve_to_policy_data(self):
        policy, record = self.go_data_fixture()
        (self.output / 'bin').mkdir()
        self.make_elf(self.output, 'bin/app')
        (self.output / 'lib64').mkdir()
        libs = self.output / 'usr/lib/x86_64-linux-gnu'
        libs.mkdir(parents=True)
        shutil.copy2(self.base / 'libfixture.so', self.output / 'lib64/ld-fixture.so')
        (libs / 'libfixture.so').symlink_to(record['path'])
        result = self.verify(self.output, policy=policy)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(any(error['path'] == '/bin/app' and 'NEEDED library resolves to nonruntime data' in error['reason']
                            for error in json.loads(result.stdout)['errors']))

    def test_runtime_interpreter_cannot_resolve_to_policy_data(self):
        policy, record = self.go_data_fixture()
        (self.output / 'bin').mkdir()
        self.make_elf(self.output, 'bin/app')
        (self.output / 'lib64').mkdir()
        libs = self.output / 'usr/lib/x86_64-linux-gnu'
        libs.mkdir(parents=True)
        shutil.copy2(self.base / 'libfixture.so', libs / 'libfixture.so')
        (self.output / 'lib64/ld-fixture.so').symlink_to(record['path'])
        result = self.verify(self.output, policy=policy)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(any(error['path'] == '/bin/app' and 'interpreter resolves to nonruntime data' in error['reason']
                            for error in json.loads(result.stdout)['errors']))

    def test_go_data_policy_rejects_stale_unowned_or_non_elf_entries(self):
        policy, record = self.go_data_fixture()
        target = self.output / record['path'].lstrip('/')
        original = target.read_bytes()
        manifest_path = self.output / 'usr/share/xtos/userspace-manifest.json'
        manifest_text = manifest_path.read_text()
        for case in ('mutated', 'unowned', 'inventory-hash', 'missing', 'non-elf', 'symlink'):
            with self.subTest(case=case):
                target.unlink(missing_ok=True)
                target.write_bytes(original)
                manifest_path.write_text(manifest_text)
                declared = dict(record)
                if case == 'mutated':
                    target.write_bytes(original + b'changed')
                elif case in ('unowned', 'inventory-hash'):
                    manifest = json.loads(manifest_text)
                    if case == 'unowned':
                        manifest['packages'][0]['package']['name'] = 'other'
                    else:
                        for entry in manifest['packages'][0]['files']:
                            if entry['path'] == record['path']:
                                entry['sha256'] = '0' * 64
                    manifest_path.write_text(json.dumps(manifest))
                elif case == 'missing':
                    target.unlink()
                elif case == 'non-elf':
                    target.write_bytes(b'plain data')
                    declared['sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
                    manifest = json.loads(manifest_text)
                    for entry in manifest['packages'][0]['files']:
                        if entry['path'] == record['path']:
                            entry['sha256'] = declared['sha256']
                    manifest_path.write_text(json.dumps(manifest))
                else:
                    target.unlink()
                    (target.parent / 'real-object').write_bytes(original)
                    target.symlink_to('real-object')
                policy.write_text(json.dumps({'schema_version': 1, 'architecture': 'x86_64',
                                              'data_files': [declared]}))
                result = self.verify(self.output, policy=policy)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('ELF data policy', result.stdout)

    def test_go_data_policy_validates_exact_schema_and_paths(self):
        policy, record = self.go_data_fixture()
        valid = {'schema_version': 1, 'architecture': 'x86_64', 'data_files': [record]}
        cases = [
            {**valid, 'schema_version': True},
            {**valid, 'architecture': 'arm64'},
            {**valid, 'extra': True},
            {**valid, 'data_files': [record, record]},
            {**valid, 'data_files': 'bad'},
            [],
        ]
        for field, value in [('path', '/usr/bin/foo'),
                             ('path', record['path'].replace('/testdata/', '/testdata/../testdata/')),
                             ('path', record['path'].replace('/testdata/', '//testdata/')),
                             ('path', record['path'].replace('foreign.syso', '*.syso')),
                             ('sha256', 'A' * 64), ('package', 'other'),
                             ('purpose', 'ignore-failures'), ('extra', True)]:
            cases.append({**valid, 'data_files': [{**record, field: value}]})
        for value in cases:
            with self.subTest(policy=value):
                policy.write_text(json.dumps(value))
                result = self.verify(self.output, policy=policy)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('ELF data policy', result.stdout)
                self.assertEqual(json.loads(result.stdout)['elf_data_files'], [])

    def make_elf(self, root, name='app'):
        source = self.base / 'fixture.c'
        source.write_text('int fixture(void) { return 1; }\n')
        library = self.base / 'libfixture.so'
        subprocess.run(['gcc', '-nostdlib', '-shared', '-fPIC', '-Wl,-soname,libfixture.so',
                        '-o', str(library), str(source)], check=True, capture_output=True)
        source.write_text('extern int fixture(void); void _start(void) { (void)fixture(); }\n')
        binary = root / name
        result = subprocess.run(['gcc', '-nostdlib', '-no-pie', '-o', str(binary), str(source),
                                 '-L' + str(self.base), '-lfixture',
                                 '-Wl,--dynamic-linker=/lib64/ld-fixture.so'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return binary

    def test_verifier_reports_missing_interpreter_and_library_without_host_lookup(self):
        self.write_lock([self.package(files={'bin/app': b'placeholder'})])
        self.assertEqual(self.build().returncode, 0)
        binary = self.make_elf(self.output, name='bin/app')
        result = self.verify(self.output)
        report = json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report['status'], 'fail')
        self.assertEqual(report['verification_level'], 'ELF closure paths and interpreter; no ABI symbol-version proof')
        reasons = ' '.join(error['reason'] for error in report['errors'])
        self.assertIn('interpreter', reasons)
        self.assertIn('missing', reasons)

    def test_verifier_rejects_wrong_elf_machine(self):
        self.write_lock([self.package(files={'bin/app': b'placeholder'})])
        self.assertEqual(self.build().returncode, 0)
        binary = self.make_elf(self.output, name='bin/app')
        data = bytearray(binary.read_bytes()); struct.pack_into('<H', data, 18, 183); binary.write_bytes(data)
        result = self.verify(self.output)
        report = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(any('wrong ELF machine' in item['reason'] for item in report['errors']))

    def test_verifier_uses_target_libraries_and_accepts_shared_root_symlink(self):
        self.write_lock([self.package(files={'bin/app': b'placeholder'})])
        self.assertEqual(self.build().returncode, 0)
        binary = self.make_elf(self.output, name='bin/app')
        (self.output / 'lib64').mkdir()
        (self.output / 'usr/lib/x86_64-linux-gnu').mkdir(parents=True)
        shutil.copy2(self.base / 'libfixture.so', self.output / 'usr/lib/x86_64-linux-gnu/libfixture.so')
        (self.output / 'lib64/ld-fixture.so').symlink_to('/usr/lib/x86_64-linux-gnu/libfixture.so')
        self.output.joinpath('lib').symlink_to('usr/lib')
        result = self.verify(self.output)
        report = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(report['status'], 'pass')
        self.assertEqual(report['elf_count'], 2)

    def test_verifier_report_file_is_written(self):
        self.write_lock([self.package()])
        self.assertEqual(self.build().returncode, 0)
        report_path = self.base / 'verify.json'
        result = subprocess.run(['python3', str(VERIFY), '--rootfs', str(self.output),
                                 '--report', str(report_path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(report_path.read_text()), json.loads(result.stdout))

    def test_fresh_userspace_build_subdirectory_is_allowed(self):
        self.write_lock([self.package()])
        import uuid
        self.output = REPO / '.build/userspace' / ('fixture-' + uuid.uuid4().hex)
        self.assertTrue(self.output.parent.is_dir())
        try:
            result = self.build('--verify')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((self.output / 'usr/share/xtos/userspace-manifest.json').is_file())
        finally:
            if self.output.exists():
                shutil.rmtree(self.output)

    def test_verify_build_does_not_publish_incomplete_elf_root(self):
        scratch_root = self.base / 'elf'
        scratch_root.mkdir()
        binary = self.make_elf(scratch_root)
        self.write_lock([self.package(files={'bin/app': binary.read_bytes()})])
        result = self.build('--verify')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.base.glob('.rootfs-stage-*')), [])

    def test_verifier_never_uses_host_libc(self):
        self.write_lock([self.package(files={'bin/app': b'placeholder'})])
        self.assertEqual(self.build().returncode, 0)
        source = self.base / 'host-libc.c'
        source.write_text('int main(void) { return 0; }\n')
        subprocess.run(['gcc', '-o', str(self.output / 'bin/app'), str(source)],
                       check=True, capture_output=True)
        result = self.verify(self.output)
        report = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(any('missing NEEDED library in target rootfs: libc.so.6' in error['reason']
                            for error in report['errors']))

    def test_origin_runpath_closure_and_host_rpath_rejection(self):
        self.write_lock([self.package(files={'bin/app': b'placeholder'})])
        self.assertEqual(self.build().returncode, 0)
        binary = self.make_elf(self.output, 'bin/app')
        (self.output / 'lib64').mkdir()
        shutil.copy2(self.base / 'libfixture.so', self.output / 'lib64/ld-fixture.so')
        (self.output / 'bin/private').mkdir()
        shutil.copy2(self.base / 'libfixture.so', self.output / 'bin/private/libfixture.so')
        source = self.base / 'fixture.c'
        command = ['gcc', '-nostdlib', '-no-pie', '-o', str(binary), str(source),
                   '-L' + str(self.base), '-lfixture', '-Wl,--dynamic-linker=/lib64/ld-fixture.so']
        subprocess.run(command + ['-Wl,-rpath,$ORIGIN/private'], check=True, capture_output=True)
        result = self.verify(self.output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        subprocess.run(command + ['-Wl,-rpath,' + str(self.base)], check=True, capture_output=True)
        result = self.verify(self.output)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(any('RPATH' in error['reason'] for error in json.loads(result.stdout)['errors']))

    def test_package_cannot_redirect_generated_manifest_through_symlink(self):
        host_like = self.base / 'external'
        host_like.mkdir()
        self.write_lock([self.package(files={'usr/share/xtos': (str(host_like),)})])
        result = self.build()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertFalse((host_like / 'userspace-manifest.json').exists())

    def test_archive_traversal_special_files_and_symlink_parents_are_rejected(self):
        def regular(name):
            info = tarfile.TarInfo(name); info.size = 4
            return info, b'evil'
        link = tarfile.TarInfo('usr/share/escape'); link.type = tarfile.SYMTYPE
        link.linkname = str(self.base)
        device = tarfile.TarInfo('dev/evil'); device.type = tarfile.CHRTYPE
        hard = tarfile.TarInfo('usr/share/hard'); hard.type = tarfile.LNKTYPE
        hard.linkname = '../outside'
        escape = tarfile.TarInfo('usr/share/link'); escape.type = tarfile.SYMTYPE
        escape.linkname = '../../../outside'
        cases = [[regular('../outside')], [regular('/absolute')], [(device, None)],
                 [(hard, None)], [(escape, None)],
                 [(link, None), regular('usr/share/escape/marker')]]
        for index, members in enumerate(cases):
            with self.subTest(case=index):
                self.write_lock([self.malicious_package(members)])
                self.output = self.base / f'unsafe-{index}'
                result = self.build()
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(self.output.exists())
                self.assertFalse((self.base / 'marker').exists())
                self.assertFalse((self.base / 'outside').exists())
                self.assertEqual(list(self.base.glob('.' + self.output.name + '-stage-*')), [])

    def test_conflicting_packages_rejected_identical_files_allowed(self):
        first = self.package('one', files={'usr/share/shared': b'same'})
        second = self.package('two', files={'usr/share/shared': b'different'})
        self.write_lock([first, second])
        result = self.build()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        third = self.package('three', files={'usr/share/shared': b'same'})
        self.write_lock([first, third])
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.output / 'usr/share/shared').read_bytes(), b'same')

    def test_absolute_target_symlink_is_preserved_without_host_access(self):
        self.write_lock([self.package(files={'usr/share/local': b'content',
                                             'usr/share/link': ('/usr/share/local',)})])
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.readlink(self.output / 'usr/share/link'), '/usr/share/local')

    def test_lock_preflight_rejects_invalid_inputs_before_creation(self):
        valid = self.package()
        cases = [
            ('sha256', {'sha256': '0' * 64}, 'x86_64'),
            ('size', {'size': valid['size'] + 1}, 'x86_64'),
            ('architecture', {}, 'arm64'),
            ('missing', {'filename': 'missing.deb'}, 'x86_64'),
            ('filename', {'filename': '../escape.deb'}, 'x86_64'),
            ('metadata', {'version': '2.0'}, 'x86_64'),
            ('digest-format', {'sha256': 'A' * 64}, 'x86_64'),
            ('size-type', {'size': True}, 'x86_64'),
        ]
        for label, changes, arch in cases:
            with self.subTest(case=label):
                record = {**valid, **changes}
                self.write_lock([record], arch)
                self.output = self.base / label
                result = self.build()
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(os.path.lexists(self.output))
                self.assertEqual(list(self.base.glob('.' + label + '-stage-*')), [])
        for records in ([valid, valid], []):
            self.write_lock(records)
            result = self.build()
            self.assertNotEqual(result.returncode, 0)

    def test_existing_output_is_untouched(self):
        self.write_lock([self.package()])
        self.output.mkdir()
        sentinel = self.output / 'sentinel'
        sentinel.write_text('keep')
        result = self.build()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sentinel.read_text(), 'keep')
        self.assertEqual(list(self.output.iterdir()), [sentinel])

    def test_source_tree_output_is_rejected(self):
        self.write_lock([self.package()])
        self.output = REPO / 'scripts/xtos-userspace-test-forbidden'
        result = self.build()
        try:
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(self.output.exists())
        finally:
            if self.output.exists():
                shutil.rmtree(self.output)

    def test_real_deb_extracts_copyright_with_deterministic_manifest(self):
        record = self.package()
        self.write_lock([record])
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.stat().st_mode & 0o7777, 0o755)
        self.assertEqual((self.output / 'usr/share/doc/tiny/copyright').read_bytes(), b'Test license\n')
        manifest_path = 'usr/share/xtos/userspace-manifest.json'
        manifest = json.loads((self.output / manifest_path).read_text())
        self.assertEqual(manifest['schema_version'], 1)
        self.assertEqual(manifest['architecture'], 'x86_64')
        self.assertEqual(manifest['lock_sha256'], hashlib.sha256(self.lock.read_bytes()).hexdigest())
        self.assertEqual(manifest['packages'][0]['package'], record)
        inventory = {entry['path']: entry for entry in manifest['packages'][0]['files']}
        self.assertEqual(inventory['/usr/share/doc/tiny/copyright']['sha256'],
                         hashlib.sha256(b'Test license\n').hexdigest())
        first = (self.output / manifest_path).read_bytes()
        self.output = self.base / 'second-rootfs'
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.output / manifest_path).read_bytes(), first)


if __name__ == '__main__':
    unittest.main()
