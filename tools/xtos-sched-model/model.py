"""Bounded, deterministic scheduling models. Logical time, never kernel timing.

Admission covers ideal independent implicit-deadline reservations on a full
CPU only. Finite DBF is diagnostic. reservation_edf is a fixed-calendar task
server, explicitly not Linux CBS/SCHED_DEADLINE.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
from threading import RLock
from typing import Iterable

MAX_TASKS = 128
MAX_JOBS = 4096
MAX_EVENTS = 100000
MAX_DBF_POINTS = 10000
MAX_NS = (1 << 63) - 1
POLICIES = ("edf", "fifo", "rr", "reservation_edf")


def _integer(value: int, name: str, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= MAX_NS:
        raise ValueError(f"{name} must be an integer in [{minimum}, {MAX_NS}]")
    return value


@dataclass(frozen=True, slots=True)
class TaskSpec:
    name: str
    cpu: int
    runtime_ns: int
    deadline_ns: int
    period_ns: int
    work_ns: int | None = None
    priority: int | None = None
    offset_ns: int = 0
    work_profile_ns: tuple[int, ...] | None = None

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name or len(self.name) > 128:
            raise ValueError("task name must contain 1..128 characters")
        _integer(self.cpu, "cpu", 0)
        for key in ("runtime_ns", "deadline_ns", "period_ns"):
            _integer(getattr(self, key), key)
        if not self.runtime_ns <= self.deadline_ns <= self.period_ns:
            raise ValueError("require runtime_ns <= deadline_ns <= period_ns")
        if self.work_ns is not None:
            _integer(self.work_ns, "work_ns")
        if self.priority is not None and (type(self.priority) is not int or not 1 <= self.priority <= 99):
            raise ValueError("priority must be an integer in 1..99")
        _integer(self.offset_ns, "offset_ns", 0)
        if self.work_profile_ns is not None:
            if (not isinstance(self.work_profile_ns, tuple) or
                    not 1 <= len(self.work_profile_ns) <= MAX_JOBS or self.work_ns is not None):
                raise ValueError("work_profile_ns must be a bounded nonempty tuple, exclusive with work_ns")
            for value in self.work_profile_ns:
                _integer(value, "work_profile_ns item")

    @property
    def utilization(self):
        return Fraction(self.runtime_ns, self.period_ns)

    @property
    def execution_ns(self):
        return self.runtime_ns if self.work_ns is None else self.work_ns


def _tasks(tasks: Iterable[TaskSpec]) -> tuple[TaskSpec, ...]:
    # Consume boundedly, including arbitrary caller-supplied iterators.
    result = []
    for task in tasks:
        if not isinstance(task, TaskSpec):
            raise ValueError("tasks must contain TaskSpec objects")
        result.append(task)
        if len(result) > MAX_TASKS:
            raise ValueError("task limit exceeded")
    if not result or len({task.name for task in result}) != len(result):
        raise ValueError("tasks must be nonempty with unique names")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class CpuPartition:
    cpus: tuple[int, ...]

    def __init__(self, cpus):
        values = []
        for cpu in cpus:
            _integer(cpu, "cpu", 0)
            values.append(cpu)
            if len(values) > MAX_TASKS:
                raise ValueError("CPU count limit exceeded")
        if not values or len(set(values)) != len(values):
            raise ValueError("partition CPUs must be nonempty and unique")
        object.__setattr__(self, "cpus", tuple(sorted(values)))


class AdmissionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AdmissionReport:
    accepted: bool
    utilization_num: int
    utilization_den: int
    task_names: tuple[str, ...]
    mode: str
    utilization_by_cpu: tuple[tuple[int, int, int], ...]

    @property
    def utilization(self):
        return Fraction(self.utilization_num, self.utilization_den)


class AdmissionController:
    """Atomic idealized admission; no hardware or Linux policy approval."""

    def __init__(self, partition: CpuPartition):
        if not isinstance(partition, CpuPartition):
            raise ValueError("partition must be CpuPartition")
        self._partition = partition
        self._lock = RLock()
        self._tasks: dict[str, TaskSpec] = {}

    @property
    def partition(self):
        return self._partition

    def tasks(self):
        with self._lock:
            return tuple(self._tasks.values())

    def utilization(self):
        with self._lock:
            return sum((task.utilization for task in self._tasks.values()), Fraction(0))

    def admit(self, task, *, allow_dbf=False, dbf_horizon_ns=None):
        return self.admit_all([task], allow_dbf=allow_dbf, dbf_horizon_ns=dbf_horizon_ns)

    def admit_all(self, tasks, *, allow_dbf=False, dbf_horizon_ns=None):
        incoming = _tasks(tasks)
        with self._lock:
            if any(task.name in self._tasks for task in incoming):
                raise AdmissionError("task name already admitted")
            candidate = _tasks((*self._tasks.values(), *incoming))
            for task in candidate:
                if task.cpu not in self.partition.cpus:
                    raise AdmissionError("task outside CPU partition")
                if task.deadline_ns != task.period_ns:
                    raise AdmissionError("constrained deadline not admitted; finite DBF is diagnostic only")
            by_cpu = []
            for cpu in self.partition.cpus:
                total = sum((task.utilization for task in candidate if task.cpu == cpu), Fraction(0))
                if total > 1:
                    raise AdmissionError("per-CPU reservation utilization exceeds one")
                by_cpu.append((cpu, total.numerator, total.denominator))
            total = sum((task.utilization for task in candidate), Fraction(0))
            report = AdmissionReport(True, total.numerator, total.denominator,
                                     tuple(task.name for task in incoming),
                                     "ideal_implicit_deadline", tuple(by_cpu))
            self._tasks.update((task.name, task) for task in incoming)
            return report


@dataclass(frozen=True, slots=True)
class DbFReport:
    feasible: bool
    horizon_ns: int
    checked_horizons: tuple[int, ...]
    max_demand_ns: int
    violating_horizons: tuple[int, ...]
    scope: str = "finite_diagnostic_only"


def dbf_demand_ns(tasks, horizon_ns):
    _integer(horizon_ns, "horizon_ns", 0)
    values = _tasks(tasks)
    if len({task.cpu for task in values}) != 1:
        raise ValueError("DBF requires one CPU")
    # Sporadic demand bound: independent of phase, pessimistic for offsets.
    return sum(max(0, (horizon_ns-task.deadline_ns)//task.period_ns+1)*task.runtime_ns
               for task in values)


def dbf_report(tasks, *, horizon_ns, step_ns=None):
    values = _tasks(tasks)
    _integer(horizon_ns, "horizon_ns")
    if len({task.cpu for task in values}) != 1:
        raise ValueError("DBF requires one CPU")
    count = sum(max(0, (horizon_ns-t.deadline_ns)//t.period_ns+1) for t in values)
    if step_ns is not None:
        _integer(step_ns, "step_ns")
        count += horizon_ns//step_ns
    if count > MAX_DBF_POINTS:
        raise ValueError("DBF point limit exceeded")
    points = {horizon_ns}
    for task in values:
        points.update(range(task.deadline_ns, horizon_ns+1, task.period_ns))
    if step_ns is not None:
        points.update(range(step_ns, horizon_ns+1, step_ns))
    checked = tuple(sorted(points))
    if len(checked) > MAX_DBF_POINTS:
        raise ValueError("DBF point limit exceeded")
    demands = [dbf_demand_ns(values, point) for point in checked]
    violations = tuple(point for point,demand in zip(checked, demands) if demand > point)
    return DbFReport(not violations, horizon_ns, checked, max(demands), violations)


@dataclass(frozen=True, slots=True)
class JobResult:
    task: str
    job_index: int
    release_ns: int
    deadline_ns: int
    execution_ns: int
    budget_used_ns: int
    budget_remaining_ns: int | None
    replenishment_ns: int | None
    start_ns: int
    finish_ns: int
    deadline_missed: bool
    budget_exhausted: bool
    replenishments: int


@dataclass(frozen=True, slots=True)
class Segment:
    task: str
    job_index: int
    start_ns: int
    end_ns: int


@dataclass(frozen=True, slots=True)
class SimulationResult:
    jobs: tuple[JobResult, ...]
    segments: tuple[Segment, ...]
    deadline_misses: int
    budget_exhaustions: int
    horizon_ns: int
    cpu: int
    policy: str


@dataclass(slots=True, eq=False)
class _Job:
    task: TaskSpec
    index: int
    release: int
    remaining: int
    start: int | None = None
    finish: int | None = None
    used: int = 0
    throttled: bool = False
    epochs: set = field(default_factory=set)
    final_budget: int | None = None
    final_epoch: int | None = None

    @property
    def deadline(self):
        return self.release + self.task.deadline_ns


def simulate_edf(tasks, *, horizon_ns, cpu=0, policy="edf", quantum_ns=None):
    """Generate periodic jobs before horizon; drain the finite backlog after it.

    Each task is a serial worker. reservation_edf shares one reset-at-period
    budget across all its jobs. Other policies have no reservation throttling.
    Event/job bounds fail explicitly rather than returning partial success.
    """
    values = _tasks(tasks)
    jobs = _prepare_jobs(values, horizon_ns, cpu, policy, quantum_ns)
    return _replay(values, jobs, horizon_ns, cpu, policy, quantum_ns)


def _prepare_jobs(values, horizon_ns, cpu, policy, quantum_ns):
    _integer(horizon_ns, "horizon_ns")
    _integer(cpu, "cpu", 0)
    if policy not in POLICIES:
        raise ValueError("unknown model policy")
    if policy == "rr":
        _integer(quantum_ns, "quantum_ns")
    elif quantum_ns is not None:
        raise ValueError("quantum_ns is valid only for rr")
    if any(task.cpu != cpu for task in values):
        raise ValueError("tasks must belong to simulation CPU")
    count = sum(max(0,(horizon_ns-1-t.offset_ns)//t.period_ns+1) for t in values)
    if count > MAX_JOBS:
        raise ValueError("job limit exceeded")
    for t in values:
        expected = max(0,(horizon_ns-1-t.offset_ns)//t.period_ns+1)
        if expected and t.offset_ns+(expected-1)*t.period_ns+t.deadline_ns > MAX_NS:
            raise ValueError("job deadline exceeds integer time range")
        if t.work_profile_ns is not None and len(t.work_profile_ns) != expected:
            raise ValueError("work profile length must match all released jobs")
    return [_Job(t,i,release,t.work_profile_ns[i] if t.work_profile_ns else t.execution_ns) for t in values
            for i,release in enumerate(range(t.offset_ns,horizon_ns,t.period_ns))]


@dataclass(frozen=True, slots=True)
class PartitionedResult:
    partition_cpus: tuple[int, ...]
    cpus: tuple[SimulationResult, ...]
    job_count: int
    deadline_misses: int
    budget_exhaustions: int
    horizon_ns: int
    policy: str


def simulate_partitioned(tasks, *, partition, horizon_ns, policy="edf", quantum_ns=None):
    """Independent fixed-CPU models; not shared-resource or migration simulation."""
    values = _tasks(tasks)
    if not isinstance(partition, CpuPartition):
        raise ValueError("partition must be CpuPartition")
    if any(t.cpu not in partition.cpus for t in values):
        raise ValueError("task outside CPU partition")
    _integer(horizon_ns, "horizon_ns")
    count = sum(max(0, (horizon_ns-1-t.offset_ns)//t.period_ns+1) for t in values)
    if count > MAX_JOBS:
        raise ValueError("global job limit exceeded")
    prepared = []
    for cpu in partition.cpus:
        selected = tuple(t for t in values if t.cpu == cpu)
        if selected:
            prepared.append((cpu, selected,
                             _prepare_jobs(selected, horizon_ns, cpu, policy, quantum_ns)))
    event_budget = [0]
    cpus = tuple(_replay(selected, jobs, horizon_ns, cpu, policy, quantum_ns, event_budget)
                 for cpu, selected, jobs in prepared)
    return PartitionedResult(partition.cpus, cpus, sum(len(r.jobs) for r in cpus),
                             sum(r.deadline_misses for r in cpus),
                             sum(r.budget_exhaustions for r in cpus), horizon_ns, policy)


def _replay(values, jobs, horizon_ns, cpu, policy, quantum_ns, event_budget=None):
    pending = sorted(jobs, key=lambda j:(j.release,j.task.name,j.index))
    waiting = {t.name: deque() for t in values}
    ready = []  # task ids; same-priority insertion order implements FIFO/RR
    specs = {t.name:t for t in values}
    budgets = {t.name:t.runtime_ns for t in values}
    epochs = {t.name:0 for t in values}
    slices = {t.name:quantum_ns for t in values}
    now = cursor = completed = events = 0
    segments = []
    reservation = policy == "reservation_edf"
    while completed < len(jobs):
        events += 1
        if event_budget is not None:
            event_budget[0] += 1
            if event_budget[0] > MAX_EVENTS:
                raise ValueError("global replay event limit exceeded")
        if events > MAX_EVENTS or now > MAX_NS:
            raise ValueError("replay event/time limit exceeded")
        if reservation:
            for t in values:
                epoch = max(0,(now-t.offset_ns)//t.period_ns)
                if epoch != epochs[t.name]:
                    budgets[t.name] = t.runtime_ns
                    epochs[t.name] = epoch
        while cursor < len(pending) and pending[cursor].release <= now:
            job = pending[cursor]
            waiting[job.task.name].append(job)
            cursor += 1
        for name in sorted(waiting):
            if waiting[name] and name not in ready:
                ready.append(name)
        if reservation:
            for name in ready:
                if budgets[name] == 0:
                    waiting[name][0].throttled = True
        eligible = [name for name in ready if not reservation or budgets[name] > 0]
        if not eligible:
            future = [pending[cursor].release] if cursor < len(pending) else []
            if reservation:
                future.extend(specs[n].offset_ns+(epochs[n]+1)*specs[n].period_ns
                              for n in ready)
            if not future:
                raise RuntimeError("replay cannot make progress")
            now = min(future)
            continue
        if policy in ("edf","reservation_edf"):
            name = min(eligible, key=lambda n:(waiting[n][0].deadline,n,waiting[n][0].index))
        else:
            name = max(eligible, key=lambda n:specs[n].priority or 1)
        job = waiting[name][0]
        if job.start is None:
            job.start = now
        run_for = job.remaining
        if cursor < len(pending):
            run_for = min(run_for,pending[cursor].release-now)
        if reservation:
            run_for = min(run_for,budgets[name])
            # Budget replenishment of another task can preempt this task.
            run_for = min(run_for,min(specs[n].offset_ns+(epochs[n]+1)*specs[n].period_ns-now
                                      for n in ready))
        if policy == "rr":
            run_for = min(run_for,slices[name])
        if run_for <= 0:
            raise RuntimeError("nonpositive execution interval")
        if now+run_for > MAX_NS:
            raise ValueError("finish exceeds integer time range")
        segments.append(Segment(name,job.index,now,now+run_for))
        job.remaining -= run_for
        job.used += run_for
        if reservation:
            budgets[name] -= run_for
            job.epochs.add(epochs[name])
        if policy == "rr":
            slices[name] -= run_for
        now += run_for
        if job.remaining == 0:
            job.finish = now
            job.final_budget = budgets[name] if reservation else None
            job.final_epoch = epochs[name] if reservation else None
            waiting[name].popleft()
            completed += 1
            if not waiting[name]:
                ready.remove(name)
                slices[name] = quantum_ns
        elif reservation and budgets[name] == 0:
            job.throttled = True
        if policy == "rr" and slices[name] == 0:
            if name in ready:
                ready.remove(name)
                ready.append(name)
            slices[name] = quantum_ns
    result = []
    for job in sorted(jobs,key=lambda j:(j.task.name,j.index)):
        result.append(JobResult(job.task.name,job.index,job.release,job.deadline,
                                job.used,job.used,job.final_budget,
                                (job.task.offset_ns+job.final_epoch*job.task.period_ns)
                                if reservation else None,
                                job.start,job.finish,job.finish>job.deadline,
                                job.throttled,max(0,len(job.epochs)-1)))
    return SimulationResult(tuple(result),tuple(segments),
                            sum(j.deadline_missed for j in result),
                            sum(j.budget_exhausted for j in result),horizon_ns,cpu,policy)
