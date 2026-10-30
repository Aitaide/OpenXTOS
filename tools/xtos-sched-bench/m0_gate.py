#!/usr/bin/env python3
"""Validate M0 evidence structure and consistency without approving hardware or M2."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
import sys

SCHEMA = "xtos-scheduler-m0-intake-v1"
INDEX_SCHEMA = "xtos-scheduler-m0-evidence-index-v1"
REPORT_SCHEMA = "xtos-scheduler-m0-gate-report-v1"
TRACE_SCHEMA = "xtos-sched-trace-v1"
POLICIES = ("fifo", "rr", "deadline")
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_TRACE_BYTES = 64 * 1024 * 1024
MAX_SAMPLES = 1000
MAX_JOBS = 100000
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _reject_constant(value):
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _load_json(path: Path, limit=MAX_JSON_BYTES):
    return _parse_json(_read_bytes(path, limit)[0])


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError('non-finite JSON number')
    return parsed


def _parse_json(raw):
    return json.loads(raw.decode('utf-8'), parse_constant=_reject_constant,
                      parse_float=_finite_float, object_pairs_hook=_unique_pairs)


def _read_bytes(path, limit):
    """Read one bounded ordinary-file snapshot without following symlinks."""
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('parent path traversal is not permitted')
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise ValueError('input must be a bounded regular file')
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            if len(raw) > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('input exceeded limit or changed during read')
            return raw, (before.st_dev, before.st_ino)
    finally:
        os.close(directory)


def _regular(path: Path, label: str):
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ValueError(f"{label} is unreadable: {path}") from exc
    if not stat.S_ISREG(mode):
        raise ValueError(f"{label} must be a regular file: {path}")


def _trace_analyzer():
    path = Path(__file__).with_name("analyze_trace.py")
    spec = importlib.util.spec_from_file_location("xtos_m0_trace_analyzer", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _resolve_child(root: Path, value, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{label} must be a non-empty path")
    root = root.resolve()
    candidate = (Path(value) if Path(value).is_absolute() else root / value).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes evidence root") from exc
    _regular(candidate, label)
    return candidate


def _nonempty(value, path, reasons):
    if isinstance(value, bool) or not isinstance(value, str) or not value.strip():
        reasons.append(f"{path}:required_nonempty_string")
        return False
    return True


def _hash(value, path, reasons, length):
    if not isinstance(value, str) or not (HEX40.fullmatch(value) if length == 40 else HEX64.fullmatch(value)):
        reasons.append(f"{path}:invalid_lowercase_hex_{length}")
        return False
    return True


def _intake_reasons(intake: dict) -> list[str]:
    reasons = []
    if not isinstance(intake, dict):
        return ["intake:object_required"]
    if intake.get("schema") != SCHEMA:
        return ["schema"]
    sections = {name: intake.get(name) for name in ("target", "runtime_image", "workload", "comparison_protocol", "evidence", "go_no_go", "configuration_changes")}
    for name, value in sections.items():
        if not isinstance(value, dict):
            reasons.append(f"{name}:object_required")
    string_fields = {
        "target": ("owner", "asset_id", "physical_platform_attestation", "vendor_model_board",
                   "cpu_model_microcode_topology", "numa_smt_online_cpus", "firmware_bios_revision",
                   "power_frequency_policy", "target_access_or_operator_run_method",
                   "maintenance_window_and_recovery_owner"),
        "runtime_image": ("xtos_source_commit", "kernel_source_commit", "kernel_config_sha256",
                           "kernel_image_sha256", "running_kernel_identity",
                           "sys_kernel_realtime_value", "independent_fallback_image_and_boot_method"),
        "workload": ("business_or_system_use_case", "workload_owner", "real_application_or_device",
                      "input_capture_and_replay_artifact", "dependencies_blocking_and_locking",
                      "device_irq_and_cpu_placement", "background_interference",
                      "overload_and_missed_deadline_behavior", "wcet_or_budget_estimation_method_and_evidence"),
        "comparison_protocol": ("primary_metric", "minimum_meaningful_improvement",
                                 "maximum_allowed_regression_by_metric", "deadline_miss_acceptance",
                                 "run_duration_and_reset_or_warmup_rule", "interleaving_or_randomization_rule",
                                 "confidence_interval_and_outlier_rule", "independent_reviewer"),
        "go_no_go": ("decision_owner", "decision_date", "rationale_and_evidence"),
        "evidence": ("evidence_root",),
        "configuration_changes": ('default', 'before_after_evidence_location', 'rollback_owner_and_method'),
    }
    for section, fields in string_fields.items():
        obj = sections[section]
        if isinstance(obj, dict):
            for field in fields:
                if not _nonempty(obj.get(field), f"{section}.{field}", reasons):
                    continue
                if field in ("xtos_source_commit", "kernel_source_commit"):
                    _hash(obj[field], f"{section}.{field}", reasons, 40)
                elif field.endswith("sha256"):
                    _hash(obj[field], f"{section}.{field}", reasons, 64)
    protocol = sections["comparison_protocol"]
    if isinstance(protocol, dict):
        policies = protocol.get("required_native_policies")
        if not isinstance(policies, list) or not policies or any(not isinstance(x, str) for x in policies):
            reasons.append("comparison_protocol.required_native_policies:nonempty_string_list")
        elif (any(x not in {'SCHED_FIFO_or_SCHED_RR', 'SCHED_FIFO', 'SCHED_RR', 'SCHED_DEADLINE'} for x in policies)
              or 'SCHED_DEADLINE' not in policies
              or not any(x in policies for x in ('SCHED_FIFO_or_SCHED_RR', 'SCHED_FIFO', 'SCHED_RR'))):
            reasons.append('comparison_protocol.required_native_policies:requires_rt_and_deadline')
        minimum = protocol.get("valid_repetitions_per_policy_minimum")
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 10:
            reasons.append("comparison_protocol.valid_repetitions_per_policy_minimum:integer_at_least_10")
        if protocol.get("identical_workload_and_placement_verified") is not True:
            reasons.append("comparison_protocol.identical_workload_and_placement_verified:boolean_true")
    workload = sections["workload"]
    if isinstance(workload, dict) and (not isinstance(workload.get("task_set"), list) or not workload["task_set"]):
        reasons.append("workload.task_set:nonempty_list")
    elif isinstance(workload, dict):
        names = set()
        for number, task in enumerate(workload['task_set']):
            label = f'workload.task_set[{number}]'
            if not isinstance(task, dict):
                reasons.append(label + ':object_required')
                continue
            name = task.get('name')
            if not isinstance(name, str) or not name.strip() or name in names:
                reasons.append(label + '.name:unique_nonempty_string')
            else:
                names.add(name)
            for field in ('period_ns', 'deadline_ns', 'runtime_ns', 'cpu'):
                value = task.get(field)
                if type(value) is not int or value < (0 if field == 'cpu' else 1):
                    reasons.append(label + '.' + field + ':integer_required')
            if all(type(task.get(k)) is int for k in ('period_ns', 'deadline_ns', 'runtime_ns')):
                if not 0 < task['runtime_ns'] <= task['deadline_ns'] <= task['period_ns']:
                    reasons.append(label + ':budget_deadline_period_invalid')
    runtime = sections['runtime_image']
    if isinstance(runtime, dict) and runtime.get('sys_kernel_realtime_value') != '1':
        reasons.append('runtime_image.sys_kernel_realtime_value:must_be_1')
    evidence = sections["evidence"]
    changes = sections['configuration_changes']
    if isinstance(changes, dict) and not isinstance(changes.get('explicitly_authorized_changes'), list):
        reasons.append('configuration_changes.explicitly_authorized_changes:list_required')
    if isinstance(evidence, dict):
        for field in ("separate_preflight_report_per_run", "all_raw_traces_retained_immutable",
                      "runner_analyzer_source_hashes_recorded", "failed_and_rejected_runs_retained"):
            if evidence.get(field) is not True:
                reasons.append(f"evidence.{field}:boolean_true")
    necessity = sections["go_no_go"].get("custom_scheduler_necessity") if isinstance(sections["go_no_go"], dict) else None
    if necessity not in {"native_sufficient", "custom_scheduler_candidate", "evidence_insufficient"}:
        reasons.append("go_no_go.custom_scheduler_necessity:known_value")
    return sorted(set(reasons))


def _selected_policies(protocol: dict, counts):
    requested = protocol.get("required_native_policies", [])
    result = set()
    if not isinstance(requested, list):
        return result
    minimum = protocol.get('valid_repetitions_per_policy_minimum', 10)
    if type(minimum) is not int or minimum < 10:
        minimum = 10
    for value in requested:
        if not isinstance(value, str):
            continue
        upper = value.upper()
        if "FIFO_OR_SCHED_RR" in upper or "FIFO_OR_RR" in upper:
            result.add("fifo" if counts['fifo'] >= minimum or counts['rr'] < minimum else "rr")
        elif "FIFO" in upper:
            result.add("fifo")
        elif "RR" in upper:
            result.add("rr")
        elif "DEADLINE" in upper:
            result.add("deadline")
    return result


def _condition_hashes(common, item):
    conditions = item.get("conditions")
    if not isinstance(conditions, dict):
        raise ValueError("conditions_missing")
    keys = ("kernel_source_commit", "kernel_config_sha256", "kernel_image_sha256",
            "workload_sha256", "placement_sha256", "runner_sha256", "analyzer_sha256")
    if any(key not in common or key not in conditions for key in keys):
        raise ValueError("conditions_incomplete")
    if any(conditions[key] != common[key] for key in keys):
        raise ValueError("conditions_common_mismatch")
    for key in keys:
        if not isinstance(conditions[key], str) or not (HEX40.fullmatch(conditions[key]) if key == "kernel_source_commit" else HEX64.fullmatch(conditions[key])):
            raise ValueError(f"conditions_{key}_format")
    return tuple(conditions[key] for key in keys)


def _compare_field(meta, args, field, errors):
    if field not in meta or field not in args or type(meta[field]) is not type(args[field]) or meta[field] != args[field]:
        errors.append(f"runner_metadata_{field}_mismatch")


def _valid_sample(root, item, policy, common, analyzer, seen_raw, seen_manifest, seen_preflight, fixture_only, runtime):
    if not isinstance(item, dict):
        return False, "sample_not_object", None, None
    if item.get("status") != "valid":
        return False, "sample_rejected", None, None
    try:
        run_id = item.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id_required")
        raw = _resolve_child(root, item.get("raw_trace"), "raw_trace")
        manifest_path = _resolve_child(root, item.get("manifest"), "manifest")
        raw_bytes, raw_id = _read_bytes(raw, MAX_TRACE_BYTES)
        manifest_bytes, manifest_id = _read_bytes(manifest_path, MAX_JSON_BYTES)
        if raw_id in seen_raw or manifest_id in seen_manifest:
            raise ValueError("duplicate_artifact_inode")
        raw_digest = hashlib.sha256(raw_bytes).hexdigest()
        if raw_digest in seen_raw:
            raise ValueError("duplicate_raw_digest")
        seen_raw.add(raw_digest)
        seen_raw.add(raw_id)
        seen_manifest.add(manifest_id)
        manifest = _parse_json(manifest_bytes)
        if not isinstance(manifest, dict) or manifest.get("schema") != "xtos-sched-manifest-v1":
            raise ValueError("manifest_schema")
        if 'run_id' in manifest and manifest['run_id'] != run_id:
            raise ValueError("run_id_mismatch")
        if manifest.get("raw_trace_sha256") != raw_digest:
            raise ValueError("raw_trace_hash_mismatch")
        if manifest.get("status", "valid") != "valid":
            raise ValueError("manifest_not_valid")
        if manifest.get("raw_trace") is None:
            raise ValueError("manifest_raw_trace_missing")
        if _resolve_child(root, manifest['raw_trace'], 'manifest_raw_trace') != raw:
            raise ValueError('manifest_raw_trace_path_mismatch')
        if manifest.get("source_unchanged_during_trial") is not True:
            raise ValueError("source_unchanged_false")
        if manifest.get('host_is_virtualized') is True and not fixture_only:
            raise ValueError('manifest_virtualized')
        source = manifest.get("source_sha256")
        if not isinstance(source, dict):
            raise ValueError('source_hashes_missing')
        binary_hash = manifest.get('binary_sha256', source.get('binary'))
        if not isinstance(binary_hash, str) or not HEX64.fullmatch(binary_hash):
            raise ValueError("binary_sha256_invalid")
        if 'binary' in source and (not isinstance(source['binary'], str)
                                   or not HEX64.fullmatch(source['binary'])
                                   or source['binary'] != binary_hash):
            raise ValueError('conflicting_binary_hash')
        conditions = _condition_hashes(common, item)
        for key, expected in zip(('kernel_source_commit', 'kernel_config_sha256', 'kernel_image_sha256',
                                  'workload_sha256', 'placement_sha256', 'runner_sha256', 'analyzer_sha256'), conditions):
            if key in manifest and manifest[key] != expected:
                raise ValueError(f'manifest_{key}_mismatch')
        source = manifest.get("source_sha256")
        if not isinstance(source, dict) or source.get("runner") != conditions[5] or source.get("analyzer") != conditions[6]:
            raise ValueError("source_hash_mismatch")
        analyzer_path = Path(__file__).with_name("analyze_trace.py")
        if conditions[6] != sha256(analyzer_path):
            raise ValueError("analyzer_hash_not_current")
        metadata = manifest.get("trace_metadata")
        if not isinstance(metadata, dict) or metadata.get("schema") != TRACE_SCHEMA:
            raise ValueError("trace_metadata_missing")
        args = manifest.get("runner_args")
        if not isinstance(args, dict):
            raise ValueError("runner_args_missing")
        if metadata.get("policy") != policy or args.get("policy") != policy:
            raise ValueError("policy_mismatch")
        errors = []
        for field in ("period_ns", "deadline_ns", "work_ns", "runtime_ns", "duration_s", "warmup_jobs"):
            _compare_field(metadata, args, field, errors)
        _compare_field(metadata, args, "actual_priority", errors) if "actual_priority" in args else _compare_field(metadata, {"actual_priority": args.get("priority")}, "actual_priority", errors)
        for field in ("cpu_requested", "affinity_mode", "cpu_affinity", "expected_jobs"):
            if field in args:
                _compare_field(metadata, args, field, errors)
        expected_cpu = args.get("cpu")
        if expected_cpu is None or metadata.get("cpu_requested") != expected_cpu or manifest.get("cpu_requested") != expected_cpu:
            errors.append("cpu_requested_mismatch")
        if manifest.get("cpu_affinity") != metadata.get("cpu_affinity") or manifest.get("cpu_affinity") != [expected_cpu]:
            errors.append("cpu_affinity_mismatch")
        if args.get("competitor_cpu") != expected_cpu or manifest.get("competitor_cpu_affinity") != [args.get("competitor_cpu")]:
            errors.append("competitor_cpu_mismatch")
        if args.get("deadline_unpinned") is not False or metadata.get("affinity_mode") != "pinned" or manifest.get("comparable") is not True:
            errors.append("diagnostic_or_noncomparable")
        if errors:
            raise ValueError(errors[0])
        lines = raw_bytes.splitlines()
        if len(lines) > MAX_JOBS + 1:
            raise ValueError('trace exceeds bounded job count')
        records = [_parse_json(line) for line in lines]
        summary = analyzer.summarize(records)
        if json.dumps(metadata, sort_keys=True) != json.dumps(records[0], sort_keys=True):
            raise ValueError('manifest_metadata_raw_mismatch')
        if summary.get("policy") != policy or summary.get("comparable") is not True:
            raise ValueError("trace_not_comparable")
        if manifest.get("trace_summary") != summary:
            raise ValueError("trace_summary_mismatch")
        if 'preflight_report' in item and 'preflight_report' in manifest:
            if _resolve_child(root, item['preflight_report'], 'index_preflight') != _resolve_child(root, manifest['preflight_report'], 'manifest_preflight'):
                raise ValueError('conflicting_preflight_reference')
        if 'preflight_sha256' in item and 'preflight_sha256' in manifest and item['preflight_sha256'] != manifest['preflight_sha256']:
            raise ValueError('conflicting_preflight_sha256')
        preflight = _resolve_child(root, item.get('preflight_report', manifest.get("preflight_report")), "preflight_report")
        preflight_bytes, preflight_inode = _read_bytes(preflight, MAX_JSON_BYTES)
        if preflight_inode in seen_preflight:
            raise ValueError('duplicate_preflight_reference')
        seen_preflight.add(preflight_inode)
        if item.get('preflight_sha256', manifest.get("preflight_sha256")) != hashlib.sha256(preflight_bytes).hexdigest():
            raise ValueError("preflight_hash_mismatch")
        report = _parse_json(preflight_bytes)
        if not isinstance(report, dict) or report.get("schema") != "xtos-sched-preflight-v1":
            raise ValueError("preflight_schema")
        if report.get("collection_mode") != "live_host" and not (fixture_only and report.get('collection_mode') == 'fixture_filesystem'):
            raise ValueError("preflight_not_live_host")
        uname = report.get('uname')
        if not isinstance(uname, dict) or uname.get('release') != runtime.get('running_kernel_identity'):
            raise ValueError('preflight_running_kernel_mismatch')
        realtime = report.get("realtime", {})
        if not isinstance(realtime, dict) or realtime.get("value") != "1":
            raise ValueError("preflight_not_realtime")
        virtualization = report.get("virtualization", {})
        if not isinstance(virtualization, dict) or virtualization.get("value") == "true" or virtualization.get("detected") is True:
            raise ValueError("preflight_virtualized")
        cpu = report.get('cpu')
        if not isinstance(cpu, dict):
            raise ValueError('preflight_cpu_missing')
        for field in ('online', 'allowed'):
            fact = cpu.get(field)
            if not isinstance(fact, dict) or fact.get('status') != 'available' or not isinstance(fact.get('value'), list) or expected_cpu not in fact['value']:
                raise ValueError(f'preflight_cpu_{field}_mismatch')
        common_fingerprint = tuple(metadata.get(key) for key in ("period_ns", "deadline_ns", "work_ns", "runtime_ns", "duration_s", "warmup_jobs", "expected_jobs", "cpu_requested")) + (tuple(metadata.get("cpu_affinity", [])),) + conditions + (binary_hash,)
        fingerprint = (metadata['actual_priority'], common_fingerprint)
        artifact = {'run_id': run_id, 'policy': policy,
                    'raw_trace': str(raw), 'raw_trace_sha256': raw_digest,
                    'manifest': str(manifest_path), 'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
                    'preflight_report': str(preflight), 'preflight_sha256': hashlib.sha256(preflight_bytes).hexdigest()}
        return True, None, fingerprint, artifact
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return False, str(exc), None, None


def evaluate(intake_path, evidence_index_path, output_path=None):
    intake_path = Path(intake_path)
    index_path = Path(evidence_index_path)
    intake_bytes, _ = _read_bytes(intake_path, MAX_JSON_BYTES)
    index_bytes, _ = _read_bytes(index_path, MAX_JSON_BYTES)
    intake = _parse_json(intake_bytes)
    index = _parse_json(index_bytes)
    blocking = _intake_reasons(intake)
    if not isinstance(index, dict) or index.get("schema") != INDEX_SCHEMA:
        blocking.append("evidence.schema")
        index = index if isinstance(index, dict) else {}
    root = index_path.parent.resolve()
    common = index.get("common")
    samples = index.get("samples")
    if not isinstance(common, dict):
        blocking.append("evidence.common")
        common = {}
    if not isinstance(samples, dict):
        blocking.append("evidence.samples")
        samples = {}
    if type(index.get('fixture_only')) is not bool:
        blocking.append('evidence.fixture_only:boolean_required')
    evidence = intake.get('evidence') if isinstance(intake, dict) else None
    if isinstance(evidence, dict) and isinstance(evidence.get('evidence_root'), str):
        declared = Path(evidence['evidence_root'])
        declared = declared if declared.is_absolute() else intake_path.parent / declared
        if declared.resolve() != root:
            blocking.append('evidence.root_mismatch')
    runtime = intake.get('runtime_image', {}) if isinstance(intake, dict) else {}
    if isinstance(runtime, dict):
        for key in ('kernel_source_commit', 'kernel_config_sha256', 'kernel_image_sha256'):
            if common.get(key) != runtime.get(key) or common.get(key) is None:
                blocking.append(f'conditions_intake_{key}_mismatch')
    analyzer = _trace_analyzer()
    counts = {policy: 0 for policy in POLICIES}
    errors = []
    fingerprints = {policy: set() for policy in POLICIES}
    artifacts = []
    seen_raw, seen_manifest, seen_runs, seen_preflight = set(), set(), set(), set()
    for policy in POLICIES:
        entries = samples.get(policy, [])
        if not isinstance(entries, list) or len(entries) > MAX_SAMPLES:
            errors.append(f"{policy}:samples_bounded_list")
            blocking.append(f'{policy}:samples_bounded_list')
            continue
        for number, item in enumerate(entries):
            run_id = item.get("run_id") if isinstance(item, dict) else None
            if run_id is not None and (not isinstance(run_id, str) or not run_id):
                errors.append(f'{policy}[{number}]:invalid_run_id')
                blocking.append(f'{policy}[{number}]:invalid_run_id')
                continue
            if run_id in seen_runs:
                errors.append(f"{policy}[{number}]:duplicate_run_id")
                blocking.append(f"{policy}[{number}]:duplicate_run_id")
                continue
            if run_id is not None:
                seen_runs.add(run_id)
            valid, error, fingerprint, artifact = _valid_sample(root, item, policy, common, analyzer, seen_raw, seen_manifest, seen_preflight, index.get('fixture_only') is True, runtime if isinstance(runtime, dict) else {})
            if valid:
                counts[policy] += 1
                fingerprints[policy].add(fingerprint)
                artifacts.append(artifact)
            else:
                text = f"{policy}[{number}]:{error}"
                errors.append(text)
                if isinstance(item, dict) and item.get("status") == "valid":
                    blocking.append(text)
    protocol = intake.get("comparison_protocol", {}) if isinstance(intake, dict) else {}
    if not isinstance(protocol, dict):
        protocol = {}
    selected = _selected_policies(protocol if isinstance(protocol, dict) else {}, counts)
    for policy in sorted(selected):
        minimum = protocol.get("valid_repetitions_per_policy_minimum", 10)
        if type(minimum) is not int or minimum < 10:
            minimum = 10
        if counts[policy] < minimum:
            blocking.append(f"{policy}:requires_at_least_{minimum}_valid_comparable_samples")
        if len(fingerprints[policy]) > 1:
            blocking.append(f"{policy}:conditions_not_identical")
    comparable = [next(iter(values))[1] for policy, values in fingerprints.items() if policy in selected and len(values) == 1]
    if len(set(comparable)) > 1:
        blocking.append("native_policy_conditions_not_identical")
    blocking = sorted(set(blocking))
    fixture_only = index.get("fixture_only") is True

    if blocking:
        status = "blocked_external_inputs" if any(x.startswith(("target.", "runtime_image.", "workload.")) for x in blocking) else "evidence_insufficient"
    elif fixture_only:
        status = "fixture_only"
    else:
        status = "ready_for_independent_review"
    report = {"schema": REPORT_SCHEMA, "status": status, "fixture_only": fixture_only,
              "valid_counts": counts, "minimum_valid_per_native_policy": protocol.get("valid_repetitions_per_policy_minimum", 10),
              "sample_errors": errors, "blocking_reasons": blocking,
              "verified_artifacts": artifacts,
              "go_no_go": "pending_independent_review" if status == "ready_for_independent_review" else "blocked",
              "hardware_gate_accepted": False, "claims_custom_scheduler_superiority": False,
              "conditions_fingerprints": {policy: len(fingerprints[policy]) for policy in POLICIES},
              "intake_sha256": hashlib.sha256(intake_bytes).hexdigest(), "evidence_index_sha256": hashlib.sha256(index_bytes).hexdigest(),
              "limitations": ["This verifier checks structural and cross-artifact consistency only.",
                              "It does not evaluate prose thresholds or human attestations.",
                              "Fixture, QEMU, virtualized, and host evidence cannot close physical PREEMPT_RT M0.",
                              "Independent human review is required before any kernel scheduler development decision."]}
    payload = (json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if output_path is not None:
        output = Path(output_path)
        try:
            if output.exists() and os.path.samefile(output, intake_path):
                raise ValueError("output aliases intake")
            if output.exists() and os.path.samefile(output, index_path):
                raise ValueError("output aliases evidence index")
        except FileNotFoundError:
            pass
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as stream:
            stream.write(payload)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("intake", type=Path)
    parser.add_argument("evidence_index", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = evaluate(args.intake, args.evidence_index, args.output)
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"m0_gate_error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
