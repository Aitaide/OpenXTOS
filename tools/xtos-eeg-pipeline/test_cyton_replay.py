import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import pty
import shutil
import sys
import tempfile
import threading
import time
import unittest

from cyton_capture import capture_fd, configure_serial_fd
from cyton_replay import CaptureReplayError, main, replay_capture
import cyton_replay
from test_openbci_cyton import packet


class CytonReplayTests(unittest.TestCase):
    def make_capture(self, parent, counters):
        master, slave = pty.openpty()
        configure_serial_fd(slave)
        os.set_blocking(slave, False)
        stop = threading.Event()
        error = []
        output = Path(parent) / f"capture-{len(counters)}"

        def run_capture():
            try:
                capture_fd(slave, output, device_label="pty-fixture",
                           duration_s=0.2, stop_event=stop)
            except BaseException as exc:
                error.append(exc)

        worker = threading.Thread(target=run_capture)
        worker.start()
        try:
            time.sleep(0.03)
            for counter in counters:
                samples = [counter * (channel + 1) for channel in range(8)]
                os.write(master, packet(counter, samples))
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive(), "capture worker failed to finish after timed capture")
            self.assertEqual(error, [])
            return output
        finally:
            stop.set()
            worker.join(timeout=3)
            if worker.is_alive():
                os.close(master)
                master = -1
                worker.join(timeout=1)
            if master >= 0:
                os.close(master)
            os.close(slave)

    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parent
        result = __import__("subprocess").run(
            ["make", "all"], cwd=cls.root, capture_output=True, text=True, timeout=120
        )
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        cls.pipeline = cls.root / "xtos-eeg-pipeline"

    def test_default_pipeline_discovery_matches_staged_rootfs_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            staged = Path(temporary) / "usr" / "share" / "xtos" / "eeg-pipeline"
            staged.mkdir(parents=True)
            binary = Path(temporary) / "usr" / "bin" / "xtos-eeg-pipeline"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\\n")
            binary.chmod(0o755)
            original = cyton_replay.__file__
            try:
                cyton_replay.__file__ = str(staged / "cyton_replay.py")
                resolved = cyton_replay._default_pipeline()
            finally:
                cyton_replay.__file__ = original
            self.assertEqual(resolved, binary)

    def test_staged_rootfs_default_replay_executes_without_pipeline_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged = root / "usr" / "share" / "xtos" / "eeg-pipeline"
            staged.mkdir(parents=True)
            for name in ("openbci_cyton.py", "cyton_capture.py", "cyton_replay.py"):
                shutil.copy2(self.root / name, staged / name)
            binary = root / "usr" / "bin" / "xtos-eeg-pipeline"
            binary.parent.mkdir(parents=True)
            shutil.copy2(self.pipeline, binary)
            bundle = self.make_capture(root, [10, 11, 12, 13])
            output = root / "processed"
            command = [sys.executable, str(staged / "cyton_replay.py"),
                       "--capture", str(bundle), "--output", str(output)]
            result = __import__("subprocess").run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "processed")

    def test_valid_capture_is_hash_checked_and_all_channels_are_filtered(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self.make_capture(temporary, [10, 11, 12, 13])
            output = Path(temporary) / "processed"
            report = replay_capture(bundle, output, pipeline=self.pipeline)
            self.assertEqual(report["status"], "processed")
            self.assertEqual(report["frames_processed"], 4)
            self.assertEqual(report["channel_count"], 8)
            self.assertEqual(report["channel_pairs_processed"], 4)
            self.assertFalse(report["physical_device_evidence"])
            for pair in range(4):
                source = output / f"pair-{pair}.csv"
                filtered = output / f"pair-{pair}-filtered.csv"
                self.assertTrue(source.is_file())
                self.assertTrue(filtered.is_file())
                self.assertEqual(len(filtered.read_text().splitlines()), 5)
                header, *rows = source.read_text().splitlines()
                self.assertEqual(header,
                                 "# xtos-eeg-v1,channels=2,sample_rate_hz=250,unit=nanovolt")
                self.assertEqual(len(rows), 4)
            saved = json.loads((output / "report.json").read_text())
            self.assertEqual(saved, report)
            self.assertEqual(report["manifest_sha256"],
                             hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest())

    def test_replay_rejects_hash_consistent_partial_trailing_packet(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self.make_capture(temporary, [1, 2, 3])
            raw = bundle / "capture.bin"
            payload = raw.read_bytes() + packet(4, [4] * 8)[:7]
            raw.write_bytes(payload)
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["raw_bytes"] = len(payload)
            manifest["raw_capture_sha256"] = __import__("hashlib").sha256(payload).hexdigest()
            manifest["trailing_packet_bytes"] = 7
            manifest["stats"]["bytes_received"] = len(payload)
            manifest_path.write_text(json.dumps(manifest))
            output = Path(temporary) / "processed"
            with self.assertRaisesRegex(CaptureReplayError, "partial packet"):
                replay_capture(bundle, output, pipeline=self.pipeline)
            self.assertFalse(output.exists())

    def test_bundle_ancestor_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "parent"
            real_parent = Path(temporary) / "real-parent"
            real_parent.mkdir()
            bundle = self.make_capture(real_parent, [1, 2, 3])
            parent.symlink_to(real_parent, target_is_directory=True)
            output = Path(temporary) / "processed"
            with self.assertRaisesRegex(CaptureReplayError, "cannot open capture bundle"):
                replay_capture(parent / bundle.name, output, pipeline=self.pipeline)
            self.assertFalse(output.exists())

    def test_stream_stop_failure_is_rejected_even_if_status_claims_completed(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self.make_capture(temporary, [1, 2, 3])
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["stream_start_command"] = "b"
            manifest["stream_stop_command"] = None
            manifest["stream_stop_error"] = "serial command write timed out"
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(CaptureReplayError, "status|error|stream"):
                replay_capture(bundle, Path(temporary) / "processed", pipeline=self.pipeline)

    def test_manifest_parser_stats_must_match_raw_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self.make_capture(temporary, [1, 2, 3])
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["stats"]["bytes_received"] += 1
            manifest_path.write_text(json.dumps(manifest))
            output = Path(temporary) / "processed"
            with self.assertRaisesRegex(CaptureReplayError, "stats"):
                replay_capture(bundle, output, pipeline=self.pipeline)
            self.assertFalse(output.exists())

    def test_counter_gap_fails_closed_without_processing_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self.make_capture(temporary, [20, 21, 23, 24])
            manifest = json.loads((bundle / "manifest.json").read_text())
            self.assertEqual(manifest["stats"]["missing_samples"], 1)
            self.assertEqual(manifest["status"], "sample_gaps")
            output = Path(temporary) / "processed"
            with self.assertRaisesRegex(CaptureReplayError, "sample gap"):
                replay_capture(bundle, output, pipeline=self.pipeline)
            self.assertFalse(output.exists())

    def test_manifest_parser_stats_are_recomputed_not_trusted(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self.make_capture(temporary, [1, 2, 3])
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["stats"].update({"discarded_bytes": 0, "invalid_packets": 0})
            raw = (bundle / "capture.bin").read_bytes()
            noisy = b"noise" + raw
            (bundle / "capture.bin").write_bytes(noisy)
            manifest["raw_bytes"] = len(noisy)
            manifest["raw_capture_sha256"] = hashlib.sha256(noisy).hexdigest()
            manifest["stats"]["bytes_received"] = len(noisy)
            manifest["stats"]["accepted_packets"] = 3
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(CaptureReplayError, "serial noise"):
                replay_capture(bundle, Path(temporary) / "processed", pipeline=self.pipeline)

    def test_raw_and_decoded_frame_count_mismatch_is_structured_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self.make_capture(temporary, [1, 2, 3])
            raw = (bundle / "capture.bin").read_bytes() + packet(4, [4] * 8)
            (bundle / "capture.bin").write_bytes(raw)
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["raw_bytes"] = len(raw)
            manifest["raw_capture_sha256"] = hashlib.sha256(raw).hexdigest()
            manifest["stats"]["bytes_received"] = len(raw)
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(CaptureReplayError, "frame count"):
                replay_capture(bundle, Path(temporary) / "processed", pipeline=self.pipeline)

    def test_quality_and_stream_state_contradictions_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self.make_capture(temporary, [1, 2, 3])
            frames_path = bundle / "frames.jsonl"
            lines = frames_path.read_text().splitlines()
            frame = json.loads(lines[2])
            frame["quality"] = "gap"
            lines[2] = json.dumps(frame)
            frame_data = ("\n".join(lines) + "\n").encode()
            frames_path.write_bytes(frame_data)
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["frames_sha256"] = hashlib.sha256(frame_data).hexdigest()
            manifest["stream_start_command"] = "b"
            manifest["stream_stop_command"] = None
            manifest["stream_stop_error"] = None
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(CaptureReplayError, "quality|stream"):
                replay_capture(bundle, Path(temporary) / "processed", pipeline=self.pipeline)
    def test_raw_capture_hash_mismatch_is_rejected_before_processing(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self.make_capture(temporary, [1, 2, 3])
            raw = bundle / "capture.bin"
            raw.write_bytes(raw.read_bytes() + b"x")
            output = Path(temporary) / "processed"
            with self.assertRaisesRegex(CaptureReplayError, "raw capture hash"):
                replay_capture(bundle, output, pipeline=self.pipeline)
            self.assertFalse(output.exists())

    def test_deeply_nested_json_returns_structured_cli_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self.make_capture(temporary, [1, 2, 3])
            frames_path = bundle / "frames.jsonl"
            lines = frames_path.read_text().splitlines()
            frame = json.loads(lines[1])
            base = json.dumps(frame, separators=(",", ":"))
            nested = "[" * 10000 + "0" + "]" * 10000
            lines[1] = base[:-1] + ',"extra":' + nested + "}"
            frame_data = ("\n".join(lines) + "\n").encode()
            frames_path.write_bytes(frame_data)
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["frames_sha256"] = hashlib.sha256(frame_data).hexdigest()
            manifest_path.write_text(json.dumps(manifest))
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = main(["--capture", str(bundle), "--output",
                               str(Path(temporary) / "processed"), "--pipeline",
                               str(self.pipeline)])
            self.assertEqual(result, 2)
            self.assertIn("cyton_replay_error:", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_relative_pipeline_argument_executes_as_given(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self.make_capture(temporary, [1, 2, 3])
            output = Path(temporary) / "processed"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                relative_pipeline = os.path.relpath(self.pipeline, Path.cwd())
                result = main(["--capture", str(bundle), "--output", str(output),
                               "--pipeline", relative_pipeline])
            self.assertEqual(result, 0, stderr.getvalue())
            self.assertEqual(json.loads(stdout.getvalue())["status"], "processed")

    def test_non_string_quality_types_return_structured_cli_errors(self):
        for invalid_quality in ([], {}):
            with self.subTest(invalid_quality=invalid_quality), tempfile.TemporaryDirectory() as temporary:
                bundle = self.make_capture(temporary, [1, 2, 3])
                frames_path = bundle / "frames.jsonl"
                lines = frames_path.read_text().splitlines()
                frame = json.loads(lines[1])
                frame["quality"] = invalid_quality
                lines[1] = json.dumps(frame)
                frame_data = ("\n".join(lines) + "\n").encode()
                frames_path.write_bytes(frame_data)
                manifest_path = bundle / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                manifest["frames_sha256"] = hashlib.sha256(frame_data).hexdigest()
                manifest_path.write_text(json.dumps(manifest))
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    result = main(["--capture", str(bundle), "--output",
                                   str(Path(temporary) / "processed"), "--pipeline",
                                   str(self.pipeline)])
                self.assertEqual(result, 2)
                self.assertIn("cyton_replay_error:", stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())
    def test_non_object_pipeline_summary_returns_structured_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self.make_capture(temporary, [1, 2, 3])
            fake = Path(temporary) / "pipeline"
            fake.write_text(
                f"#!{sys.executable}\n"
                "import pathlib, sys\n"
                "out = pathlib.Path(sys.argv[sys.argv.index('--dump-filtered') + 1])\n"
                "out.write_text('filtered\\n')\n"
                "print('[]')\n"
            )
            fake.chmod(0o755)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = main(["--capture", str(bundle), "--output",
                               str(Path(temporary) / "processed"), "--pipeline", str(fake)])
            self.assertEqual(result, 2)
            self.assertIn("cyton_replay_error:", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_refuses_to_overwrite_existing_processed_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self.make_capture(temporary, [1, 2, 3])
            output = Path(temporary) / "processed"
            output.mkdir()
            marker = output / "keep"
            marker.write_text("prior evidence")
            with self.assertRaises(FileExistsError):
                replay_capture(bundle, output, pipeline=self.pipeline)
            self.assertEqual(marker.read_text(), "prior evidence")


if __name__ == "__main__":
    unittest.main()
