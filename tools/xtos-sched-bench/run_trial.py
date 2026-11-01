#!/usr/bin/env python3
"""Run periodic CPU jobs against a same-CPU SCHED_OTHER competitor."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
BURNER_PATH = Path(__file__).with_name('burner.py')
PERIODIC_SOURCE = Path(__file__).with_name('periodic.c')
WORKLOAD_TIMEOUT_MARGIN_S = 5.0
WORKLOAD_READY_TIMEOUT_S = 2.0
SCHED_DEADLINE = getattr(os, 'SCHED_DEADLINE', 6)
BURNER_READY_TIMEOUT_S = 2.0
BURNER_PROGRESS_TIMEOUT_S = 1.0
PROCESS_TERM_TIMEOUT_S = 2.0


def manifest_path(trace):
    return Path(trace).with_suffix(Path(trace).suffix + '.manifest.json')


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f'{name} must be a positive integer')
    return value


def _nonnegative_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f'{name} must be a non-negative integer')
    return value


def expected_job_count(duration_s, period_ns):
    duration_s = _positive_int(duration_s, 'duration_s')
    period_ns = _positive_int(period_ns, 'period_ns')
    run_ns = duration_s * 1_000_000_000
    if period_ns > run_ns:
        raise ValueError('period_ns must not exceed duration_s * 1e9')
    return (run_ns + period_ns - 1) // period_ns


def validate_args(args, allowed_cpus):
    policy = getattr(args, 'policy', None)
    if policy not in ('other', 'fifo', 'rr', 'deadline'):
        raise ValueError('policy must be other, fifo, rr, or deadline')

    period_ns = _positive_int(args.period_ns, 'period_ns')
    deadline_ns = _positive_int(args.deadline_ns, 'deadline_ns')
    work_ns = _positive_int(args.work_ns, 'work_ns')
    if work_ns > 100_000_000:
        raise ValueError('work_ns must be <= 100000000')
    duration_s = _positive_int(args.duration_s, 'duration_s')
    if duration_s > 3600:
        raise ValueError('duration_s must be <= 3600')
    if not work_ns <= deadline_ns <= period_ns:
        raise ValueError('task contract requires work <= deadline <= period')

    runtime_ns = args.runtime_ns
    if runtime_ns is None:
        runtime_ns = min(work_ns + 200_000, deadline_ns)
    runtime_ns = _positive_int(runtime_ns, 'runtime_ns')
    if not work_ns <= runtime_ns <= deadline_ns:
        raise ValueError('runtime budget must satisfy work_ns <= runtime_ns <= deadline_ns')

    cpu = _nonnegative_int(args.cpu, 'CPU')
    if cpu >= 1024 or cpu not in set(allowed_cpus):
        raise ValueError(f'CPU {cpu} is outside current affinity {sorted(allowed_cpus)}')

    priority = _nonnegative_int(args.priority, 'priority')
    if policy in ('fifo', 'rr') and not 1 <= priority <= 99:
        raise ValueError('FIFO/RR priority must be between 1 and 99')
    if policy in ('other', 'deadline') and priority != 0:
        raise ValueError(f'{policy} priority must be 0')
    if policy == 'deadline' and runtime_ns >= period_ns:
        raise ValueError('SCHED_DEADLINE runtime must be less than period_ns')

    deadline_unpinned = getattr(args, 'deadline_unpinned', False)
    if type(deadline_unpinned) is not bool:
        raise ValueError('deadline_unpinned must be boolean')
    if deadline_unpinned and policy != 'deadline':
        raise ValueError('--deadline-unpinned is only valid with --policy deadline')

    jobs = expected_job_count(duration_s, period_ns)
    if jobs + 2 > 200_000:
        raise ValueError(f'job count {jobs} plus capacity 2 exceeds trace bound 200000')
    warmup_jobs = _nonnegative_int(args.warmup_jobs, 'warmup_jobs')
    if warmup_jobs >= jobs:
        raise ValueError(f'warmup_jobs must be less than actual job count {jobs}')
    if getattr(args, 'require_preempt_rt', False) and _kernel_realtime() != '1':
        raise ValueError('--require-preempt-rt requested but PREEMPT_RT is not confirmed')
    return runtime_ns, priority


def schedule_command(args, binary, runtime_ns, priority):
    effective_cpu = -1 if args.policy == 'deadline' and args.deadline_unpinned else args.cpu
    # C pins every non-negative CPU before applying policy. The diagnostic
    # deadline mode deliberately omits taskset and passes -1 to retain the
    # inherited mask; it never silently falls back to this mode.
    prefix = [] if effective_cpu == -1 else ['taskset', '--cpu-list', str(args.cpu)]
    return prefix + [str(binary), str(args.trace), args.policy, str(priority),
                     str(args.period_ns), str(args.deadline_ns), str(args.work_ns),
                     str(runtime_ns), str(args.duration_s), str(effective_cpu),
                     str(args.warmup_jobs)]


def _kernel_realtime():
    # Share the collector's bounded, root-contained evidence rules. In
    # particular, an unreadable or malformed legacy node cannot be bypassed.
    spec = importlib.util.spec_from_file_location(
        'xtos_sched_preflight_rt', Path(__file__).with_name('preflight.py'))
    if spec is None or spec.loader is None:
        return None
    preflight = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preflight)
    try:
        fact = preflight.Collector(Path('/'))._collect_realtime()
    except (OSError, ValueError):
        return None
    return fact['value'] if fact['status'] == 'available' else None


def _detect_virtualization():
    detector = shutil.which('systemd-detect-virt')
    if detector:
        try:
            result = subprocess.run([detector, '--quiet'], check=False,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            if result.returncode == 0:
                return True, 'systemd-detect-virt'
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        cpuinfo = Path('/proc/cpuinfo').read_text(encoding='utf-8', errors='replace').lower()
    except OSError:
        cpuinfo = ''
    for line in cpuinfo.splitlines():
        if line.startswith('flags') and ' hypervisor' in f' {line} ':
            return True, '/proc/cpuinfo:hypervisor'
        if line.startswith('hypervisor vendor'):
            return True, '/proc/cpuinfo:hypervisor-vendor'
    return None, 'no virtualization signal detected; physical hardware not established'


def _git_provenance():
    def git(*command):
        return subprocess.run(['git', *command], cwd=ROOT, check=False,
                              capture_output=True, text=True)

    head_result = git('rev-parse', 'HEAD')
    head = head_result.stdout.strip() if head_result.returncode == 0 else 'unknown'
    dirty_files = []
    status = subprocess.run(['git', 'status', '--porcelain=v1', '-z',
                             '--untracked-files=all'], cwd=ROOT, check=False,
                            capture_output=True)
    if status.returncode == 0:
        entries = iter(status.stdout.decode('utf-8', errors='surrogateescape').split('\0'))
        for entry in entries:
            if not entry:
                continue
            path_text = entry[3:]
            paths = [path_text]
            # Porcelain v1 uses a second NUL-delimited path for renames/copies.
            if entry[0] in 'RC' or entry[1] in 'RC':
                original = next(entries, '')
                if original:
                    paths.append(original)
            for path_text in paths:
                path = Path(path_text)
                if any(item['path'] == path_text for item in dirty_files):
                    continue
                absolute = ROOT / path
                digest = None
                if absolute.is_file():
                    try:
                        digest = sha256_file(absolute)
                    except OSError:
                        digest = None
                dirty_files.append({'path': path_text, 'sha256': digest})
    return head, dirty_files


def _parse_ready_line(process):
    if process.stdout is None:
        raise RuntimeError('competitor readiness pipe is unavailable')
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        events = selector.select(BURNER_READY_TIMEOUT_S)
        if not events:
            raise RuntimeError('competitor readiness timeout')
        # readline() can wait forever on a partial line from a failed helper.
        fd = process.stdout.fileno()
        os.set_blocking(fd, False)
        raw = bytearray()
        deadline = time.monotonic() + BURNER_READY_TIMEOUT_S
        while b'\n' not in raw:
            if time.monotonic() >= deadline:
                raise RuntimeError('competitor readiness timeout')
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                chunk = b''
            if chunk:
                raw.extend(chunk)
                if len(raw) > 8192:
                    raise RuntimeError('competitor readiness record too large')
            elif process.poll() is not None:
                break
            selector.select(min(0.02, max(0.001, deadline - time.monotonic())))
        line = bytes(raw).split(b'\n', 1)[0].decode('utf-8')
    finally:
        selector.close()
    if not line:
        raise RuntimeError('competitor exited before readiness')
    try:
        ready = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'invalid competitor readiness: {line.strip()!r}') from exc
    if not isinstance(ready, dict) or ready.get('ready') is not True:
        raise RuntimeError(f'invalid competitor readiness record: {ready!r}')
    return ready


def _proc_stat_ticks(pid):
    try:
        text = Path(f'/proc/{pid}/stat').read_text(encoding='utf-8')
    except OSError as exc:
        raise RuntimeError(f'cannot read /proc/{pid}/stat: {exc}') from exc
    closing = text.rfind(')')
    if closing < 0:
        raise RuntimeError(f'malformed /proc/{pid}/stat')
    fields = text[closing + 2:].split()
    if len(fields) < 13:
        raise RuntimeError(f'malformed /proc/{pid}/stat')
    if fields[0] == 'Z':
        raise RuntimeError(f'competitor {pid} is not alive')
    try:
        return int(fields[11]) + int(fields[12])
    except ValueError as exc:
        raise RuntimeError(f'malformed CPU time in /proc/{pid}/stat') from exc


def _verify_competitor(process, cpu):
    if process.poll() is not None:
        raise RuntimeError(f'competitor exited {process.returncode}')
    try:
        affinity = sorted(os.sched_getaffinity(process.pid))
        policy = os.sched_getscheduler(process.pid)
    except OSError as exc:
        raise RuntimeError(f'competitor readback failed: {exc}') from exc
    if affinity != [cpu]:
        raise RuntimeError(f'competitor affinity readback mismatch: {affinity!r}')
    if policy != os.SCHED_OTHER:
        raise RuntimeError(f'competitor scheduler readback mismatch: {policy}')
    return affinity


def _wait_for_cpu_progress(process, initial_ticks):
    deadline = time.monotonic() + BURNER_PROGRESS_TIMEOUT_S
    ticks = initial_ticks
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f'competitor exited {process.returncode} before CPU progress')
        ticks = _proc_stat_ticks(process.pid)
        if ticks > initial_ticks:
            return ticks
        time.sleep(0.02)
    raise RuntimeError('competitor made no CPU-time progress')


def _kill_process_group(process):
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=PROCESS_TERM_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=PROCESS_TERM_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            # Do not leave a reapable direct child behind even if a hostile
            # descendant keeps the process group alive.
            process.kill()
            process.wait(timeout=PROCESS_TERM_TIMEOUT_S)
    # A leader may exit on SIGTERM while its descendants ignore the signal.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    for stream_name in ('stdout', 'stderr'):
        stream = getattr(process, stream_name, None)
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _spawn_owned(owned, name, command, **kwargs):
    # Popen may be interrupted after fork but before returning its handle.
    # Register ownership before delivering pending termination signals, and
    # restore inside run()'s cleanup scope even when restoring itself raises.
    original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
    try:
        # Masks survive exec. Restore in a fresh interpreter, never preexec_fn
        # (unsafe in a threaded parent), then exec without changing PID/PGID.
        launcher = ('import os, signal, sys; '
                    'signal.pthread_sigmask(signal.SIG_SETMASK, '
                    '{int(n) for n in sys.argv[1].split(",") if n}); '
                    'os.execvpe(sys.argv[2], sys.argv[2:], os.environ)')
        mask = ','.join(str(int(sig)) for sig in sorted(original_mask))
        process = subprocess.Popen([sys.executable, '-c', launcher, mask, *command], **kwargs)
        owned[name] = process
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, original_mask)
    return process


def _start_competitor(cpu, owned):
    process = _spawn_owned(
        owned, 'burner',
        [sys.executable, str(BURNER_PATH), '--cpu', str(cpu)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    ready = _parse_ready_line(process)
    if ready.get('pid') != process.pid or ready.get('cpu') != cpu:
        raise RuntimeError(f'competitor readiness mismatch: {ready!r}')
    if ready.get('policy') != 'SCHED_OTHER' or ready.get('affinity') != [cpu]:
        raise RuntimeError(f'competitor setup readback mismatch: {ready!r}')
    affinity = _verify_competitor(process, cpu)
    ticks = _proc_stat_ticks(process.pid)
    _wait_for_cpu_progress(process, ticks)
    return process, affinity


def _verify_workload(process, args):
    if process.poll() is not None:
        return
    try:
        policy = os.sched_getscheduler(process.pid)
        affinity = sorted(os.sched_getaffinity(process.pid))
    except OSError as exc:
        raise RuntimeError(f'workload readback failed: {exc}') from exc
    if policy != ({'other': os.SCHED_OTHER, 'fifo': os.SCHED_FIFO,
                   'rr': os.SCHED_RR, 'deadline': SCHED_DEADLINE}[args.policy]):
        raise RuntimeError(f'workload scheduler readback mismatch: {policy}')
    expected = sorted(os.sched_getaffinity(0)) if args.policy == 'deadline' and args.deadline_unpinned else [args.cpu]
    if affinity != expected:
        raise RuntimeError(f'workload affinity readback mismatch: {affinity!r}')


def _trace_metadata(trace):
    try:
        with Path(trace).open('r', encoding='utf-8') as stream:
            first = stream.readline()
    except OSError as exc:
        raise RuntimeError(f'cannot read generated trace: {exc}') from exc
    if not first:
        raise RuntimeError('generated trace is empty')
    try:
        metadata = json.loads(first)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'generated trace metadata is not JSON: {exc}') from exc
    if not isinstance(metadata, dict) or metadata.get('type') != 'metadata':
        raise RuntimeError('generated trace metadata record is missing')
    return metadata


def _write_manifest(path, manifest):
    try:
        with Path(path).open('x', encoding='utf-8') as stream:
            json.dump(manifest, stream, sort_keys=True, indent=2)
            stream.write('\n')
    except FileExistsError as exc:
        raise RuntimeError(f'manifest already exists: {path}') from exc


def run(args):
    allowed = os.sched_getaffinity(0)
    runtime_ns, priority = validate_args(args, allowed)
    binary = Path(args.binary) if args.binary else ROOT / '.build/xtos-sched-bench/periodic'
    if not binary.is_file():
        raise ValueError(f'benchmark binary missing: {binary}; build it first')
    if not BURNER_PATH.is_file():
        raise ValueError(f'competitor source missing: {BURNER_PATH}')
    if shutil.which('taskset') is None and not (args.policy == 'deadline' and args.deadline_unpinned):
        raise ValueError('taskset is required for pinned benchmark trials')

    trace = Path(args.trace)
    sidecar = manifest_path(trace)
    trace.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(trace):
        raise ValueError(f'trace already exists: {trace}')
    if os.path.lexists(sidecar):
        raise ValueError(f'manifest already exists: {sidecar}')

    command = schedule_command(args, binary, runtime_ns, priority)
    bound_sources = {'runner': Path(__file__), 'burner': BURNER_PATH,
                     'periodic.c': PERIODIC_SOURCE, 'binary': binary,
                     'analyzer': Path(__file__).with_name('analyze_trace.py')}
    hashes_before = {key: sha256_file(path) for key, path in bound_sources.items()}
    git_before = _git_provenance()
    owned = {'burner': None, 'workload': None}
    try:
        burner, competitor_affinity = _start_competitor(args.cpu, owned)
        workload = _spawn_owned(owned, 'workload', command, start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True)
        competitor_ticks_before = _proc_stat_ticks(burner.pid)
        timeout = float(args.duration_s) + WORKLOAD_TIMEOUT_MARGIN_S
        deadline = time.monotonic() + timeout
        while workload.poll() is None:
            if time.monotonic() >= deadline:
                raise RuntimeError(f'workload timed out after {timeout:.3f}s')
            _verify_competitor(burner, args.cpu)
            time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))
        returncode = workload.wait()
        if returncode != 0:
            detail = ''
            if workload.stderr:
                os.set_blocking(workload.stderr.fileno(), False)
                try:
                    detail = os.read(workload.stderr.fileno(), 8192).decode('utf-8', errors='replace')
                except BlockingIOError:
                    pass
            raise RuntimeError(f'workload exited {returncode}: {detail.strip()}')
        if not trace.is_file() or trace.stat().st_size == 0:
            raise RuntimeError('workload succeeded but did not create a non-empty trace')
        metadata = _trace_metadata(trace)
        spec = importlib.util.spec_from_file_location('xtos_trace_analyzer', Path(__file__).with_name('analyze_trace.py'))
        analyzer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(analyzer)
        summary = analyzer.summarize(analyzer._load_jsonl(trace))
        expected_cpu = -1 if args.policy == 'deadline' and args.deadline_unpinned else args.cpu
        for field, expected in {
            'policy': args.policy, 'actual_priority': priority,
            'period_ns': args.period_ns, 'deadline_ns': args.deadline_ns,
            'work_ns': args.work_ns, 'runtime_ns': runtime_ns,
            'duration_s': args.duration_s, 'warmup_jobs': args.warmup_jobs,
            'cpu_requested': expected_cpu,
        }.items():
            if metadata.get(field) != expected:
                raise RuntimeError(f'trace metadata mismatch for {field}')
        _verify_competitor(burner, args.cpu)
        competitor_ticks_after = _proc_stat_ticks(burner.pid)
        if competitor_ticks_after <= competitor_ticks_before:
            raise RuntimeError('competitor made no CPU-time progress during trial')
        _kill_process_group(workload)
        owned['workload'] = None
        _kill_process_group(burner)
        owned['burner'] = None
        raw_bytes = trace.read_bytes()
        virtualized, virtualization_source = _detect_virtualization()
        git_head, dirty_files = _git_provenance()
        hashes_after = {key: sha256_file(path) for key, path in bound_sources.items()}
        if hashes_before != hashes_after or git_before != (git_head, dirty_files):
            raise RuntimeError('source or binary provenance changed during trial')
        effective_cpu = -1 if args.policy == 'deadline' and args.deadline_unpinned else args.cpu
        trace_affinity_mode = metadata.get('affinity_mode')
        manifest = {
            'schema': 'xtos-sched-manifest-v1',
            'raw_trace': str(trace),
            'raw_trace_sha256': hashlib.sha256(raw_bytes).hexdigest(),
            'trace_metadata': metadata,
            'host_is_virtualized': virtualized,
            'virtualization_source': virtualization_source,
            'uname': os.uname().release,
            'realtime_sysfs': _kernel_realtime(),
            'runner_commit': git_head,
            'dirty_files': dirty_files,
            'source_sha256': hashes_after,
            'source_unchanged_during_trial': True,
            'hardware_performance_evidence': False,
            'runner_args': {
                'policy': args.policy, 'priority': priority,
                'period_ns': args.period_ns, 'deadline_ns': args.deadline_ns,
                'work_ns': args.work_ns, 'runtime_ns': runtime_ns,
                'duration_s': args.duration_s, 'cpu': args.cpu,
                'competitor_cpu': args.cpu, 'warmup_jobs': args.warmup_jobs,
                'deadline_unpinned': bool(args.deadline_unpinned),
                'effective_cpu_argument': effective_cpu,
            },
            # These values are copied from the C metadata, never fabricated
            # from runner arguments. Inherited-mask trials are diagnostics.
            'cpu_requested': metadata.get('cpu_requested'),
            'expected_jobs': metadata.get('expected_jobs'),
            'affinity_mode': trace_affinity_mode,
            'cpu_affinity': metadata.get('cpu_affinity'),
            'comparable': trace_affinity_mode == 'pinned',
            'diagnostic': trace_affinity_mode == 'inherited',
            'competitor_cpu_affinity': competitor_affinity,
            'competitor_ticks_before': competitor_ticks_before,
            'competitor_ticks_after': competitor_ticks_after,
            'trace_summary': summary,
        }
        _write_manifest(sidecar, manifest)
    finally:
        try:
            _kill_process_group(owned['workload'])
        finally:
            _kill_process_group(owned['burner'])


def main():
    def terminate(signum, frame):
        raise InterruptedError(f'benchmark interrupted by signal {signum}')
    signal.signal(signal.SIGTERM, terminate)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', choices=('other', 'fifo', 'rr', 'deadline'), required=True)
    parser.add_argument('--trace', type=Path, required=True)
    parser.add_argument('--period-ns', type=int, default=10_000_000)
    parser.add_argument('--deadline-ns', type=int, default=10_000_000)
    parser.add_argument('--work-ns', type=int, default=1_000_000)
    parser.add_argument('--runtime-ns', type=int)
    parser.add_argument('--duration-s', type=int, default=10)
    parser.add_argument('--cpu', type=int, required=True)
    parser.add_argument('--warmup-jobs', type=int, default=100)
    parser.add_argument('--priority', type=int, default=0)
    parser.add_argument('--deadline-unpinned', action='store_true',
                        help='pass CPU -1 to SCHED_DEADLINE and retain inherited affinity')
    parser.add_argument('--require-preempt-rt', action='store_true',
                        help='require confirmed PREEMPT_RT from sysfs or current proc evidence')
    parser.add_argument('--binary', type=Path)
    args = parser.parse_args()
    try:
        run(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'benchmark_error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
