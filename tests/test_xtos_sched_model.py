"""M1 user-space scheduler model tests."""
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "xtos-sched-model"))

from model import (  # noqa: E402
    AdmissionController,
    AdmissionError,
    CpuPartition,
    TaskSpec,
    dbf_demand_ns,
    dbf_report,
    simulate_edf,
)


class ContractTests(unittest.TestCase):
    def test_task_contract_rejects_invalid_integer_and_timing_values(self):
        cases = [
            {"runtime_ns": 0, "deadline_ns": 5, "period_ns": 10},
            {"runtime_ns": 6, "deadline_ns": 0, "period_ns": 10},
            {"runtime_ns": 6, "deadline_ns": 11, "period_ns": 10},
            {"runtime_ns": 12, "deadline_ns": 10, "period_ns": 10},
            {"runtime_ns": True, "deadline_ns": 5, "period_ns": 10},
        ]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                TaskSpec(name="bad", cpu=0, **values)

    def test_priority_is_a_bounded_integer_for_native_rt_policies(self):
        with self.assertRaises(ValueError):
            TaskSpec("bad-low", 0, 1, 5, 5, priority=0)
        with self.assertRaises(ValueError):
            TaskSpec("bad-high", 0, 1, 5, 5, priority=100)

    def test_partition_rejects_task_on_unassigned_cpu(self):
        controller = AdmissionController(CpuPartition((2,)))
        task = TaskSpec("wrong-cpu", 1, 1, 10, 10)
        with self.assertRaises(AdmissionError):
            controller.admit(task)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.controller = AdmissionController(CpuPartition((0,)))

    def test_exact_utilization_is_admitted_atomically(self):
        tasks = [TaskSpec("a", 0, 2, 10, 10), TaskSpec("b", 0, 3, 10, 10)]
        report = self.controller.admit_all(tasks)
        self.assertTrue(report.accepted)
        self.assertEqual(report.utilization_num, 1)
        self.assertEqual(report.utilization_den, 2)
        self.assertEqual(tuple(t.name for t in self.controller.tasks()), ("a", "b"))

    def test_over_capacity_is_rejected_without_partial_commit(self):
        self.controller.admit(TaskSpec("existing", 0, 8, 10, 10))
        with self.assertRaises(AdmissionError):
            self.controller.admit_all([TaskSpec("new-a", 0, 2, 10, 10),
                                       TaskSpec("new-b", 0, 1, 10, 10)])
        self.assertEqual(tuple(t.name for t in self.controller.tasks()), ("existing",))

    def test_concurrent_admission_cannot_oversell_capacity(self):
        def attempt(index):
            task = TaskSpec(f"t{index}", 0, 6, 10, 10)
            try:
                self.controller.admit(task)
                return True
            except AdmissionError:
                return False

        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = list(pool.map(attempt, range(8)))
        self.assertEqual(sum(accepted), 1)
        self.assertLessEqual(self.controller.utilization().numerator,
                             self.controller.utilization().denominator)

    def test_multi_cpu_capacity_is_checked_per_cpu(self):
        controller = AdmissionController(CpuPartition((0, 1)))
        report = controller.admit_all([
            TaskSpec("cpu0", 0, 8, 10, 10),
            TaskSpec("cpu1", 1, 8, 10, 10),
        ])
        self.assertTrue(report.accepted)
        self.assertEqual(report.utilization_by_cpu, ((0, 4, 5), (1, 4, 5)))

    def test_non_task_and_empty_input_are_rejected_without_partial_commit(self):
        controller=AdmissionController(CpuPartition((0,)))
        for values in [[], [None], [TaskSpec('a',0,1,5,5),None]]:
            with self.subTest(values=values),self.assertRaises(ValueError):
                controller.admit_all(values)
            self.assertEqual(controller.tasks(),())

    def test_constrained_deadline_requires_explicit_dbf_mode(self):
        task = TaskSpec("constrained", 0, 2, 5, 10)
        with self.assertRaises(AdmissionError):
            self.controller.admit(task)
        with self.assertRaises(AdmissionError):
            self.controller.admit(task, allow_dbf=True, dbf_horizon_ns=1)
        report = dbf_report([task], horizon_ns=30)
        self.assertTrue(report.feasible)
        self.assertEqual(dbf_demand_ns([task], 5), 2)


class SimulationTests(unittest.TestCase):
    def test_edf_uses_absolute_deadline_and_deterministic_tie_break(self):
        tasks = [
            TaskSpec("zeta", 0, 2, 5, 10),
            TaskSpec("alpha", 0, 2, 5, 10),
        ]
        result = simulate_edf(tasks, horizon_ns=10)
        self.assertEqual([segment.task for segment in result.segments[:2]], ["alpha", "zeta"])
        self.assertEqual(len(result.jobs), 2)
        self.assertTrue(all(not job.deadline_missed for job in result.jobs))

    def test_budget_debit_and_replenishment_are_visible_per_job(self):
        task = TaskSpec("periodic", 0, 3, 5, 10)
        result = simulate_edf([task], horizon_ns=25, policy="reservation_edf")
        self.assertEqual([job.budget_used_ns for job in result.jobs], [3, 3, 3])
        self.assertEqual([job.replenishment_ns for job in result.jobs], [0, 10, 20])
        self.assertTrue(all(job.budget_remaining_ns == 0 for job in result.jobs))

    def test_work_exceeding_budget_is_throttled_until_replenishment(self):
        task = TaskSpec("throttled", 0, 2, 5, 10, work_ns=5)
        result = simulate_edf([task], horizon_ns=10, policy="reservation_edf")
        job = result.jobs[0]
        self.assertEqual(job.execution_ns, 5)
        self.assertTrue(job.budget_exhausted)
        self.assertEqual(job.replenishments, 2)
        self.assertGreater(job.finish_ns, job.deadline_ns)

    def test_native_policy_modes_are_deterministic_and_reported(self):
        tasks = [TaskSpec("low", 0, 2, 10, 10, priority=10),
                 TaskSpec("high", 0, 2, 10, 10, priority=20)]
        fifo = simulate_edf(tasks, horizon_ns=10, policy="fifo")
        rr_tasks = [TaskSpec("beta", 0, 2, 10, 10, priority=20),
                    TaskSpec("alpha", 0, 2, 10, 10, priority=20)]
        rr = simulate_edf(rr_tasks, horizon_ns=10, policy="rr", quantum_ns=1)
        self.assertEqual([segment.task for segment in fifo.segments[:2]], ["high", "low"])
        self.assertEqual([segment.task for segment in rr.segments[:3]], ["alpha", "beta", "alpha"])
        self.assertEqual(fifo.policy, "fifo")
        self.assertEqual(rr.policy, "rr")

    def test_unknown_policy_and_invalid_quantum_are_rejected(self):
        task = TaskSpec("a", 0, 1, 5, 5)
        with self.assertRaises(ValueError):
            simulate_edf([task], horizon_ns=10, policy="unknown")
        with self.assertRaises(ValueError):
            simulate_edf([task], horizon_ns=10, policy="rr", quantum_ns=0)

    def test_rr_always_runs_highest_priority_before_lower_priority(self):
        result = simulate_edf([TaskSpec("high", 0, 3, 10, 10, priority=20),
                               TaskSpec("low", 0, 2, 10, 10, priority=10)],
                              horizon_ns=10, policy="rr", quantum_ns=1)
        self.assertEqual([s.task for s in result.segments[:3]], ["high"] * 3)

    def test_normal_completion_at_budget_is_not_throttling(self):
        result = simulate_edf([TaskSpec("a", 0, 2, 10, 10)], horizon_ns=10,
                              policy="reservation_edf")
        self.assertFalse(result.jobs[0].budget_exhausted)
        self.assertEqual(result.budget_exhaustions, 0)

    def test_reservation_budget_is_shared_across_backlogged_jobs(self):
        task = TaskSpec("a", 0, 2, 10, 10, work_ns=5)
        result = simulate_edf([task], horizon_ns=20, policy="reservation_edf")
        for window in range(0, 50, 10):
            used = sum(max(0, min(s.end_ns, window+10)-max(s.start_ns, window))
                       for s in result.segments)
            self.assertLessEqual(used, 2)
        self.assertEqual(result.jobs[0].finish_ns, 21)
        self.assertEqual(result.jobs[1].finish_ns, 42)

    def test_higher_priority_release_preempts_and_same_priority_does_not(self):
        tasks = [TaskSpec("low", 0, 8, 20, 20, priority=10),
                 TaskSpec("high", 0, 2, 20, 20, priority=20, offset_ns=3)]
        result = simulate_edf(tasks, horizon_ns=10, policy="fifo")
        self.assertEqual([(s.task,s.start_ns,s.end_ns) for s in result.segments],
                         [("low",0,3),("high",3,5),("low",5,10)])

    def test_variable_job_work_profile_preserves_each_job_demand(self):
        task=TaskSpec('profile',0,4,10,10,work_profile_ns=(1,3,2))
        result=simulate_edf([task],horizon_ns=30)
        self.assertEqual([j.execution_ns for j in result.jobs],[1,3,2])
        self.assertEqual([j.finish_ns for j in result.jobs],[1,13,22])
        with self.assertRaises(ValueError):
            simulate_edf([task],horizon_ns=20)

    def test_new_job_waiting_for_shared_budget_is_marked_throttled(self):
        task=TaskSpec('a',0,2,10,10,work_profile_ns=(4,1))
        result=simulate_edf([task],horizon_ns=20,policy='reservation_edf')
        self.assertTrue(result.jobs[1].budget_exhausted)
        self.assertEqual(result.jobs[1].start_ns,20)

    def test_time_overflow_is_rejected_instead_of_wrapping_or_returning_success(self):
        maximum=(1<<63)-1
        with self.assertRaises(ValueError):
            simulate_edf([TaskSpec('overflow',0,1,10,10,offset_ns=maximum-2)],horizon_ns=maximum)

    def test_duplicate_task_ids_and_huge_job_expansion_fail_closed(self):
        task = TaskSpec("a", 0, 1, 1, 1)
        with self.assertRaises(ValueError):
            simulate_edf([task, task], horizon_ns=10)
        with self.assertRaises(ValueError):
            simulate_edf([task], horizon_ns=10**12)


class DbfTests(unittest.TestCase):
    def test_all_repeated_deadlines_are_checked(self):
        tasks = [TaskSpec("a",0,1,1,2), TaskSpec("b",0,2,4,5)]
        report = dbf_report(tasks, horizon_ns=10)
        self.assertIn(3, report.checked_horizons)
        for t in range(1, 11):
            expected = sum(max(0,(t-task.deadline_ns)//task.period_ns+1)*task.runtime_ns
                           for task in tasks)
            self.assertEqual(dbf_demand_ns(tasks,t), expected)

    def test_dbf_rejects_oversized_enumeration(self):
        with self.assertRaises(ValueError):
            dbf_report([TaskSpec("a",0,1,1,1)], horizon_ns=10**12)

    def test_dbf_allows_exact_distinct_point_limit_when_horizon_is_included(self):
        report = dbf_report([TaskSpec("a", 0, 1, 1, 2)], horizon_ns=19999)
        self.assertEqual(len(report.checked_horizons), 10000)
        self.assertEqual(report.checked_horizons[-1], 19999)

    def test_dbf_rejects_distinct_horizon_that_exceeds_point_limit(self):
        with self.assertRaisesRegex(ValueError, "DBF point limit exceeded"):
            dbf_report([TaskSpec("a", 0, 1, 1, 2)], horizon_ns=20000)

    def test_overload_is_reported_as_deadline_misses_without_losing_jobs(self):
        tasks = [TaskSpec("a", 0, 8, 10, 10), TaskSpec("b", 0, 8, 10, 10)]
        result = simulate_edf(tasks, horizon_ns=10)
        self.assertEqual(len(result.jobs), 2)
        self.assertGreaterEqual(result.deadline_misses, 1)
        self.assertEqual(sum(job.budget_used_ns for job in result.jobs), 16)

    def test_cpu_partition_mismatch_is_rejected_before_simulation(self):
        with self.assertRaises(ValueError):
            simulate_edf([TaskSpec("wrong", 1, 1, 5, 5)], horizon_ns=10, cpu=0)


if __name__ == "__main__":
    unittest.main()
