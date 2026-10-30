#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import importlib.util
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_cyton_runtime.py"
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("cyton_runtime", SCRIPT)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class CytonRuntimeTests(unittest.TestCase):
    def make_rootfs(self, parent):
        root = Path(parent) / "rootfs"
        source = root / "usr" / "share" / "xtos" / "eeg-pipeline"
        source.mkdir(parents=True)
        for name in ("openbci_cyton.py", "cyton_capture.py", "cyton_replay.py"):
            (source / name).write_text("# fixture\n")
        bin_dir = root / "usr" / "bin"
        bin_dir.mkdir(parents=True)
        interpreter = bin_dir / "python3.13"
        interpreter.write_text("#!/bin/sh\n")
        interpreter.chmod(0o755)
        (bin_dir / "python3").symlink_to("python3.13")
        pipeline = bin_dir / "xtos-eeg-pipeline"
        pipeline.write_text("#!/bin/sh\n")
        pipeline.chmod(0o755)
        return root

    def run_check(self, root, *extra, cwd=None):
        return subprocess.run([sys.executable, str(SCRIPT), "--rootfs", str(root), *extra],
                              capture_output=True, text=True, cwd=cwd, timeout=10)

    def test_source_staged_mode_does_not_require_python(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_rootfs(temporary)
            (root / "usr/bin/python3").unlink()
            (root / "usr/bin/python3.13").unlink()
            result = self.run_check(root, "--mode", "source-staged")
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["mode"], "source-staged")
            self.assertFalse(report["interpreter_checked"])
            self.assertFalse(report["interpreter_runtime_smoke"])
            self.assertFalse(report["physical_device_evidence"])

    def test_python_layouts_and_relative_root_are_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_rootfs(temporary)
            interpreter = root / "usr/bin/python3"
            layouts = ("relative-link", "absolute-link", "regular-file")
            for layout in layouts:
                with self.subTest(layout=layout):
                    interpreter.unlink()
                    if layout == "relative-link":
                        interpreter.symlink_to("python3.13")
                    elif layout == "absolute-link":
                        interpreter.symlink_to("/usr/bin/python3.13")
                    else:
                        interpreter.write_text("#!/bin/sh\n")
                        interpreter.chmod(0o755)
                    result = self.run_check("rootfs", cwd=temporary)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_interpreter_loop_is_a_structured_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_rootfs(temporary)
            target = root / "usr/bin/python3.13"
            target.unlink()
            target.symlink_to("python3")
            result = self.run_check(root)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "fail")

    def test_symlinked_source_ancestor_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_rootfs(temporary)
            original = root / "usr/share/xtos"
            actual = root / "actual"
            original.rename(actual)
            original.symlink_to(actual)
            result = self.run_check(root)
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertEqual(json.loads(result.stdout)["status"], "fail")

    def test_static_check_never_executes_target_interpreter(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_rootfs(temporary)
            marker = Path(temporary) / "executed"
            target = root / "usr/bin/python3.13"
            target.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
            result = self.run_check(root)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertFalse(marker.exists())
            self.assertFalse(json.loads(result.stdout)["interpreter_runtime_smoke"])

    def test_runtime_mode_requires_python(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_rootfs(temporary)
            (root / "usr/bin/python3").unlink()
            result = self.run_check(root)
            self.assertEqual(result.returncode, 2)
            self.assertTrue(json.loads(result.stdout)["interpreter_checked"])

    def test_nonexecutable_filter_is_rejected_in_both_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_rootfs(temporary)
            (root / "usr/bin/xtos-eeg-pipeline").chmod(0o644)
            for mode in ("runtime", "source-staged"):
                with self.subTest(mode=mode):
                    result = self.run_check(root, "--mode", mode)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("executable", result.stdout)

    def test_missing_root_is_structured_and_never_physical_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.run_check(Path(temporary) / "missing")
            self.assertEqual(result.returncode, 2)
            report = json.loads(result.stdout)
            self.assertFalse(report["physical_device_evidence"])
            self.assertFalse(report["interpreter_checked"])

    def test_smoke_cannot_run_on_source_only_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.run_check(self.make_rootfs(temporary), "--mode", "source-staged", "--smoke")
            self.assertEqual(result.returncode, 2)
            self.assertIn("requires runtime mode", result.stderr)

    def test_failed_target_smoke_never_returns_static_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_rootfs(temporary)
            process = mock.Mock(returncode=1, pid=424242)
            process.communicate.return_value = ("", "import error")
            with mock.patch.object(runtime.os, "geteuid", return_value=0), \
                 mock.patch.object(runtime.os, "killpg"), \
                 mock.patch.object(runtime.subprocess, "Popen", return_value=process) as run:
                report = runtime.verify(root, smoke=True)
            self.assertEqual(report["status"], "fail")
            self.assertFalse(report["interpreter_runtime_smoke"])
            self.assertFalse(report["physical_device_evidence"])
            self.assertEqual(report["smoke"]["exit_code"], 1)
            self.assertIn("import error", report["smoke"]["stderr"])
            argv = run.call_args.args[0]
            self.assertEqual(argv[:4], ["chroot", str(root), "/usr/bin/python3", "-I"])
            self.assertIn("-B", argv)
            self.assertFalse(run.call_args.kwargs.get("shell", False))
            self.assertEqual(run.call_args.kwargs["env"].get("PATH"), "/usr/sbin:/usr/bin:/sbin:/bin")
            self.assertGreater(process.communicate.call_args.kwargs["timeout"], 0)
            self.assertTrue(run.call_args.kwargs["start_new_session"])

    def test_malformed_target_smoke_summary_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_rootfs(temporary)
            for output in ("[]", "{}", "not json"):
                process = mock.Mock(returncode=0, pid=424242)
                process.communicate.return_value = (output, "")
                with self.subTest(output=output), \
                     mock.patch.object(runtime.os, "geteuid", return_value=0), \
                     mock.patch.object(runtime.os, "killpg"), \
                     mock.patch.object(runtime.subprocess, "Popen", return_value=process):
                    report = runtime.verify(root, smoke=True)
                    self.assertEqual(report["status"], "fail")
                    self.assertFalse(report["interpreter_runtime_smoke"])

    def test_summary_must_bind_exact_packaged_bytes(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_rootfs(temporary)
            hashes = {name: hashlib.sha256((root / runtime.SOURCE_DIR / name).read_bytes()).hexdigest()
                      for name in runtime.SOURCES}
            value = {"schema": "xtos-cyton-runtime-smoke-v1", "status": "pass",
                     "physical_device_evidence": False, "python_version": [3, 13, 5],
                     "checks": dict.fromkeys(("imports", "renameat2", "synthetic_capture",
                                              "default_replay_cli", "eight_channel_filter"), True),
                     "source_sha256": hashes,
                     "filter_sha256": hashlib.sha256((root / runtime.FILTER_BINARY).read_bytes()).hexdigest(),
                     "python_executable_sha256": hashlib.sha256((root / "usr/bin/python3.13").read_bytes()).hexdigest()}
            for mutate in (False, True):
                with self.subTest(mutate=mutate):
                    if mutate:
                        value["source_sha256"]["cyton_replay.py"] = "0" * 64
                    process = mock.Mock(returncode=0, pid=424242)
                    process.communicate.return_value = (json.dumps(value), "")
                    with mock.patch.object(runtime.os, "geteuid", return_value=0), \
                         mock.patch.object(runtime.os, "killpg"), \
                         mock.patch.object(runtime.subprocess, "Popen", return_value=process):
                        report = runtime.verify(root, smoke=True)
                    self.assertEqual(report["interpreter_runtime_smoke"], not mutate)
                    self.assertEqual(report["status"], "fail" if mutate else "pass")

    def test_timeout_kills_real_process_group_and_reaps_parent(self):
        import time
        real_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_rootfs(temporary)
            pidfile = Path(temporary) / "child.pid"
            script = ("import pathlib,subprocess,sys,time\n"
                      "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                      f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid))\n"
                      "time.sleep(60)\n")
            owned = []
            def launch(*args, **kwargs):
                child = real_popen([sys.executable, "-c", script], **kwargs)
                owned.append(child)
                return child
            try:
                with mock.patch.object(runtime.os, "geteuid", return_value=0), \
                     mock.patch.object(runtime, "SMOKE_TIMEOUT_S", 1), \
                     mock.patch.object(runtime.subprocess, "Popen", side_effect=launch):
                    report = runtime.verify(root, smoke=True)
                self.assertEqual(report["status"], "fail")
                self.assertTrue(report["smoke"]["timed_out"])
                self.assertEqual(owned[0].returncode, -9)
                self.assertTrue(pidfile.is_file(), "descendant must exist before timeout")
                child_pid = int(pidfile.read_text())
                status = Path(f"/proc/{child_pid}/status")
                for _ in range(20):
                    if not status.exists() or "State:\tZ" in status.read_text():
                        break
                    time.sleep(0.01)
                self.assertTrue(not status.exists() or "State:\tZ" in status.read_text(),
                                "smoke descendant remains live after verifier returned")
            finally:
                for child in owned:
                    try:
                        os.killpg(child.pid, 9)
                    except ProcessLookupError:
                        pass
                    child.wait(timeout=3)

    def test_staged_runtime_contract_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.run_check(self.make_rootfs(temporary))
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "pass")
            self.assertFalse(report["interpreter_runtime_smoke"])

    def test_missing_filter_binary_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_rootfs(temporary)
            (root / "usr" / "bin" / "xtos-eeg-pipeline").unlink()
            result = self.run_check(root)
            self.assertNotEqual(result.returncode, 0)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "fail")
            self.assertTrue(any("xtos-eeg-pipeline" in error["path"] for error in report["errors"]))

    def test_symlinked_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_rootfs(temporary)
            source = root / "usr" / "share" / "xtos" / "eeg-pipeline"
            victim = root / "tmp"
            victim.mkdir()
            (source / "cyton_replay.py").unlink()
            (source / "cyton_replay.py").symlink_to(victim)
            result = self.run_check(root)
            self.assertNotEqual(result.returncode, 0)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "fail")
            self.assertTrue(any("cyton_replay.py" in error["path"] for error in report["errors"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
