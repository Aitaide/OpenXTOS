#!/usr/bin/env python3
"""Bounded M1 logical replay; report generation is not policy admission."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys

from model import (AdmissionController, AdmissionError, CpuPartition, TaskSpec,
                   MAX_TASKS, MAX_JOBS, MAX_NS, POLICIES, dbf_report, simulate_edf)

SCHEMA = "xtos-scheduler-model-input-v1"
REPORT_SCHEMA = "xtos-scheduler-model-report-v1"
MAX_INPUT_BYTES = 1048576
TRACE_METADATA_FIELDS = {
    'type', 'schema', 'policy', 'actual_policy', 'actual_priority', 'period_ns',
    'deadline_ns', 'work_ns', 'runtime_ns', 'duration_s', 'cpu_requested',
    'clock', 'warmup_jobs', 'expected_jobs', 'affinity_mode', 'cpu_affinity',
    'wake_semantics',
}
TRACE_JOB_FIELDS = {
    'type', 'id', 'release_ns', 'wake_ns', 'finish_ns', 'deadline_ns',
    'work_ns', 'cpu_work_ns', 'cpu', 'warmup',
}
TRACE_METADATA_TIME_FIELDS = ('period_ns', 'deadline_ns', 'work_ns', 'runtime_ns')
TRACE_JOB_TIME_FIELDS = ('release_ns', 'wake_ns', 'finish_ns', 'deadline_ns',
                         'work_ns', 'cpu_work_ns')


def _pairs(items):
    result = {}
    for key,value in items:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f"non-finite JSON value: {value}")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def _read_input(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_INPUT_BYTES:
            raise ValueError("input must be a regular file at most 1 MiB")
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_INPUT_BYTES+1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("input exceeds 1 MiB")
    finally:
        os.close(fd)
    return raw


def _parse(raw):
    return json.loads(raw, object_pairs_hook=_pairs,
                          parse_constant=_constant, parse_float=_float)


def _json(path):
    raw = _read_input(path)
    document = _parse(raw.decode('utf-8'))
    if not isinstance(document, dict):
        raise ValueError("input must be a JSON object")
    return document, raw


def _validate_trace_records(records):
    metadata = records[0]
    if isinstance(metadata, dict):
        unknown = set(metadata) - TRACE_METADATA_FIELDS
        if unknown:
            raise ValueError('unknown trace metadata fields: ' + ', '.join(sorted(unknown)))
        for field in TRACE_METADATA_TIME_FIELDS:
            if field in metadata and (type(metadata[field]) is not int or
                                      not 0 <= metadata[field] <= MAX_NS):
                raise ValueError(f'trace time field {field} must be an integer in [0, {MAX_NS}]')
        if 'duration_s' in metadata and (
                type(metadata['duration_s']) is not int or metadata['duration_s'] < 0 or
                metadata['duration_s'] * 1_000_000_000 > MAX_NS):
            raise ValueError(f'trace time field duration_s must convert to nanoseconds in [0, {MAX_NS}]')
    for index, row in enumerate(records[1:], 1):
        if not isinstance(row, dict):
            continue
        unknown = set(row) - TRACE_JOB_FIELDS
        if unknown:
            raise ValueError(f'unknown trace job fields at record {index}: ' + ', '.join(sorted(unknown)))
        for field in TRACE_JOB_TIME_FIELDS:
            if field in row and (type(row[field]) is not int or not 0 <= row[field] <= MAX_NS):
                raise ValueError(f'trace time field {field} at record {index} must be an integer in [0, {MAX_NS}]')


def _trace(path):
    import importlib.util
    analyzer_path = Path(__file__).resolve().parent.parent/'xtos-sched-bench/analyze_trace.py'
    spec = importlib.util.spec_from_file_location('m1_trace_analyzer', analyzer_path)
    analyzer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(analyzer)
    raw = _read_input(path)
    lines = raw.decode('utf-8').splitlines()
    if not lines or len(lines) > MAX_JOBS+1 or any(not line.strip() for line in lines):
        raise ValueError('empty/blank trace records')
    records = [_parse(line) for line in lines]
    _validate_trace_records(records)
    summary = analyzer.summarize(records)
    metadata = records[0]
    if not summary['comparable']:
        raise ValueError('inherited-affinity diagnostic trace cannot enter pinned replay')
    profile = [row['cpu_work_ns'] for row in records[1:]]
    horizon = len(profile)*metadata['period_ns']
    task = {'name':'trace-task','cpu':metadata['cpu_requested'],
            'runtime_ns':metadata['runtime_ns'],'deadline_ns':metadata['deadline_ns'],
            'period_ns':metadata['period_ns'],'work_profile_ns':profile,
            'priority':metadata['actual_priority'] or 1}
    document = {'schema':SCHEMA,'cpu':task['cpu'],'horizon_ns':horizon,
                'dbf_horizon_ns':horizon,'tasks':[task]}
    source = {'schema':metadata['schema'],'summary':summary,
              'first_release_ns':records[1]['release_ns'],
              'work_mapping':'cpu_work_ns per job; planned releases normalized to zero; all warmup retained',
              'analyzer_sha256':hashlib.sha256(analyzer_path.read_bytes()).hexdigest(),
              'authenticity':'syntax validated only; no binary/collector attestation',
              'limitations':'No observed wake/finish delays, IRQ interference or original competitors replayed.'}
    return document,raw,source,metadata['warmup_jobs']


def _validate_document(document):
    if not isinstance(document, dict) or document.get('schema') != SCHEMA:
        raise ValueError(f"schema must be {SCHEMA}")
    allowed = {'schema','cpu','partition_cpus','horizon_ns','dbf_horizon_ns','tasks',
               'status','production_workload_approved','limitations'}
    if set(document)-allowed:
        raise ValueError("unknown input fields: " + ', '.join(sorted(set(document)-allowed)))
    cpu, horizon = document.get('cpu'), document.get('horizon_ns')
    if type(cpu) is not int or cpu < 0:
        raise ValueError("cpu must be a nonnegative integer")
    if type(horizon) is not int or horizon <= 0:
        raise ValueError("horizon_ns must be a positive integer")
    raw_tasks = document.get('tasks')
    if not isinstance(raw_tasks,list) or not 1 <= len(raw_tasks) <= MAX_TASKS:
        raise ValueError("tasks must be a bounded nonempty array")
    tasks = []
    required = {'name','cpu','runtime_ns','deadline_ns','period_ns'}
    for raw in raw_tasks:
        if not isinstance(raw,dict) or not required <= raw.keys():
            raise ValueError("task must contain name/cpu/runtime/deadline/period")
        if set(raw)-(required | {'work_ns','priority','offset_ns','work_profile_ns'}):
            raise ValueError("unknown task fields")
        item = dict(raw)
        if 'work_profile_ns' in item:
            if not isinstance(item['work_profile_ns'],list):
                raise ValueError('work_profile_ns must be a JSON array')
            item['work_profile_ns'] = tuple(item['work_profile_ns'])
        tasks.append(TaskSpec(**item))
    if len({t.name for t in tasks}) != len(tasks):
        raise ValueError("task names must be unique")
    if any(t.cpu != cpu for t in tasks):
        raise ValueError("tasks must belong to selected CPU")
    raw_partition = document.get('partition_cpus',[cpu])
    if not isinstance(raw_partition,list):
        raise ValueError("partition_cpus must be an array")
    partition = CpuPartition(raw_partition)
    if cpu not in partition.cpus:
        raise ValueError("selected CPU outside partition")
    dbf_horizon = document.get('dbf_horizon_ns')
    if any(t.deadline_ns < t.period_ns for t in tasks) and dbf_horizon is None:
        raise ValueError("constrained deadline requires diagnostic dbf_horizon_ns")
    if dbf_horizon is not None and (type(dbf_horizon) is not int or dbf_horizon <= 0):
        raise ValueError("dbf_horizon_ns must be a positive integer")
    if 'production_workload_approved' in document and type(document['production_workload_approved']) is not bool:
        raise ValueError("production_workload_approved label must be boolean")
    if 'status' in document and not isinstance(document['status'],str):
        raise ValueError("status label must be string")
    if 'limitations' in document and (not isinstance(document['limitations'],list) or
                                    any(not isinstance(v,str) for v in document['limitations'])):
        raise ValueError("limitations label must be string array")
    return cpu,horizon,tuple(tasks),partition,dbf_horizon


def build_report(document, input_sha256, policies, quantum_ns):
    cpu,horizon,tasks,partition,dbf_horizon = _validate_document(document)
    if not policies or any(p not in POLICIES for p in policies) or len(set(policies)) != len(policies):
        raise ValueError("policies must be unique supported model names")
    if ('rr' in policies and (type(quantum_ns) is not int or quantum_ns <= 0)) or (
            'rr' not in policies and quantum_ns is not None):
        raise ValueError("rr requires positive --quantum-ns; other policies do not")
    try:
        admitted = AdmissionController(partition).admit_all(tasks)
        admission = {'status':'ideal_implicit_deadline_only', **asdict(admitted),
                     'assumptions':'full CPU supply; independent tasks; no higher classes/IRQ/blocking/overhead'}
    except AdmissionError as exc:
        admission = {'status':'not_admitted','accepted':False,'reason':str(exc)}
    diagnostic = asdict(dbf_report(tasks,horizon_ns=dbf_horizon)) if dbf_horizon is not None else None
    results = []
    for policy in policies:
        result = simulate_edf(tasks,horizon_ns=horizon,cpu=cpu,policy=policy,
                              quantum_ns=quantum_ns if policy == 'rr' else None)
        results.append({'policy':policy,'quantum_ns':quantum_ns if policy=='rr' else None,
                        'cpu':cpu,'horizon_ns':horizon,'job_count':len(result.jobs),
                        'deadline_misses':result.deadline_misses,
                        'budget_exhaustions':result.budget_exhaustions,
                        'budget_enforced':policy=='reservation_edf',
                        'jobs':[asdict(j) for j in result.jobs],
                        'segments':[asdict(s) for s in result.segments]})
    here = Path(__file__).resolve().parent
    return {'schema':REPORT_SCHEMA,'status':'model_only','input_sha256':input_sha256,
            'input_schema':SCHEMA,'cpu':cpu,'horizon_ns':horizon,'dbf_horizon_ns':dbf_horizon,
            'reservation_admission':admission,'dbf':diagnostic,'policies':results,
            'model_source_sha256':{name:hashlib.sha256((here/name).read_bytes()).hexdigest()
                                   for name in ('model.py','replay.py')},
            'limitations':['Logical-time models, not kernel/physical measurements or Linux CBS.',
                           'Finite DBF never authorizes admission; implicit admission assumes full CPU supply.',
                           'FIFO/RR have no reservation budget; reservation_edf uses task-shared fixed-period resets.',
                           'No IRQ, PI, lock blocking, overhead, CPU migration, hotplug, yield or device semantics.',
                           'Drain finite jobs after release horizon; no steady-state or hardware superiority claim.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--input',type=Path)
    inputs.add_argument('--trace',type=Path,help='validated pinned xtos-sched-trace-v1 CPU-demand replay')
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--policies',default='edf',help=','.join(POLICIES))
    parser.add_argument('--quantum-ns',type=int)
    args = parser.parse_args(argv)
    try:
        # Do not resolve the output leaf: a dangling symlink is an existing entry.
        output = args.output.absolute()
        if os.path.lexists(output):
            raise ValueError('output must not already exist and must not alias input')
        trace_source = None
        if args.trace is not None:
            document,raw,trace_source,warmup = _trace(args.trace)
        else:
            document,raw = _json(args.input)
        policies = tuple(args.policies.split(','))
        report = build_report(document,hashlib.sha256(raw).hexdigest(),policies,args.quantum_ns)
        if trace_source is not None:
            report['trace_source'] = trace_source
            report['input_schema'] = trace_source['schema']
            for result in report['policies']:
                for job in result['jobs']:
                    job['warmup'] = job['job_index'] < warmup
        serialized = json.dumps(report,ensure_ascii=False,indent=2,sort_keys=True,allow_nan=False)+'\n'
        output.parent.mkdir(parents=True,exist_ok=True)
        with output.open('x',encoding='utf-8') as stream:
            stream.write(serialized)
        return 0
    except (OSError,ValueError,TypeError,RecursionError) as exc:
        print(f'model_error: {exc}',file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
