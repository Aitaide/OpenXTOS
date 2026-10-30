"""Fixed-partition logical scheduling; never physical SMP evidence."""
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools/xtos-sched-model'))
from unittest.mock import patch
import model
from model import CpuPartition, TaskSpec


class PartitionedSimulationTests(unittest.TestCase):
    def test_combined_job_limit_is_checked_before_any_cpu_execution(self):
        tasks = [TaskSpec('a', 0, 1, 1, 1), TaskSpec('b', 1, 1, 1, 1)]
        with patch.object(model, '_replay', side_effect=AssertionError('executed too early')):
            with self.assertRaisesRegex(ValueError, 'global job limit'):
                model.simulate_partitioned(tasks, partition=CpuPartition((0, 1)),
                                           horizon_ns=2049)

    def test_combined_event_limit_is_not_multiplied_by_cpu_count(self):
        tasks = [TaskSpec('a', 0, 1, 1, 1), TaskSpec('b', 1, 1, 1, 1)]
        with patch.object(model, 'MAX_EVENTS', 3):
            with self.assertRaisesRegex(ValueError, 'global replay event limit'):
                model.simulate_partitioned(tasks, partition=CpuPartition((0, 1)), horizon_ns=2)

    def test_all_policies_match_independent_per_cpu_models(self):
        from dataclasses import asdict
        tasks = [TaskSpec('z', 3, 2, 5, 5, work_ns=4, priority=10),
                 TaskSpec('a', 3, 1, 4, 4, priority=20, offset_ns=1),
                 TaskSpec('b', 1, 1, 6, 6, work_profile_ns=(2, 1, 3))]
        for policy in model.POLICIES:
            with self.subTest(policy=policy):
                quantum = 2 if policy == 'rr' else None
                actual = model.simulate_partitioned(tasks, partition=CpuPartition((3, 1, 7)),
                                                     horizon_ns=15, policy=policy, quantum_ns=quantum)
                for cpu_result in actual.cpus:
                    expected = model.simulate_edf([t for t in tasks if t.cpu == cpu_result.cpu],
                                                 cpu=cpu_result.cpu, horizon_ns=15,
                                                 policy=policy, quantum_ns=quantum)
                    self.assertEqual(asdict(cpu_result), asdict(expected))
                self.assertEqual(actual.job_count, sum(len(r.jobs) for r in actual.cpus))
                self.assertEqual(actual.deadline_misses, sum(r.deadline_misses for r in actual.cpus))
                self.assertEqual(actual.budget_exhaustions, sum(r.budget_exhaustions for r in actual.cpus))

    def test_invalid_later_cpu_is_rejected_before_replay(self):
        tasks = [TaskSpec('a', 0, 1, 10, 10),
                 TaskSpec('b', 1, 1, 10, 10, work_profile_ns=(1,))]
        with patch.object(model, '_replay', side_effect=AssertionError('executed too early')):
            with self.assertRaisesRegex(ValueError, 'work profile length'):
                model.simulate_partitioned(tasks, partition=CpuPartition((0, 1)), horizon_ns=20)

    def test_rejects_duplicate_names_outside_cpu_and_invalid_arguments(self):
        cases = [([TaskSpec('a', 0, 1, 10, 10), TaskSpec('a', 1, 1, 10, 10)], {}),
                 ([TaskSpec('a', 2, 1, 10, 10)], {}),
                 ([TaskSpec('a', 0, 1, 10, 10)], {'policy':'rr', 'quantum_ns':True}),
                 ([TaskSpec('a', 0, 1, 10, 10)], {'policy':'unknown'}),
                 ([], {})]
        for tasks, kwargs in cases:
            with self.subTest(tasks=tasks, kwargs=kwargs), self.assertRaises(ValueError):
                model.simulate_partitioned(tasks, partition=CpuPartition((0, 1)),
                                           horizon_ns=10, **kwargs)

    def test_no_releases_still_reports_selected_cpus_and_empty_aggregates(self):
        result = model.simulate_partitioned([TaskSpec('a', 4, 1, 10, 10, offset_ns=20)],
                                           partition=CpuPartition((4, 8)), horizon_ns=10)
        self.assertEqual([r.cpu for r in result.cpus], [4])
        self.assertEqual(result.cpus[0].jobs, ())
        self.assertEqual(result.job_count, 0)

    def test_global_job_limit_accepts_exact_boundary(self):
        result = model.simulate_partitioned([TaskSpec('a', 0, 1, 1, 1),
                                            TaskSpec('b', 1, 1, 1, 1)],
                                           partition=CpuPartition((0, 1)), horizon_ns=2048)
        self.assertEqual(result.job_count, model.MAX_JOBS)

    def test_global_event_limit_accepts_exact_boundary(self):
        with patch.object(model, 'MAX_EVENTS', 4):
            result = model.simulate_partitioned([TaskSpec('a', 0, 1, 1, 1),
                                                TaskSpec('b', 1, 1, 1, 1)],
                                               partition=CpuPartition((0, 1)), horizon_ns=2)
        self.assertEqual(result.job_count, 4)

    def test_partitioned_timelines_match_unit_tick_oracle(self):
        from dataclasses import replace, asdict
        from test_xtos_sched_model_oracle import (
            _adversarial_workloads, _random_workloads, _unit_tick_oracle, _expanded_segments)
        for label, original, horizon, quantum in _adversarial_workloads()+_random_workloads():
            tasks = tuple(replace(t, name=t.name+'_cpu'+str(cpu), cpu=cpu,
                                  offset_ns=t.offset_ns+(1 if cpu==3 else 0))
                          for cpu in (1, 3) for t in original)
            for policy in model.POLICIES:
                with self.subTest(workload=label, policy=policy):
                    q = quantum if policy=='rr' else None
                    result = model.simulate_partitioned(tasks, partition=CpuPartition((1, 3, 7)),
                                                        horizon_ns=horizon, policy=policy, quantum_ns=q)
                    for cpu_result in result.cpus:
                        expected = _unit_tick_oracle(tuple(t for t in tasks if t.cpu==cpu_result.cpu),
                                                     horizon, policy, q)
                        self.assertEqual(_expanded_segments(cpu_result), expected.occupancy)
                        self.assertEqual(tuple(asdict(j) for j in cpu_result.jobs), expected.jobs)

    def test_two_cpus_execute_independently_and_preserve_partition_order(self):
        entry = getattr(model, 'simulate_partitioned', None)
        self.assertTrue(callable(entry), 'fixed-partition model entry point is missing')
        tasks = [TaskSpec('cpu3', 3, 2, 10, 10), TaskSpec('cpu1', 1, 3, 10, 10)]
        result = entry(tasks, partition=CpuPartition((3, 1, 7)), horizon_ns=10)
        self.assertEqual(result.partition_cpus, (1, 3, 7))
        self.assertEqual([r.cpu for r in result.cpus], [1, 3])
        self.assertEqual([r.jobs[0].start_ns for r in result.cpus], [0, 0])
        self.assertEqual([r.jobs[0].finish_ns for r in result.cpus], [3, 2])
        self.assertEqual(result.job_count, 2)
        self.assertEqual(result.deadline_misses, 0)
        self.assertEqual(result.budget_exhaustions, 0)


if __name__ == '__main__':
    unittest.main()
