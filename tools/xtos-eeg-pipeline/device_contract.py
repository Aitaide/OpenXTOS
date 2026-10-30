#!/usr/bin/env python3
"""Validate XTOS EEG device reports without claiming physical or medical proof."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import stat
import sys

SCHEMA = "xtos-eeg-device-validation-v1"
REPORT_SCHEMA = "xtos-eeg-device-validation-report-v1"
UINT64_MAX = (1 << 64) - 1
MAX_CHANNEL_COUNT = 256
MAX_SAMPLE_RATE_HZ = 1_000_000
MAX_FRAMES = 100_000
MAX_EVENTS = 1_024
MAX_MISSING_SEQUENCES = 1_000_000
MAX_FILE_BYTES = 16 * 1024 * 1024
HASH64 = re.compile(r"[0-9a-f]{64}\Z")

TOP_FIELDS = {
    "schema", "mode", "device", "sample_contract", "events", "frames",
    "recovery", "loss_accounting", "evidence",
}
DEVICE_FIELDS = {
    "owner", "transport", "device_identity", "firmware",
    "physical_attestation", "capture_artifact",
}
CONTRACT_FIELDS = {
    "timestamp", "sequence", "channel_count", "sample_rate_hz", "unit",
    "quality_flags",
}
FRAME_FIELDS = {
    "sequence", "timestamp_ns", "channels", "sample_rate_hz", "unit",
    "quality", "enqueue_ns", "consume_ns",
}
EVENT_FIELDS = {"type", "timestamp_ns"}
RECOVERY_FIELDS = {
    "disconnect_tested", "reconnect_tested", "recovered", "hotplug_tested",
    "device_unbind_tested",
}
LOSS_FIELDS = {"received", "dropped", "duplicates", "missing_sequences"}
EVIDENCE_FIELDS = {
    "raw_capture_sha256", "driver_source_sha256", "reader_source_sha256",
    "preflight_report", "independent_reviewer",
}
EVENT_TYPES = {"connected", "disconnected", "reconnected", "hotplug", "device_unbind"}
EVENT_TRANSITIONS = {
    "connected": {"disconnected"},
    "disconnected": {"reconnected"},
    "reconnected": {"disconnected", "hotplug", "device_unbind"},
    "hotplug": {"reconnected"},
    "device_unbind": {"reconnected"},
}


def _required(value, name, reasons):
    if value is None or value == "" or value == []:
        reasons.append(name)


def _object(value, name, allowed, required=()):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ValueError(f"{name} contains unknown field {unknown[0]}")
    missing = sorted(set(required) - set(value))
    if missing:
        raise ValueError(f"{name} missing {missing[0]}")
    return value


def _string(value, name, reasons=None):
    if value is None or value == "":
        if reasons is not None:
            reasons.append(name)
            return
        raise ValueError(f"{name} must be a nonempty string")
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a nonempty string")


def _uint64(value, name, nonzero=False):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a uint64")
    if value < (1 if nonzero else 0) or value > UINT64_MAX:
        qualifier = "nonzero uint64" if nonzero else "uint64"
        raise ValueError(f"{name} must be a {qualifier}")
    return value


def _bounded_int(value, name, lower, upper):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < lower or value > upper:
        raise ValueError(f"{name} is outside the allowed range")
    return value


def _percentiles(values):
    ordered = sorted(values)
    if not ordered:
        raise ValueError("latency samples are empty")

    def rank(percentile):
        return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]

    return {"p50": rank(0.50), "p95": rank(0.95),
            "p99": rank(0.99), "max": ordered[-1]}


def _validate_events(events, blocking):
    if not isinstance(events, list) or not events:
        raise ValueError("events must be a nonempty list")
    if len(events) > MAX_EVENTS:
        raise ValueError("events exceed capacity")

    previous_type = None
    previous_clock = None
    observed = set()
    for index, event in enumerate(events):
        _object(event, f"event {index}", EVENT_FIELDS, EVENT_FIELDS)
        event_type = event["type"]
        if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
            raise ValueError(f"event {index} has unknown lifecycle event type")
        event_clock = _uint64(event["timestamp_ns"],
                              f"event {index} timestamp", nonzero=True)
        if previous_clock is not None and event_clock <= previous_clock:
            raise ValueError(f"event {index} timestamp is not strictly increasing")
        if previous_type is None:
            if event_type != "connected":
                raise ValueError("event order must start with connected")
        elif event_type not in EVENT_TRANSITIONS[previous_type]:
            raise ValueError(f"event order does not allow {previous_type}->{event_type}")
        observed.add(event_type)
        previous_type = event_type
        previous_clock = event_clock

    for event_type in sorted(EVENT_TYPES):
        if event_type not in observed:
            blocking.append(f"events.{event_type}")
    if previous_type not in {"connected", "reconnected"}:
        blocking.append("events.final_connected_state")


def _validate_recovery(recovery, mode, blocking):
    if not isinstance(recovery, dict):
        raise ValueError("recovery must be an object")
    unknown = sorted(set(recovery) - RECOVERY_FIELDS)
    if unknown:
        raise ValueError(f"recovery contains unknown field {unknown[0]}")
    for field in RECOVERY_FIELDS:
        if field not in recovery:
            blocking.append(f"recovery.{field}")
        elif not isinstance(recovery[field], bool):
            raise ValueError(f"recovery.{field} must be a boolean")
    if recovery.get("recovered") is not True:
        blocking.append("recovery.recovered")
    if mode == "live_device":
        for field in ("disconnect_tested", "reconnect_tested", "hotplug_tested",
                      "device_unbind_tested"):
            if recovery.get(field) is not True:
                blocking.append(f"recovery.{field}")


def _validate_loss(loss, frames, expected_missing):
    _object(loss, "loss_accounting", LOSS_FIELDS, LOSS_FIELDS)
    received = _uint64(loss["received"], "loss_accounting.received")
    dropped = _uint64(loss["dropped"], "loss_accounting.dropped")
    duplicates = _uint64(loss["duplicates"], "loss_accounting.duplicates")
    declared = loss["missing_sequences"]
    if not isinstance(declared, list):
        raise ValueError("loss accounting missing_sequences must be a list")
    if len(declared) > MAX_MISSING_SEQUENCES:
        raise ValueError("missing sequence ledger exceeds capacity")
    previous = None
    for index, sequence in enumerate(declared):
        sequence = _uint64(sequence, f"missing_sequences[{index}]")
        if previous is not None and sequence <= previous:
            raise ValueError("missing sequence ledger must be strictly increasing")
        previous = sequence
    if received != len(frames):
        raise ValueError("loss accounting received does not match frames")
    if duplicates != 0:
        raise ValueError("loss accounting duplicates must be zero")
    if dropped != len(expected_missing) or declared != expected_missing:
        raise ValueError("loss accounting does not match observed sequence gaps")
    return dropped, duplicates, declared


def evaluate(document: dict) -> dict:
    if not isinstance(document, dict):
        raise ValueError("device report must be an object")
    unknown = sorted(set(document) - TOP_FIELDS)
    if unknown:
        raise ValueError(f"device report contains unknown field {unknown[0]}")
    for field in TOP_FIELDS:
        if field not in document:
            raise ValueError(f"device report missing {field}")

    if document.get("schema") != SCHEMA:
        raise ValueError("unsupported device contract schema")
    mode = document.get("mode")
    if mode not in {"live_device", "fixture_replay"}:
        raise ValueError("mode must be live_device or fixture_replay")
    device = _object(document["device"], "device", DEVICE_FIELDS)
    contract = _object(document["sample_contract"], "sample_contract",
                        CONTRACT_FIELDS, CONTRACT_FIELDS)
    recovery = document["recovery"]
    evidence = _object(document["evidence"], "evidence", EVIDENCE_FIELDS)
    loss = document["loss_accounting"]
    frames = document["frames"]
    events = document["events"]
    if not isinstance(frames, list) or not frames:
        raise ValueError("frames must be a nonempty list")
    if len(frames) > MAX_FRAMES:
        raise ValueError("frames exceed capacity")

    blocking = []
    device_required = ("owner", "transport", "device_identity",
                       "physical_attestation", "capture_artifact")
    for name in device_required:
        _required(device.get(name), f"device.{name}", blocking)
        if name in device and device[name] not in (None, ""):
            _string(device[name], f"device.{name}")
    if "firmware" in device and device["firmware"] is not None:
        _string(device["firmware"], "device.firmware")

    for name in ("raw_capture_sha256", "driver_source_sha256", "reader_source_sha256",
                 "preflight_report", "independent_reviewer"):
        _required(evidence.get(name), f"evidence.{name}", blocking)
        if name in evidence and evidence[name] not in (None, ""):
            _string(evidence[name], f"evidence.{name}")
    for name in ("raw_capture_sha256", "driver_source_sha256", "reader_source_sha256"):
        if name in evidence and evidence[name] not in (None, "") \
                and HASH64.fullmatch(evidence[name]) is None:
            raise ValueError(f"evidence.{name} must be lowercase 64-hex hash")

    if contract["timestamp"] != "device_monotonic_ns":
        blocking.append("sample_contract.timestamp")
    if contract["sequence"] != "uint64":
        blocking.append("sample_contract.sequence")
    if contract["unit"] != "nanovolt":
        blocking.append("sample_contract.unit")
    channel_count = _bounded_int(contract["channel_count"],
                                 "sample_contract.channel_count", 1,
                                 MAX_CHANNEL_COUNT)
    sample_rate = _bounded_int(contract["sample_rate_hz"],
                                "sample_contract.sample_rate_hz", 1,
                                MAX_SAMPLE_RATE_HZ)
    quality_flags = contract["quality_flags"]
    if not isinstance(quality_flags, list) or not quality_flags \
            or any(not isinstance(flag, str) or not flag for flag in quality_flags):
        raise ValueError("sample_contract.quality_flags must be nonempty strings")

    previous_sequence = None
    previous_timestamp = None
    latencies = []
    expected_missing = []
    for index, frame in enumerate(frames):
        _object(frame, f"frame {index}", FRAME_FIELDS, FRAME_FIELDS)
        sequence = _uint64(frame["sequence"], f"frame {index} sequence")
        timestamp = _uint64(frame["timestamp_ns"],
                            f"frame {index} timestamp", nonzero=True)
        _bounded_int(frame["channels"], f"frame {index} channels", 1,
                     MAX_CHANNEL_COUNT)
        _bounded_int(frame["sample_rate_hz"], f"frame {index} sample_rate_hz",
                     1, MAX_SAMPLE_RATE_HZ)
        if frame["channels"] != channel_count:
            raise ValueError(f"frame {index} channel_count mismatch")
        if frame["sample_rate_hz"] != sample_rate:
            raise ValueError(f"frame {index} sample_rate mismatch")
        if frame["unit"] != contract["unit"] or frame["unit"] != "nanovolt":
            raise ValueError(f"frame {index} unit mismatch")
        if not isinstance(frame["quality"], str) or not frame["quality"]:
            raise ValueError(f"frame {index} quality must be a nonempty string")
        if frame["quality"] not in quality_flags:
            raise ValueError(f"frame {index} quality is not in sample_contract.quality_flags")
        enqueue = _uint64(frame["enqueue_ns"], f"frame {index} enqueue_ns",
                          nonzero=True)
        consume = _uint64(frame["consume_ns"], f"frame {index} consume_ns",
                          nonzero=True)
        if consume < enqueue:
            raise ValueError(f"frame {index} consume before enqueue")
        if previous_timestamp is not None and timestamp <= previous_timestamp:
            raise ValueError(f"frame {index} timestamp is not strictly increasing")
        if previous_sequence is not None:
            if sequence <= previous_sequence:
                raise ValueError(f"frame {index} sequence is not strictly increasing")
            gap = sequence - previous_sequence - 1
            if len(expected_missing) + gap > MAX_MISSING_SEQUENCES:
                raise ValueError("missing sequence ledger exceeds capacity")
            expected_missing.extend(range(previous_sequence + 1, sequence))
        previous_sequence = sequence
        previous_timestamp = timestamp
        latencies.append(consume - enqueue)

    dropped, duplicates, missing = _validate_loss(loss, frames, expected_missing)
    _validate_events(events, blocking)
    _validate_recovery(recovery, mode, blocking)
    blocking = sorted(set(blocking))
    if mode == "fixture_replay":
        status = "fixture_only"
    elif blocking:
        status = "blocked_external_inputs"
    else:
        status = "live_device_report_consistent"
    return {
        "schema": REPORT_SCHEMA,
        "status": status,
        "physical_device_evidence": False,
        "physical_device_evidence_verified": False,
        "medical_or_hard_rt_acceptance": False,
        "frames_received": len(frames),
        "dropped_frames": dropped,
        "duplicate_frames": duplicates,
        "missing_sequences": missing,
        "latency_ns": _percentiles(latencies),
        "blocking_reasons": blocking,
        "limitations": [
            "source and artifact references are declarations, not verified files",
            "physical transport and capture provenance were not verified",
            "device timestamps and host event clocks are separate domains",
            "enqueue-to-consume is queue interval, not device-to-consumer latency",
            "fixture replay is not physical-device evidence",
            "this checker makes no medical or hard-real-time claim",
        ],
    }


def write_report(report: dict, output: Path):
    payload = (json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2,
                          allow_nan=False) + "\n").encode("utf-8")
    if len(payload) > MAX_FILE_BYTES:
        raise ValueError("report exceeds file capacity")
    if output.is_symlink() or output.exists():
        raise FileExistsError(f"refusing to overwrite output {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_symlink() or output.exists():
        raise FileExistsError(f"refusing to overwrite output {output}")
    with output.open("xb") as stream:
        stream.write(payload)


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value):
    raise ValueError(f"non-finite JSON number: {value}")


def _read_json(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        information = os.fstat(stream.fileno())
        if not stat.S_ISREG(information.st_mode):
            raise ValueError("input must be a regular file")
        if information.st_size > MAX_FILE_BYTES:
            raise ValueError("input exceeds file capacity")
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("input exceeds file capacity")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys,
                      parse_constant=_reject_nonfinite)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        document = _read_json(args.input)
        report = evaluate(document)
        if args.output:
            write_report(report, args.output)
        print(json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2,
                         allow_nan=False))
    except (OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"device_contract_error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())