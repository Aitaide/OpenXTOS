"""Native OpenXR ABI tests; every invocation reserves fresh evidence."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
class NativeXRTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = Path(os.environ.get('XTOS_XR_TEST_OUTPUT', ROOT / '.build/p6-xr-next/native-tests'))
        base.mkdir(parents=True, exist_ok=True)
        cls.out = Path(tempfile.mkdtemp(prefix='run-', dir=base))
        cls.env = dict(os.environ, LC_ALL='C')
        flags = ['-std=c11', '-Wall', '-Wextra', '-Werror', '-g', '-D_POSIX_C_SOURCE=200809L', '-DXR_NO_PROTOTYPES', '-DXR_USE_TIMESPEC']
        if os.environ.get('XTOS_XR_SANITIZE') == '1':
            flags += ['-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-fno-omit-frame-pointer', '-no-pie']
            cls.env.update(ASAN_OPTIONS='detect_leaks=1:halt_on_error=1', UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1')
        inc = ['-I'+str(ROOT/'tools/xtos-xr/include'), '-I'+str(ROOT/'tools/xtos-xr/vendor/openxr/include')]
        src = ROOT/'tools/xtos-xr'
        cls.run_build(['cc', *flags, *inc, '-shared', '-fPIC', str(src/'tests/native/fixture_runtime.c'), '-o', str(cls.out/'simulated.so')])
        cls.run_build(['cc', *flags, *inc, '-shared', '-fPIC', '-DFIXTURE_NO_DESTROY_EXPORT', str(src/'tests/native/fixture_runtime.c'), '-o', str(cls.out/'missing-destroy.so')])
        cls.run_build(['cc', *flags, *inc, str(src/'src/runtime_adapter.c'), str(src/'tests/native/test_adapter.c'), '-ldl', '-o', str(cls.out/'test_adapter')])
        cls.run_build(['cc', *flags, *inc, '-DXTOS_XR_TEST_GENERATION_START=UINT64_MAX-1', str(src/'src/runtime_adapter.c'), str(src/'tests/native/test_adapter.c'), '-ldl', '-o', str(cls.out/'generation_adapter')])
        if (src/'src/xr_probe.c').exists():
            cls.run_build(['cc', *flags, *inc, str(src/'src/runtime_adapter.c'), str(src/'src/xr_probe.c'), '-ldl', '-o', str(cls.out/'xr-probe')])
    @classmethod
    def run_build(cls, cmd):
        p = subprocess.run(cmd, env=cls.env, capture_output=True, text=True)
        with (cls.out/'build.log').open('a') as f: f.write(json.dumps(cmd)+'\n'+p.stdout+p.stderr+'\nexit='+str(p.returncode)+'\n')
        if p.returncode: raise AssertionError(p.stderr)
    def run_case(self, name):
        p = subprocess.run([str(self.out/'test_adapter'), name, str(self.out/'simulated.so')], env=self.env, capture_output=True, text=True)
        (self.out/(name+'.log')).write_text(p.stdout+p.stderr+'\nexit='+str(p.returncode)+'\n')
        self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
    def test_destroy_resolution_failure(self): self.run_case('destroy_failure')
    def test_destroy_error_is_observable_and_retry_retains_ownership(self):
        self.run_case('destroy_error_retry')
    def test_cli_never_reports_success_with_unresolved_destroy(self):
        p=subprocess.run([str(self.out/'xr-probe'),'--loader',str(self.out/'simulated.so'),'--simulated'],
            env=dict(self.env,XTOS_XR_FIXTURE_MODE='18'),capture_output=True,text=True)
        (self.out/'cli-destroy-error.log').write_text(p.stdout+p.stderr+'\nexit='+str(p.returncode)+'\n')
        self.assertNotEqual(p.returncode,0)
        self.assertEqual(json.loads(p.stdout)['status'],'cleanup_failed')
    def test_boundaries(self): self.run_case('boundaries')
    def test_counter_sequence(self): self.run_case('counter_sequence')
    def test_counter_barrier(self): self.run_case('counter_barrier')
    def test_counter_conversion(self): self.run_case('counter_conversion')
    def test_counter_generation(self):
        p = subprocess.run([str(self.out/'generation_adapter'), 'counter_generation', str(self.out/'simulated.so')], env=self.env, capture_output=True, text=True)
        (self.out/'counter_generation.log').write_text(p.stdout+p.stderr+'\nexit='+str(p.returncode)+'\n')
        self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
    def test_cli_boundaries(self):
        for mode in [11, 12]:
            args = [str(self.out/'xr-probe'), '--loader', str(self.out/'simulated.so'), '--simulated']
            if mode == 12: args += ['--xr-time', '9223372036854775807']
            p = subprocess.run(args, env=dict(self.env, XTOS_XR_FIXTURE_MODE=str(mode)), capture_output=True, text=True)
            (self.out/('cli-boundary-'+str(mode)+'.log')).write_text(p.stdout+p.stderr+'\nexit='+str(p.returncode)+'\n')
            self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
            report = json.loads(p.stdout)
            if mode == 11:
                self.assertEqual(len(report['events']), 256)
                self.assertIs(report['event_limit_reached'], True)
                self.assertEqual([e['sequence'] for e in report['events']], list(range(1, 257)))
                self.assertEqual(report['barrier'], 256)
            else:
                self.assertEqual(report['conversion']['raw_xr_time'], 9223372036854775807)
                self.assertEqual(report['conversion']['host_ns'], 9223372036854775807)
    def test_missing_destroy_export(self):
        p = subprocess.run([str(self.out/'test_adapter'), 'missing_destroy_export', str(self.out/'missing-destroy.so')], env=self.env, capture_output=True, text=True)
        (self.out/'missing_destroy_export.log').write_text(p.stdout+p.stderr+'\nexit='+str(p.returncode)+'\n')
        self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
    def test_cli_numeric_overflow(self):
        for value in ['9223372036854775808', '99999999999999999999999999999999999', '-1', '', '1x', '+1', '01', ' 1', '0']:
            with self.subTest(value=value):
                args = [str(self.out/'xr-probe'), '--loader', str(self.out/'simulated.so'), '--xr-time', value]
                p = subprocess.run(args, env=self.env, capture_output=True, text=True)
                with (self.out/'cli-numeric.log').open('a') as f: f.write(json.dumps(args)+'\n'+p.stdout+p.stderr+'exit='+str(p.returncode)+'\n')
                self.assertEqual(p.returncode, 2, p.stdout+p.stderr)
                self.assertEqual(json.loads(p.stdout)['status'], 'usage_error')
    def test_missing_loader(self): self.run_case('missing_loader')
    def test_loader_lifecycle(self): self.run_case('loader')
    def test_events(self): self.run_case('events')
    def test_clock(self): self.run_case('clock')
    def test_cli(self):
        exe = self.out/'xr-probe'
        self.assertTrue(exe.exists(), 'xr-probe missing')
        for args, expected in [([], 2), (['--loader', 'relative.so'], 2), (['--loader', '/no/xtos/loader.so'], 3), (['--loader', str(self.out/'simulated.so'), '--simulated'], 0)]:
            p = subprocess.run([str(exe), *args], env=self.env, capture_output=True, text=True)
            with (self.out/'cli.log').open('a') as f: f.write(json.dumps(args)+'\n'+p.stdout+p.stderr+'exit='+str(p.returncode)+'\n')
            self.assertEqual(p.returncode, expected, p.stdout+p.stderr)
            report = json.loads(p.stdout)
            self.assertEqual(report['safe_action'], 'no_action')
            self.assertIs(report['physical_acceptance'], False)
            if expected == 0:
                self.assertEqual(report['source_kind'], 'simulated_runtime')
                self.assertEqual(report['events'], [])


if __name__ == '__main__': unittest.main()
