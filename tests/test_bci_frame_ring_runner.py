"""Evidence preservation regressions for the production-C frame-ring runner."""
import concurrent.futures
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "frame_ring_evidence_runner", ROOT / "tests/test_bci_frame_ring.py")
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class FrameRingRunnerEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="frame-ring-runner-")
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(RUNNER, "EVIDENCE", self.out).start()

    def invoke(self):
        return RUNNER.run(case="alloc", label="collision")

    def fake_process(self, command, **unused):
        if command[0] == "gcc":
            Path(command[command.index("-o") + 1]).write_bytes(b"new compiler output")
            return SimpleNamespace(returncode=0, stdout="")
        return SimpleNamespace(returncode=0, stdout="alloc\n")

    def test_existing_log_and_binary_reject_before_compilation(self):
        binary = self.out / "collision-normal"
        log = self.out / "collision-normal.json"
        binary.write_bytes(b"original binary")
        log.write_bytes(b"original evidence log")
        with patch.object(RUNNER.subprocess, "run", side_effect=self.fake_process) as process:
            with self.assertRaisesRegex(RuntimeError, "refusing to overwrite evidence"):
                self.invoke()
        self.assertEqual(binary.read_bytes(), b"original binary")
        self.assertEqual(log.read_bytes(), b"original evidence log")
        process.assert_not_called()

    def test_orphan_binary_rejects_without_log_or_compiler_side_effect(self):
        binary = self.out / "collision-normal"
        binary.write_bytes(b"partial earlier build")
        with patch.object(RUNNER.subprocess, "run", side_effect=self.fake_process) as process:
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "refusing to overwrite evidence"):
                    self.invoke()
        process.assert_not_called()
        self.assertEqual(binary.read_bytes(), b"partial earlier build")
        self.assertFalse((self.out / "collision-normal.json").exists())

    def test_two_same_label_calls_allow_only_one_compiler(self):
        started = threading.Event()
        resume = threading.Event()
        calls = []
        guard = threading.Lock()

        def process(command, **unused):
            with guard:
                calls.append(list(command))
                compiler_calls = sum(args[0] == "gcc" for args in calls)
            if command[0] == "gcc":
                if compiler_calls > 1:
                    raise RuntimeError("duplicate compiler reached the claimed destination")
                started.set()
                if not resume.wait(5):
                    raise RuntimeError("test compiler was not released")
            return self.fake_process(command)

        with patch.object(RUNNER.subprocess, "run", side_effect=process):
            with contextlib.redirect_stdout(io.StringIO()):
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(self.invoke)
                    try:
                        self.assertTrue(started.wait(5))
                        second = pool.submit(self.invoke)
                        with self.assertRaisesRegex(RuntimeError, "refusing to overwrite evidence"):
                            second.result(timeout=2)
                    finally:
                        resume.set()
                    self.assertEqual(first.result(timeout=5), 0)
        self.assertEqual(sum(command[0] == "gcc" for command in calls), 1)
        self.assertEqual(len(calls), 2)
        report = json.loads((self.out / "collision-normal.json").read_text())
        self.assertEqual(report["results"][0]["exit"], 0)
        self.assertEqual(report["results"][1]["output"], "alloc\n")
        self.assertEqual((self.out / "collision-normal").read_bytes(), b"new compiler output")

    def test_fresh_run_keeps_existing_report_and_command_schema(self):
        with patch.object(RUNNER.subprocess, "run", side_effect=self.fake_process):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.invoke(), 0)
        report = json.loads((self.out / "collision-normal.json").read_text())
        self.assertEqual(report["mode"], "normal")
        self.assertEqual(report["label"], "collision")
        self.assertEqual(len(report["results"]), 2)
        self.assertEqual(report["results"][1]["command"][-1], "alloc")


if __name__ == "__main__":
    unittest.main()
