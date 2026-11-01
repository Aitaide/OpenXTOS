"""Offline source admission tests, isolated from the moving canonical kernel."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SourceGateTests(unittest.TestCase):
    def setUp(self):
        base = os.environ.get('SOURCE_GATE_FIXTURES')
        if base:
            Path(base).mkdir(parents=True, exist_ok=True)
            self.repo = Path(base) / self._testMethodName
            self.repo.mkdir()
        else:
            self.temp = tempfile.TemporaryDirectory()
            self.addCleanup(self.temp.cleanup)
            self.repo = Path(self.temp.name)
        (self.repo / 'scripts').mkdir()
        for name in ('prepare_linux.sh', 'verify_source_tree.sh', 'kernel_source.py'):
            src = ROOT / 'scripts' / name
            if src.exists():
                shutil.copyfile(src, self.repo / 'scripts' / name)
        self.pin = json.loads((ROOT / 'kernel/source-pin.json').read_bytes())
        self.source = self.repo / 'kernel/linux'
        self.source.mkdir(parents=True)
        self.put('Makefile', 'VERSION = 7\nPATCHLEVEL = 3\nSUBLEVEL = 1\nEXTRAVERSION =\n')
        self.put('kernel/Kconfig.preempt', 'config PREEMPT_RT\n\tbool "Fully Preemptible Kernel (Real-Time)"\n\tdepends on EXPERT && ARCH_SUPPORTS_RT && !COMPILE_TEST\n\tselect PREEMPTION\n')
        self.put('arch/x86/Kconfig', 'config X86\n\tdef_bool y\n\tselect ARCH_SUPPORTS_RT\n')
        for rel in ('kernel/sched/xtos_sched.c', 'kernel/sched/xtos/Kconfig', 'drivers/xtos/bci/xtos_bci_core.c', 'include/linux/xtos.h'):
            self.put(rel, '/* XTOS minimal admission fixture, not a kernel build */\n')
        patch = self.repo / self.pin['xtos_patch']
        patch.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / self.pin['xtos_patch'], patch)
        shutil.copyfile(ROOT / 'kernel/xtos_defconfig', self.repo / 'kernel/xtos_defconfig')
        self.save_pin()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (self.repo / '.gitignore').write_text('.build/\n')

    def put(self, rel, value):
        path = self.source / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def save_pin(self):
        (self.repo / 'kernel/source-pin.json').write_text(json.dumps(self.pin))

    def gate(self, script, **env):
        command = ['bash', str(self.repo / 'scripts' / script)]
        result = subprocess.run(command, cwd=self.repo,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1', **env),
                                capture_output=True, text=True, timeout=20)
        if os.environ.get('SOURCE_GATE_FIXTURES'):
            with (self.repo / '.build-executions.jsonl').open('a') as f:
                f.write(json.dumps({'command': command, 'env': env,
                                    'pythonoptimize': os.environ.get('PYTHONOPTIMIZE'),
                                    'returncode': result.returncode,
                                    'stdout': result.stdout, 'stderr': result.stderr}) + '\n')
        return result

    def assertBoth(self, accepted, **env):
        for script in ('prepare_linux.sh', 'verify_source_tree.sh'):
            with self.subTest(script=script):
                p = self.gate(script, **env)
                self.assertEqual(p.returncode == 0, accepted, p.stdout + p.stderr)

    def test_pin_schema_rejects_invalid_values(self):
        original = dict(self.pin)
        for field, values in {
            'schema': ['bad'], 'version': ['7.3.1x', '6.6.0'],
            'archive_bytes': [True, 0, -1, 1.0, '162406920'],
            'archive_sha256': ['x' * 64, '0' * 63],
            'xtos_patch_sha256': ['x' * 64],
            'xtos_patch': ['../outside.patch', '/outside.patch', 'kernel/../outside.patch'],
            'rt_mode': ['external'], 'rt_patch': ['old.patch'],
            'rt_config': ['CONFIG_PREEMPT_RT=n'], 'source_signer_fingerprint': ['bad'],
            'upstream': ['https://example.org/linux.tar.xz'],
            'status': ['accepted'],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.pin = dict(original, **{field: value})
                    self.save_pin()
                    self.assertBoth(False)
        self.pin = dict(original, extra=True)
        self.save_pin()
        self.assertBoth(False)
        self.pin = dict(original)
        del self.pin['archive_sha256']
        self.save_pin()
        self.assertBoth(False)

    def test_pin_file_symlink_escape_rejected(self):
        path = self.repo / 'kernel/source-pin.json'
        outside = self.repo.parent / (self.repo.name + '-outside.json')
        shutil.copyfile(path, outside)
        self.addCleanup(outside.unlink)
        path.unlink()
        path.symlink_to(outside)
        self.assertBoth(False)

    def test_duplicate_pin_field_rejected(self):
        path = self.repo / 'kernel/source-pin.json'
        path.write_text(path.read_text().replace('{', '{"version":"7.3.1",', 1))
        self.assertBoth(False)

    def test_required_source_symlink_escape_rejected(self):
        path = self.source / 'include/linux/xtos.h'
        outside = self.repo.parent / (self.repo.name + '-outside.h')
        shutil.copyfile(path, outside)
        self.addCleanup(outside.unlink)
        path.unlink()
        path.symlink_to(outside)
        self.assertBoth(False)

    def test_missing_patch_rejected(self):
        (self.repo / self.pin['xtos_patch']).unlink()
        self.assertBoth(False)

    def test_missing_xtos_rejected(self):
        (self.source / 'include/linux/xtos.h').unlink()
        self.assertBoth(False)

    def test_wrong_version_rejected(self):
        self.put('Makefile', 'VERSION = 6\nPATCHLEVEL = 6\nSUBLEVEL = 0\nEXTRAVERSION =\n')
        self.assertBoth(False)

    def test_refresh_rejected(self):
        self.assertNotEqual(self.gate('prepare_linux.sh', REFRESH_BUNDLED_KERNEL='1').returncode, 0)

    def test_bad_rt_rejected(self):
        self.put('kernel/Kconfig.preempt', '# config PREEMPT_RT\n')
        self.assertBoth(False)

    def test_rt_dependency_required(self):
        self.put('kernel/Kconfig.preempt', 'config PREEMPT_RT\n\tbool "fake"\n')
        self.assertBoth(False)

    def test_bad_arch_rt_rejected(self):
        self.put('arch/x86/Kconfig', 'config X86\n\t# select ARCH_SUPPORTS_RT\n')
        self.assertBoth(False)

    def test_extra_version_rejected(self):
        self.put('Makefile', 'VERSION = 7\nPATCHLEVEL = 3\nSUBLEVEL = 1\nEXTRAVERSION = -rc1\n')
        self.assertBoth(False)

    def test_patch_symlink_escape_rejected(self):
        patch = self.repo / self.pin['xtos_patch']
        outside = self.repo.parent / (self.repo.name + '-outside.patch')
        shutil.copyfile(patch, outside)
        self.addCleanup(outside.unlink)
        patch.unlink()
        patch.symlink_to(outside)
        self.assertBoth(False)

    def test_kernel_dir_override(self):
        selected = self.repo / '.build/selected'
        selected.parent.mkdir()
        self.source.rename(selected)
        self.source.mkdir()
        self.put('Makefile', 'VERSION = 6\nPATCHLEVEL = 6\nSUBLEVEL = 0\n')
        self.assertBoth(True, KERNEL_DIR=str(selected))
        (selected / 'leaked.o').write_text('generated')
        p = self.gate('verify_source_tree.sh', KERNEL_DIR=str(selected))
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_generated_tracked_untracked_and_ignored_kernel_rejected(self):
        for rel, tracked in [('leak.o', True), ('leak.zip', False), ('kernel/linux/ignored/Module.symvers', False)]:
            with self.subTest(rel=rel):
                path = self.repo / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('generated')
                if tracked:
                    subprocess.run(['git', '-C', str(self.repo), 'add', rel], check=True)
                else:
                    with (self.repo / '.gitignore').open('a') as f:
                        if 'kernel/linux' in rel:
                            f.write(rel + '\n')
                p = self.gate('verify_source_tree.sh')
                self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
                path.unlink()
                if tracked:
                    subprocess.run(['git', '-C', str(self.repo), 'rm', '--cached', rel], check=True, capture_output=True)

    def test_vendor_log_exact_bytes_only(self):
        self.put('tools/testing/kunit/test_data/test_insufficient_memory.log', '')
        self.assertBoth(True)
        self.put('tools/testing/kunit/test_data/test_insufficient_memory.log', 'replaced build output')
        self.assertNotEqual(self.gate('verify_source_tree.sh').returncode, 0)

    def test_patch_corruption_rejected(self):
        with (self.repo / self.pin['xtos_patch']).open('ab') as f:
            f.write(b'corrupt\n')
        self.assertBoth(False)

    def test_accept_pinned_mainline_rt_without_fake_rt_marker(self):
        self.assertBoth(True)


if __name__ == '__main__':
    unittest.main()
