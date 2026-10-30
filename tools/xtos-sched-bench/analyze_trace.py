#!/usr/bin/env python3
"""Validate and summarize xtos-sched-trace-v1 JSON-lines records.

The trace measures time from the intended absolute release to the first
userspace clock read after sleep, and userspace execution intervals. It does
not measure kernel wakeup timestamps or kernel scheduler overhead.
"""
import argparse
import json
import math
import os
import sys
from pathlib import Path

SCHEMA = 'xtos-sched-trace-v1'
POLICY_IDS = {'other': 0, 'fifo': 1, 'rr': 2, 'deadline': 6}
POLICY_NAMES = {value: key for key, value in POLICY_IDS.items()}


def _integer(record, field, *, minimum=0):
    if field not in record:
        raise ValueError(f'missing required field: {field}')
    value = record[field]
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f'{field} must be an integer >= {minimum}')
    return value


def _percentile(sorted_values, percentile):
    # Nearest-rank definition: rank = ceil(p * n), with one-based ranks.
    rank = math.ceil((percentile / 100) * len(sorted_values))
    return sorted_values[max(rank - 1, 0)]


def _distribution(values):
    ordered = sorted(values)
    return {
        'p50': _percentile(ordered, 50),
        'p95': _percentile(ordered, 95),
        'p99': _percentile(ordered, 99),
        'p99_9': _percentile(ordered, 99.9),
        'max': ordered[-1],
    }


def _metadata_checks(metadata):
    if metadata.get('type') != 'metadata':
        raise ValueError('metadata record must be first')
    if metadata.get('schema') != SCHEMA:
        raise ValueError(f'unsupported schema: {metadata.get("schema")}')
    policy = metadata.get('policy')
    if not isinstance(policy, str) or policy not in POLICY_IDS:
        raise ValueError('metadata policy must be other, fifo, rr, or deadline')
    actual_policy = _integer(metadata, 'actual_policy')
    if actual_policy not in POLICY_NAMES or POLICY_NAMES[actual_policy] != policy:
        raise ValueError('actual_policy does not match policy')
    actual_priority = _integer(metadata, 'actual_priority')
    if policy in ('other', 'deadline') and actual_priority != 0:
        raise ValueError('actual_priority must be zero for other/deadline')
    if policy in ('fifo', 'rr') and not 1 <= actual_priority <= 99:
        raise ValueError('actual_priority must be in [1, 99] for fifo/rr')

    period_ns = _integer(metadata, 'period_ns', minimum=1)
    relative_deadline_ns = _integer(metadata, 'deadline_ns', minimum=1)
    work_ns = _integer(metadata, 'work_ns', minimum=1)
    runtime_ns = _integer(metadata, 'runtime_ns', minimum=1)
    duration_s = _integer(metadata, 'duration_s', minimum=1)
    warmup_jobs = _integer(metadata, 'warmup_jobs')
    expected_jobs = _integer(metadata, 'expected_jobs', minimum=1)
    if relative_deadline_ns > period_ns:
        raise ValueError('deadline_ns must be positive and <= period_ns')
    if not work_ns <= runtime_ns <= relative_deadline_ns:
        raise ValueError('work_ns/runtime_ns/deadline_ns contract is invalid')
    if policy == 'deadline' and runtime_ns >= period_ns:
        raise ValueError('deadline policy runtime_ns must be less than period_ns')
    expected_from_duration = (duration_s * 1_000_000_000 + period_ns - 1) // period_ns
    if expected_jobs != expected_from_duration:
        raise ValueError('expected_jobs does not equal ceil(duration_s / period_ns)')
    if warmup_jobs >= expected_jobs:
        raise ValueError('warmup_jobs must leave at least one measured job')
    if metadata.get('clock') != 'CLOCK_MONOTONIC':
        raise ValueError('clock must be CLOCK_MONOTONIC')
    if metadata.get('wake_semantics') != 'first_userspace_timestamp_after_absolute_sleep':
        raise ValueError('wake_semantics must be first_userspace_timestamp_after_absolute_sleep')

    affinity_mode = metadata.get('affinity_mode')
    cpu_requested = _integer(metadata, 'cpu_requested', minimum=-1)
    if affinity_mode not in ('pinned', 'inherited'):
        raise ValueError('affinity_mode must be pinned or inherited')
    if affinity_mode == 'inherited':
        if policy != 'deadline' or cpu_requested != -1:
            raise ValueError('inherited affinity is permitted only for deadline with cpu_requested=-1')
    elif cpu_requested < 0:
        raise ValueError('pinned affinity requires cpu_requested >= 0')

    cpus = metadata.get('cpu_affinity')
    if (not isinstance(cpus, list) or not cpus or
            any(isinstance(cpu, bool) or not isinstance(cpu, int) or cpu < 0 for cpu in cpus) or
            cpus != sorted(set(cpus))):
        raise ValueError('cpu_affinity must be a nonempty sorted list of unique nonnegative CPU ids')
    if affinity_mode == 'pinned' and cpu_requested not in cpus:
        raise ValueError('requested pinned CPU is outside cpu_affinity')
    if affinity_mode == 'pinned' and cpus != [cpu_requested]:
        raise ValueError('pinned cpu_affinity must contain only cpu_requested')
    return {
        'policy': policy, 'actual_policy': actual_policy,
        'actual_priority': actual_priority, 'period_ns': period_ns,
        'deadline_ns': relative_deadline_ns, 'work_ns': work_ns,
        'runtime_ns': runtime_ns, 'duration_s': duration_s,
        'warmup_jobs': warmup_jobs, 'expected_jobs': expected_jobs,
        'affinity_mode': affinity_mode, 'cpu_affinity': cpus,
        'cpu_requested': cpu_requested,
    }


def summarize(records):
    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        raise ValueError('metadata record must be first')
    metadata = _metadata_checks(records[0])
    rows = records[1:]
    if len(rows) != metadata['expected_jobs']:
        raise ValueError(f'job record count must equal expected_jobs ({metadata["expected_jobs"]})')

    releases = []
    wake_latencies = []
    completion_latencies = []
    response_latencies = []
    cpu_work_values = []
    observed_cpus = []
    missed = 0
    previous_finish = None
    for expected_id, row in enumerate(rows):
        if not isinstance(row, dict) or row.get('type') != 'job':
            raise ValueError('all records after metadata must be job records')
        job_id = _integer(row, 'id')
        if job_id != expected_id:
            raise ValueError(f'job ids must be contiguous and ordered from zero; expected {expected_id}')
        release = _integer(row, 'release_ns')
        wake = _integer(row, 'wake_ns')
        finish = _integer(row, 'finish_ns')
        deadline = _integer(row, 'deadline_ns')
        work = _integer(row, 'work_ns', minimum=1)
        cpu_work = _integer(row, 'cpu_work_ns', minimum=1)
        cpu = _integer(row, 'cpu')
        warmup = row.get('warmup')
        if type(warmup) is not bool:
            raise ValueError(f'warmup must be a boolean for job {job_id}')
        if warmup != (job_id < metadata['warmup_jobs']):
            raise ValueError(f'warmup position does not match metadata for job {job_id}')
        if work != metadata['work_ns']:
            raise ValueError(f'work_ns does not match metadata for job {job_id}')
        if deadline != release + metadata['deadline_ns']:
            raise ValueError(f'deadline_ns does not match relative metadata for job {job_id}')
        if expected_id and release != releases[0] + expected_id * metadata['period_ns']:
            raise ValueError(f'release_ns is not evenly spaced by period_ns for job {job_id}')
        if not release < deadline or not release <= wake <= finish:
            raise ValueError(f'invalid timestamp/deadline interval for job {job_id}')
        if previous_finish is not None and wake < previous_finish:
            raise ValueError(f'job execution intervals overlap at job {job_id}')
        previous_finish = finish
        # Apply no clock-resolution tolerance: inconsistent CPU/wall intervals fail closed.
        if cpu_work < work or cpu_work > finish - wake:
            raise ValueError(f'cpu_work_ns is outside measured interval for job {job_id}')
        if cpu not in metadata['cpu_affinity']:
            raise ValueError(f'job {job_id} ran outside effective cpu_affinity')
        if metadata['affinity_mode'] == 'pinned' and cpu != metadata['cpu_requested']:
            raise ValueError(f'job {job_id} did not run on requested pinned CPU')

        releases.append(release)
        observed_cpus.append(cpu)
        if warmup:
            continue
        wake_latencies.append(wake - release)
        completion_latencies.append(finish - release)
        response_latencies.append(finish - wake)
        cpu_work_values.append(cpu_work)
        missed += finish > deadline

    if not wake_latencies:
        raise ValueError('trace must contain at least one measured job')
    migrations = sum(before != after for before, after in zip(observed_cpus, observed_cpus[1:]))
    return {
        'schema': SCHEMA,
        'policy': metadata['policy'],
        'actual_policy': metadata['actual_policy'],
        'actual_priority': metadata['actual_priority'],
        'total_jobs': len(rows),
        'job_count': len(wake_latencies),
        'warmup_jobs_excluded': metadata['warmup_jobs'],
        'deadline_misses': missed,
        'release_latency_ns': _distribution(wake_latencies),
        'completion_latency_ns': _distribution(completion_latencies),
        'response_latency_ns': _distribution(response_latencies),
        'cpu_work_ns': _distribution(cpu_work_values),
        'actual_cpu_ids': sorted(set(observed_cpus)),
        'cpu_migrations_observed': migrations,
        'cpu_migration_definition': 'count of adjacent job samples (including warmup) whose recorded CPU differs; does not detect intra-job migrations',
        'work_ns_total': sum(cpu_work_values),
        'requested_work_ns_total': metadata['work_ns'] * len(wake_latencies),
        'comparable': metadata['affinity_mode'] == 'pinned',
        'limitations': [
            'wake_ns is the first userspace clock read after the absolute sleep, not a kernel wakeup timestamp',
            'latencies do not measure kernel scheduler overhead',
            'CPU migration count compares adjacent measured job samples and does not detect intra-job migrations',
        ],
    }


def _reject_constant(value):
    raise ValueError(f'non-finite JSON constant is not allowed: {value}')


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f'non-finite JSON number is not allowed: {value}')
    return parsed


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON object key: {key}')
        result[key] = value
    return result


def _load_jsonl(path):
    records = []
    with path.open('r', encoding='utf-8') as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                raise ValueError(f'blank JSONL record at line {line_number}')
            try:
                records.append(json.loads(line, parse_constant=_reject_constant,
                                          parse_float=_finite_float,
                                          object_pairs_hook=_unique_object))
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f'invalid JSON at line {line_number}: {exc}') from exc
    return records


def _output_aliases_input(source, destination):
    if source.resolve() == destination.resolve():
        return True
    try:
        return os.path.samefile(source, destination)
    except FileNotFoundError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trace', type=Path, help='raw JSON-lines trace')
    parser.add_argument('--output', type=Path, help='write summary JSON to a new path')
    args = parser.parse_args()
    try:
        records = _load_jsonl(args.trace)
        report = summarize(records)
        payload = json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + '\n'
        if args.output:
            if _output_aliases_input(args.trace, args.output):
                raise ValueError('--output must not alias the raw input')
            # Exclusive creation prevents silently replacing any existing file.
            with args.output.open('x', encoding='utf-8') as output:
                output.write(payload)
        else:
            print(payload, end='')
    except (OSError, UnicodeError, ValueError) as exc:
        print(f'trace_error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
