"""Read-only RT evidence regressions; no real-time workloads are launched."""
from argparse import Namespace
from contextlib import ExitStack
import gzip
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'tools/xtos-sched-bench'


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'cannot load {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PREFLIGHT = load_module('kernel_sched_rt_preflight', TOOLS / 'preflight.py')
RUNNER = load_module('kernel_sched_rt_runner', TOOLS / 'run_trial.py')


class KernelSchedRtTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get('TMPDIR', '/root/.hermes/cache/scratch'))
        scratch.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix='kernel-sched-rt-', dir=scratch)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def write(self, relative, contents):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(contents, bytes):
            path.write_bytes(contents)
        else:
            path.write_text(contents, encoding='utf-8')
        return path

    def rt_evidence(self, config=b'CONFIG_PREEMPT_RT=y\n',
                    version='Linux version 7.3.1-fixture #1 SMP PREEMPT_RT fixture\n'):
        self.write('proc/config.gz', gzip.compress(config))
        self.write('proc/version', version)

    def test_missing_legacy_node_uses_same_root_config_and_version(self):
        self.rt_evidence()
        report = PREFLIGHT.Collector(self.root).collect()
        self.assertEqual(report['realtime']['status'], 'available')
        self.assertEqual(report['realtime']['value'], '1')
        self.assertIn('/proc/config.gz', report['realtime']['source'])
        self.assertIn('/proc/version', report['realtime']['source'])
        self.assertIsNone(report['realtime']['error'])
        self.assertEqual(report['collection_mode'], 'fixture_filesystem')
        self.assertEqual(report['uname'], {'release': 'unknown', 'machine': 'unknown'})

    def test_legacy_node_remains_authoritative(self):
        self.rt_evidence()
        for contents, status, value in [('0\n', 'available', '0'),
                                         ('1\n', 'available', '1'),
                                         ('yes\n', 'error', None),
                                         ('', 'error', None),
                                         ('1 0\n', 'error', None)]:
            with self.subTest(contents=contents):
                self.write('sys/kernel/realtime', contents)
                fact = PREFLIGHT.Collector(self.root)._collect_realtime()
                self.assertEqual((fact['status'], fact['value']), (status, value))
                self.assertEqual(fact['source'], '/sys/kernel/realtime')

    def test_exact_legacy_node_bytes(self):
        self.rt_evidence()
        for payload in (b'0', b'0\n', b'1', b'1\n'):
            with self.subTest(payload=payload):
                self.write('sys/kernel/realtime', payload)
                fact = PREFLIGHT.Collector(self.root)._collect_realtime()
                self.assertEqual((fact['status'], fact['value'], fact['error']),
                                 ('available', chr(payload[0]), None))
        for payload in (b' 1\n', b'1\n\n', b'\t1\t', b'1\r\n',
                        b'0 ', b'\n0', b'0\n\n', b'1\x00\n'):
            with self.subTest(payload=payload):
                self.write('sys/kernel/realtime', payload)
                fact = PREFLIGHT.Collector(self.root)._collect_realtime()
                self.assertEqual((fact['status'], fact['value'], fact['error']),
                                 ('error', None, 'malformed_realtime_value'))
                self.assertEqual(fact['source'], '/sys/kernel/realtime')

    def test_exact_legacy_runner_guard(self):
        self.rt_evidence()
        for payload in (b'1', b'1\n'):
            with self.subTest(payload=payload), self.runner_fixture():
                self.write('sys/kernel/realtime', payload)
                with self.assertRaisesRegex(ValueError, 'benchmark binary missing'):
                    RUNNER.run(self.args())
        for payload in (b' 1\n', b'1\n\n', b'\t1\t', b'1\r\n'):
            with self.subTest(payload=payload), self.runner_fixture():
                self.write('sys/kernel/realtime', payload)
                with self.assertRaisesRegex(ValueError, '--require-preempt-rt'):
                    RUNNER.run(self.args())
                self.assertFalse(self.args().trace.exists())
                self.assertFalse(RUNNER.manifest_path(self.args().trace).exists())

    def test_legacy_permission_error_does_not_fall_back(self):
        self.rt_evidence()
        self.write('sys/kernel/realtime', '1\n')
        collector = PREFLIGHT.Collector(self.root)
        original = collector._open_resolved
        def deny(path, flags):
            if path.name == 'realtime':
                raise PermissionError('fixture denial')
            return original(path, flags)
        with mock.patch.object(collector, '_open_resolved', side_effect=deny):
            fact = collector._collect_realtime()
        self.assertEqual(fact['status'], 'unknown')
        self.assertEqual(fact['error'], 'permission_denied')
        self.assertEqual(fact['source'], '/sys/kernel/realtime')

    def test_fallback_requires_both_evidence_files_and_full_version_token(self):
        self.rt_evidence()
        for version in ['', 'Linux 7.3.1-rt42 SMP PREEMPT_DYNAMIC',
                        'Linux version PREEMPT_RT_extra',
                        'Linux version XPREEMPT_RT',
                        'Linux version (PREEMPT_RT)']:
            with self.subTest(version=version):
                self.write('proc/version', version)
                self.assertEqual(PREFLIGHT.Collector(self.root)._collect_realtime()['status'],
                                 'unknown')
        (self.root / 'proc/version').unlink()
        self.assertEqual(PREFLIGHT.Collector(self.root)._collect_realtime()['status'], 'unknown')
        self.rt_evidence()
        (self.root / 'proc/config.gz').unlink()
        with mock.patch.object(PREFLIGHT.os, 'uname',
                               side_effect=AssertionError('fixture mixed host uname')):
            self.assertEqual(PREFLIGHT.Collector(self.root)._collect_realtime()['status'],
                             'unknown')

    def test_config_off_is_explicit_zero_and_inexact_or_conflicting_config_is_unknown(self):
        for config, expected in [(b'# CONFIG_PREEMPT_RT is not set\n', '0'),
                                 (b'CONFIG_PREEMPT_RT=n\n', '0'),
                                 (b'CONFIG_PREEMPT_RT=y_extra\n', None),
                                 (b'CONFIG_PREEMPT_RT=y\nCONFIG_PREEMPT_RT=n\n', None),
                                 (b'CONFIG_PREEMPT_RT_FULL=y\n', None),
                                 (b'', None)]:
            with self.subTest(config=config):
                self.rt_evidence(config=config)
                fact = PREFLIGHT.Collector(self.root)._collect_realtime()
                self.assertEqual(fact['value'], expected)
                self.assertEqual(fact['status'], 'available' if expected else 'unknown')

    def test_corrupt_truncated_and_oversized_gzip_never_confirm_rt(self):
        samples = [b'not gzip', gzip.compress(b'CONFIG_PREEMPT_RT=y\n')[:-5],
                   gzip.compress(b'CONFIG_PREEMPT_RT=y\n' +
                                 b'#' * PREFLIGHT.MAX_BINARY_BYTES),
                   b'X' * (PREFLIGHT.MAX_BINARY_BYTES + 1),
                   gzip.compress(b'CONFIG_PREEMPT_RT=y\n\xff')]
        self.rt_evidence()
        for data in samples:
            with self.subTest(size=len(data)):
                self.write('proc/config.gz', data)
                fact = PREFLIGHT.Collector(self.root)._collect_realtime()
                self.assertEqual(fact['status'], 'unknown')
                self.assertIsNone(fact['value'])

    def test_bounded_gzip_accepts_exact_decompressed_limit(self):
        header = b'CONFIG_PREEMPT_RT=y\n'
        self.rt_evidence(config=header + b'#' * (PREFLIGHT.MAX_BINARY_BYTES - len(header)))
        self.assertEqual(PREFLIGHT.Collector(self.root)._collect_realtime()['value'], '1')

    def test_fallback_symlink_escape_and_nonregular_file_are_rejected(self):
        self.rt_evidence()
        outside = self.root / 'outside'
        outside.mkdir()
        nested = self.root / 'selected'
        nested.mkdir()
        (nested / 'proc').mkdir()
        (outside / 'config.gz').write_bytes(gzip.compress(b'CONFIG_PREEMPT_RT=y\n'))
        (nested / 'proc/config.gz').symlink_to(outside / 'config.gz')
        (nested / 'proc/version').write_text('Linux version PREEMPT_RT\n')
        fact = PREFLIGHT.Collector(nested)._collect_realtime()
        self.assertEqual(fact['error'], 'unsafe_path')
        (nested / 'proc/config.gz').unlink()
        os.mkfifo(nested / 'proc/config.gz')
        fact = PREFLIGHT.Collector(nested)._collect_realtime()
        self.assertEqual(fact['error'], 'not_regular')

    def test_fallback_rejects_config_replaced_after_resolution_without_fd_leak(self):
        self.rt_evidence()
        collector = PREFLIGHT.Collector(self.root)
        original = collector._safe_path
        config = self.root / 'proc/config.gz'
        def replace(path):
            resolved = original(path)
            if path == '/proc/config.gz':
                config.unlink()
                config.symlink_to('/proc/config.gz')
            return resolved
        before = len(list(Path('/proc/self/fd').iterdir()))
        with mock.patch.object(collector, '_safe_path', side_effect=replace):
            fact = collector._collect_realtime()
        self.assertEqual(fact['status'], 'unknown')
        self.assertIsNone(fact['value'])
        self.assertEqual(len(list(Path('/proc/self/fd').iterdir())), before)

    def test_fallback_rejects_root_replacement_after_missing_node(self):
        selected = self.root / 'selected'
        selected.mkdir()
        collector = PREFLIGHT.Collector(selected)
        original = collector._read_text
        def replace(path, *args):
            result = original(path, *args)
            if path == '/sys/kernel/realtime':
                selected.rename(self.root / 'saved')
                selected.mkdir()
                (selected / 'proc').mkdir()
                (selected / 'proc/config.gz').write_bytes(gzip.compress(b'CONFIG_PREEMPT_RT=y\n'))
                (selected / 'proc/version').write_text('Linux version PREEMPT_RT\n')
            return result
        before = len(list(Path('/proc/self/fd').iterdir()))
        with mock.patch.object(collector, '_read_text', side_effect=replace):
            fact = collector._collect_realtime()
        self.assertEqual(fact['status'], 'unknown')
        self.assertEqual(fact['error'], 'read_error')
        self.assertEqual(len(list(Path('/proc/self/fd').iterdir())), before)

    def args(self):
        return Namespace(policy='other', trace=self.root / 'trace.jsonl',
                         period_ns=10_000_000, deadline_ns=8_000_000,
                         work_ns=1_000_000, runtime_ns=None, duration_s=1,
                         cpu=0, warmup_jobs=0, priority=0,
                         binary=self.root / 'missing-binary',
                         deadline_unpinned=False, require_preempt_rt=True)

    def runner_fixture(self):
        # Redirect only the runner's selected filesystem root/read seam;
        # real collector traversal and the complete run() guard remain active.
        def fixture_path(value):
            path = Path(value)
            if path == Path('/'):
                return self.root
            if str(path) in ('/sys/kernel/realtime', '/proc/config.gz', '/proc/version'):
                return self.root / str(path).lstrip('/')
            return path
        stack = ExitStack()
        stack.enter_context(mock.patch.object(RUNNER, 'Path', side_effect=fixture_path))
        stack.enter_context(mock.patch.object(RUNNER.os, 'sched_getaffinity', return_value={0}))
        stack.enter_context(mock.patch.object(RUNNER.subprocess, 'Popen',
                                             side_effect=AssertionError('unexpected process')))
        stack.enter_context(mock.patch.object(RUNNER.subprocess, 'run',
                                             side_effect=AssertionError('unexpected external command')))
        return stack

    def test_runner_rt_guard_accepts_corroborated_fixture_before_binary_validation(self):
        self.rt_evidence()
        with self.runner_fixture():
            with self.assertRaisesRegex(ValueError, 'benchmark binary missing'):
                RUNNER.run(self.args())
        self.assertFalse((self.root / 'trace.jsonl').exists())
    def test_runner_refuses_unconfirmed_rt_before_binary_or_process_access(self):
        for config, version, node in [
                (b'CONFIG_PREEMPT_RT=y\n', 'Linux PREEMPT_DYNAMIC', None),
                (b'CONFIG_PREEMPT_RT=n\n', 'Linux PREEMPT_RT', None),
                (b'CONFIG_PREEMPT_RT=y\n', 'Linux PREEMPT_RT', '0\n'),
                (b'CONFIG_PREEMPT_RT=y\n', 'Linux PREEMPT_RT', 'broken\n')]:
            with self.subTest(config=config, version=version, node=node):
                self.rt_evidence(config, version)
                legacy = self.root / 'sys/kernel/realtime'
                if legacy.exists():
                    legacy.unlink()
                if node is not None:
                    self.write('sys/kernel/realtime', node)
                with self.runner_fixture():
                    with self.assertRaisesRegex(ValueError, '--require-preempt-rt'):
                        RUNNER.run(self.args())
                self.assertFalse((self.root / 'trace.jsonl').exists())
                self.assertFalse(RUNNER.manifest_path(self.root / 'trace.jsonl').exists())

    def test_runner_refuses_legacy_permission_error_despite_positive_proc_evidence(self):
        self.rt_evidence()
        self.write('sys/kernel/realtime', '1\n')
        original = os.open
        def deny(path, *args, **kwargs):
            if str(path) == 'realtime':
                raise PermissionError('fixture denial')
            return original(path, *args, **kwargs)
        with self.runner_fixture(), mock.patch.object(os, 'open', side_effect=deny):
            with self.assertRaisesRegex(ValueError, '--require-preempt-rt'):
                RUNNER.run(self.args())
        self.assertFalse((self.root / 'trace.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
