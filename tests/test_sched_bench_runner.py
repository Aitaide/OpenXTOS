"""Focused fixture tests for the scheduler benchmark runner (never real-time)."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import textwrap
import time
import unittest
from argparse import Namespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / 'tools/xtos-sched-bench/run_trial.py'
SPEC = importlib.util.spec_from_file_location('xtos_sched_runner', MODULE)
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)

SCRATCH = Path(os.environ.get('TMPDIR', str(Path.home() / '.hermes/cache/scratch')))
SCRATCH.mkdir(parents=True, exist_ok=True)


def make_args(directory, **overrides):
    cpu = min(os.sched_getaffinity(0))
    data = {
        'policy': 'other', 'trace': Path(directory) / 'trace.jsonl',
        'period_ns': 10_000_000, 'deadline_ns': 8_000_000,
        'work_ns': 1_000_000, 'runtime_ns': None, 'duration_s': 1,
        'cpu': cpu, 'warmup_jobs': 0, 'priority': 0,
        'binary': Path(directory) / 'fixture-periodic',
        'deadline_unpinned': False,
    }
    data.update(overrides)
    return Namespace(**data)


def write_script(path, body):
    path.write_text('#!/usr/bin/env python3\n' + textwrap.dedent(body), encoding='utf-8')
    path.chmod(0o755)
    return path


class SchedulerBenchmarkRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiled_dir = tempfile.TemporaryDirectory(prefix='sched-real-binary-', dir=SCRATCH)
        cls.compiled = Path(cls.compiled_dir.name) / 'periodic'
        subprocess.run(['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                        str(MODULE.with_name('periodic.c')), '-o', str(cls.compiled)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.compiled_dir.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sched-runner-', dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def fixture_binary(self, args, body='import pathlib, sys\npathlib.Path(sys.argv[1]).write_text("fixture trace\\n")\n'):
        return write_script(args.binary, body)

    def test_deadline_diagnostic_passes_minus_one_without_taskset(self):
        config = make_args(self.directory, policy='deadline', deadline_unpinned=True)
        runtime, priority = RUNNER.validate_args(config, os.sched_getaffinity(0))
        command = RUNNER.schedule_command(config, config.binary, runtime, priority)
        self.assertNotIn('taskset', command)
        self.assertEqual(command[-2], '-1')

    def test_default_deadline_keeps_selected_cpu_and_pinned_mode(self):
        config = make_args(self.directory, policy='deadline')
        runtime, priority = RUNNER.validate_args(config, {config.cpu})
        command = RUNNER.schedule_command(config, config.binary, runtime, priority)
        self.assertEqual(command[:3], ['taskset', '--cpu-list', str(config.cpu)])
        self.assertEqual(command[-2], str(config.cpu))

    def test_non_deadline_cannot_request_inherited_mask_diagnostic(self):
        config = make_args(self.directory, deadline_unpinned=True)
        with self.assertRaisesRegex(ValueError, 'deadline'):
            RUNNER.validate_args(config, {config.cpu})

    def test_rejects_boolean_or_nonpositive_timing_values(self):
        for field, value in (('period_ns', True), ('deadline_ns', 0),
                             ('work_ns', -1), ('duration_s', False),
                             ('runtime_ns', True)):
            with self.subTest(field=field, value=value):
                config = make_args(self.directory, **{field: value})
                with self.assertRaises(ValueError):
                    RUNNER.validate_args(config, {config.cpu})

    def test_rejects_invalid_task_contract_and_duration_limits(self):
        cases = (
            {'work_ns': 9_000_000},
            {'deadline_ns': 11_000_000},
            {'runtime_ns': 9_000_000},
            {'duration_s': 3601},
            {'period_ns': 1_000_000_001},
        )
        for override in cases:
            with self.subTest(override=override):
                config = make_args(self.directory, **override)
                with self.assertRaises(ValueError):
                    RUNNER.validate_args(config, {config.cpu})

    def test_bounds_ceil_job_count_plus_capacity_and_warmup(self):
        too_many = make_args(self.directory, duration_s=1,
                             period_ns=1, deadline_ns=1, work_ns=1,
                             runtime_ns=1, warmup_jobs=0)
        with self.assertRaisesRegex(ValueError, 'job count'):
            RUNNER.validate_args(too_many, {too_many.cpu})
        too_much_warmup = make_args(self.directory, warmup_jobs=100)
        with self.assertRaisesRegex(ValueError, 'warmup'):
            RUNNER.validate_args(too_much_warmup, {too_much_warmup.cpu})

    def test_rejects_invalid_priority_and_cpu_before_launch(self):
        config = make_args(self.directory, policy='fifo', priority=0)
        with self.assertRaisesRegex(ValueError, 'priority'):
            RUNNER.validate_args(config, {config.cpu})
        config = make_args(self.directory, cpu=-1)
        with self.assertRaisesRegex(ValueError, 'CPU'):
            RUNNER.validate_args(config, {0, 1, 2})

    def test_refuses_existing_trace_or_manifest_before_starting_competitor(self):
        for existing in ('trace', 'manifest'):
            config = make_args(self.directory, policy='deadline', deadline_unpinned=True)
            self.fixture_binary(config)
            target = config.trace if existing == 'trace' else RUNNER.manifest_path(config.trace)
            target.write_text('existing artifact', encoding='utf-8')
            with self.subTest(existing=existing), mock.patch.object(
                    RUNNER.subprocess, 'Popen', side_effect=AssertionError('spawned')):
                with self.assertRaisesRegex(ValueError, 'exists'):
                    RUNNER.run(config)

    def test_real_other_workload_writes_truthful_provenance(self):
        config = make_args(self.directory, binary=self.compiled)
        RUNNER.run(config)
        manifest = json.loads(RUNNER.manifest_path(config.trace).read_text(encoding='utf-8'))
        self.assertEqual(manifest['affinity_mode'], 'pinned')
        self.assertTrue(manifest['comparable'])
        self.assertEqual(manifest['runner_args']['effective_cpu_argument'], config.cpu)
        self.assertEqual(manifest['runner_args']['competitor_cpu'], config.cpu)
        self.assertEqual(manifest['runner_args']['runtime_ns'], 1_200_000)
        self.assertEqual(manifest['cpu_affinity'], [config.cpu])
        self.assertEqual(manifest['competitor_cpu_affinity'], [config.cpu])
        self.assertIsInstance(manifest['host_is_virtualized'], (bool, type(None)))
        self.assertIn('realtime_sysfs', manifest)
        self.assertIn('dirty_files', manifest)
        self.assertIn('runner_commit', manifest)
        for key in ('runner', 'burner', 'periodic.c', 'binary'):
            self.assertEqual(len(manifest['source_sha256'][key]), 64)
        self.assertEqual(manifest['raw_trace_sha256'], RUNNER.sha256_file(config.trace))

    def test_fixture_workload_failure_never_creates_success_manifest(self):
        config = make_args(self.directory, policy='deadline', deadline_unpinned=True)
        self.fixture_binary(config, 'import sys\nsys.exit(9)\n')
        with self.assertRaisesRegex(RuntimeError, 'exited 9'):
            RUNNER.run(config)
        self.assertFalse(config.trace.exists())
        self.assertFalse(RUNNER.manifest_path(config.trace).exists())

    def test_fixture_timeout_cleans_workload_process_group_and_competitor(self):
        config = make_args(self.directory, policy='deadline', deadline_unpinned=True)
        self.fixture_binary(config, 'import time\ntime.sleep(30)\n')
        workload_timeout = getattr(RUNNER, 'WORKLOAD_TIMEOUT_MARGIN_S', None)
        self.assertIsNotNone(workload_timeout)
        with mock.patch.object(RUNNER, 'WORKLOAD_TIMEOUT_MARGIN_S', 0.1):
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                RUNNER.run(config)
        self.assertFalse(RUNNER.manifest_path(config.trace).exists())

    def _assert_startup_signal_cleanup(self, target):
        # Delegate to real Popen, then signal before the caller gets its handle.
        # Each target creates a real descendant before that exact boundary.
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(target=target, signal=signum):
                case = self.directory / f'{target}-{signum}'
                case.mkdir()
                config = make_args(case, policy='deadline', deadline_unpinned=True)
                marker = case / 'descendant.pid'
                script = f'''
                    import pathlib, subprocess, sys, time
                    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
                    pending = pathlib.Path({str(marker)!r} + '.pending')
                    pending.write_text(str(child.pid))
                    pending.replace({str(marker)!r})
                    time.sleep(30)
                '''
                self.fixture_binary(config, script if target == 'workload' else 'import time; time.sleep(30)\n')
                burner = (write_script(case / 'burner.py', script)
                          if target == 'burner' else RUNNER.BURNER_PATH)
                real_popen = subprocess.Popen
                children = []
                delivered = []
                signal_sent_at = []

                def spawn(command, **kwargs):
                    process = real_popen(command, **kwargs)
                    if kwargs.get('start_new_session'):
                        children.append(process)
                        expected = str(burner if target == 'burner' else config.binary)
                        if expected in command:
                            deadline = time.monotonic() + 10
                            while not marker.exists() and time.monotonic() < deadline:
                                time.sleep(0.005)
                            self.assertTrue(marker.exists(), 'target did not create descendant')
                            delivered.append(process.pid)
                            signal_sent_at.append(time.monotonic())
                            os.kill(os.getpid(), signum)
                    return process

                def interrupt(received, frame):
                    raise InterruptedError(f'startup signal {received}')

                def alive(pid):
                    try:
                        stat = Path(f'/proc/{pid}/stat').read_text()
                    except FileNotFoundError:
                        return False
                    return stat[stat.rfind(')') + 2:].split()[0] != 'Z'

                previous = signal.signal(signum, interrupt)
                original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
                started = time.monotonic()
                try:
                    with mock.patch.object(RUNNER, 'BURNER_PATH', burner), \
                         mock.patch.object(RUNNER.subprocess, 'Popen', side_effect=spawn):
                        with self.assertRaisesRegex(InterruptedError, f'startup signal {signum}'):
                            RUNNER.run(config)
                    self.assertEqual(len(delivered), 1)
                    self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, set()), original_mask)
                    self.assertLess(time.monotonic() - signal_sent_at[0], 3, 'startup cleanup exceeded bound')
                    self.assertFalse(RUNNER.manifest_path(config.trace).exists())
                    pids = [p.pid for p in children] + [int(marker.read_text())]
                    deadline = time.monotonic() + 0.5
                    while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
                        time.sleep(0.005)
                    self.assertEqual([pid for pid in pids if alive(pid)], [],
                                     'real child or descendant survived runner unwind')
                    self.assertTrue(all(p.poll() == -signal.SIGTERM for p in children),
                                    'ordinary SIGTERM cleanup must work without SIGKILL fallback')
                finally:
                    signal.signal(signum, previous)
                    signal.pthread_sigmask(signal.SIG_SETMASK, original_mask)
                    for process in children:
                        RUNNER._kill_process_group(process)

    def test_startup_signal_cleans_real_competitor_and_descendant(self):
        self._assert_startup_signal_cleanup('burner')

    def test_startup_signal_cleans_real_workload_and_descendant(self):
        self._assert_startup_signal_cleanup('workload')

    def test_fixture_readiness_failure_kills_competitor_process_group(self):
        config = make_args(self.directory, policy='deadline', deadline_unpinned=True)
        self.fixture_binary(config)
        child_pid_file = self.directory / 'descendant.pid'
        fake_burner = write_script(self.directory / 'fixture-burner.py', f'''
            import os, subprocess, sys, time
            marker = {str(child_pid_file)!r}
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
            with open(marker, 'w', encoding='utf-8') as stream:
                stream.write(str(child.pid))
            time.sleep(30)
        ''')
        with mock.patch.object(RUNNER, 'BURNER_PATH', fake_burner), \
             mock.patch.object(RUNNER, 'BURNER_READY_TIMEOUT_S', 2.0):
            with self.assertRaisesRegex(RuntimeError, 'readiness'):
                RUNNER.run(config)
        self.assertTrue(child_pid_file.exists())
        child_pid = int(child_pid_file.read_text(encoding='utf-8'))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            stat_path = Path(f'/proc/{child_pid}/stat')
            if not stat_path.exists():
                break
            stat = stat_path.read_text(encoding='utf-8')
            state = stat[stat.rfind(')') + 2:stat.rfind(')') + 3]
            if state == 'Z':
                break
            time.sleep(0.02)
        else:
            self.fail(f'competitor descendant {child_pid} remained alive')
        self.assertFalse(config.trace.exists())
        self.assertFalse(RUNNER.manifest_path(config.trace).exists())


if __name__ == '__main__':
    unittest.main()
