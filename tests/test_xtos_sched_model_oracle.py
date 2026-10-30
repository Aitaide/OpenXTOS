"""Independent unit-tick oracle tests for the deterministic M1 model.

The reference scheduler below advances one logical time unit at a time.  It
keeps its own ready queues, worker backlogs, budgets, and RR slices so that
these tests do not reproduce the event-driven implementation in model.py.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import random
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "xtos-sched-model"))

from model import (  # noqa: E402
    TaskSpec,
    dbf_demand_ns,
    dbf_report,
    simulate_edf,
)

POLICIES = ("edf", "fifo", "rr", "reservation_edf")


@dataclass
class _TickJob:
    task: TaskSpec
    index: int
    release: int
    remaining: int
    start: int | None = None
    finish: int | None = None
    used: int = 0
    epochs: set[int] | None = None
    budget_exhausted: bool = False
    final_budget: int | None = None
    final_epoch: int | None = None

    def __post_init__(self) -> None:
        self.epochs = set()

    @property
    def deadline(self) -> int:
        return self.release + self.task.deadline_ns


@dataclass(frozen=True)
class _OracleResult:
    jobs: tuple[dict[str, object], ...]
    occupancy: tuple[tuple[str, int] | None, ...]


def _jobs_for(tasks: tuple[TaskSpec, ...], horizon: int) -> list[_TickJob]:
    jobs = [
        _TickJob(task, index, release, task.execution_ns)
        for task in tasks
        for index, release in enumerate(range(task.offset_ns, horizon, task.period_ns))
    ]
    return sorted(jobs, key=lambda job: (job.release, job.task.name, job.index))


def _first_highest(ready: list[str], tasks: dict[str, TaskSpec], eligible: set[str]) -> str:
    highest = max((tasks[name].priority or 1) for name in ready if name in eligible)
    return next(name for name in ready
                if name in eligible and (tasks[name].priority or 1) == highest)


def _unit_tick_oracle(
    tasks: tuple[TaskSpec, ...],
    horizon: int,
    policy: str,
    quantum: int | None,
) -> _OracleResult:
    """Simulate one CPU one unit at a time, including the drain phase."""
    jobs = _jobs_for(tasks, horizon)
    if not jobs:
        return _OracleResult((), ())

    task_by_name = {task.name: task for task in tasks}
    releases: dict[int, list[_TickJob]] = defaultdict(list)
    for job in jobs:
        releases[job.release].append(job)
    for at in releases:
        releases[at].sort(key=lambda job: (job.release, job.task.name, job.index))

    waiting = {task.name: deque() for task in tasks}
    ready: list[str] = []
    budgets = {task.name: 0 for task in tasks}
    epochs: dict[str, int | None] = {task.name: None for task in tasks}
    slices = {task.name: quantum for task in tasks}
    occupancy: list[tuple[str, int] | None] = []
    current: str | None = None
    completed = 0
    now = 0
    reservation = policy == "reservation_edf"

    while completed < len(jobs):
        # Calendar resets happen at the unit boundary, before that boundary's
        # releases.  There is no budget available before a task's first offset.
        if reservation:
            for task in tasks:
                if now >= task.offset_ns and (now - task.offset_ns) % task.period_ns == 0:
                    budgets[task.name] = task.runtime_ns
                    epochs[task.name] = (now - task.offset_ns) // task.period_ns

        # Prior-unit completion and quantum rotation have already been applied
        # below, so arrivals at this boundary are appended after them.
        for job in releases.get(now, ()):
            queue = waiting[job.task.name]
            became_ready = not queue
            queue.append(job)
            if became_ready:
                ready.append(job.task.name)

        # A released head can be throttled before its first CPU unit when an
        # earlier job consumed the shared server budget. Record the whole
        # unit of blocked eligibility, not just depletion by the running job.
        if reservation:
            for name in ready:
                if budgets[name] == 0:
                    waiting[name][0].budget_exhausted = True
        eligible = {
            name for name in ready
            if not reservation or budgets[name] > 0
        }
        if not eligible:
            occupancy.append(None)
            now += 1
            if now > 10000:
                raise AssertionError("oracle did not drain its finite backlog")
            continue

        if policy in ("edf", "reservation_edf"):
            name = min(
                eligible,
                key=lambda item: (
                    waiting[item][0].deadline,
                    item,
                    waiting[item][0].index,
                ),
            )
        elif policy == "fifo":
            # A FIFO worker keeps the CPU across same-priority arrivals.  A
            # higher-priority ready worker can preempt it at this boundary.
            highest = max((task_by_name[item].priority or 1) for item in eligible)
            if (current in eligible
                    and (task_by_name[current].priority or 1) >= highest):
                name = current
            else:
                name = _first_highest(ready, task_by_name, eligible)
        elif policy == "rr":
            if quantum is None:
                raise AssertionError("RR oracle requires a quantum")
            name = _first_highest(ready, task_by_name, eligible)
        else:
            raise AssertionError(f"unknown oracle policy: {policy}")

        current = name if policy == "fifo" else current
        job = waiting[name][0]
        if job.start is None:
            job.start = now
        job.remaining -= 1
        job.used += 1
        occupancy.append((name, job.index))

        if reservation:
            budgets[name] -= 1
            epoch = epochs[name]
            if epoch is None:
                raise AssertionError("a reservation job ran before its offset")
            job.epochs.add(epoch)

        if policy == "rr":
            slices[name] -= 1

        if job.remaining == 0:
            job.finish = now + 1
            job.final_budget = budgets[name] if reservation else None
            job.final_epoch = epochs[name] if reservation else None
            waiting[name].popleft()
            completed += 1
            if not waiting[name]:
                ready.remove(name)
                slices[name] = quantum
                if policy == "fifo" and current == name:
                    current = None

        # Exact-budget completion is a completion, not an exhausted job.
        if reservation and job.remaining > 0 and budgets[name] == 0:
            job.budget_exhausted = True

        # This is deliberately after completion and before the next loop's
        # releases, which enforces the specified boundary ordering.
        if policy == "rr" and slices[name] == 0:
            if name in ready:
                ready.remove(name)
                ready.append(name)
            slices[name] = quantum

        now += 1
        if now > 10000:
            raise AssertionError("oracle did not drain its finite backlog")

    records = []
    for job in sorted(jobs, key=lambda item: (item.task.name, item.index)):
        if job.finish is None or job.start is None:
            raise AssertionError("oracle left a job unfinished")
        records.append({
            "task": job.task.name,
            "job_index": job.index,
            "release_ns": job.release,
            "deadline_ns": job.deadline,
            "execution_ns": job.task.execution_ns,
            "budget_used_ns": job.used,
            "budget_remaining_ns": job.final_budget if reservation else None,
            "replenishment_ns": (
                job.task.offset_ns + job.final_epoch * job.task.period_ns
                if reservation and job.final_epoch is not None else None
            ),
            "start_ns": job.start,
            "finish_ns": job.finish,
            "deadline_missed": job.finish > job.deadline,
            "budget_exhausted": job.budget_exhausted,
            "replenishments": max(0, len(job.epochs) - 1),
        })
    return _OracleResult(tuple(records), tuple(occupancy))


def _expanded_segments(result: object) -> tuple[tuple[str, int] | None, ...]:
    jobs = getattr(result, "jobs")
    segments = getattr(result, "segments")
    end = max(
        [job.finish_ns for job in jobs]
        + [segment.end_ns for segment in segments]
        + [0]
    )
    occupancy: list[tuple[str, int] | None] = [None] * end
    for segment in segments:
        if segment.start_ns < 0 or segment.end_ns <= segment.start_ns:
            raise AssertionError(f"invalid segment: {segment!r}")
        if segment.end_ns > end:
            raise AssertionError(f"segment exceeds result end: {segment!r}")
        for tick in range(segment.start_ns, segment.end_ns):
            if occupancy[tick] is not None:
                raise AssertionError(f"overlapping segments at tick {tick}")
            occupancy[tick] = (segment.task, segment.job_index)
    return tuple(occupancy)


def _assert_model_matches_oracle(
    case: unittest.TestCase,
    tasks: tuple[TaskSpec, ...],
    horizon: int,
    policy: str,
    quantum: int | None,
) -> None:
    expected = _unit_tick_oracle(tasks, horizon, policy, quantum)
    result = simulate_edf(
        tasks,
        horizon_ns=horizon,
        cpu=0,
        policy=policy,
        quantum_ns=quantum if policy == "rr" else None,
    )

    case.assertEqual(result.cpu, 0)
    case.assertEqual(result.policy, policy)
    case.assertEqual(len(result.jobs), len(expected.jobs))
    case.assertEqual(
        _expanded_segments(result),
        expected.occupancy,
        "expanded CPU occupancy differs from the independent unit-tick oracle",
    )

    actual_jobs = {
        (job.task, job.job_index): {
            "task": job.task,
            "job_index": job.job_index,
            "release_ns": job.release_ns,
            "deadline_ns": job.deadline_ns,
            "execution_ns": job.execution_ns,
            "budget_used_ns": job.budget_used_ns,
            "budget_remaining_ns": job.budget_remaining_ns,
            "replenishment_ns": job.replenishment_ns,
            "start_ns": job.start_ns,
            "finish_ns": job.finish_ns,
            "deadline_missed": job.deadline_missed,
            "budget_exhausted": job.budget_exhausted,
            "replenishments": job.replenishments,
        }
        for job in result.jobs
    }
    expected_jobs = {(job["task"], job["job_index"]): job for job in expected.jobs}
    case.assertEqual(set(actual_jobs), set(expected_jobs), "job loss or duplication")
    for key in sorted(expected_jobs):
        case.assertEqual(actual_jobs[key], expected_jobs[key], f"job result mismatch for {key}")

    case.assertEqual(
        result.deadline_misses,
        sum(bool(job["deadline_missed"]) for job in expected.jobs),
    )
    case.assertEqual(
        result.budget_exhaustions,
        sum(bool(job["budget_exhausted"]) for job in expected.jobs),
    )


def _adversarial_workloads() -> list[tuple[str, tuple[TaskSpec, ...], int, int]]:
    """Small fixtures that place events on exact unit boundaries."""
    return [
        (
            "edf_absolute_deadline_tie",
            (
                TaskSpec("zeta", 0, 1, 4, 7, work_ns=2),
                TaskSpec("alpha", 0, 1, 4, 7, work_ns=2),
            ),
            10,
            2,
        ),
        (
            "edf_constrained_deadlines_and_offsets",
            (
                TaskSpec("late", 0, 1, 3, 8, work_ns=3, offset_ns=2),
                TaskSpec("early", 0, 2, 5, 9, work_ns=2, offset_ns=0),
            ),
            15,
            3,
        ),
        (
            "fifo_higher_priority_release",
            (
                TaskSpec("low", 0, 1, 20, 20, work_ns=8, priority=10),
                TaskSpec("high", 0, 1, 10, 10, work_ns=2, priority=20, offset_ns=3),
            ),
            12,
            2,
        ),
        (
            "fifo_same_priority_no_preemption",
            (
                TaskSpec("first", 0, 1, 20, 20, work_ns=7, priority=40),
                TaskSpec("second", 0, 1, 20, 20, work_ns=2, priority=40, offset_ns=2),
            ),
            9,
            2,
        ),
        (
            "rr_leftover_slice_after_preemption",
            (
                TaskSpec("low", 0, 1, 20, 20, work_ns=8, priority=10),
                TaskSpec("high", 0, 1, 7, 7, work_ns=2, priority=20, offset_ns=2),
            ),
            13,
            3,
        ),
        (
            "rr_same_priority_rotation_and_arrival",
            (
                TaskSpec("beta", 0, 1, 20, 20, work_ns=5, priority=30),
                TaskSpec("alpha", 0, 1, 20, 20, work_ns=3, priority=30, offset_ns=1),
                TaskSpec("gamma", 0, 1, 20, 20, work_ns=2, priority=30, offset_ns=2),
            ),
            12,
            2,
        ),
        (
            "reservation_shared_budget_backlog",
            (TaskSpec("server", 0, 2, 6, 6, work_ns=5),),
            16,
            2,
        ),
        (
            "reservation_exact_budget_completion",
            (
                TaskSpec("exact", 0, 3, 8, 8, work_ns=3),
                TaskSpec("other", 0, 1, 5, 5, work_ns=2, offset_ns=1),
            ),
            13,
            2,
        ),
        (
            "reservation_unrelated_replenishment",
            (
                TaskSpec("a", 0, 2, 7, 7, work_ns=4, offset_ns=0),
                TaskSpec("b", 0, 1, 5, 5, work_ns=3, offset_ns=2),
            ),
            17,
            2,
        ),
        (
            "offset_calendar_and_horizon_boundary",
            (
                TaskSpec("offset", 0, 2, 6, 6, work_ns=4, offset_ns=3),
                TaskSpec("at_horizon", 0, 1, 4, 4, work_ns=1, offset_ns=8),
            ),
            8,
            1,
        ),
        (
            "all_jobs_drain_after_horizon",
            (
                TaskSpec("drain", 0, 2, 5, 5, work_ns=7),
                TaskSpec("short", 0, 1, 4, 4, work_ns=1, offset_ns=1),
            ),
            10,
            3,
        ),
        (
            "simultaneous_arrivals_sorted_by_name",
            (
                TaskSpec("charlie", 0, 1, 9, 9, work_ns=2, priority=50),
                TaskSpec("bravo", 0, 1, 9, 9, work_ns=2, priority=50),
                TaskSpec("alpha", 0, 1, 9, 9, work_ns=2, priority=50),
            ),
            9,
            1,
        ),
    ]


def _random_workloads() -> list[tuple[str, tuple[TaskSpec, ...], int, int]]:
    rng = random.Random(0x4D31_2026)
    workloads = []
    for case_index in range(36):
        horizon = rng.randint(8, 22)
        task_count = rng.randint(1, 3)
        tasks = []
        for task_index in range(task_count):
            period = rng.randint(3, 8)
            runtime = rng.randint(1, min(3, period))
            deadline = rng.randint(runtime, period)
            offset = rng.randint(0, period - 1)
            priority = rng.randint(5, 95)
            if rng.random() < 0.35:
                work = None
            else:
                work = rng.randint(runtime, min(2 * runtime + 2, 8))
            tasks.append(TaskSpec(
                f"r{case_index:02d}_t{task_index}",
                0,
                runtime,
                deadline,
                period,
                work_ns=work,
                priority=priority,
                offset_ns=offset,
            ))
        workloads.append((
            f"random_{case_index:02d}",
            tuple(tasks),
            horizon,
            rng.randint(1, 4),
        ))
    return workloads


class SchedulerOracleTests(unittest.TestCase):
    def test_oracle_marks_head_waiting_for_previous_jobs_shared_budget(self):
        tasks = (
            TaskSpec('a', 0, 2, 2, 4, work_ns=6),
            TaskSpec('b', 0, 1, 3, 5, offset_ns=4),
            TaskSpec('c', 0, 3, 4, 5, work_ns=6),
        )
        result = _unit_tick_oracle(tasks, 15, 'reservation_edf', None)
        job = next(j for j in result.jobs if j['task'] == 'b' and j['job_index'] == 1)
        # b0 finishes at 15 using its epoch's last unit. b1 is already
        # released and becomes head, blocked until the reset at 19.
        self.assertEqual(next(j['finish_ns'] for j in result.jobs
                              if j['task'] == 'b' and j['job_index'] == 0), 15)
        self.assertTrue(job['budget_exhausted'])

    def test_unit_tick_oracle_covers_adversarial_and_random_policy_cases(self):
        workloads = _adversarial_workloads() + _random_workloads()
        case_count = 0
        for label, tasks, horizon, quantum in workloads:
            for policy in POLICIES:
                case_count += 1
                with self.subTest(workload=label, policy=policy):
                    _assert_model_matches_oracle(
                        self, tasks, horizon, policy, quantum if policy == "rr" else None
                    )
        self.assertGreaterEqual(case_count, 100)

    def test_dbf_points_and_demands_match_independent_formula(self):
        workloads = _adversarial_workloads()[:4] + _random_workloads()[:12]
        for label, tasks, horizon, _quantum in workloads:
            with self.subTest(workload=label):
                points = {horizon}
                for task in tasks:
                    points.update(range(task.deadline_ns, horizon + 1, task.period_ns))
                expected_points = tuple(sorted(points))
                expected_demands = {
                    point: sum(
                        max(0, (point - task.deadline_ns) // task.period_ns + 1)
                        * task.runtime_ns
                        for task in tasks
                    )
                    for point in expected_points
                }

                report = dbf_report(tasks, horizon_ns=horizon)
                self.assertEqual(report.checked_horizons, expected_points)
                self.assertEqual(report.max_demand_ns, max(expected_demands.values()))
                self.assertEqual(
                    report.violating_horizons,
                    tuple(point for point in expected_points
                          if expected_demands[point] > point),
                )
                self.assertEqual(report.feasible, not report.violating_horizons)
                self.assertEqual(report.scope, "finite_diagnostic_only")
                for point, demand in expected_demands.items():
                    self.assertEqual(dbf_demand_ns(tasks, point), demand)

    def test_dbf_step_points_extend_deadline_point_set_without_changing_formula(self):
        tasks = (
            TaskSpec("a", 0, 2, 3, 7),
            TaskSpec("b", 0, 1, 5, 9),
        )
        horizon = 20
        step = 4
        points = {horizon}
        for task in tasks:
            points.update(range(task.deadline_ns, horizon + 1, task.period_ns))
        points.update(range(step, horizon + 1, step))
        report = dbf_report(tasks, horizon_ns=horizon, step_ns=step)
        self.assertEqual(report.checked_horizons, tuple(sorted(points)))
        for point in report.checked_horizons:
            expected = sum(
                max(0, (point - task.deadline_ns) // task.period_ns + 1)
                * task.runtime_ns
                for task in tasks
            )
            self.assertEqual(dbf_demand_ns(tasks, point), expected)


if __name__ == "__main__":
    unittest.main()
