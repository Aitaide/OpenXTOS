#!/usr/bin/env python3
"""Independent fixed-CPU logical replay; not a Linux SMP scheduling model."""
from dataclasses import asdict
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from model import (AdmissionController, AdmissionError, CpuPartition, TaskSpec,
                   MAX_TASKS, MAX_NS, MAX_DBF_POINTS, POLICIES, dbf_report,
                   simulate_partitioned)
from replay import _json

SCHEMA = 'xtos-scheduler-partitioned-input-v1'
REPORT_SCHEMA = 'xtos-scheduler-partitioned-report-v1'


def _validate(document):
    if not isinstance(document, dict) or document.get('schema') != SCHEMA:
        raise ValueError(f'schema must be {SCHEMA}')
    allowed = {'schema','partition_cpus','horizon_ns','dbf_horizon_ns','tasks'}
    if set(document)-allowed:
        raise ValueError('unknown partitioned input fields')
    horizon = document.get('horizon_ns')
    if type(horizon) is not int or not 1 <= horizon <= MAX_NS:
        raise ValueError('horizon_ns must be a positive int64 integer')
    cpus = document.get('partition_cpus')
    if not isinstance(cpus, list):
        raise ValueError('partition_cpus must be an array')
    partition = CpuPartition(cpus)
    raw_tasks = document.get('tasks')
    if not isinstance(raw_tasks, list) or not 1 <= len(raw_tasks) <= MAX_TASKS:
        raise ValueError('tasks must be a bounded nonempty array')
    required = {'name','cpu','runtime_ns','deadline_ns','period_ns'}
    task_fields = required | {'work_ns','work_profile_ns','priority','offset_ns'}
    tasks = []
    for raw in raw_tasks:
        if not isinstance(raw, dict) or not required <= raw.keys() or set(raw)-task_fields:
            raise ValueError('task has missing or unknown fields')
        item = dict(raw)
        if 'work_profile_ns' in item:
            if not isinstance(item['work_profile_ns'], list):
                raise ValueError('work_profile_ns must be an array')
            item['work_profile_ns'] = tuple(item['work_profile_ns'])
        tasks.append(TaskSpec(**item))
    if len({t.name for t in tasks}) != len(tasks):
        raise ValueError('task names must be globally unique')
    if any(t.cpu not in partition.cpus for t in tasks):
        raise ValueError('task outside CPU partition')
    dbf_horizon = document.get('dbf_horizon_ns')
    if dbf_horizon is not None and (type(dbf_horizon) is not int or not 1 <= dbf_horizon <= MAX_NS):
        raise ValueError('dbf_horizon_ns must be a positive int64 integer')
    if any(t.deadline_ns < t.period_ns for t in tasks) and dbf_horizon is None:
        raise ValueError('constrained deadline requires diagnostic dbf_horizon_ns')
    return tuple(tasks), partition, horizon, dbf_horizon


def build_report(document, input_sha256, policies, quantum_ns):
    tasks, partition, horizon, dbf_horizon = _validate(document)
    if not policies or len(set(policies)) != len(policies) or any(p not in POLICIES for p in policies):
        raise ValueError('policies must be unique supported model names')
    if ('rr' in policies and (type(quantum_ns) is not int or not 1 <= quantum_ns <= MAX_NS)) or (
            'rr' not in policies and quantum_ns is not None):
        raise ValueError('rr requires positive int64 quantum; other policies do not')
    try:
        report = AdmissionController(partition).admit_all(tasks)
        admission = {'status':'ideal_implicit_deadline_only', **asdict(report),
                     'assumptions':'independent fixed CPUs with full supply; no IRQ/blocking/overhead'}
    except AdmissionError as exc:
        admission = {'status':'not_admitted','accepted':False,'reason':str(exc)}
    active = sorted({t.cpu for t in tasks})
    diagnostics = None
    if dbf_horizon is not None:
        # Conservative global enumeration cap, including one horizon per CPU.
        points_bound = len(active) + sum(max(0, (dbf_horizon-t.deadline_ns)//t.period_ns+1)
                                         for t in tasks)
        if points_bound > MAX_DBF_POINTS:
            raise ValueError('global DBF enumeration limit exceeded')
        diagnostics = [{'cpu':cpu, **asdict(dbf_report([t for t in tasks if t.cpu==cpu],
                                                     horizon_ns=dbf_horizon))}
                       for cpu in active]
    results = []
    for policy in policies:
        result = simulate_partitioned(tasks, partition=partition, horizon_ns=horizon,
                                      policy=policy, quantum_ns=quantum_ns if policy=='rr' else None)
        cpus = []
        for cpu_result in result.cpus:
            row = asdict(cpu_result)
            row['job_count'] = len(cpu_result.jobs)
            row['budget_enforced'] = policy=='reservation_edf'
            cpus.append(row)
        results.append({'policy':policy,'quantum_ns':quantum_ns if policy=='rr' else None,
                        'job_count':result.job_count,'deadline_misses':result.deadline_misses,
                        'budget_exhaustions':result.budget_exhaustions,'cpus':cpus})
    here = Path(__file__).resolve().parent
    return {'schema':REPORT_SCHEMA,'status':'model_only','input_schema':SCHEMA,
            'input_sha256':input_sha256,'partition_cpus':list(partition.cpus),
            'active_cpus':active,'unused_cpus':[c for c in partition.cpus if c not in active],
            'horizon_ns':horizon,'dbf_horizon_ns':dbf_horizon,'reservation_admission':admission,
            'dbf_by_cpu':diagnostics,'policies':results,
            'model_source_sha256':{name:hashlib.sha256((here/name).read_bytes()).hexdigest()
                                   for name in ('model.py','replay.py','partitioned.py')},
            'limitations':['Independent fixed-CPU logical timelines; not physical SMP evidence.',
                           'No migration, shared locks/resources, IRQ, PI, CPU interference or scheduling overhead.',
                           'reservation_edf is fixed-calendar shared-task budget, not Linux CBS.',
                           'Admission is ideal budget admission; finite DBF is diagnostic only.',
                           'Per-CPU clocks share logical origin; no cross-CPU completion guarantee.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--policies',default='edf')
    parser.add_argument('--quantum-ns',type=int)
    args = parser.parse_args(argv)
    try:
        output = args.output.absolute()
        if os.path.lexists(output):
            raise ValueError('output must not already exist and must not alias input')
        document, raw = _json(args.input)
        report = build_report(document,hashlib.sha256(raw).hexdigest(),
                              tuple(args.policies.split(',')),args.quantum_ns)
        payload = json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n'
        encoded = payload.encode('utf-8')
        output.parent.mkdir(parents=True,exist_ok=True)
        with output.open('xb') as stream:
            stream.write(encoded)
        return 0
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        print(f'partitioned_model_error: {exc}',file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
