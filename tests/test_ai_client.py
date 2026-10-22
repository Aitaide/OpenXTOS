import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "tools/xtos-ai-runtime"))
from xtos_ai_runtime.client import request  # noqa: E402
from xtos_ai_runtime import __main__ as cli_main  # noqa: E402
from xtos_ai_runtime.server import InferenceService, UnixInferenceServer  # noqa: E402


class ClientTests(unittest.TestCase):
    def test_cli_reports_shutdown_close_failure(self):
        class FailingService:
            def __init__(self, model_root):
                pass
            def close(self):
                raise RuntimeError("fixture shutdown failure")

        class PassiveServer:
            def __init__(self, path, service):
                pass
            def serve_forever(self):
                return

        with tempfile.TemporaryDirectory() as directory:
            diagnostics = io.StringIO()
            with mock.patch.object(cli_main, "InferenceService", FailingService), \
                    mock.patch.object(cli_main, "UnixInferenceServer", PassiveServer), \
                    mock.patch.object(cli_main.signal, "signal"), \
                    mock.patch.object(cli_main.sys, "stderr", diagnostics):
                self.assertEqual(cli_main.main(["--model-root", directory,
                                                "--socket", str(Path(directory) / "sock")]), 1)
            self.assertIn("shutdown failed", diagnostics.getvalue())

    def test_cli_reports_close_failure_after_socket_creation_failure(self):
        class FailingService:
            def __init__(self, model_root):
                pass
            def close(self):
                raise RuntimeError("fixture bind cleanup failure")

        with tempfile.TemporaryDirectory() as directory:
            diagnostics = io.StringIO()
            with mock.patch.object(cli_main, "InferenceService", FailingService), \
                    mock.patch.object(cli_main, "UnixInferenceServer", side_effect=OSError("bind failed")), \
                    mock.patch.object(cli_main.sys, "stderr", diagnostics):
                result = cli_main.main(["--model-root", directory,
                                        "--socket", str(Path(directory) / "sock")])
            self.assertEqual(result, 2)
            self.assertIn("bind failed", diagnostics.getvalue())
            self.assertIn("cleanup failed", diagnostics.getvalue())

    def test_roundtrip_over_unix_socket_and_validate_response_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sock"
            service = InferenceService(Path(directory))
            server = UnixInferenceServer(path, service)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                deadline = __import__("time").monotonic() + 2
                while not path.exists() and __import__("time").monotonic() < deadline:
                    __import__("time").sleep(.01)
                result = request(path, {"id": "client-1", "op": "list"}, timeout=1)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["models"], [])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            finally:
                server.shutdown()
                server.server_close()
                service.close()
                thread.join(timeout=2)

    def test_cli_runs_service_and_removes_owned_socket_on_term(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "cli.sock"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(ROOT / "tools/xtos-ai-runtime")
            process = __import__("subprocess").Popen(
                [sys.executable, "-m", "xtos_ai_runtime", "--socket", str(path),
                 "--model-root", str(root)],
                stdout=__import__("subprocess").PIPE, stderr=__import__("subprocess").PIPE,
                text=True, env=environment,
            )
            try:
                deadline = __import__("time").monotonic() + 3
                while not path.exists() and process.poll() is None and __import__("time").monotonic() < deadline:
                    __import__("time").sleep(.01)
                if not path.exists():
                    if process.poll() is not None:
                        self.fail(str(process.communicate(timeout=1)))
                    self.fail("CLI did not create its socket within the bounded startup wait")
                self.assertEqual(request(path, {"id": "cli", "op": "list"}, timeout=1)["status"], "ok")
                process.send_signal(__import__("signal").SIGTERM)
                self.assertEqual(process.wait(timeout=3), 0, process.stderr.read())
                self.assertFalse(path.exists())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
                process.communicate(timeout=1)
                process.stdout.close()
                process.stderr.close()

    def test_cli_shutdown_closes_active_and_recovering_worker_children(self):
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        original_subreaper = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(original_subreaper), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
        self.addCleanup(libc.prctl, 36, original_subreaper.value, 0, 0, 0)
        cases = [(mode, signum) for mode in ("active", "recovering")
                 for signum in (__import__("signal").SIGTERM, __import__("signal").SIGINT)]
        for mode, signum in cases:
            with self.subTest(mode=mode, signum=signum), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                script = root / "cli_fixture.py"
                package = ROOT / "tools/xtos-ai-runtime"
                script.write_text(f'''import json, os, subprocess, sys, threading, time
from pathlib import Path
sys.path.insert(0, {str(package)!r})
sys.path.insert(0, {str(ROOT / "tests")!r})
from ai_service_trust_fixtures import FixtureTrustPolicy, bind_fixture_models
from xtos_ai_runtime import __main__ as cli
from xtos_ai_runtime.server import InferenceService, SCHEMA
root = Path(sys.argv.pop(1))
mode = sys.argv.pop(1)
class FixtureWorker:
    def __init__(self):
        self.proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.alive_value = mode == "active"
        stat = Path(f"/proc/{{self.proc.pid}}/stat").read_text().split()
        (root / "child.json").write_text(json.dumps({{"pid": self.proc.pid, "starttime": stat[21]}}))
    @property
    def pid(self): return self.proc.pid
    @property
    def alive(self): return self.alive_value and self.proc.poll() is None
    def infer(self, request_id, value, deadline_ms, cancel=None):
        (root / "infer-started").write_text("yes")
        if mode == "active":
            while not cancel.wait(.01): pass
            return {{"schema": SCHEMA, "id": request_id, "status": "cancelled", "safe_action": "no_action", "reason": "cancelled", "output": None}}
        return {{"schema": SCHEMA, "id": request_id, "status": "error", "safe_action": "no_action", "reason": "worker_failed", "output": None}}
    def restart(self, cancel=None):
        (root / "recovering").write_text("yes")
        cancel.wait(60)
        raise RuntimeError("recovery cancelled")
    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        (root / "worker-closed").write_text("closed-and-reaped")
class FixtureService(InferenceService):
    def __init__(self, model_root):
        super().__init__(model_root, trust_policy=FixtureTrustPolicy())
        bind_fixture_models(self)
        self._models["controlled"] = FixtureWorker()
cli.InferenceService = FixtureService
raise SystemExit(cli.main())
''', encoding="utf-8")
                socket_path = root / "cli.sock"
                environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
                process = __import__("subprocess").Popen(
                    [sys.executable, str(script), str(root), mode, "--socket", str(socket_path),
                     "--model-root", str(root)],
                    stdout=__import__("subprocess").PIPE,
                    stderr=__import__("subprocess").PIPE, text=True, env=environment)
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                child = None
                try:
                    deadline = __import__("time").monotonic() + 3
                    while not socket_path.exists() and process.poll() is None and __import__("time").monotonic() < deadline:
                        __import__("time").sleep(.01)
                    if not socket_path.exists():
                        self.fail("CLI fixture failed to create socket")
                    client.settimeout(3)
                    client.connect(str(socket_path))
                    client.sendall(json.dumps({"id": "active", "op": "infer", "model": "controlled",
                                               "input": [[0]], "deadline_ms": 5000}).encode() + bytes((10,)))
                    marker = root / ("infer-started" if mode == "active" else "recovering")
                    deadline = __import__("time").monotonic() + 3
                    while not marker.exists() and __import__("time").monotonic() < deadline:
                        __import__("time").sleep(.01)
                    self.assertTrue(marker.exists(), f"{mode} fixture never reached its worker phase")
                    child = json.loads((root / "child.json").read_text())
                    process.send_signal(signum)
                    self.assertEqual(process.wait(timeout=6), 0, process.stderr.read())
                    self.assertTrue((root / "worker-closed").exists(), f"{mode} worker was not closed")
                    stat_path = Path(f"/proc/{child['pid']}/stat")
                    if stat_path.exists():
                        fields = stat_path.read_text().split()
                        self.assertNotEqual(fields[21], child["starttime"], "worker child still exists after CLI exit")
                finally:
                    client.close()
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=2)
                    process.communicate(timeout=1)
                    if child is None and (root / "child.json").exists():
                        child = json.loads((root / "child.json").read_text())
                    if child is not None:
                        try:
                            os.kill(child["pid"], __import__("signal").SIGKILL)
                        except ProcessLookupError:
                            pass
                        try:
                            os.waitpid(child["pid"], 0)
                        except ChildProcessError:
                            pass

    def test_client_rejects_invalid_request_and_wrong_response_identity(self):
        with self.assertRaises(ValueError):
            request("/unused", {"id": "bad", "value": float("nan")})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fake.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            listener.listen(1)
            def respond():
                conn, _ = listener.accept()
                with conn:
                    conn.recv(65536)
                    conn.sendall(b'{"schema":"xtos-ai-runtime-v1","id":"wrong","status":"ok","safe_action":"no_action"}\n')
            thread = threading.Thread(target=respond, daemon=True)
            thread.start()
            try:
                with self.assertRaises(ValueError):
                    request(path, {"id": "expected", "op": "list"}, timeout=1)
            finally:
                listener.close()
                thread.join(timeout=1)


    def _request_from_fake_peer(self, response, document=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fake.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            listener.listen(1)
            errors = []

            def respond():
                try:
                    conn, _ = listener.accept()
                    with conn:
                        conn.recv(65536)
                        # An untrusted peer can send non-finite JSON constants.
                        conn.sendall(json.dumps(response, allow_nan=True).encode() + bytes((10,)))
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=respond, daemon=True)
            thread.start()
            try:
                return request(path, document or {"id": "x", "op": "infer", "model": "m"}, timeout=1)
            finally:
                listener.close()
                thread.join(timeout=1)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])

    def test_client_rejects_incomplete_or_mistyped_no_action_responses(self):
        base = {"schema": "xtos-ai-runtime-v1", "id": "x", "safe_action": "no_action",
                "status": "unavailable", "reason": "model_unavailable", "output": None}
        invalid = (
            *({key: value for key, value in base.items() if key != field}
              for field in ("status", "reason", "output")),
            dict(base, status={"invalid": "type"}),
            dict(base, status="unknown_status"),
            dict(base, reason=7), dict(base, reason=None), dict(base, reason=""),
            dict(base, model=[]), dict(base, model=""),
            dict(base, provider=True), dict(base, provider="not-a-provider"),
            dict(base, latency_ms=float("inf")),
            dict(base, latency_ms=True),
            dict(base, output=[[1.0]]),
        )
        for response in invalid:
            with self.subTest(response=response), self.assertRaises(ValueError):
                self._request_from_fake_peer(response, {"id": "x", "op": "infer"})

    def test_client_rejects_untyped_and_invalid_action_allowed_outputs(self):
        base = {"schema": "xtos-ai-runtime-v1", "id": "x", "status": "success",
                "safe_action": "action_allowed", "reason": None, "model": "m",
                "provider": "CPUExecutionProvider", "latency_ms": 1.0, "output": [[1.0]]}
        invalid = (
            "arbitrary", True, {}, 1, [], [[]], [[1.0], [2.0, 3.0]],
            [[True]], [["1"]], [[float("inf")]], [[3.5e38]],
        )
        for output in invalid:
            with self.subTest(output=output), self.assertRaises(ValueError):
                self._request_from_fake_peer(dict(base, output=output))
        for response in (
            *({key: value for key, value in base.items() if key != field}
              for field in ("status", "reason", "model", "provider", "latency_ms", "output")),
            dict(base, status="unavailable"), dict(base, reason="not-null"),
            dict(base, model=""), dict(base, provider="not-a-provider"),
            dict(base, latency_ms=-1), dict(base, latency_ms=True),
            dict(base, output=[[1.0]] * 65),
        ):
            with self.subTest(response=response), self.assertRaises(ValueError):
                self._request_from_fake_peer(response)
        with self.assertRaises(ValueError):
            self._request_from_fake_peer(base, {"id": "x", "op": "list"})
        with self.assertRaises(ValueError):
            self._request_from_fake_peer(base, {"id": "x", "op": "infer", "model": "other"})

    def test_client_accepts_complete_no_action_and_finite_float32_success(self):
        no_action = {"schema": "xtos-ai-runtime-v1", "id": "x", "status": "ok",
                     "safe_action": "no_action", "reason": None, "output": None}
        self.assertEqual(self._request_from_fake_peer(no_action, {"id": "x", "op": "list"}), no_action)
        success = {"schema": "xtos-ai-runtime-v1", "id": "x", "status": "success",
                   "safe_action": "action_allowed", "reason": None, "model": "m",
                   "provider": "CPUExecutionProvider", "latency_ms": 0, "output": [[1, -2.5]]}
        self.assertEqual(self._request_from_fake_peer(
            success, {"id": "x", "op": "infer", "model": "m"}), success)

    def test_client_rejects_action_allowed_without_requested_model_identity(self):
        response = {"schema": "xtos-ai-runtime-v1", "id": "x", "status": "success",
                    "safe_action": "action_allowed", "reason": None,
                    "model": "unrequested-model", "provider": "CPUExecutionProvider",
                    "latency_ms": 1.0, "output": [[1.0]]}
        documents = ({"id": "x", "op": "infer"}, *(
            {"id": "x", "op": "infer", "model": value}
            for value in (None, "", True, 7, [], {})))
        for document in documents:
            with self.subTest(document=document), self.assertRaisesRegex(
                    ValueError, "infer request requires a non-empty model identity"):
                self._request_from_fake_peer(response, document)

    def test_client_rejects_no_action_with_conflicting_model_identity(self):
        response = {"schema": "xtos-ai-runtime-v1", "id": "x", "status": "unavailable",
                    "safe_action": "no_action", "reason": "model_unavailable",
                    "model": "other-model", "output": None}
        for op in ("infer", "unload"):
            for status in ("ok", "invalid", "unavailable", "busy", "timeout", "cancelled", "error"):
                with self.subTest(op=op, status=status), self.assertRaisesRegex(
                        ValueError, "no_action response model identity mismatch"):
                    self._request_from_fake_peer(
                        dict(response, status=status),
                        {"id": "x", "op": op, "model": "expected-model"})

    def test_client_accepts_no_action_with_matching_or_absent_model_identity(self):
        base = {"schema": "xtos-ai-runtime-v1", "id": "x", "status": "unavailable",
                "safe_action": "no_action", "reason": "model_unavailable", "output": None}
        for response in (base, dict(base, model=None), dict(base, model="expected-model")):
            with self.subTest(response=response):
                self.assertEqual(self._request_from_fake_peer(
                    response, {"id": "x", "op": "infer", "model": "expected-model"}), response)
        response = dict(base, model="unrequested-model")
        self.assertEqual(self._request_from_fake_peer(
            response, {"id": "x", "op": "infer"}), response)


if __name__ == "__main__":
    unittest.main()
