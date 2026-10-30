import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "tools/xtos-sched-bench/m0_gate.py"
SPEC = importlib.util.spec_from_file_location("xtos_m0_gate", MODULE)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)
ANALYZER_HASH = hashlib.sha256((ROOT / "tools/xtos-sched-bench/analyze_trace.py").read_bytes()).hexdigest()
RUNNER_HASH = hashlib.sha256((ROOT / "tools/xtos-sched-bench/run_trial.py").read_bytes()).hexdigest()


def intake(necessity="native_sufficient"):
    return {"schema": "xtos-scheduler-m0-intake-v1", "target": {
        "owner": "owner", "asset_id": "target", "physical_platform_attestation": "attest",
        "vendor_model_board": "board", "cpu_model_microcode_topology": "topology",
        "numa_smt_online_cpus": "cpus", "firmware_bios_revision": "bios",
        "power_frequency_policy": "fixed", "target_access_or_operator_run_method": "operator",
        "maintenance_window_and_recovery_owner": "recovery"}, "runtime_image": {
        "xtos_source_commit": "a" * 40, "kernel_source_commit": "b" * 40,
        "kernel_config_sha256": "c" * 64, "kernel_image_sha256": "d" * 64,
        "running_kernel_identity": "kernel", "sys_kernel_realtime_value": "1",
        "independent_fallback_image_and_boot_method": "fallback"}, "workload": {
        "business_or_system_use_case": "synthetic", "workload_owner": "owner",
        "real_application_or_device": "fixture", "input_capture_and_replay_artifact": "input",
        "task_set": [{"name": "task", "period_ns": 100000000, "deadline_ns": 100000000,
                      "runtime_ns": 200000, "cpu": 2}], "dependencies_blocking_and_locking": "none",
        "device_irq_and_cpu_placement": "cpu2", "background_interference": "declared",
        "overload_and_missed_deadline_behavior": "stop",
        "wcet_or_budget_estimation_method_and_evidence": "wcet"}, "comparison_protocol": {
        "required_native_policies": ["SCHED_FIFO_or_SCHED_RR", "SCHED_DEADLINE"],
        "identical_workload_and_placement_verified": True, "primary_metric": "p99",
        "minimum_meaningful_improvement": "10%", "maximum_allowed_regression_by_metric": "5%",
        "deadline_miss_acceptance": "zero", "valid_repetitions_per_policy_minimum": 10,
        "run_duration_and_reset_or_warmup_rule": "warmup", "interleaving_or_randomization_rule": "round-robin",
        "confidence_interval_and_outlier_rule": "bootstrap", "independent_reviewer": "reviewer"},
        "configuration_changes": {"default": "none", "explicitly_authorized_changes": [],
          "before_after_evidence_location": "config-evidence", "rollback_owner_and_method": "owner-method"},
        "evidence": {"separate_preflight_report_per_run": True, "all_raw_traces_retained_immutable": True,
        "runner_analyzer_source_hashes_recorded": True, "failed_and_rejected_runs_retained": True,
        "evidence_root": "evidence"}, "go_no_go": {"custom_scheduler_necessity": necessity,
        "decision_owner": "owner", "decision_date": "2026-10-25", "rationale_and_evidence": "decision"}}


def trace(policy, start):
    actual = {"fifo": 1, "rr": 2, "deadline": 6}[policy]
    priority = 20 if policy in ("fifo", "rr") else 0
    meta = {"type": "metadata", "schema": "xtos-sched-trace-v1", "policy": policy,
            "actual_policy": actual, "actual_priority": priority, "period_ns": 100_000_000,
            "deadline_ns": 100_000_000, "work_ns": 100, "runtime_ns": 200_000,
            "duration_s": 1, "warmup_jobs": 1, "expected_jobs": 10, "clock": "CLOCK_MONOTONIC",
            "affinity_mode": "pinned", "cpu_requested": 2, "cpu_affinity": [2],
            "wake_semantics": "first_userspace_timestamp_after_absolute_sleep"}
    rows = [meta]
    for i in range(10):
        release = start + i * 100_000_000
        rows.append({"type": "job", "id": i, "release_ns": release, "wake_ns": release + 10,
                     "finish_ns": release + 110, "deadline_ns": release + 100_000_000,
                     "work_ns": 100, "cpu_work_ns": 100, "cpu": 2, "warmup": i == 0})
    return rows


class M0GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def put(self, rel, value):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value, allow_nan=False, indent=2) + "\n", encoding="utf-8")
        return p

    def evidence(self, fixture=True, duplicate=False, drift=False, bad_summary=False, mode="fixture_filesystem"):
        root = self.root / "evidence"
        root.mkdir()
        common = {"kernel_source_commit": "b" * 40, "kernel_config_sha256": "c" * 64,
                  "kernel_image_sha256": "d" * 64, "workload_sha256": "1" * 64,
                  "placement_sha256": "2" * 64, "runner_sha256": RUNNER_HASH,
                  "analyzer_sha256": ANALYZER_HASH}
        samples = {"fifo": [], "deadline": [], "rr": []}
        analyzer = GATE._trace_analyzer()
        for policy in ("fifo", "deadline"):
            for i in range(10):
                name = f"{policy}-{i}"
                raw = root / f"{name}.jsonl"
                source = root / f"{policy}-0.jsonl" if duplicate else raw
                if not source.exists():
                    source.write_text("\n".join(json.dumps(x) for x in trace(policy, i * 1_000_000_000)) + "\n", encoding="utf-8")
                if raw != source:
                    raw.write_bytes(source.read_bytes())
                summary = analyzer.summarize(analyzer._load_jsonl(raw))
                pre = root / f"{name}.preflight.json"
                self.put(pre.relative_to(self.root), {"schema": "xtos-sched-preflight-v1", "collection_mode": mode,
                    "virtualization": {"status": "unknown", "value": "unknown", "source": "synthetic-unit-control", "error": "physical identity remains manual"},
                    "cpu": {"online": {"status": "available", "value": [2]}, "allowed": {"status": "available", "value": [2]}},
                    "uname": {"release": "kernel", "machine": "x86_64"},
                    "realtime": {"status": "available", "value": "1"}})
                meta = json.loads(raw.read_text(encoding="utf-8").splitlines()[0])
                manifest = {"schema": "xtos-sched-manifest-v1", "run_id": name,
                    "raw_trace": str(raw), "raw_trace_sha256": GATE.sha256(raw), "trace_metadata": meta,
                    "source_sha256": {"runner": RUNNER_HASH, "analyzer": ANALYZER_HASH},
                    "source_unchanged_during_trial": True, "binary_sha256": "e" * 64,
                    "runner_args": {"policy": policy, "priority": 20 if policy == "fifo" else 0,
                        "period_ns": 100_000_000, "deadline_ns": 100_000_000, "work_ns": 100,
                        "runtime_ns": 200_000, "duration_s": 1, "cpu": 2, "competitor_cpu": 2,
                        "warmup_jobs": 1, "deadline_unpinned": False, "effective_cpu_argument": 2},
                    "cpu_requested": 2, "expected_jobs": 10, "affinity_mode": "pinned", "cpu_affinity": [2],
                    "competitor_cpu_affinity": [2], "comparable": True, "diagnostic": False,
                    "status": "valid", "trace_summary": {"bad": True} if bad_summary and i == 0 else summary,
                    "preflight_report": str(pre), "preflight_sha256": GATE.sha256(pre)}
                if drift and i == 0:
                    manifest["runner_args"]["cpu"] = 99
                mp = root / f"{name}.manifest.json"
                self.put(mp.relative_to(self.root), manifest)
                samples[policy].append({"status": "valid", "run_id": name, "raw_trace": str(raw),
                    "manifest": str(mp), "conditions": common.copy()})
        return self.put("evidence/index.json", {"schema": "xtos-scheduler-m0-evidence-index-v1",
            "fixture_only": fixture, "common": common, "samples": samples})

    def run_gate(self, necessity="native_sufficient", **kwargs):
        ip = self.put("intake.json", intake(necessity))
        return GATE.evaluate(ip, self.evidence(**kwargs))

    def test_m01_strict_intake_types_and_policy_group(self):
        value = intake(); value["target"]["owner"] = None
        value["comparison_protocol"]["identical_workload_and_placement_verified"] = 1
        value["runtime_image"]["xtos_source_commit"] = "A" * 40
        result = GATE.evaluate(self.put("intake.json", value), self.evidence())
        self.assertEqual(result["status"], "blocked_external_inputs")
        self.assertTrue(any("target.owner" in x for x in result["blocking_reasons"]))
        self.assertTrue(any("boolean" in x or "lowercase" in x for x in result["blocking_reasons"]))

    def test_m02_duplicate_artifacts_and_condition_drift_are_rejected(self):
        result = self.run_gate(duplicate=True, drift=True)
        self.assertEqual(result["status"], "evidence_insufficient")
        self.assertTrue(any("duplicate" in x or "cpu" in x for x in result["blocking_reasons"]))

    def test_m03_summary_and_preflight_provenance_are_verified(self):
        result = self.run_gate(bad_summary=True, mode="live_host", fixture=False)
        self.assertEqual(result["status"], "evidence_insufficient")
        self.assertTrue(any("summary" in x for x in result["blocking_reasons"]))

    def test_m04_is_review_only_and_candidate_does_not_require_xtos_runs(self):
        result = self.run_gate(necessity="custom_scheduler_candidate", fixture=False, mode="live_host")
        self.assertEqual(result["status"], "ready_for_independent_review")
        self.assertEqual(result["go_no_go"], "pending_independent_review")
        self.assertFalse(result["hardware_gate_accepted"])
        self.assertFalse(result["claims_custom_scheduler_superiority"])
        self.assertNotIn("xtos:required_for_custom_scheduler_candidate", result["blocking_reasons"])

    def test_fixture_only_is_not_physical_ready(self):
        result = self.run_gate()
        self.assertEqual(result["status"], "fixture_only")
        self.assertFalse(result["hardware_gate_accepted"])


    def test_unmodified_runner_manifest_is_accepted_with_index_supplements(self):
        ip = self.put('intake.json', intake())
        index = self.evidence()
        data = json.loads(index.read_text())
        for entries in data['samples'].values():
            for item in entries:
                mp = Path(item['manifest'])
                manifest = json.loads(mp.read_text())
                item['preflight_report'] = manifest.pop('preflight_report')
                item['preflight_sha256'] = manifest.pop('preflight_sha256')
                manifest['source_sha256']['binary'] = manifest.pop('binary_sha256')
                manifest.pop('run_id')
                manifest.pop('status')
                mp.write_text(json.dumps(manifest))
        index.write_text(json.dumps(data))
        before = {item['manifest']: GATE.sha256(Path(item['manifest']))
                  for group in data['samples'].values() for item in group}
        result = GATE.evaluate(ip, index)
        self.assertEqual(result['status'], 'fixture_only', result['blocking_reasons'])
        self.assertEqual(result['valid_counts']['fifo'], 10)
        self.assertTrue(all(GATE.sha256(Path(p)) == h for p, h in before.items()))


    def test_manifest_metadata_must_match_actual_raw_trace(self):
        ip = self.put('intake.json', intake())
        index = self.evidence()
        data = json.loads(index.read_text())
        item = data['samples']['fifo'][0]
        mp = Path(item['manifest'])
        manifest = json.loads(mp.read_text())
        manifest['trace_metadata']['work_ns'] = 999
        manifest['runner_args']['work_ns'] = 999
        mp.write_text(json.dumps(manifest))
        result = GATE.evaluate(ip, index)
        self.assertTrue(any('metadata' in x for x in result['blocking_reasons']), result)

    def test_conditions_kernel_must_match_intake(self):
        value = intake()
        value['runtime_image']['kernel_image_sha256'] = 'f' * 64
        result = GATE.evaluate(self.put('intake.json', value), self.evidence())
        self.assertEqual(result['status'], 'evidence_insufficient')
        self.assertTrue(any('kernel_image' in x for x in result['blocking_reasons']))

    def test_manifest_raw_path_must_match_index(self):
        ip = self.put('intake.json', intake())
        index = self.evidence()
        data = json.loads(index.read_text())
        mp = Path(data['samples']['fifo'][0]['manifest'])
        manifest = json.loads(mp.read_text())
        manifest['raw_trace'] = data['samples']['fifo'][1]['raw_trace']
        mp.write_text(json.dumps(manifest))
        result = GATE.evaluate(ip, index)
        self.assertEqual(result['status'], 'evidence_insufficient')
        self.assertTrue(any('raw_trace' in x for x in result['blocking_reasons']))

    def test_unknown_policy_protocol_cannot_select_no_runs(self):
        value = intake()
        value['comparison_protocol']['required_native_policies'] = ['unknown']
        result = GATE.evaluate(self.put('intake.json', value), self.evidence())
        self.assertNotEqual(result['status'], 'fixture_only')
        self.assertTrue(any('required_native_policies' in x for x in result['blocking_reasons']))

    def test_missing_all_common_identities_is_rejected(self):
        ip = self.put('intake.json', intake())
        index = self.evidence()
        data = json.loads(index.read_text()); data['common'] = {}
        for entries in data['samples'].values():
            for item in entries:
                item['conditions'] = {}
        index.write_text(json.dumps(data))
        result = GATE.evaluate(ip, index)
        self.assertEqual(result['valid_counts']['fifo'], 0)
        self.assertEqual(result['status'], 'evidence_insufficient')


    def test_malformed_protocol_and_run_ids_fail_closed(self):
        ip = self.put('intake.json', intake())
        index = self.evidence()
        original = json.loads(index.read_text())
        for label in ('minimum', 'policies', 'run_id', 'preflight'):
            with self.subTest(label=label):
                value = intake(); data = json.loads(json.dumps(original))
                if label == 'minimum': value['comparison_protocol']['valid_repetitions_per_policy_minimum'] = None
                if label == 'policies': value['comparison_protocol']['required_native_policies'] = [1]
                if label == 'run_id': data['samples']['fifo'][0]['run_id'] = []
                if label == 'preflight':
                    mp = Path(data['samples']['fifo'][0]['manifest'])
                    manifest = json.loads(mp.read_text()); pre = Path(manifest['preflight_report'])
                    pre.write_text('[]'); manifest['preflight_sha256'] = GATE.sha256(pre)
                    mp.write_text(json.dumps(manifest))
                ip.write_text(json.dumps(value)); index.write_text(json.dumps(data))
                result = GATE.evaluate(ip, index)
                self.assertNotIn(result['status'], ('fixture_only', 'ready_for_independent_review'))

    def test_every_required_intake_field_is_enforced(self):
        fields = [('runtime_image', 'kernel_source_commit'), ('runtime_image', 'running_kernel_identity'),
                  ('runtime_image', 'sys_kernel_realtime_value'), ('target', 'numa_smt_online_cpus'),
                  ('go_no_go', 'decision_owner'), ('go_no_go', 'rationale_and_evidence'),
                  ('evidence', 'evidence_root')]
        for section, field in fields:
            value = intake(); value[section][field] = None
            reasons = GATE._intake_reasons(value)
            self.assertTrue(any(f'{section}.{field}' in r for r in reasons), (section, field))

    def test_index_condition_does_not_override_manifest_identity(self):
        ip = self.put('intake.json', intake()); index = self.evidence()
        data = json.loads(index.read_text()); mp = Path(data['samples']['fifo'][0]['manifest'])
        manifest = json.loads(mp.read_text()); manifest['kernel_image_sha256'] = 'f' * 64
        mp.write_text(json.dumps(manifest))
        result = GATE.evaluate(ip, index)
        self.assertEqual(result['status'], 'evidence_insufficient')
        self.assertTrue(any('kernel_image' in r for r in result['blocking_reasons']))

    def test_duplicate_references_are_not_ten_distinct_executions(self):
        ip = self.put('intake.json', intake()); index = self.evidence()
        data = json.loads(index.read_text())
        for key in ('fifo', 'deadline'): data['samples'][key] = [data['samples'][key][0]] * 10
        index.write_text(json.dumps(data)); result = GATE.evaluate(ip, index)
        self.assertEqual(result['valid_counts']['fifo'], 1)
        self.assertEqual(result['status'], 'evidence_insufficient')

    def test_raw_trace_digest_drift_is_rejected(self):
        ip = self.put('intake.json', intake()); index = self.evidence()
        data = json.loads(index.read_text()); Path(data['samples']['fifo'][0]['raw_trace']).write_text('{}\n')
        result = GATE.evaluate(ip, index)
        self.assertTrue(any('hash_mismatch' in r for r in result['blocking_reasons']))

    def test_output_never_overwrites_intake_or_existing_report(self):
        ip = self.put('intake.json', intake()); index = self.evidence()
        output = self.root / 'out.json'
        GATE.evaluate(ip, index, output); original = output.read_bytes()
        with self.assertRaises(FileExistsError): GATE.evaluate(ip, index, output)
        self.assertEqual(output.read_bytes(), original)
        before = ip.read_bytes()
        with self.assertRaises((ValueError, FileExistsError)): GATE.evaluate(ip, index, ip)
        self.assertEqual(ip.read_bytes(), before)


    def test_preflight_virtualization_and_cpu_are_bound(self):
        ip = self.put('intake.json', intake()); index = self.evidence(fixture=False, mode='live_host')
        data = json.loads(index.read_text()); item = data['samples']['fifo'][0]
        mp = Path(item['manifest']); manifest = json.loads(mp.read_text()); pre = Path(manifest['preflight_report'])
        report = json.loads(pre.read_text()); report['virtualization'] = {'status': 'unknown', 'value': 'unknown'}
        report['cpu'] = {'online': {'value': [99]}, 'allowed': {'value': [99]}}
        pre.write_text(json.dumps(report)); manifest['preflight_sha256'] = GATE.sha256(pre); mp.write_text(json.dumps(manifest))
        result = GATE.evaluate(ip, index)
        self.assertEqual(result['status'], 'evidence_insufficient')
        self.assertTrue(any('preflight' in x for x in result['blocking_reasons']))

    def test_priority_drift_within_policy_is_not_ignored(self):
        ip = self.put('intake.json', intake()); index = self.evidence()
        data = json.loads(index.read_text()); item = data['samples']['fifo'][0]
        raw = Path(item['raw_trace']); rows = [json.loads(line) for line in raw.read_text().splitlines()]
        rows[0]['actual_priority'] = 30; raw.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
        mp = Path(item['manifest']); manifest = json.loads(mp.read_text())
        manifest['trace_metadata'] = rows[0]; manifest['runner_args']['priority'] = 30
        manifest['raw_trace_sha256'] = GATE.sha256(raw); manifest['trace_summary'] = GATE._trace_analyzer().summarize(rows)
        mp.write_text(json.dumps(manifest)); result = GATE.evaluate(ip, index)
        self.assertEqual(result['status'], 'evidence_insufficient')
        self.assertTrue(any('conditions_not_identical' in x for x in result['blocking_reasons']))

    def test_nonfinite_json_and_special_files_fail_before_output(self):
        import subprocess
        ip = self.root / 'bad.json'; ip.write_text('{"schema": 1e999}')
        index = self.evidence(); output = self.root / 'result.json'
        with self.assertRaises(ValueError): GATE.evaluate(ip, index, output)
        self.assertFalse(output.exists())
        fifo = self.root / 'trace.fifo'; os.mkfifo(fifo)
        result = subprocess.run(['python3', str(MODULE), str(fifo), str(index)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2)


    def test_preflight_running_kernel_must_match_intake(self):
        ip = self.put('intake.json', intake()); index = self.evidence()
        data = json.loads(index.read_text()); item = data['samples']['fifo'][0]
        mp = Path(item['manifest']); manifest = json.loads(mp.read_text()); pre = Path(manifest['preflight_report'])
        report = json.loads(pre.read_text()); report['uname']['release'] = 'wrong-kernel'
        pre.write_text(json.dumps(report)); manifest['preflight_sha256'] = GATE.sha256(pre); mp.write_text(json.dumps(manifest))
        result = GATE.evaluate(ip, index)
        self.assertEqual(result['status'], 'evidence_insufficient')
        self.assertTrue(any('kernel' in x for x in result['blocking_reasons']))

    def test_real_time_kernel_value_must_be_one(self):
        value = intake(); value['runtime_image']['sys_kernel_realtime_value'] = '0'
        reasons = GATE._intake_reasons(value)
        self.assertTrue(any('sys_kernel_realtime_value' in x for x in reasons))

    def test_report_binds_consumed_artifact_bytes(self):
        result = self.run_gate()
        self.assertEqual(len(result['verified_artifacts']), 20)
        for item in result['verified_artifacts']:
            self.assertEqual(len(item['raw_trace_sha256']), 64)
            self.assertEqual(len(item['manifest_sha256']), 64)
            self.assertEqual(len(item['preflight_sha256']), 64)

    def test_malformed_top_level_sections_are_not_tracebacks(self):
        ip = self.put('intake.json', intake()); index = self.evidence()
        for section in ('go_no_go', 'comparison_protocol', 'evidence', 'runtime_image', 'target', 'workload'):
            with self.subTest(section=section):
                value = intake(); value[section] = []
                ip.write_text(json.dumps(value))
                result = GATE.evaluate(ip, index)
                self.assertNotIn(result['status'], ('ready_for_independent_review', 'fixture_only'))


    def test_boolean_runner_parameter_cannot_equal_integer(self):
        ip = self.put('intake.json', intake()); index = self.evidence()
        data = json.loads(index.read_text()); mp = Path(data['samples']['fifo'][0]['manifest'])
        manifest = json.loads(mp.read_text()); manifest['runner_args']['duration_s'] = True
        mp.write_text(json.dumps(manifest)); result = GATE.evaluate(ip, index)
        self.assertEqual(result['status'], 'evidence_insufficient')

    def test_fixture_origin_must_be_explicit_boolean(self):
        ip = self.put('intake.json', intake()); index = self.evidence(fixture=False, mode='live_host')
        data = json.loads(index.read_text()); data.pop('fixture_only'); index.write_text(json.dumps(data))
        result = GATE.evaluate(ip, index)
        self.assertEqual(result['status'], 'evidence_insufficient')
        self.assertIn('evidence.fixture_only:boolean_required', result['blocking_reasons'])

    def test_virtualized_manifest_is_not_review_ready(self):
        ip = self.put('intake.json', intake()); index = self.evidence(fixture=False, mode='live_host')
        data = json.loads(index.read_text()); mp = Path(data['samples']['fifo'][0]['manifest'])
        manifest = json.loads(mp.read_text()); manifest['host_is_virtualized'] = True
        mp.write_text(json.dumps(manifest)); result = GATE.evaluate(ip, index)
        self.assertEqual(result['status'], 'evidence_insufficient')

    def test_repeated_preflight_reference_cannot_cover_independent_runs(self):
        ip = self.put('intake.json', intake()); index = self.evidence()
        data = json.loads(index.read_text()); entries = data['samples']['fifo']
        first = json.loads(Path(entries[0]['manifest']).read_text())
        mp = Path(entries[1]['manifest']); manifest = json.loads(mp.read_text())
        manifest['preflight_report'] = first['preflight_report']; manifest['preflight_sha256'] = first['preflight_sha256']
        mp.write_text(json.dumps(manifest)); result = GATE.evaluate(ip, index)
        self.assertEqual(result['status'], 'evidence_insufficient')

    def test_configuration_change_record_is_required(self):
        value = intake(); value['configuration_changes'] = None
        self.assertTrue(any('configuration_changes' in x for x in GATE._intake_reasons(value)))


    def test_task_contract_requires_timing_budget_and_cpu(self):
        value = intake(); value['workload']['task_set'] = [{'name': 'only-name'}]
        self.assertTrue(any('task_set' in x for x in GATE._intake_reasons(value)))

    def test_evidence_root_is_bound_to_intake(self):
        value = intake(); value['evidence']['evidence_root'] = 'different-evidence'
        result = GATE.evaluate(self.put('intake.json', value), self.evidence())
        self.assertEqual(result['status'], 'evidence_insufficient')
        self.assertIn('evidence.root_mismatch', result['blocking_reasons'])

    def test_malformed_extra_policy_group_cannot_be_ignored(self):
        ip = self.put('intake.json', intake()); index = self.evidence()
        data = json.loads(index.read_text()); data['samples']['rr'] = {}
        index.write_text(json.dumps(data)); result = GATE.evaluate(ip, index)
        self.assertEqual(result['status'], 'evidence_insufficient')


    def test_conflicting_binary_hash_representations_are_rejected(self):
        ip = self.put('intake.json', intake()); index = self.evidence(fixture=False, mode='live_host')
        data = json.loads(index.read_text()); mp = Path(data['samples']['fifo'][0]['manifest'])
        manifest = json.loads(mp.read_text()); manifest['source_sha256']['binary'] = 'f' * 64
        mp.write_text(json.dumps(manifest)); result = GATE.evaluate(ip, index)
        self.assertEqual(result['status'], 'evidence_insufficient')
        self.assertTrue(any('binary' in r for r in result['blocking_reasons']))

    def test_conflicting_index_and_manifest_preflight_are_rejected(self):
        ip = self.put('intake.json', intake()); index = self.evidence(fixture=False, mode='live_host')
        data = json.loads(index.read_text()); item = data['samples']['fifo'][0]; mp = Path(item['manifest'])
        manifest = json.loads(mp.read_text()); item['preflight_report'] = manifest['preflight_report']
        item['preflight_sha256'] = manifest['preflight_sha256']; manifest['preflight_report'] = 'missing-conflicting.json'
        manifest['preflight_sha256'] = '0' * 64
        mp.write_text(json.dumps(manifest)); index.write_text(json.dumps(data)); result = GATE.evaluate(ip, index)
        self.assertEqual(result['status'], 'evidence_insufficient')
        self.assertTrue(any('preflight' in r for r in result['blocking_reasons']))


if __name__ == "__main__":
    unittest.main()
