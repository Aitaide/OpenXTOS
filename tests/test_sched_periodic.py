import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'tools/xtos-sched-bench/periodic.c'


class PeriodicExecutableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='xtos-periodic-tests-')
        cls.binary = Path(cls.directory.name) / 'periodic'
        subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', '-std=c11',
                        str(SOURCE), '-o', str(cls.binary)], check=True, capture_output=True)
        cls.cpu = min(os.sched_getaffinity(0))

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def invoke(self, path, **changes):
        config = {'policy': 'other', 'priority': '0', 'period': '10000000',
                  'deadline': '8000000', 'work': '100000', 'runtime': '500000',
                  'duration': '1', 'cpu': str(self.cpu), 'warmup': '10'}
        config.update(changes)
        return subprocess.run([str(self.binary), str(path), *config.values()],
                              capture_output=True, text=True, timeout=5)

    def test_existing_trace_is_not_overwritten(self):
        path = Path(self.directory.name) / 'caller-trace.jsonl'
        path.write_bytes(b'caller-owned-data')
        result = self.invoke(path)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_bytes(), b'caller-owned-data')

    def test_unsigned_fields_refuse_signed_and_blank(self):
        for index, value in enumerate(('-1', '+1', ' 1', '')):
            with self.subTest(value=value):
                path = Path(self.directory.name) / f'invalid-warmup-{index}.jsonl'
                result = self.invoke(path, warmup=value)
                self.assertNotEqual(result.returncode, 0)

    def test_warmup_must_leave_at_least_one_measured_job(self):
        path = Path(self.directory.name) / 'all-warmup.jsonl'
        result = self.invoke(path, warmup='100')
        self.assertNotEqual(result.returncode, 0)

    def test_inherited_affinity_is_not_allowed_for_other_policy(self):
        path = Path(self.directory.name) / 'invalid-inherited.jsonl'
        result = self.invoke(path, cpu='-1')
        self.assertNotEqual(result.returncode, 0)

    def test_direct_binary_does_not_expand_callers_affinity(self):
        allowed = sorted(os.sched_getaffinity(0))
        if len(allowed) < 2:
            self.skipTest('requires two allowed CPUs to test affinity expansion refusal')
        path = Path(self.directory.name) / 'expanded-affinity.jsonl'
        result = subprocess.run(['taskset', '--cpu-list', str(allowed[0]), str(self.binary),
                                 str(path), 'other', '0', '10000000', '8000000',
                                 '100000', '500000', '1', str(allowed[1]), '10'],
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(path.exists())

    def test_priority_and_cpu_tokens_are_literal_decimal_numbers(self):
        for field in ('priority', 'cpu'):
            for index, value in enumerate(('', '+0', ' 0')):
                with self.subTest(field=field, value=value):
                    path = Path(self.directory.name) / f'invalid-token-{field}-{index}.jsonl'
                    result = self.invoke(path, **{field: value})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(path.exists())

    def test_other_trace_records_complete_contract_and_effective_affinity(self):
        path = Path(self.directory.name) / 'valid-other.jsonl'
        result = self.invoke(path)
        self.assertEqual(result.returncode, 0, result.stderr)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        metadata, jobs = records[0], records[1:]
        self.assertEqual(metadata['schema'], 'xtos-sched-trace-v1')
        self.assertEqual(metadata['actual_policy'], 0)
        self.assertEqual(metadata['actual_priority'], 0)
        self.assertEqual(metadata['affinity_mode'], 'pinned')
        self.assertEqual(metadata['cpu_affinity'], [self.cpu])
        self.assertEqual(metadata['expected_jobs'], 100)
        self.assertEqual(len(jobs), metadata['expected_jobs'])
        self.assertEqual(sum(job['warmup'] for job in jobs), 10)
        for index, job in enumerate(jobs):
            self.assertEqual(job['id'], index)
            self.assertEqual(job['cpu'], self.cpu)
            self.assertEqual(job['warmup'], index < 10)
            self.assertLessEqual(job['release_ns'], job['wake_ns'])
            self.assertLessEqual(job['wake_ns'], job['finish_ns'])
            self.assertGreaterEqual(job['cpu_work_ns'], job['work_ns'])
            self.assertEqual(job['deadline_ns'], job['release_ns'] + metadata['deadline_ns'])
            if index:
                self.assertEqual(job['release_ns'] - jobs[index - 1]['release_ns'], metadata['period_ns'])


if __name__ == '__main__':
    unittest.main()
