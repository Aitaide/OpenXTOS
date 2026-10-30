"""M1 model CLI contract tests."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "tools" / "xtos-sched-model" / "replay.py"
sys.path.insert(0, str(CLI.parent))
import replay
from unittest.mock import patch


class ModelCliTests(unittest.TestCase):
    def run_cli(self, payload, *extra):
        directory = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        source = base / "input.json"
        output = base / "report.json"
        source.write_text(json.dumps(payload, indent=2) + "\n")
        result = subprocess.run(
            [sys.executable, str(CLI), "--input", str(source), "--output", str(output), *extra],
            capture_output=True, text=True,
        )
        return result, source, output

    @staticmethod
    def payload():
        return {
            "schema": "xtos-scheduler-model-input-v1",
            "cpu": 0,
            "horizon_ns": 20,
            "tasks": [
                {"name": "beta", "cpu": 0, "runtime_ns": 2,
                 "work_ns": 2, "deadline_ns": 10, "period_ns": 10},
                {"name": "alpha", "cpu": 0, "runtime_ns": 2,
                 "work_ns": 2, "deadline_ns": 10, "period_ns": 10},
            ],
        }

    @staticmethod
    def trace_rows(first_release=100):
        metadata = {
            "type": "metadata", "schema": "xtos-sched-trace-v1", "policy": "fifo",
            "actual_policy": 1, "actual_priority": 20, "period_ns": 500000000,
            "deadline_ns": 500000000, "work_ns": 2, "runtime_ns": 5,
            "duration_s": 1, "warmup_jobs": 1, "expected_jobs": 2,
            "cpu_requested": 0, "clock": "CLOCK_MONOTONIC", "affinity_mode": "pinned",
            "cpu_affinity": [0],
            "wake_semantics": "first_userspace_timestamp_after_absolute_sleep",
        }
        rows = [metadata]
        for index, cpu_work in enumerate((3, 4)):
            release = first_release + index * metadata["period_ns"]
            rows.append({
                "type": "job", "id": index, "release_ns": release,
                "wake_ns": release + 1, "finish_ns": release + cpu_work + 1,
                "deadline_ns": release + metadata["deadline_ns"], "work_ns": 2,
                "cpu_work_ns": cpu_work, "cpu": 0, "warmup": index == 0,
            })
        return rows

    def run_trace(self, rows, directory):
        source = Path(directory) / "trace.jsonl"
        output = Path(directory) / "report.json"
        raw = "".join(json.dumps(row) + "\n" for row in rows).encode()
        source.write_bytes(raw)
        result = subprocess.run(
            [sys.executable, str(CLI), "--trace", str(source), "--output", str(output)],
            capture_output=True, text=True,
        )
        return result, source, output, raw

    def test_replay_writes_reproducible_multi_policy_report(self):
        result, source, output = self.run_cli(self.payload(), "--policies", "edf,fifo,rr",
                                               "--quantum-ns", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report["schema"], "xtos-scheduler-model-report-v1")
        self.assertEqual(report["status"], "model_only")
        self.assertEqual(report["input_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual([item["policy"] for item in report["policies"]], ["edf", "fifo", "rr"])
        self.assertTrue(all(item["job_count"] == 4 for item in report["policies"]))
        self.assertEqual(report["policies"][0]["deadline_misses"], 0)
        self.assertEqual(report["policies"][2]["quantum_ns"], 1)

    def test_existing_output_is_never_overwritten(self):
        result, _, output = self.run_cli(self.payload())
        self.assertEqual(result.returncode, 0, result.stderr)
        before = output.read_bytes()
        result = subprocess.run(
            [sys.executable, str(CLI), "--input", str(output.parent / "input.json"),
             "--output", str(output)], capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not already exist", result.stderr)
        self.assertEqual(output.read_bytes(), before)

    def test_invalid_schema_fails_without_creating_output(self):
        payload = self.payload()
        payload["schema"] = "wrong"
        result, _, output = self.run_cli(payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("schema", result.stderr)
        self.assertFalse(output.exists())

    def test_constrained_deadline_requires_explicit_dbf_horizon_in_input(self):
        payload = self.payload()
        payload["tasks"][0]["deadline_ns"] = 5
        payload["tasks"][0]["period_ns"] = 10
        result, _, output = self.run_cli(payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dbf_horizon_ns", result.stderr)
        self.assertFalse(output.exists())


    def test_finite_dbf_diagnostic_never_becomes_admission(self):
        payload = self.payload()
        payload['tasks'][0]['deadline_ns'] = 5
        payload['dbf_horizon_ns'] = 30
        result, _, output = self.run_cli(payload, '--policies', 'edf,reservation_edf')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report['reservation_admission']['status'], 'not_admitted')
        self.assertEqual(report['dbf']['scope'], 'finite_diagnostic_only')
        self.assertTrue(report['dbf']['feasible'])

    def test_duplicate_keys_and_unknown_fields_fail_closed(self):
        for raw in ['{"schema":"wrong","schema":"xtos-scheduler-model-input-v1"}',
                    json.dumps({**self.payload(), 'typo_horizon': 12})]:
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp)/'input.json'; out = Path(tmp)/'out.json'
                src.write_text(raw)
                result = subprocess.run([sys.executable, str(CLI), '--input',str(src),
                                         '--output',str(out)],capture_output=True,text=True)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(out.exists())

    def test_output_dangling_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            src=Path(tmp)/'input.json'; link=Path(tmp)/'link'; target=Path(tmp)/'target'
            src.write_text(json.dumps(self.payload())); link.symlink_to(target)
            result=subprocess.run([sys.executable,str(CLI),'--input',str(src),'--output',str(link)],
                                  capture_output=True,text=True)
            self.assertEqual(result.returncode,2)
            self.assertFalse(target.exists())
            self.assertTrue(link.is_symlink())

    def test_report_hashes_the_same_input_bytes_it_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            src=Path(tmp)/'input.json'; out=Path(tmp)/'out.json'
            raw=json.dumps(self.payload()).encode();src.write_bytes(raw)
            original=replay._json
            def mutate_after_parse(*args,**kwargs):
                parsed=original(*args,**kwargs)
                src.write_text('{}')
                return parsed
            with patch.object(replay,'_json',mutate_after_parse):
                status=replay.main(['--input',str(src),'--output',str(out)])
            self.assertEqual(status,0)
            self.assertEqual(json.loads(out.read_text())['input_sha256'],hashlib.sha256(raw).hexdigest())

    def test_special_and_oversized_inputs_are_rejected_promptly(self):
        with tempfile.TemporaryDirectory() as tmp:
            src=Path(tmp)/'input.json';out=Path(tmp)/'out.json'
            os.mkfifo(src)
            result=subprocess.run([sys.executable,str(CLI),'--input',str(src),'--output',str(out)],
                                  capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,2);self.assertFalse(out.exists())
            src.unlink();src.write_bytes(b' '*(1048576+1))
            result=subprocess.run([sys.executable,str(CLI),'--input',str(src),'--output',str(out)],
                                  capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,2);self.assertFalse(out.exists())


    def test_competing_output_creation_after_check_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            src=Path(tmp)/'input.json';out=Path(tmp)/'out.json'
            src.write_text(json.dumps(self.payload()))
            original=replay.build_report
            def competitor(*args,**kwargs):
                result=original(*args,**kwargs);out.write_text('other writer\n');return result
            with patch.object(replay,'build_report',competitor):
                status=replay.main(['--input',str(src),'--output',str(out)])
            self.assertEqual(status,2)
            self.assertEqual(out.read_text(),'other writer\n')

    def test_raw_numeric_and_duplicate_json_errors_leave_no_report(self):
        good=json.dumps(self.payload())
        cases=[good.replace('"cpu": 0','"cpu": NaN',1),
               good.replace('"cpu": 0','"cpu": 1e999',1),
               good.replace('"cpu": 0','"cpu": 0, "cpu": 1',1),
               good.replace('"horizon_ns": 20','"horizon_ns": true',1)]
        for raw in cases:
            with self.subTest(raw=raw),tempfile.TemporaryDirectory() as tmp:
                src=Path(tmp)/'input.json';out=Path(tmp)/'out.json';src.write_text(raw)
                result=subprocess.run([sys.executable,str(CLI),'--input',str(src),'--output',str(out)],
                                      capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,2)
                self.assertFalse(out.exists())


    def test_trace_import_replays_exact_cpu_work_and_preserves_warmup(self):
        metadata={'type':'metadata','schema':'xtos-sched-trace-v1','policy':'fifo',
                  'actual_policy':1,'actual_priority':20,'period_ns':500000000,
                  'deadline_ns':500000000,'work_ns':2,'runtime_ns':5,'duration_s':1,
                  'warmup_jobs':1,'expected_jobs':2,'cpu_requested':0,'clock':'CLOCK_MONOTONIC',
                  'affinity_mode':'pinned','cpu_affinity':[0],
                  'wake_semantics':'first_userspace_timestamp_after_absolute_sleep'}
        rows=[metadata]
        for index,work in enumerate([3,4]):
            release=100+index*500000000
            rows.append({'type':'job','id':index,'release_ns':release,'wake_ns':release+1,
                         'finish_ns':release+work+1,'deadline_ns':release+500000000,
                         'work_ns':2,'cpu_work_ns':work,'cpu':0,'warmup':index==0})
        with tempfile.TemporaryDirectory() as tmp:
            src=Path(tmp)/'raw.jsonl';out=Path(tmp)/'report.json'
            raw=''.join(json.dumps(row)+'\n' for row in rows).encode();src.write_bytes(raw)
            result=subprocess.run([sys.executable,str(CLI),'--trace',str(src),'--output',str(out),
                                   '--policies','edf,reservation_edf'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            report=json.loads(out.read_text())
            self.assertEqual(report['input_sha256'],hashlib.sha256(raw).hexdigest())
            self.assertEqual(report['trace_source']['summary']['warmup_jobs_excluded'],1)
            self.assertEqual([j['execution_ns'] for j in report['policies'][0]['jobs']],[3,4])
            self.assertEqual([j['warmup'] for j in report['policies'][0]['jobs']],[True,False])
            self.assertEqual(src.read_bytes(),raw)
            del rows[-1]
            bad=Path(tmp)/'bad.jsonl';bad.write_text(''.join(json.dumps(row)+'\n' for row in rows))
            badout=Path(tmp)/'badout.json'
            result=subprocess.run([sys.executable,str(CLI),'--trace',str(bad),'--output',str(badout)],
                                  capture_output=True,text=True)
            self.assertEqual(result.returncode,2)
            self.assertFalse(badout.exists())

    def test_trace_rejects_unknown_metadata_and_job_fields_without_output(self):
        for record_index, field in ((0, "typo_duration_ns"), (1, "typo_cpu_work_ns")):
            with self.subTest(record_index=record_index), tempfile.TemporaryDirectory() as tmp:
                rows = self.trace_rows()
                rows[record_index][field] = 1
                result, source, output, raw = self.run_trace(rows, tmp)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("unknown trace", result.stderr)
                self.assertFalse(output.exists())
                self.assertEqual(source.read_bytes(), raw)

    def test_trace_rejects_out_of_range_time_fields_without_output(self):
        metadata_fields = ("period_ns", "deadline_ns", "work_ns", "runtime_ns")
        job_fields = ("release_ns", "wake_ns", "finish_ns", "deadline_ns",
                      "work_ns", "cpu_work_ns")
        cases = [(0, field, (1 << 63)) for field in metadata_fields]
        cases += [(1, field, (1 << 63)) for field in job_fields]
        cases += [(0, "duration_s", ((1 << 63) - 1) // 1_000_000_000 + 1)]
        cases += [(1, "release_ns", True), (1, "wake_ns", 1.0)]
        for record_index, field, value in cases:
            with self.subTest(record_index=record_index, field=field, value=value), \
                    tempfile.TemporaryDirectory() as tmp:
                rows = self.trace_rows()
                rows[record_index][field] = value
                result, source, output, raw = self.run_trace(rows, tmp)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(output.exists())
                self.assertEqual(source.read_bytes(), raw)

    def test_trace_accepts_valid_absolute_times_near_max_ns(self):
        maximum = (1 << 63) - 1
        rows = self.trace_rows(maximum - 1_000_000_000)
        with tempfile.TemporaryDirectory() as tmp:
            result, _, output, _ = self.run_trace(rows, tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report["status"], "model_only")
            self.assertEqual(report["trace_source"]["first_release_ns"], maximum - 1_000_000_000)
            self.assertEqual(report["policies"][0]["job_count"], 2)


if __name__ == "__main__":
    unittest.main()
