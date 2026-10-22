"""Killable worker contract; real integration uses an explicit AI Python."""
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/xtos-ai-runtime"))
from xtos_ai_runtime.protocol import RequestError, validate_manifest
from xtos_ai_runtime.worker import MAX_LINE, Worker, WorkerError
from ai_service_trust_fixtures import fixture_ticket, make_real_signed_service_fixture


def _wait_for_file(path, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size:
            return
        time.sleep(.005)
    raise AssertionError(f"fixture did not reach marker: {path}")


def _fixture_command(directory, manifest, mode, marker=None, stderr_size=0):
    """Process mechanism fixture, not ONNX execution evidence."""
    script = Path(directory) / f"worker-fixture-{mode}.py"
    source = '''import json
import os
from pathlib import Path
import sys
import time

MODE, MARKER, HASH, STDERR_SIZE = CONFIG

def reply(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)

def hang():
    if MARKER is not None:
        Path(MARKER).write_text("entered", encoding="ascii")
    while True:
        time.sleep(.05)

load = sys.stdin.buffer.readline(65537)
if not load or not load.endswith(b"\\n"):
    raise SystemExit(2)
if MODE == "hang_load":
    hang()
if MODE == "load_error":
    if STDERR_SIZE:
        data = b"fixture-stderr-" + b"x" * STDERR_SIZE + b"-tail"
        while data:
            data = data[os.write(2, data):]
    reply({"status": "error", "reason": "fixture load failure"})
    raise SystemExit(0)
if MODE == "diagnostic_start":
    os.write(2, b"fixture diagnostic before ready\\n")
if MODE == "fork_descendant":
    import signal
    pid = os.fork()
    if pid == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if MARKER is not None:
            Path(MARKER).write_text(str(os.getpid()), encoding="ascii")
        while True:
            time.sleep(.05)
reply({"status": "ready", "provider": "CPUExecutionProvider", "model_sha256": HASH})
while True:
    line = sys.stdin.buffer.readline(65537)
    if not line:
        raise SystemExit(0)
    request = json.loads(line)
    if MODE == "hang_infer":
        hang()
    if MODE == "diagnostic_infer":
        os.write(2, b"fixture diagnostic before infer result\\n")
    if MODE == "oversized_output":
        data = b"z" * 65537 + b"\\n"
        while data:
            data = data[os.write(1, data):]
        hang()
    reply({"status": "ok", "id": request["id"], "output": [[0.0, 0.0, 0.0]]})
'''
    config = (mode, str(marker) if marker is not None else None, manifest.sha256, stderr_size)
    script.write_text(source.replace("CONFIG", repr(config)), encoding="utf-8")
    return [sys.executable, str(script)]


class FixtureWorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        raw = json.loads((ROOT / "tools/xtos-ai-runtime/model-manifest.example.json").read_text())
        self.manifest = validate_manifest(raw, ROOT)

    def test_bare_manifest_is_not_execution_authority(self):
        with self.assertRaisesRegex((TypeError, ValueError), "AuthorizedModel"):
            Worker(self.manifest)

    def test_invalidated_worker_cannot_restart_and_reaps_sigstop_child(self):
        worker = self.make_worker("ready")
        worker.start()
        pid = worker.pid
        os.kill(pid, signal.SIGSTOP)
        worker.invalidate("model_revoked")
        self.assertTrue(worker.authorized_model.deny_event.is_set())
        self.assertReaped(worker, pid)
        for operation in (worker.start, worker.restart):
            with self.assertRaisesRegex(WorkerError, "model_revoked"):
                operation()
        result = worker.infer("revoked", [[1., 0., 0., 0.]], 1000)
        self.assertEqual(result["reason"], "model_revoked")
        self.assertIsNone(result["output"])

    def make_worker(self, mode, marker=None, manifest=None, stderr_size=0, **kwargs):
        manifest = manifest or self.manifest
        command = _fixture_command(self.root, manifest, mode, marker, stderr_size)
        worker = Worker(fixture_ticket(manifest), python_command=command, **kwargs)
        self.addCleanup(worker.close)
        return worker

    def assertReaped(self, worker, pid):
        self.assertFalse(worker.alive)
        if pid is not None:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            with self.assertRaises(ProcessLookupError):
                os.killpg(pid, 0)
        if worker.process is not None:
            for stream in (worker.process.stdin, worker.process.stdout, worker.process.stderr):
                self.assertTrue(stream is None or stream.closed)

    def test_start_timeout_is_worker_error_and_reaps_child(self):
        worker = self.make_worker("hang_load", marker=self.root / "load.marker", load_timeout=.1)
        with self.assertRaisesRegex(WorkerError, "timeout"):
            worker.start()
        self.assertReaped(worker, worker.pid)

    def test_start_and_restart_cancel_reap_loading_child(self):
        for method in ("start", "restart"):
            marker = self.root / f"{method}.marker"
            worker = self.make_worker("hang_load", marker=marker, load_timeout=5)
            cancel = threading.Event()
            errors = []

            def start():
                try:
                    getattr(worker, method)(cancel=cancel)
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=start)
            thread.start()
            try:
                _wait_for_file(marker)
                began = time.monotonic()
                cancel.set()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertLess(time.monotonic() - began, 2)
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], WorkerError)
                self.assertIn("cancel", str(errors[0]))
                self.assertReaped(worker, worker.pid)
            finally:
                cancel.set()
                thread.join(2)
                worker.close()

    def test_spawn_failure_is_worker_error_without_fd_leak(self):
        before = len(tuple(Path("/proc/self/fd").iterdir()))
        for _ in range(5):
            worker = Worker(fixture_ticket(self.manifest), python_command=[str(self.root / "missing")])
            try:
                with self.assertRaisesRegex(WorkerError, "startup"):
                    worker.start()
                self.assertFalse(worker.alive)
            finally:
                worker.close()
        self.assertEqual(len(tuple(Path("/proc/self/fd").iterdir())), before)

    def test_failed_load_bounds_diagnostics_and_closes_pipes(self):
        worker = self.make_worker("load_error", stderr_size=MAX_LINE * 4)
        with self.assertRaises(WorkerError):
            worker.start()
        diagnostics = worker.diagnostics()
        self.assertLessEqual(len(diagnostics), 8192)
        self.assertTrue(diagnostics.endswith("-tail"), diagnostics[-80:])
        self.assertReaped(worker, worker.pid)
        worker.close()
        self.assertEqual(worker.diagnostics(), diagnostics)

    def test_external_group_kill_then_infer_is_safe_and_closes_pipes(self):
        worker = self.make_worker("ready")
        worker.start()
        pid = worker.pid
        os.killpg(pid, signal.SIGKILL)
        deadline = time.monotonic() + 2
        while worker.alive and time.monotonic() < deadline:
            time.sleep(.005)
        result = worker.infer("after-kill", [[1.0, 0.0, 0.0, 0.0]], 100)
        self.assertEqual(result["safe_action"], "no_action")
        self.assertEqual(result["reason"], "worker_unavailable")
        self.assertReaped(worker, pid)

    def test_written_stderr_blocks_ready_and_inference_success_before_reader_runs(self):
        for mode in ("diagnostic_start", "diagnostic_infer"):
            with self.subTest(mode=mode):
                worker = self.make_worker(mode)
                release_reader = threading.Event()
                reader_started = threading.Event()
                original_reader = worker._read_stderr

                def delayed_reader(stream):
                    reader_started.set()
                    release_reader.wait()
                    return original_reader(stream)

                worker._read_stderr = delayed_reader
                completed = threading.Event()
                results, errors = [], []

                def operate():
                    try:
                        if mode == "diagnostic_start":
                            results.append(worker.start())
                        else:
                            results.append(worker.infer("diagnostic-race", [[1.0, 0.0, 0.0, 0.0]], 1000))
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        completed.set()

                operation = threading.Thread(target=operate)
                try:
                    if mode == "diagnostic_infer":
                        worker.start()
                        self.assertTrue(reader_started.wait(2), "stderr collector did not start")
                    operation.start()
                    if mode == "diagnostic_start":
                        self.assertTrue(reader_started.wait(2), "stderr collector did not start")
                    deadline = time.monotonic() + 2
                    while not completed.wait(.005) and not worker.diagnostics() and time.monotonic() < deadline:
                        pass
                    # Only release the reader after the operation has rejected
                    # written stderr or has already (incorrectly) succeeded.
                    release_reader.set()
                    operation.join(2)
                    self.assertFalse(operation.is_alive())
                    self.assertIn("fixture diagnostic", worker.diagnostics())
                    if mode == "diagnostic_start":
                        self.assertFalse(results, results)
                        self.assertEqual(len(errors), 1, errors)
                        self.assertIsInstance(errors[0], WorkerError)
                        self.assertIn("diagnostics", str(errors[0]))
                    else:
                        self.assertFalse(errors, errors)
                        self.assertEqual(len(results), 1, results)
                        result = results[0]
                        self.assertEqual(result["status"], "error", result)
                        self.assertEqual(result["safe_action"], "no_action", result)
                        self.assertIn("fixture diagnostic", result["diagnostics"])
                finally:
                    release_reader.set()
                    if operation.ident is not None:
                        operation.join(2)
                    worker.close()

    def test_close_kills_group_descendant_after_leader_exits_first(self):
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        previous_subreaper = ctypes.c_int()
        if libc.prctl(37, ctypes.byref(previous_subreaper), 0, 0, 0) != 0:
            self.fail("could not read test subreaper status")
        if libc.prctl(36, 1, 0, 0, 0) != 0:
            err = ctypes.get_errno()
            self.fail(f"could not enable test subreaper: errno={err}")
        before = len(tuple(Path("/proc/self/fd").iterdir()))
        try:
            for leader_already_reaped in (False, True):
                with self.subTest(leader_already_reaped=leader_already_reaped):
                    marker = self.root / f"descendant-{leader_already_reaped}.pid"
                    worker = self.make_worker("fork_descendant", marker=marker)
                    descendant = None
                    reader = None
                    try:
                        worker.start()
                        _wait_for_file(marker)
                        descendant = int(marker.read_text(encoding="ascii"))
                        reader = worker._stderr_thread
                        leader = worker.pid
                        if leader_already_reaped:
                            os.kill(leader, signal.SIGTERM)
                            worker.process.wait(timeout=2)
                        began = time.monotonic()
                        worker.close()
                        self.assertLess(time.monotonic() - began, 2)
                        self.assertEqual(worker.process.returncode, -signal.SIGTERM)
                        self.assertFalse(worker.alive)
                        with self.assertRaises(ProcessLookupError):
                            os.kill(leader, 0)
                        deadline = time.monotonic() + 2
                        while time.monotonic() < deadline:
                            try:
                                waited, _ = os.waitpid(descendant, os.WNOHANG)
                            except ChildProcessError:
                                waited = descendant
                            if waited == descendant:
                                break
                            time.sleep(.005)
                        self.assertEqual(waited, descendant, "descendant was not killed and reaped")
                        self.assertFalse((Path("/proc") / str(descendant)).exists())
                        self.assertIsNotNone(reader)
                        self.assertFalse(reader.is_alive(), "stderr collector survived worker.close()")
                        self.assertTrue(worker._stderr_thread is None or not worker._stderr_thread.is_alive())
                        for stream in (worker.process.stdin, worker.process.stdout, worker.process.stderr):
                            self.assertTrue(stream is None or stream.closed)
                    finally:
                        worker.close()
                        if descendant is not None:
                            try:
                                os.kill(descendant, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            deadline = time.monotonic() + 2
                            while time.monotonic() < deadline:
                                try:
                                    waited, _ = os.waitpid(descendant, os.WNOHANG)
                                except ChildProcessError:
                                    break
                                if waited == descendant:
                                    break
                                time.sleep(.005)
                        if reader is not None:
                            reader.join(2)
            self.assertEqual(len(tuple(Path("/proc/self/fd").iterdir())), before)
        finally:
            libc.prctl(36, previous_subreaper.value, 0, 0, 0)

    def test_direct_api_ids_are_rejected_before_contacting_child(self):
        worker = self.make_worker("ready")
        worker.start()
        for request_id in (None, True, "", "x" * 129):
            with self.subTest(request_id=request_id), self.assertRaisesRegex(RequestError, "id"):
                worker.infer(request_id, [[1.0, 0.0, 0.0, 0.0]], 100)
            self.assertTrue(worker.alive)
        self.assertEqual(worker.infer("valid", [[1.0, 0.0, 0.0, 0.0]], 1000)["status"], "success")

    def test_oversized_input_fails_closed(self):
        large = replace(self.manifest, input_shape=(64, 1024))
        worker = self.make_worker("ready", manifest=large)
        worker.start()
        result = worker.infer("large-input", [[0.0] * 1024 for _ in range(64)], 1000)
        self.assertEqual(result["safe_action"], "no_action", result)
        self.assertEqual(result["status"], "error")
        self.assertReaped(worker, worker.pid)

    def test_oversized_output_fails_closed(self):
        worker = self.make_worker("oversized_output")
        worker.start()
        result = worker.infer("large-output", [[1.0, 0.0, 0.0, 0.0]], 1000)
        self.assertEqual(result["safe_action"], "no_action", result)
        self.assertEqual(result["status"], "error")
        self.assertReaped(worker, worker.pid)


class RealWorkerTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("XTOS_TEST_AI_PYTHON"), "explicit locked AI Python not selected")
    def test_fixed_model_runs_in_separate_killable_process(self):
        directory = tempfile.TemporaryDirectory(prefix="xtos-real-worker-")
        self.addCleanup(directory.cleanup)
        fx, envelope = make_real_signed_service_fixture(Path(directory.name))
        authorized = fx.policy.authorize(envelope, fx.model_root)
        manifest = authorized.manifest
        worker = Worker(authorized, python_command=[os.environ["XTOS_TEST_AI_PYTHON"]], load_timeout=90)
        try:
            health = worker.start()
            self.assertEqual(health["status"], "ready")
            self.assertEqual(health["provider"], "CPUExecutionProvider")
            self.assertEqual(health["model_sha256"], manifest.sha256)
            self.assertNotEqual(worker.pid, os.getpid())
            result = worker.infer("real-1", [[2.0, 0.0, -1.0, 0.0]], deadline_ms=3000)
            self.assertEqual(result["safe_action"], "action_allowed", result)
            self.assertEqual(result["status"], "success")
            expected = [0.8437947034835815, 0.11419519782066345, 0.04201006868481636]
            self.assertLessEqual(max(abs(a-b) for a,b in zip(result["output"][0], expected)), 1e-5)
            self.assertEqual(result["model"], manifest.name)
            self.assertEqual(worker.diagnostics(), "")
            cancel = __import__("threading").Event()
            cancel.set()
            cancelled = worker.infer("cancelled-1", [[2.0, 0.0, -1.0, 0.0]],
                                     deadline_ms=3000, cancel=cancel)
            self.assertEqual(cancelled["status"], "cancelled", cancelled)
            self.assertFalse(worker.alive)
            self.assertEqual(worker.restart()["status"], "ready")
            recovered = worker.infer("recovered-1", [[2.0, 0.0, -1.0, 0.0]], deadline_ms=3000)
            self.assertEqual(recovered["status"], "success", recovered)
            stopped_pid = worker.pid
            os.kill(stopped_pid, signal.SIGSTOP)
            stopped = worker.infer("stopped-1", [[2.0, 0.0, -1.0, 0.0]], deadline_ms=250)
            self.assertEqual(stopped["status"], "timeout", stopped)
            self.assertEqual(stopped["safe_action"], "no_action")
            self.assertFalse(worker.alive)
            with self.assertRaises(ProcessLookupError):
                os.kill(stopped_pid, 0)
            self.assertEqual(worker.restart(cancel=None)["status"], "ready")
            restarted = worker.infer("restarted-1", [[2.0, 0.0, -1.0, 0.0]], deadline_ms=3000)
            self.assertEqual(restarted["status"], "success", restarted)
            self.assertEqual(worker.diagnostics(), "")
        finally:
            pid = worker.pid
            worker.close()
        self.assertFalse(worker.alive)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
