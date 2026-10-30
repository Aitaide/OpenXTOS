import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / 'tools/xtos-sched-bench/analyze_trace.py'
SPEC = importlib.util.spec_from_file_location('xtos_sched_trace', MODULE)
ANALYZER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ANALYZER)


def fixture():
    metadata = {
        'type': 'metadata', 'schema': 'xtos-sched-trace-v1', 'policy': 'other',
        'actual_policy': 0, 'actual_priority': 0, 'period_ns': 1_000_000_000,
        'deadline_ns': 80_000_000, 'work_ns': 20, 'runtime_ns': 40_000_000,
        'duration_s': 3, 'cpu_requested': 2, 'warmup_jobs': 1,
        'expected_jobs': 3, 'clock': 'CLOCK_MONOTONIC',
        'affinity_mode': 'pinned', 'cpu_affinity': [2],
        'wake_semantics': 'first_userspace_timestamp_after_absolute_sleep',
    }
    jobs = [
        {'type': 'job', 'id': 0, 'release_ns': 100, 'wake_ns': 110,
         'finish_ns': 130, 'deadline_ns': 80_000_100, 'work_ns': 20,
         'cpu_work_ns': 20, 'cpu': 2, 'warmup': True},
        {'type': 'job', 'id': 1, 'release_ns': 1_000_000_100,
         'wake_ns': 1_000_000_110, 'finish_ns': 1_000_000_160,
         'deadline_ns': 1_080_000_100, 'work_ns': 20,
         'cpu_work_ns': 25, 'cpu': 2, 'warmup': False},
        {'type': 'job', 'id': 2, 'release_ns': 2_000_000_100,
         'wake_ns': 2_000_000_120, 'finish_ns': 2_080_000_101,
         'deadline_ns': 2_080_000_100, 'work_ns': 20,
         'cpu_work_ns': 30, 'cpu': 2, 'warmup': False},
    ]
    return [metadata, *jobs]


class SchedulerTraceTests(unittest.TestCase):
    def test_summary_matches_known_oracle_and_states_limits(self):
        result = ANALYZER.summarize(fixture())
        self.assertEqual(result['total_jobs'], 3)
        self.assertEqual(result['job_count'], 2)
        self.assertEqual(result['warmup_jobs_excluded'], 1)
        self.assertEqual(result['release_latency_ns'], {
            'p50': 10, 'p95': 20, 'p99': 20, 'p99_9': 20, 'max': 20,
        })
        self.assertEqual(result['completion_latency_ns'], {
            'p50': 60, 'p95': 80_000_001, 'p99': 80_000_001,
            'p99_9': 80_000_001, 'max': 80_000_001,
        })
        self.assertEqual(result['response_latency_ns'], {
            'p50': 50, 'p95': 79_999_981, 'p99': 79_999_981,
            'p99_9': 79_999_981, 'max': 79_999_981,
        })
        self.assertEqual(result['cpu_work_ns'], {
            'p50': 25, 'p95': 30, 'p99': 30, 'p99_9': 30, 'max': 30,
        })
        self.assertEqual(result['deadline_misses'], 1)
        self.assertEqual(result['actual_cpu_ids'], [2])
        self.assertEqual(result['cpu_migrations_observed'], 0)
        self.assertEqual(result['work_ns_total'], 55)
        self.assertEqual(result['requested_work_ns_total'], 40)
        self.assertTrue(result['comparable'])
        self.assertTrue(any('not a kernel wakeup' in limit for limit in result['limitations']))

    def test_rejects_missing_or_false_wakeup_semantics(self):
        for value in (None, 'kernel_sched_wakeup_timestamp', '', True):
            rows = fixture()
            if value is None:
                del rows[0]['wake_semantics']
            else:
                rows[0]['wake_semantics'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'wake_semantics'):
                ANALYZER.summarize(rows)

    def test_rejects_cross_job_execution_overlap_including_warmup(self):
        for previous, current in ((1, 2), (2, 3)):
            rows = fixture()
            rows[previous]['finish_ns'] = rows[current]['wake_ns'] + 100
            with self.subTest(previous=previous), self.assertRaisesRegex(ValueError, 'overlap'):
                ANALYZER.summarize(rows)

    def test_late_planned_release_is_valid_when_execution_remains_serial(self):
        rows = fixture()
        rows[1]['finish_ns'] = rows[2]['release_ns'] + 1000
        rows[2]['wake_ns'] = rows[1]['finish_ns'] + 1
        rows[2]['finish_ns'] = rows[2]['wake_ns'] + 100
        self.assertEqual(ANALYZER.summarize(rows)['job_count'], 2)

    def test_finishing_exactly_at_deadline_is_on_time(self):
        rows = fixture()
        rows[3]['finish_ns'] = rows[3]['deadline_ns']
        self.assertEqual(ANALYZER.summarize(rows)['deadline_misses'], 0)

    def test_rejects_empty_or_missing_jobs_and_wrong_job_count(self):
        for rows in ([fixture()[0]], fixture()[:-1]):
            with self.subTest(count=len(rows)), self.assertRaises(ValueError):
                ANALYZER.summarize(rows)

    def test_rejects_noncontiguous_or_reordered_job_ids(self):
        rows = fixture()
        rows[1]['id'] = 2
        with self.assertRaisesRegex(ValueError, 'id'):
            ANALYZER.summarize(rows)
        rows = fixture()
        rows[1], rows[2] = rows[2], rows[1]
        with self.assertRaisesRegex(ValueError, 'id|release|order'):
            ANALYZER.summarize(rows)

    def test_rejects_missing_or_wrongly_typed_required_fields(self):
        cases = [
            (0, 'actual_policy', None), (0, 'duration_s', True),
            (1, 'warmup', None), (1, 'deadline_ns', '80000100'),
            (2, 'cpu_work_ns', None),
        ]
        for record_index, field, value in cases:
            rows = fixture()
            if value is None:
                del rows[record_index][field]
            else:
                rows[record_index][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                ANALYZER.summarize(rows)

    def test_rejects_bad_warmup_types_and_positions(self):
        rows = fixture()
        rows[1]['warmup'] = 1
        with self.assertRaisesRegex(ValueError, 'warmup'):
            ANALYZER.summarize(rows)
        rows = fixture()
        rows[2]['warmup'] = True
        with self.assertRaisesRegex(ValueError, 'warmup'):
            ANALYZER.summarize(rows)
        rows = fixture()
        rows[1]['cpu_work_ns'] = rows[1]['finish_ns'] - rows[1]['wake_ns'] + 1
        with self.assertRaisesRegex(ValueError, 'cpu_work_ns'):
            ANALYZER.summarize(rows)

    def test_rejects_bad_metadata_policy_priority_and_affinity(self):
        for field, value in (
            ('actual_policy', 1), ('actual_priority', 7),
            ('affinity_mode', 'inherited'), ('cpu_requested', -1),
            ('cpu_affinity', [2, 2]),
        ):
            rows = fixture()
            rows[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                ANALYZER.summarize(rows)

    def test_rejects_pinned_cpu_outside_effective_affinity(self):
        rows = fixture()
        rows[2]['cpu'] = 4
        with self.assertRaisesRegex(ValueError, 'CPU|cpu|affinity'):
            ANALYZER.summarize(rows)

    def test_rejects_deadline_metadata_and_job_deadline_mismatch(self):
        rows = fixture()
        rows[0]['deadline_ns'] = rows[0]['period_ns'] + 1
        with self.assertRaisesRegex(ValueError, 'deadline'):
            ANALYZER.summarize(rows)
        rows = fixture()
        rows[2]['deadline_ns'] -= 1
        with self.assertRaisesRegex(ValueError, 'deadline'):
            ANALYZER.summarize(rows)

    def test_rejects_irregular_releases_and_invalid_cpu_work(self):
        rows = fixture()
        rows[2]['release_ns'] += 1
        rows[2]['deadline_ns'] += 1
        with self.assertRaisesRegex(ValueError, 'release_ns'):
            ANALYZER.summarize(rows)
        rows = fixture()
        rows[2]['cpu_work_ns'] = rows[2]['work_ns'] - 1
        with self.assertRaisesRegex(ValueError, 'cpu_work_ns'):
            ANALYZER.summarize(rows)
        rows = fixture()
        rows[2]['finish_ns'] = rows[2]['wake_ns'] + 10
        with self.assertRaisesRegex(ValueError, 'cpu_work_ns'):
            ANALYZER.summarize(rows)

    def test_allows_only_explicit_deadline_inherited_affinity_and_marks_uncomparable(self):
        rows = fixture()
        rows[0].update(policy='deadline', actual_policy=6, actual_priority=0,
                       affinity_mode='inherited', cpu_requested=-1, cpu_affinity=[2, 3])
        rows[2]['cpu'] = 3
        result = ANALYZER.summarize(rows)
        self.assertFalse(result['comparable'])
        self.assertEqual(result['policy'], 'deadline')
        self.assertEqual(result['actual_cpu_ids'], [2, 3])
        self.assertEqual(result['cpu_migrations_observed'], 2)
        rows[3]['finish_ns'] = rows[3]['deadline_ns']
        self.assertEqual(ANALYZER.summarize(rows)['deadline_misses'], 0)

    def test_rejects_duplicate_json_keys_and_nonfinite_constants(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            path = Path(tmp) / 'trace.jsonl'
            path.write_text('{"type":"metadata","type":"job"}\n', encoding='utf-8')
            with patch('sys.argv', ['analyze_trace.py', str(path)]):
                self.assertEqual(ANALYZER.main(), 2)
            path.write_text('{"type":"metadata","value":NaN}\n', encoding='utf-8')
            with patch('sys.argv', ['analyze_trace.py', str(path)]):
                self.assertEqual(ANALYZER.main(), 2)

    def test_rejects_overflowing_float_json_number(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            path = Path(tmp) / 'trace.jsonl'
            path.write_text('{"type":"metadata","value":1e999}', encoding='utf-8')
            with patch('sys.argv', ['analyze_trace.py', str(path)]):
                self.assertEqual(ANALYZER.main(), 2)

    def test_cli_rejects_blank_or_truncated_jsonl(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            path = Path(tmp) / 'trace.jsonl'
            for contents in ('', '{"type":"metadata"\n', '{}\n\n{}\n'):
                path.write_text(contents, encoding='utf-8')
                with self.subTest(contents=contents), patch('sys.argv', ['analyze_trace.py', str(path)]):
                    self.assertEqual(ANALYZER.main(), 2)

    def test_output_cannot_alias_or_overwrite_raw_input(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = Path(tmp)
            raw = root / 'raw.jsonl'
            raw.write_text('\n'.join(json.dumps(r) for r in fixture()) + '\n', encoding='utf-8')
            original = raw.read_bytes()
            for output in (raw, root / '.' / 'raw.jsonl', root / 'hardlink.jsonl', root / 'symlink.jsonl'):
                if output.name == 'hardlink.jsonl':
                    os.link(raw, output)
                elif output.name == 'symlink.jsonl':
                    output.symlink_to(raw)
                with self.subTest(output=output), patch('sys.argv', ['analyze_trace.py', str(raw), '--output', str(output)]):
                    self.assertEqual(ANALYZER.main(), 2)
                self.assertEqual(raw.read_bytes(), original)
            fresh = root / 'summary.json'
            with patch('sys.argv', ['analyze_trace.py', str(raw), '--output', str(fresh)]):
                self.assertEqual(ANALYZER.main(), 0)
            with patch('sys.argv', ['analyze_trace.py', str(raw), '--output', str(fresh)]):
                self.assertEqual(ANALYZER.main(), 2)
            self.assertEqual(raw.read_bytes(), original)
            summary = json.loads(fresh.read_text(encoding='utf-8'))
            self.assertEqual(summary['job_count'], 2)


if __name__ == '__main__':
    unittest.main()
