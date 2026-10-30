import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "tools/xtos-eeg-pipeline/device_contract.py"
SPEC = importlib.util.spec_from_file_location("xtos_device_contract", MODULE)
CONTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTRACT)


def document(mode="live_device", frames=None, disconnected=False, recovered=True):
    frames = frames if frames is not None else [
        {"sequence": 10, "timestamp_ns": 1_000_000_000,
         "channels": 8, "sample_rate_hz": 250, "unit": "nanovolt",
         "quality": "ok", "enqueue_ns": 1_000_001_000,
         "consume_ns": 1_000_003_000},
        {"sequence": 11, "timestamp_ns": 1_004_000_000,
         "channels": 8, "sample_rate_hz": 250, "unit": "nanovolt",
         "quality": "ok", "enqueue_ns": 1_004_001_000,
         "consume_ns": 1_004_004_000},
        {"sequence": 12, "timestamp_ns": 1_008_000_000,
         "channels": 8, "sample_rate_hz": 250, "unit": "nanovolt",
         "quality": "ok", "enqueue_ns": 1_008_001_000,
         "consume_ns": 1_008_005_000},
    ]
    return {
        "schema": "xtos-eeg-device-validation-v1",
        "mode": mode,
        "device": {
            "owner": "bci-owner", "transport": "usb-serial",
            "device_identity": "openbci-cyton-01", "firmware": "fw-1",
            "physical_attestation": "hardware.json",
            "capture_artifact": "capture.bin",
        },
        "sample_contract": {
            "timestamp": "device_monotonic_ns", "sequence": "uint64",
            "channel_count": 8, "sample_rate_hz": 250,
            "unit": "nanovolt", "quality_flags": ["ok", "gap"],
        },
        "events": [
            {"type": "connected", "timestamp_ns": 900_000_000},
            {"type": "disconnected", "timestamp_ns": 1_006_000_000},
            {"type": "reconnected", "timestamp_ns": 1_007_000_000},
            {"type": "hotplug", "timestamp_ns": 1_009_000_000},
            {"type": "reconnected", "timestamp_ns": 1_010_000_000},
            {"type": "device_unbind", "timestamp_ns": 1_011_000_000},
            {"type": "reconnected", "timestamp_ns": 1_012_000_000},
        ],
        "frames": frames,
        "recovery": {
            "disconnect_tested": True,
            "reconnect_tested": True,
            "recovered": recovered,
            "hotplug_tested": True,
            "device_unbind_tested": True,
        },
        "loss_accounting": {"received": len(frames), "dropped": 0,
                             "duplicates": 0, "missing_sequences": []},
        "evidence": {
            "raw_capture_sha256": "a" * 64,
            "driver_source_sha256": "b" * 64,
            "reader_source_sha256": "c" * 64,
            "preflight_report": "preflight.json",
            "independent_reviewer": "reviewer",
        },
    }


class DeviceContractTests(unittest.TestCase):
    def test_live_device_report_is_valid_but_not_medical_acceptance(self):
        result = CONTRACT.evaluate(document())
        self.assertEqual(result["status"], "live_device_report_consistent")
        self.assertEqual(result["frames_received"], 3)
        self.assertEqual(result["dropped_frames"], 0)
        self.assertEqual(result["latency_ns"], {"p50": 3000, "p95": 4000,
                                                "p99": 4000, "max": 4000})
        self.assertFalse(result["medical_or_hard_rt_acceptance"])
        self.assertFalse(result["physical_device_evidence"])
        self.assertFalse(result["physical_device_evidence_verified"])

    def test_fixture_replay_is_explicitly_not_real_device_evidence(self):
        result = CONTRACT.evaluate(document(mode="fixture_replay"))
        self.assertEqual(result["status"], "fixture_only")
        self.assertFalse(result["physical_device_evidence"])

    def test_missing_live_device_fields_are_blocked(self):
        value = document()
        value["device"]["physical_attestation"] = None
        value["recovery"]["disconnect_tested"] = False
        result = CONTRACT.evaluate(value)
        self.assertEqual(result["status"], "blocked_external_inputs")
        self.assertIn("device.physical_attestation", result["blocking_reasons"])
        self.assertIn("recovery.disconnect_tested", result["blocking_reasons"])

    def test_sequence_gap_must_match_loss_accounting(self):
        value = document()
        value["frames"][1]["sequence"] = 12
        value["frames"][2]["sequence"] = 13
        with self.assertRaisesRegex(ValueError, "loss accounting"):
            CONTRACT.evaluate(value)

    def test_duplicate_and_decreasing_sequences_are_rejected(self):
        for sequences in ([10, 10, 11], [10, 9, 8]):
            value = document()
            for frame, sequence in zip(value["frames"], sequences):
                frame["sequence"] = sequence
            with self.subTest(sequences=sequences):
                with self.assertRaisesRegex(ValueError, "strictly increasing"):
                    CONTRACT.evaluate(value)

    def test_multiple_sequence_gaps_are_reconciled_as_one_ledger(self):
        value = document()
        value["frames"][1]["sequence"] = 12
        value["frames"][2]["sequence"] = 14
        value["loss_accounting"] = {
            "received": 3, "dropped": 2, "duplicates": 0,
            "missing_sequences": [11, 13],
        }
        result = CONTRACT.evaluate(value)
        self.assertEqual(result["missing_sequences"], [11, 13])
        self.assertEqual(result["dropped_frames"], 2)

    def test_fake_missing_and_duplicate_ledger_is_rejected(self):
        value = document()
        value["loss_accounting"] = {
            "received": 3, "dropped": 1, "duplicates": 7,
            "missing_sequences": [999],
        }
        with self.assertRaisesRegex(ValueError, "loss accounting"):
            CONTRACT.evaluate(value)

    def test_timestamp_and_sample_contract_must_be_monotonic_and_exact(self):
        value = document()
        value["frames"][1]["timestamp_ns"] = value["frames"][0]["timestamp_ns"]
        with self.assertRaisesRegex(ValueError, "timestamp"):
            CONTRACT.evaluate(value)
        value = document()
        value["frames"][0]["unit"] = "microvolt"
        with self.assertRaisesRegex(ValueError, "unit"):
            CONTRACT.evaluate(value)

    def test_disconnect_without_recovery_is_blocked(self):
        value = document(disconnected=True, recovered=False)
        result = CONTRACT.evaluate(value)
        self.assertEqual(result["status"], "blocked_external_inputs")
        self.assertIn("recovery.recovered", result["blocking_reasons"])

    def test_missing_lifecycle_coverage_is_blocked(self):
        value = document()
        value["events"] = [{"type": "connected", "timestamp_ns": 900_000_000}]
        result = CONTRACT.evaluate(value)
        self.assertEqual(result["status"], "blocked_external_inputs")
        self.assertIn("events.disconnected", result["blocking_reasons"])
        self.assertIn("events.hotplug", result["blocking_reasons"])

    def test_arbitrary_lifecycle_event_is_malformed(self):
        value = document()
        value["events"][1]["type"] = "arbitrary"
        with self.assertRaisesRegex(ValueError, "unknown lifecycle event type"):
            CONTRACT.evaluate(value)

    def test_lifecycle_order_and_event_clock_are_validated(self):
        value = document()
        value["events"][2]["timestamp_ns"] = value["events"][1]["timestamp_ns"]
        with self.assertRaisesRegex(ValueError, "timestamp is not strictly increasing"):
            CONTRACT.evaluate(value)
        value = document()
        value["events"][1], value["events"][2] = value["events"][2], value["events"][1]
        with self.assertRaisesRegex(ValueError, "event order"):
            CONTRACT.evaluate(value)

    def test_failed_recovery_is_blocked_without_disconnect_event(self):
        value = document(recovered=False)
        value["events"] = [{"type": "connected", "timestamp_ns": 900_000_000}]
        result = CONTRACT.evaluate(value)
        self.assertEqual(result["status"], "blocked_external_inputs")
        self.assertIn("recovery.recovered", result["blocking_reasons"])

    def test_fixture_replay_never_infers_physical_evidence(self):
        result = CONTRACT.evaluate(document(mode="fixture_replay"))
        self.assertEqual(result["status"], "fixture_only")
        self.assertFalse(result["physical_device_evidence"])
        self.assertFalse(result["physical_device_evidence_verified"])

    def test_output_is_exclusive(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            root = Path(directory)
            input_path = root / "report.json"
            output_path = root / "summary.json"
            input_path.write_text(json.dumps(document()) + "\n", encoding="utf-8")
            CONTRACT.main([str(input_path), "--output", str(output_path)])
            with self.assertRaises(FileExistsError):
                CONTRACT.write_report(CONTRACT.evaluate(document()), output_path)


    def test_unknown_frame_quality_is_rejected(self):
        value = document()
        value['frames'][0]['quality'] = 'undeclared'
        with self.assertRaisesRegex(ValueError, 'quality'):
            CONTRACT.evaluate(value)

    def test_lifecycle_must_finish_in_a_connected_state(self):
        value = document()
        value['events'].pop()
        result = CONTRACT.evaluate(value)
        self.assertEqual(result['status'], 'blocked_external_inputs')
        self.assertIn('events.final_connected_state', result['blocking_reasons'])


    def test_cli_refuses_fifo_input_without_hanging(self):
        import subprocess
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as directory:
            fifo = Path(directory) / 'input.fifo'
            os.mkfifo(fifo)
            try:
                result = subprocess.run(['python3', str(MODULE), str(fifo)],
                                        capture_output=True, text=True, timeout=10)
            except subprocess.TimeoutExpired:
                self.fail('FIFO input blocked the validator before regular-file validation')
            self.assertEqual(result.returncode, 2)
            self.assertNotIn('Traceback', result.stderr)


if __name__ == "__main__":
    unittest.main()
