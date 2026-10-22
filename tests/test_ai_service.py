import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "tools/xtos-ai-runtime"))

from xtos_ai_runtime.server import (  # noqa: E402
    MAX_REQUEST_BYTES,
    InferenceService,
    UnixInferenceServer,
    parse_request_line,
)


from xtos_ai_runtime.protocol import safe_result  # noqa: E402
from ai_service_trust_fixtures import bind_fixture_models, FixtureTrustPolicy


class ControlledWorker:
    """Event-controlled process boundary fixture; never used as AI evidence."""
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.alive = True
        self.pid = os.getpid()

    def infer(self, request_id, value, deadline_ms, cancel=None):
        self.calls += 1
        self.started.set()
        until = time.monotonic() + deadline_ms / 1000
        while not self.release.wait(.005):
            if cancel.is_set():
                return safe_result(request_id, "cancelled", "cancelled")
            if time.monotonic() >= until:
                return safe_result(request_id, "timeout", "timeout")
        return safe_result(request_id, "unavailable", "fixture_completed")

    def close(self):
        self.release.set()
        self.alive = False


class ServiceProtocolTests(unittest.TestCase):
    def test_close_serializes_active_retirement_with_shutdown_close(self):
        class CloseRaceWorker(ControlledWorker):
            def __init__(self):
                super().__init__()
                self.allow_infer_return = threading.Event()
                self.close_started = threading.Event()
                self.allow_close_return = threading.Event()
                self.close_lock = threading.Lock()
                self.close_calls = 0
                self.active_close_calls = 0
                self.max_concurrent_close_calls = 0

            def infer(self, request_id, value, deadline_ms, cancel=None):
                self.started.set()
                self.allow_infer_return.wait(2)
                return safe_result(request_id, "unavailable", "fixture_completed")

            def close(self):
                with self.close_lock:
                    self.close_calls += 1
                    self.active_close_calls += 1
                    self.max_concurrent_close_calls = max(
                        self.max_concurrent_close_calls, self.active_close_calls)
                self.close_started.set()
                try:
                    self.allow_close_return.wait(2)
                    self.release.set()
                    self.alive = False
                finally:
                    with self.close_lock:
                        self.active_close_calls -= 1

        service = bind_fixture_models(InferenceService(ROOT, trust_policy=FixtureTrustPolicy()))
        worker = CloseRaceWorker()
        service._models["controlled"] = worker
        result = {}
        retirement_close_entered = threading.Event()
        original_safe_close = service._safe_close
        infer_thread = threading.Thread(target=lambda: result.update(service.dispatch({
            "id": "active", "op": "infer", "model": "controlled",
            "input": [[0]], "deadline_ms": 5000})))
        shutdown_result = []

        def record_retirement_close(target):
            if threading.current_thread() is infer_thread:
                retirement_close_entered.set()
            return original_safe_close(target)

        def shutdown():
            try:
                service.close()
            except Exception as exc:
                shutdown_result.append(exc)

        shutdown_thread = threading.Thread(target=shutdown)
        with mock.patch.object(service, "_safe_close", side_effect=record_retirement_close):
            infer_thread.start()
            try:
                self.assertTrue(worker.started.wait(1))
                shutdown_thread.start()
                self.assertTrue(worker.close_started.wait(1))
                worker.allow_infer_return.set()
                self.assertTrue(retirement_close_entered.wait(1))
                worker.allow_close_return.set()
                infer_thread.join(2)
                shutdown_thread.join(2)
                self.assertFalse(infer_thread.is_alive())
                self.assertFalse(shutdown_thread.is_alive())
                self.assertEqual(shutdown_result, [])
                self.assertEqual(worker.max_concurrent_close_calls, 1)
                self.assertEqual(worker.close_calls, 1)
                self.assertEqual(result["reason"], "fixture_completed")
            finally:
                worker.allow_infer_return.set()
                worker.allow_close_return.set()
                infer_thread.join(2)
                if shutdown_thread.ident is not None:
                    shutdown_thread.join(2)
                service.close()

    def test_close_retries_retained_worker_after_failed_shutdown(self):
        class FlakyCloseWorker(ControlledWorker):
            def __init__(self, failures):
                super().__init__()
                self.failures = failures
                self.close_attempts = 0
                self.process = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            def close(self):
                self.close_attempts += 1
                if self.close_attempts <= self.failures:
                    raise OSError("fixture transient close failed")
                self.process.terminate()
                self.process.wait(timeout=2)
                super().close()

        service = bind_fixture_models(InferenceService(ROOT, trust_policy=FixtureTrustPolicy()))
        first, second = FlakyCloseWorker(1), FlakyCloseWorker(2)
        service._models.update(first=first, second=second)
        try:
            with self.assertRaisesRegex(RuntimeError, "shutdown"):
                service.close()
            self.assertIsNone(first.process.poll())
            self.assertIsNone(second.process.poll())
            self.assertIsNotNone(service._shutdown_error)

            with self.assertRaisesRegex(RuntimeError, "shutdown"):
                service.close()
            self.assertIsNotNone(first.process.poll(), "a later close did not reap the retained failed child")
            self.assertIsNone(second.process.poll())
            self.assertEqual(first.close_attempts, 2)
            self.assertEqual(second.close_attempts, 2, "retry did not attempt every retained failed handle")
            self.assertIsNotNone(service._shutdown_error, "shutdown error cleared before every child was reaped")

            service.close()
            self.assertIsNotNone(second.process.poll())
            self.assertFalse(first.alive or second.alive)
            self.assertEqual(first.close_attempts, 2)
            self.assertEqual(second.close_attempts, 3)
            self.assertIsNone(service._shutdown_error)
            self.assertEqual(service._retired, set())
            service.close()
        finally:
            for worker in (first, second):
                if worker.process.poll() is None:
                    worker.process.kill()
                worker.process.wait(timeout=2)

    def test_shutdown_retries_handle_when_retirement_close_failed(self):
        class FlakyCloseWorker(ControlledWorker):
            def __init__(self):
                super().__init__()
                self.close_attempts = 0

            def close(self):
                self.close_attempts += 1
                if self.close_attempts == 1:
                    raise OSError("fixture first close failed")
                super().close()

        manifest = json.loads((ROOT / "tools/xtos-ai-runtime/model-manifest.example.json").read_text())
        old = FlakyCloseWorker()
        candidate = ControlledWorker()
        candidate.start = lambda cancel=None: {"status": "ready", "runtime_version": "fixture-only"}
        service = bind_fixture_models(InferenceService(ROOT, worker_factory=lambda unused: candidate, trust_policy=FixtureTrustPolicy()))
        service._models[manifest["name"]] = old
        try:
            loaded = service.dispatch({"id": "swap", "op": "reload", "manifest": manifest})
            self.assertEqual(loaded["status"], "ok")
            self.assertTrue(old.alive)
            service.close()
            self.assertFalse(old.alive, "failed retirement close was not retried during shutdown")
            self.assertEqual(old.close_attempts, 2)
        finally:
            service.close()
            old.close()

    def test_close_retry_waits_for_prior_attempt_without_reclosing_good_handles(self):
        release = threading.Event()
        finished = threading.Event()

        class CountingWorker(ControlledWorker):
            def __init__(self):
                super().__init__()
                self.close_attempts = 0

            def close(self):
                self.close_attempts += 1
                super().close()

        class SlowWorker(CountingWorker):
            def close(self):
                self.close_attempts += 1
                release.wait(3)
                ControlledWorker.close(self)
                finished.set()

        service = bind_fixture_models(InferenceService(ROOT, trust_policy=FixtureTrustPolicy()))
        slow, good = SlowWorker(), CountingWorker()
        service._models.update(slow=slow, good=good)
        try:
            with mock.patch("xtos_ai_runtime.server.SHUTDOWN_TIMEOUT", .05):
                for attempt in range(2):
                    began = time.monotonic()
                    with self.assertRaisesRegex(RuntimeError, "shutdown"):
                        service.close()
                    self.assertLess(time.monotonic() - began, .5)
                self.assertTrue(slow.alive)
                self.assertEqual(slow.close_attempts, 1, "retry invoked close concurrently on the same handle")
                self.assertEqual(good.close_attempts, 1, "retry closed an already-successful handle again")
        finally:
            release.set()
            self.assertTrue(finished.wait(2))
        service.close()
        self.assertIsNone(service._shutdown_error)
        self.assertEqual(slow.close_attempts, 1)
        self.assertEqual(good.close_attempts, 1)

    def test_shutdown_reports_close_errors_after_attempting_every_handle(self):
        closed = threading.Event()
        class BrokenWorker(ControlledWorker):
            def close(self):
                raise OSError("fixture close failure")
        class GoodWorker(ControlledWorker):
            def close(self):
                super().close()
                closed.set()
        service = bind_fixture_models(InferenceService(ROOT, trust_policy=FixtureTrustPolicy()))
        service._models["broken"] = BrokenWorker()
        service._models["good"] = GoodWorker()
        with self.assertRaisesRegex(RuntimeError, "shutdown"):
            service.close()
        self.assertTrue(closed.is_set(), "one close error prevented another handle from being closed")

    def test_shutdown_wait_is_bounded_when_one_close_stalls(self):
        release = threading.Event()
        finished = threading.Event()
        class SlowWorker(ControlledWorker):
            def close(self):
                try:
                    release.wait(1)
                finally:
                    finished.set()
        service = bind_fixture_models(InferenceService(ROOT, trust_policy=FixtureTrustPolicy()))
        service._models["slow"] = SlowWorker()
        try:
            with mock.patch("xtos_ai_runtime.server.SHUTDOWN_TIMEOUT", .1, create=True):
                began = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, "shutdown"):
                    service.close()
                self.assertLess(time.monotonic() - began, .5)
        finally:
            release.set()
            self.assertTrue(finished.wait(2))

    def test_oversized_physical_line_is_one_error_and_suffix_is_not_dispatched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "runtime.sock"
            service = bind_fixture_models(InferenceService(root, trust_policy=FixtureTrustPolicy()))
            runtime = UnixInferenceServer(path, service)
            thread = threading.Thread(target=runtime.serve_forever, daemon=True)
            thread.start()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(2)
                    client.connect(str(path))
                    client.sendall(b" " * (MAX_REQUEST_BYTES + 1) + b'{"id":"suffix-command","op":"list"}\n')
                    data = bytearray()
                    while True:
                        try:
                            chunk = client.recv(65536)
                        except socket.timeout:
                            break
                        if not chunk:
                            break
                        data.extend(chunk)
                    responses = [json.loads(line) for line in bytes(data).splitlines() if line]
                self.assertEqual(len(responses), 1, responses)
                self.assertEqual(responses[0]["reason"], "invalid_json")
                self.assertNotEqual(responses[0].get("id"), "suffix-command")
                self.assertEqual(service.dispatch({"id": "check", "op": "list"})["models"], [])
            finally:
                runtime.shutdown()
                runtime.server_close()
                service.close()
                thread.join(timeout=2)

    def test_oversized_line_closes_connection_before_a_following_physical_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "runtime.sock"
            service = bind_fixture_models(InferenceService(root, trust_policy=FixtureTrustPolicy()))
            runtime = UnixInferenceServer(path, service)
            thread = threading.Thread(target=runtime.serve_forever, daemon=True)
            thread.start()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(2)
                    client.connect(str(path))
                    client.sendall(b" " * (MAX_REQUEST_BYTES + 1) + b"\n" +
                                   b'{"id":"must-not-run","op":"list"}\n')
                    data = bytearray()
                    while True:
                        try:
                            chunk = client.recv(65536)
                        except socket.timeout:
                            break
                        if not chunk:
                            break
                        data.extend(chunk)
                    responses = [json.loads(line) for line in bytes(data).splitlines() if line]
                self.assertEqual(len(responses), 1, responses)
                self.assertEqual(responses[0]["reason"], "invalid_json")
                self.assertNotEqual(responses[0].get("id"), "must-not-run")
            finally:
                runtime.shutdown()
                runtime.server_close()
                service.close()
                thread.join(timeout=2)

    def test_oversized_fragmented_line_is_not_resynchronized_as_a_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "runtime.sock"
            service = bind_fixture_models(InferenceService(root, trust_policy=FixtureTrustPolicy()))
            runtime = UnixInferenceServer(path, service)
            thread = threading.Thread(target=runtime.serve_forever, daemon=True)
            thread.start()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(2)
                    client.connect(str(path))
                    client.sendall(b" " * (MAX_REQUEST_BYTES - 8))
                    time.sleep(.02)
                    client.sendall(b" " * 16 + b'{"id":"fragment-suffix","op":"list"}' + bytes((10,)))
                    data = bytearray()
                    while True:
                        try:
                            chunk = client.recv(65536)
                        except socket.timeout:
                            break
                        if not chunk:
                            break
                        data.extend(chunk)
                    responses = [json.loads(line) for line in bytes(data).splitlines() if line]
                self.assertEqual(len(responses), 1, responses)
                self.assertEqual(responses[0]["reason"], "invalid_json")
                self.assertNotEqual(responses[0].get("id"), "fragment-suffix")
            finally:
                runtime.shutdown()
                runtime.server_close()
                service.close()
                thread.join(timeout=2)

    def test_oversized_line_closes_connection_when_limit_chunk_already_has_newline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "runtime.sock"
            service = bind_fixture_models(InferenceService(root, trust_policy=FixtureTrustPolicy()))
            runtime = UnixInferenceServer(path, service)
            thread = threading.Thread(target=runtime.serve_forever, daemon=True)
            thread.start()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(2)
                    client.connect(str(path))
                    client.sendall(b" " * MAX_REQUEST_BYTES + b"\n" +
                                   b'{"id":"must-not-run","op":"list"}\n')
                    data = bytearray()
                    while True:
                        try:
                            chunk = client.recv(65536)
                        except socket.timeout:
                            break
                        if not chunk:
                            break
                        data.extend(chunk)
                    responses = [json.loads(line) for line in bytes(data).splitlines() if line]
                self.assertEqual(len(responses), 1, responses)
                self.assertEqual(responses[0]["reason"], "invalid_json")
                self.assertNotEqual(responses[0].get("id"), "must-not-run")
            finally:
                runtime.shutdown()
                runtime.server_close()
                service.close()
                thread.join(timeout=2)

    def test_parser_rejects_duplicate_keys_nan_and_non_objects(self):
        self.assertEqual(parse_request_line(b'{"id":"x","op":"list"}\n')["op"], "list")
        for raw in (
            b'{"id":"x","id":"y","op":"list"}\n',
            b'{"id":"x","value":NaN}\n',
            b'[]\n',
            b'{"id":"x"}\n' + b" " * MAX_REQUEST_BYTES,
        ):
            with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                parse_request_line(raw)

    def test_missing_model_and_bad_load_are_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            service = InferenceService(Path(directory), python_command=[sys.executable])
            listed = service.dispatch({"id": "l", "op": "list"})
            self.assertEqual(listed["status"], "ok")
            self.assertEqual(listed["models"], [])
            missing = service.dispatch({"id": "i", "op": "infer", "model": "none",
                                        "input": [[1]], "deadline_ms": 100})
            self.assertEqual(missing["safe_action"], "no_action")
            self.assertEqual(missing["reason"], "model_unavailable")
            bad = service.dispatch({"id": "b", "op": "load", "manifest": {}})
            self.assertEqual(bad["safe_action"], "no_action")
            self.assertEqual(bad["reason"], "invalid_manifest")
            service.close()

    def test_invalid_ids_and_closed_service_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            service = bind_fixture_models(InferenceService(Path(directory), trust_policy=FixtureTrustPolicy()))
            for value in (None, [], True, "", "x" * 129):
                result = service.dispatch({"id": value, "op": "list"})
                self.assertEqual(result["safe_action"], "no_action")
                self.assertEqual(result["reason"], "invalid_id")
            service.close()
            self.assertEqual(service.dispatch({"id": "l", "op": "list"})["reason"],
                             "service_closed")

    def test_one_active_one_queued_and_duplicate_cannot_cancel_original(self):
        with tempfile.TemporaryDirectory() as directory:
            service = bind_fixture_models(InferenceService(Path(directory), trust_policy=FixtureTrustPolicy()))
            gate = ControlledWorker()
            service._models["controlled"] = gate
            responses = {}
            first = {"id": "a", "op": "infer", "model": "controlled",
                     "input": [[0]], "deadline_ms": 5000}
            second = dict(first, id="b")
            a = threading.Thread(target=lambda: responses.update(a=service.dispatch(first)))
            b = threading.Thread(target=lambda: responses.update(b=service.dispatch(second)))
            try:
                a.start()
                self.assertTrue(gate.started.wait(1))
                duplicate = service.dispatch(first)
                self.assertEqual(duplicate["reason"], "duplicate_id")
                b.start()
                limit = time.monotonic() + 1
                while time.monotonic() < limit:
                    if service.dispatch({"id": "h", "op": "health"})["queued"] == 1:
                        break
                    time.sleep(.01)
                third = service.dispatch(dict(first, id="c"))
                self.assertEqual(third["reason"], "queue_full")
                self.assertEqual(gate.calls, 1)
                cancelled = service.dispatch({"id": "cancel-b", "op": "cancel", "target_id": "b"})
                self.assertEqual(cancelled["status"], "ok")
                b.join(1)
                self.assertFalse(b.is_alive())
                self.assertEqual(responses["b"]["status"], "cancelled")
                self.assertEqual(gate.calls, 1)
                service.dispatch({"id": "cancel-a", "op": "cancel", "target_id": "a"})
                a.join(1)
                self.assertFalse(a.is_alive())
                self.assertEqual(responses["a"]["status"], "cancelled")
                self.assertEqual(service.dispatch({"id": "h", "op": "health"})["inflight"], 0)
            finally:
                gate.release.set()
                a.join(1)
                if b.ident is not None:
                    b.join(1)
                service.close()

    def test_service_shutdown_reaps_staging_worker(self):
        class BlockingWorker(ControlledWorker):
            def __init__(self):
                super().__init__()
                self.allow_exit = threading.Event()
            def start(self, cancel=None):
                self.started.set()
                cancel.wait(2)
                self.allow_exit.wait(2)
                self.alive = False
                raise RuntimeError("startup cancelled")
            def close(self):
                self.allow_exit.set()
                super().close()
        worker = BlockingWorker()
        service = bind_fixture_models(InferenceService(ROOT, worker_factory=lambda _ticket: worker, trust_policy=FixtureTrustPolicy()))
        manifest = json.loads((ROOT / "tools/xtos-ai-runtime/model-manifest.example.json").read_text())
        response = {}
        thread = threading.Thread(target=lambda: response.update(
            service.dispatch({"id": "staging", "op": "load", "manifest": manifest})))
        thread.start()
        try:
            self.assertTrue(worker.started.wait(1))
            service.close()
            thread.join(2)
            self.assertFalse(thread.is_alive(), "service.close returned with a live staging worker")
            self.assertFalse(worker.alive)
        finally:
            worker.close()
            thread.join(3)

    def test_service_shutdown_cancels_active_work(self):
        service = bind_fixture_models(InferenceService(ROOT, trust_policy=FixtureTrustPolicy()))
        worker = ControlledWorker()
        service._models["controlled"] = worker
        result = {}
        job = threading.Thread(target=lambda: result.update(service.dispatch({
            "id": "active", "op": "infer", "model": "controlled", "input": [[0]], "deadline_ms": 5000})))
        job.start()
        try:
            self.assertTrue(worker.started.wait(1))
            service.close()
            job.join(1)
            self.assertFalse(job.is_alive())
            self.assertEqual(result["safe_action"], "no_action")
            self.assertFalse(worker.alive)
        finally:
            worker.release.set()
            job.join(1)
            service.close()

    def test_failed_worker_recovers_before_accepting_new_inference(self):
        service = bind_fixture_models(InferenceService(ROOT, trust_policy=FixtureTrustPolicy()))
        worker = ControlledWorker()
        worker.alive = False
        recovered = threading.Event()
        unblock = threading.Event()
        def restart(cancel=None):
            recovered.set()
            unblock.wait(1)
            worker.alive = not cancel.is_set()
            return {"status": "ready"}
        worker.restart = restart
        worker.release.set()
        service._models["controlled"] = worker
        request = {"id": "failed", "op": "infer", "model": "controlled", "input": [[0]], "deadline_ms": 500}
        try:
            result = service.dispatch(request)
            self.assertEqual(result["safe_action"], "no_action")
            self.assertTrue(recovered.wait(1))
            recovering = service.dispatch(dict(request, id="not-yet"))
            self.assertEqual(recovering["reason"], "worker_recovering", recovering)
            unblock.set()
            until = time.monotonic() + 1
            while time.monotonic() < until:
                state = service.dispatch({"id": "health", "op": "health"})["models"]["controlled"]
                if not state["recovering"]:
                    break
                time.sleep(.01)
            self.assertTrue(state["alive"])
        finally:
            unblock.set()
            service.close()

    def test_registry_is_bounded_and_unload_frees_capacity(self):
        created = []
        def factory(ticket):
            worker = ControlledWorker()
            worker.manifest = ticket.manifest
            worker.start = lambda cancel=None: {"status": "ready", "runtime_version": "fixture-only"}
            created.append(worker)
            return worker
        service = bind_fixture_models(InferenceService(ROOT, worker_factory=factory, trust_policy=FixtureTrustPolicy()))
        manifest = json.loads((ROOT / "tools/xtos-ai-runtime/model-manifest.example.json").read_text())
        try:
            for number in range(4):
                result = service.dispatch({"id": f"l{number}", "op": "load",
                                           "manifest": dict(manifest, name=f"model{number}")})
                self.assertEqual(result["status"], "ok", result)
            excess = service.dispatch({"id": "l4", "op": "load", "manifest": dict(manifest, name="model4")})
            self.assertEqual(excess["reason"], "model_registry_full", excess)
            self.assertLessEqual(len(created), 4)
            service.dispatch({"id": "u", "op": "unload", "model": "model0"})
            self.assertFalse(created[0].alive)
            self.assertEqual(service.dispatch({"id": "l5", "op": "load",
                "manifest": dict(manifest, name="model5")})["status"], "ok")
        finally:
            service.close()
        self.assertTrue(all(not worker.alive for worker in created))

    def test_failed_staging_keeps_previous_worker(self):
        created = []
        def factory(ticket):
            worker = ControlledWorker()
            worker.manifest = ticket.manifest
            worker.fail_load = len(created) != 0
            def start(cancel=None):
                if worker.fail_load:
                    raise RuntimeError("controlled load failure")
                return {"status": "ready", "runtime_version": "fixture-only"}
            worker.start = start
            created.append(worker)
            return worker
        service = bind_fixture_models(InferenceService(ROOT, worker_factory=factory, trust_policy=FixtureTrustPolicy()))
        manifest = json.loads((ROOT / "tools/xtos-ai-runtime/model-manifest.example.json").read_text())
        try:
            first = service.dispatch({"id": "load", "op": "load", "manifest": manifest})
            self.assertEqual(first["status"], "ok", first)
            failed = service.dispatch({"id": "reload", "op": "reload", "manifest": manifest})
            self.assertEqual(failed["reason"], "model_load_failed", failed)
            self.assertIs(service._models[manifest["name"]], created[0])
            self.assertTrue(created[0].alive)
            self.assertFalse(created[1].alive)
        finally:
            service.close()
        self.assertFalse(created[0].alive)

    def test_socket_is_private_at_first_observable_chmod_under_zero_umask(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.chmod(root, 0o755)
            path = root / "private.sock"
            service = bind_fixture_models(InferenceService(root, trust_policy=FixtureTrustPolicy()))
            original_chmod = os.chmod
            original_bind = socket.socket.bind
            previous_umask = os.umask(0)
            observed = []
            bound_modes = []

            def inspect_bind(connection, target):
                result = original_bind(connection, target)
                bound_modes.append(Path(target).lstat().st_mode & 0o777)
                self.assertEqual(bound_modes[-1], 0o600)
                return result

            def inspect_before_chmod(target, *args, **kwargs):
                info = Path(target).lstat()
                observed.append(info.st_mode & 0o777)
                self.assertEqual(info.st_mode & 0o777, 0o600)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(1)
                    probe.connect(str(target))
                return original_chmod(target, *args, **kwargs)

            try:
                with mock.patch("xtos_ai_runtime.server.os.chmod", side_effect=inspect_before_chmod), \
                        mock.patch.object(socket.socket, "bind", inspect_bind):
                    runtime = UnixInferenceServer(path, service)
                self.assertEqual(bound_modes, [0o600])
                self.assertEqual(observed, [0o600])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                runtime.server_close()
            finally:
                os.umask(previous_umask)
                service.close()

    def test_concurrent_socket_creation_serializes_process_umask(self):
        import xtos_ai_runtime.server as runtime_module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_umask = os.umask
            first_in_umask = threading.Event()
            second_in_umask = threading.Event()
            release_first_umask = threading.Event()
            call_lock = threading.Lock()
            calls = 0
            failures = []
            servers, services = [], []

            def controlled_umask(mask):
                nonlocal calls
                previous = original_umask(mask)
                if mask == 0o177:
                    with call_lock:
                        calls += 1
                        call = calls
                    if call == 1:
                        first_in_umask.set()
                        release_first_umask.wait(1)
                    elif call == 2:
                        second_in_umask.set()
                return previous

            def create(name):
                service = bind_fixture_models(InferenceService(root, trust_policy=FixtureTrustPolicy()))
                services.append(service)
                try:
                    servers.append(UnixInferenceServer(root / name, service))
                except Exception as exc:
                    failures.append(exc)

            threads = [threading.Thread(target=create, args=(name,))
                       for name in ("one.sock", "two.sock")]
            previous_umask = original_umask(0)
            try:
                with mock.patch.object(runtime_module.os, "umask", side_effect=controlled_umask):
                    threads[0].start()
                    self.assertTrue(first_in_umask.wait(1))
                    threads[1].start()
                    time.sleep(.05)
                    self.assertFalse(second_in_umask.is_set(), "concurrent socket bind entered process umask scope")
                    release_first_umask.set()
                    for thread in threads:
                        thread.join(2)
                    self.assertFalse(any(thread.is_alive() for thread in threads))
                self.assertEqual(failures, [])
                self.assertEqual(len(servers), 2)
            finally:
                release_first_umask.set()
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(2)
                original_umask(previous_umask)
                for server in servers:
                    server.server_close()
                for service in services:
                    service.close()

    def test_socket_bind_failure_restores_process_umask(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "failure.sock"
            service = bind_fixture_models(InferenceService(root, trust_policy=FixtureTrustPolicy()))
            previous_umask = os.umask(0o027)
            try:
                with mock.patch("xtos_ai_runtime.server._UnixServer", side_effect=OSError("bind failed")):
                    with self.assertRaisesRegex(OSError, "bind failed"):
                        UnixInferenceServer(path, service)
                observed = os.umask(0)
                os.umask(observed)
                self.assertEqual(observed, 0o027)
            finally:
                os.umask(previous_umask)
                service.close()

    def test_unix_server_close_retries_socket_left_in_quarantine_after_unlink_error(self):
        import errno
        import xtos_ai_runtime.server as runtime_module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "runtime.sock"
            service = bind_fixture_models(InferenceService(root, trust_policy=FixtureTrustPolicy()))
            runtime = UnixInferenceServer(path, service)
            reference_fd = runtime._socket_reference_fd
            server_socket = runtime._server.socket
            original_unlink = runtime_module.os.unlink
            attempts = []

            def fail_first_entry_unlink(target, *args, **kwargs):
                if target == "entry":
                    attempts.append(target)
                    if len(attempts) == 1:
                        raise OSError(errno.EIO, "fixture unlink failure")
                return original_unlink(target, *args, **kwargs)

            try:
                with mock.patch.object(runtime_module.os, "unlink",
                                       side_effect=fail_first_entry_unlink):
                    with self.assertRaises(OSError) as caught:
                        runtime.server_close()
                    self.assertEqual(caught.exception.errno, errno.EIO)
                    quarantine = list(root.glob(".xtos-socket-cleanup-*"))
                    self.assertEqual(len(quarantine), 1)
                    self.assertTrue((quarantine[0] / "entry").exists())
                    self.assertFalse(path.exists())
                    self.assertFalse(runtime._socket_cleanup_complete)
                    self.assertFalse(runtime._closed)
                    os.fstat(reference_fd)

                    runtime.server_close()

                self.assertEqual(attempts, ["entry", "entry"])
                self.assertFalse(path.exists())
                self.assertEqual(list(root.glob(".xtos-socket-cleanup-*")), [])
                self.assertTrue(runtime._socket_cleanup_complete)
                self.assertTrue(runtime._closed)
                self.assertEqual(server_socket.fileno(), -1)
                with self.assertRaises(OSError):
                    os.fstat(reference_fd)
            finally:
                if not runtime._closed:
                    runtime.server_close()
                service.close()

    def test_unix_server_close_retries_underlying_close_error_without_leaking_fd(self):
        import xtos_ai_runtime.server as runtime_module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "runtime.sock"
            service = bind_fixture_models(InferenceService(root, trust_policy=FixtureTrustPolicy()))
            runtime = UnixInferenceServer(path, service)
            reference_fd = runtime._socket_reference_fd
            server_socket = runtime._server.socket
            original_server_close = runtime._server.server_close
            attempts = []

            def fail_before_first_close():
                attempts.append(None)
                if len(attempts) == 1:
                    raise OSError("fixture close failed before closing fd")
                return original_server_close()

            try:
                with mock.patch.object(runtime._server, "server_close",
                                       side_effect=fail_before_first_close):
                    with self.assertRaisesRegex(OSError, "before closing fd"):
                        runtime.server_close()
                    self.assertFalse(runtime._server_closed)
                    self.assertEqual(runtime._close_state, "cleanup_failed")
                    self.assertGreaterEqual(server_socket.fileno(), 0)
                    self.assertTrue(path.exists())
                    self.assertFalse(runtime._socket_cleanup_complete)
                    self.assertFalse(runtime._closed)
                    os.fstat(reference_fd)

                    runtime.server_close()

                self.assertEqual(len(attempts), 2)
                self.assertTrue(runtime._server_closed)
                self.assertEqual(runtime._close_state, "closed")
                self.assertEqual(server_socket.fileno(), -1)
                self.assertFalse(path.exists())
                self.assertEqual(list(root.glob(".xtos-socket-cleanup-*")), [])
                with self.assertRaises(OSError):
                    os.fstat(reference_fd)
            finally:
                if not runtime._closed:
                    runtime.server_close()
                service.close()

    def test_unix_server_close_only_removes_its_original_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for replacement in ("regular", "symlink", "directory", "socket"):
                with self.subTest(replacement=replacement):
                    path = root / "runtime.sock"
                    service = bind_fixture_models(InferenceService(root, trust_policy=FixtureTrustPolicy()))
                    runtime = UnixInferenceServer(path, service)
                    path.unlink()
                    replacement_socket = None
                    if replacement == "regular":
                        path.write_text("caller-owned sentinel")
                    elif replacement == "symlink":
                        target = root / "target"
                        target.write_text("target")
                        path.symlink_to(target)
                    elif replacement == "directory":
                        path.mkdir()
                        (path / "sentinel").write_text("keep")
                    else:
                        replacement_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        replacement_socket.bind(str(path))
                        replacement_socket.listen(1)
                    try:
                        runtime.server_close()
                        self.assertTrue(path.exists() or path.is_symlink())
                        if replacement == "regular":
                            self.assertEqual(path.read_text(), "caller-owned sentinel")
                        elif replacement == "symlink":
                            self.assertTrue(path.is_symlink())
                        elif replacement == "directory":
                            self.assertTrue((path / "sentinel").exists())
                    finally:
                        if replacement_socket is not None:
                            replacement_socket.close()
                        if path.is_dir() and not path.is_symlink():
                            (path / "sentinel").unlink(missing_ok=True)
                            path.rmdir()
                        else:
                            path.unlink(missing_ok=True)
                        service.close()

    def test_unix_server_close_preserves_path_replaced_during_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "runtime.sock"
            service = bind_fixture_models(InferenceService(root, trust_policy=FixtureTrustPolicy()))
            runtime = UnixInferenceServer(path, service)
            bound_socket = runtime._server.socket
            original_inode = os.open(path, os.O_PATH | os.O_NOFOLLOW)
            original_identity = os.fstat(original_inode)
            original_stat, original_lstat = os.stat, os.lstat
            replaced = False

            def replace_path_after_identity_read(read_stat, target, *args, **kwargs):
                nonlocal replaced
                info = read_stat(target, *args, **kwargs)
                if not replaced and (info.st_dev, info.st_ino) == (original_identity.st_dev, original_identity.st_ino):
                    replaced = True
                    path.unlink(missing_ok=True)
                    path.write_text("caller-owned sentinel")
                return info

            try:
                with mock.patch("xtos_ai_runtime.server.os.lstat", side_effect=lambda *args, **kwargs:
                                replace_path_after_identity_read(original_lstat, *args, **kwargs)), \
                        mock.patch("xtos_ai_runtime.server.os.stat", side_effect=lambda *args, **kwargs:
                                   replace_path_after_identity_read(original_stat, *args, **kwargs)):
                    runtime.server_close()
                self.assertTrue(replaced, "cleanup did not inspect the original socket inode")
                self.assertTrue(path.is_file())
                self.assertEqual(path.read_text(), "caller-owned sentinel")
                self.assertEqual(bound_socket.fileno(), -1, "the original socket handle was not closed")
                self.assertEqual(os.fstat(original_inode).st_nlink, 0, "the original socket inode was not unlinked")
                self.assertEqual(list(root.glob(".xtos-socket-cleanup-*")), [], "cleanup left its original socket behind")
            finally:
                os.close(original_inode)
                path.unlink(missing_ok=True)
                service.close()

    def test_unix_server_close_preserves_inode_reuse_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "runtime.sock"
            service = bind_fixture_models(InferenceService(root, trust_policy=FixtureTrustPolicy()))
            runtime = UnixInferenceServer(path, service)
            original_socket = runtime._server.socket
            original_info = path.lstat()
            original_identity = (original_info.st_dev, original_info.st_ino, stat.S_IFMT(original_info.st_mode))
            reference_fd = getattr(runtime, "_socket_reference_fd", -1)
            replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            original_server_close = runtime._server.server_close
            original_fstat, original_rename = os.fstat, os.rename
            stable_at_replacement = False
            moved = False
            checked_after_move = False

            def inspect_reference(fd):
                nonlocal checked_after_move
                info = original_fstat(fd)
                if fd == reference_fd and moved:
                    checked_after_move = True
                return info

            def record_move(*args, **kwargs):
                nonlocal moved
                result = original_rename(*args, **kwargs)
                moved = True
                return result

            def replace_after_server_close():
                nonlocal stable_at_replacement
                original_server_close()
                path.unlink()
                if reference_fd >= 0:
                    info = original_fstat(reference_fd)
                    stable_at_replacement = (
                        info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)) == original_identity
                replacement.bind(str(path))
                replacement.listen(1)

                info = path.lstat()
                self.assertNotEqual((info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)),
                                    original_identity, "the held inode was reused by the replacement")

            try:
                with mock.patch.object(runtime._server, "server_close",
                                       side_effect=replace_after_server_close), \
                        mock.patch("xtos_ai_runtime.server.os.fstat", side_effect=inspect_reference), \
                        mock.patch("xtos_ai_runtime.server.os.rename", side_effect=record_move):
                    runtime.server_close()
                self.assertTrue(path.exists(), "cleanup removed the replacement socket")
                self.assertTrue(stable_at_replacement, "the original inode was released before quarantine")
                self.assertTrue(checked_after_move, "the moved entry was not compared with the stable reference")
                self.assertEqual(original_socket.fileno(), -1)
                with self.assertRaises(OSError):
                    original_fstat(reference_fd)
                self.assertEqual(list(root.glob(".xtos-socket-cleanup-*")), [])
                self.assertTrue(stat.S_ISSOCK(path.lstat().st_mode))
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(1)
                    probe.connect(str(path))
            finally:
                replacement.close()
                path.unlink(missing_ok=True)
                service.close()

    def test_unix_server_close_retains_replacement_when_restore_is_blocked(self):
        import xtos_ai_runtime.server as runtime_module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "runtime.sock"
            service = bind_fixture_models(InferenceService(root, trust_policy=FixtureTrustPolicy()))
            runtime = UnixInferenceServer(path, service)
            path.unlink()
            path.write_text("captured replacement")
            reference_fd = runtime._socket_reference_fd
            original_restore = runtime_module._rename_noreplace

            def block_restore(source, source_fd, target, target_fd):
                path.write_text("new public owner")
                return original_restore(source, source_fd, target, target_fd)

            try:
                with mock.patch.object(runtime_module, "_rename_noreplace",
                                       side_effect=block_restore):
                    with self.assertRaises(FileExistsError):
                        runtime.server_close()
                quarantine = list(root.glob(".xtos-socket-cleanup-*"))
                self.assertEqual(path.read_text(), "new public owner")
                self.assertEqual(len(quarantine), 1)
                self.assertEqual((quarantine[0] / "entry").read_text(), "captured replacement")
                os.fstat(reference_fd)
                with self.assertRaises(FileExistsError):
                    runtime.server_close()
                self.assertEqual(path.read_text(), "new public owner")
                retained = list(root.glob(".xtos-socket-cleanup-*"))
                self.assertEqual(len(retained), 1)
                self.assertEqual((retained[0] / "entry").read_text(), "captured replacement")
                self.assertFalse(runtime._closed)
                os.fstat(reference_fd)

                path.unlink()
                runtime.server_close()
                self.assertEqual(path.read_text(), "captured replacement")
                self.assertEqual(list(root.glob(".xtos-socket-cleanup-*")), [])
                self.assertTrue(runtime._closed)
                with self.assertRaises(OSError):
                    os.fstat(reference_fd)
            finally:
                path.unlink(missing_ok=True)
                for entry in root.glob(".xtos-socket-cleanup-*"):
                    (entry / "entry").unlink(missing_ok=True)
                    entry.rmdir()
                service.close()

    def test_socket_cleanup_quarantine_is_usable_under_restrictive_umask(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "runtime.sock"
            service = bind_fixture_models(InferenceService(root, trust_policy=FixtureTrustPolicy()))
            runtime = UnixInferenceServer(path, service)
            original_open = os.open

            def check_quarantine_access(target, flags, *args, **kwargs):
                if str(target).startswith(".xtos-socket-cleanup-"):
                    info = os.stat(target, dir_fd=kwargs["dir_fd"], follow_symlinks=False)
                    self.assertEqual(info.st_mode & 0o777, 0o700,
                                     "the service owner cannot traverse/open its cleanup directory")
                return original_open(target, flags, *args, **kwargs)

            previous_umask = os.umask(0o777)
            try:
                with mock.patch("xtos_ai_runtime.server.os.open", side_effect=check_quarantine_access):
                    runtime.server_close()
                self.assertFalse(path.exists())
                observed_umask = os.umask(0o777)
                self.assertEqual(observed_umask, 0o777)
            finally:
                os.umask(previous_umask)
                path.unlink(missing_ok=True)
                service.close()

    def test_unix_server_does_not_claim_socket_replaced_during_bind(self):
        import xtos_ai_runtime.server as runtime_module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "runtime.sock"
            service = bind_fixture_models(InferenceService(root, trust_policy=FixtureTrustPolicy()))
            replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            original_activate = runtime_module._UnixServer.server_activate

            def replace_after_bind(bound_server):
                original_activate(bound_server)
                path.unlink()
                replacement.bind(str(path))
                replacement.listen(1)

            runtime = None
            try:
                with mock.patch.object(runtime_module._UnixServer, "server_activate", replace_after_bind):
                    with self.assertRaisesRegex(RuntimeError, "socket identity"):
                        runtime = UnixInferenceServer(path, service)
                self.assertTrue(path.exists(), "replacement socket was removed during failed initialization")
            finally:
                if runtime is not None:
                    runtime.server_close()
                if path.exists():
                    path.unlink()
                replacement.close()
                service.close()

    def test_unix_server_is_reachable_without_tcp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sock"
            service = InferenceService(Path(directory), python_command=[sys.executable])
            server = UnixInferenceServer(path, service)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 2
                while not path.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(path.exists())
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.connect(str(path))
                    client.sendall(b'{"id":"x","op":"list"}\n')
                    response = json.loads(client.recv(65536))
                self.assertEqual(response["status"], "ok")
                self.assertEqual(response["models"], [])
            finally:
                server.shutdown()
                try:
                    thread.join(timeout=2)
                    self.assertFalse(thread.is_alive(), "serving thread did not finish automatic close")
                    server.server_close()
                finally:
                    service.close()


    def test_unix_server_constructor_cleans_bound_socket_after_activation_failure(self):
        import xtos_ai_runtime.server as runtime_module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "runtime.sock"
            service = bind_fixture_models(InferenceService(root, trust_policy=FixtureTrustPolicy()))
            original_activate = runtime_module._UnixServer.server_activate

            def fail_after_bind(server):
                original_activate(server)
                raise OSError("fixture activation failure")

            try:
                with mock.patch.object(runtime_module._UnixServer, "server_activate", fail_after_bind):
                    with self.assertRaisesRegex(OSError, "fixture activation failure"):
                        UnixInferenceServer(path, service)
                self.assertFalse(path.exists(), "failed construction left its bound socket path")
                self.assertEqual(list(root.glob(".xtos-socket-cleanup-*")), [])
            finally:
                path.unlink(missing_ok=True)
                service.close()

    def _check_constructor_cleanup_unlink_retry(self, failure_stage):
        import errno
        import xtos_ai_runtime.server as runtime_module
        for persistent in (False, True):
            for replace_public in (False, True):
                with self.subTest(persistent=persistent, replace_public=replace_public), \
                        tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    path = root / "runtime.sock"
                    service = bind_fixture_models(InferenceService(root, trust_policy=FixtureTrustPolicy()))
                    original_activate = runtime_module._UnixServer.server_activate
                    original_unlink = runtime_module.os.unlink
                    captured = {}
                    attempts = []
                    replacement = None

                    def identity(info):
                        return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))

                    def activate(server):
                        original_activate(server)
                        captured["socket"] = server.socket
                        captured["reference_fd"] = server._bound_socket_reference_fd
                        captured["identity"] = identity(os.fstat(captured["reference_fd"]))
                        captured["witness_fd"] = os.open(path, os.O_PATH | os.O_NOFOLLOW)
                        if failure_stage == "activation":
                            raise OSError("fixture activation failure")

                    def fail_entry_unlink(target, *args, **kwargs):
                        if target == "entry":
                            info = os.stat(target, dir_fd=kwargs["dir_fd"], follow_symlinks=False)
                            self.assertEqual(identity(info), captured["identity"])
                            self.assertFalse(path.exists(), "EIO must occur after quarantine rename")
                            attempts.append(identity(info))
                            if persistent or len(attempts) == 1:
                                raise OSError(errno.EIO, "fixture quarantined unlink failure")
                        return original_unlink(target, *args, **kwargs)

                    retry_code = (
                        "import sys; from pathlib import Path; "
                        "sys.path.insert(0, sys.argv[1]); "
                        "from xtos_ai_runtime.server import InferenceService, UnixInferenceServer; "
                        "p=Path(sys.argv[2]); s=InferenceService(p.parent); "
                        "r=UnixInferenceServer(p, s); r.server_close(); s.close()"
                    )
                    try:
                        began = time.monotonic()
                        with mock.patch.object(runtime_module._UnixServer, "server_activate", activate), \
                                mock.patch.object(runtime_module.os, "unlink", side_effect=fail_entry_unlink):
                            if failure_stage == "initialization":
                                with mock.patch.object(runtime_module.os, "chmod",
                                                       side_effect=OSError("fixture chmod failure")):
                                    with self.assertRaises(OSError):
                                        UnixInferenceServer(path, service)
                            else:
                                with self.assertRaises(OSError):
                                    UnixInferenceServer(path, service)
                        self.assertLess(time.monotonic() - began, 5, "constructor cleanup retried without a bound")
                        self.assertEqual(len(attempts), 1, "constructor cleanup retried a persistent I/O error")
                        self.assertEqual(captured["socket"].fileno(), -1)
                        with self.assertRaises(OSError) as closed:
                            os.fstat(captured["reference_fd"])
                        self.assertEqual(closed.exception.errno, errno.EBADF)
                        quarantine = list(root.glob(".xtos-socket-cleanup-*"))
                        if quarantine:
                            self.assertEqual(len(quarantine), 1)
                            self.assertEqual(identity((quarantine[0] / "entry").lstat()), captured["identity"])

                        if persistent:
                            with mock.patch.object(runtime_module.os, "unlink", side_effect=fail_entry_unlink):
                                with self.assertRaises(OSError) as retry_error:
                                    UnixInferenceServer(path, service)
                            self.assertEqual(retry_error.exception.errno, errno.EIO)
                            self.assertFalse(path.exists())
                            quarantine = list(root.glob(".xtos-socket-cleanup-*"))
                            self.assertEqual(len(quarantine), 1)
                            self.assertEqual(identity((quarantine[0] / "entry").lstat()), captured["identity"])

                        if replace_public:
                            replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                            replacement.bind(str(path))
                            replacement.listen(4)
                            replacement_identity = identity(path.lstat())
                            with self.assertRaises(FileExistsError):
                                UnixInferenceServer(path, service)
                            self.assertEqual(identity(path.lstat()), replacement_identity)
                            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                                probe.settimeout(1)
                                probe.connect(str(path))
                            self.assertEqual(list(root.glob(".xtos-socket-cleanup-*")), [],
                                             "startup did not recover old cleanup beside a caller replacement")
                            self.assertEqual(os.fstat(captured["witness_fd"]).st_nlink, 0)
                            path.rename(root / "caller.sock")

                        retry = subprocess.run(
                            [sys.executable, "-c", retry_code,
                             str(ROOT / "tools/xtos-ai-runtime"), str(path)],
                            text=True, capture_output=True, timeout=5)
                        self.assertEqual(retry.returncode, 0, retry.stderr)
                        self.assertEqual(list(root.glob(".xtos-socket-cleanup-*")), [],
                                         "ordinary fresh-process startup lost constructor cleanup state")
                        self.assertEqual(os.fstat(captured["witness_fd"]).st_nlink, 0,
                                         "failed constructor's original socket remains linked")
                        self.assertFalse(path.exists())
                        if replace_public:
                            self.assertEqual(identity((root / "caller.sock").lstat()), replacement_identity)
                    finally:
                        if "witness_fd" in captured:
                            os.close(captured["witness_fd"])
                        if replacement is not None:
                            replacement.close()
                        service.close()

    def test_unix_server_constructor_recovers_activation_failure_after_quarantine_unlink_error(self):
        self._check_constructor_cleanup_unlink_retry("activation")

    def test_unix_server_constructor_recovers_initialization_failure_after_quarantine_unlink_error(self):
        self._check_constructor_cleanup_unlink_retry("initialization")

    def _strand_failed_constructor_socket(self, root):
        import errno
        import xtos_ai_runtime.server as runtime_module
        path = root / "runtime.sock"
        service = bind_fixture_models(InferenceService(root, trust_policy=FixtureTrustPolicy()))
        self.addCleanup(service.close)
        original_unlink = runtime_module.os.unlink

        def fail_entry(target, *args, **kwargs):
            if target == "entry":
                raise OSError(errno.EIO, "fixture stranded entry")
            return original_unlink(target, *args, **kwargs)

        with mock.patch.object(runtime_module.os, "chmod", side_effect=OSError("fixture initialization")), \
                mock.patch.object(runtime_module.os, "unlink", side_effect=fail_entry):
            with self.assertRaises(OSError) as caught:
                UnixInferenceServer(path, service)
        self.assertEqual(caught.exception.errno, errno.EIO)
        quarantine, = root.glob(".xtos-socket-cleanup-*")
        state, = root.glob(".xtos-socket-state-*")
        self.assertEqual((quarantine / "entry").stat().st_ino, (quarantine / "owner").stat().st_ino)
        return path, service, quarantine, state

    def test_unix_server_constructor_rejects_fifo_cleanup_state_without_blocking(self):
        retry_code = chr(10).join([
            "import fcntl, json, os, sys",
            "from pathlib import Path",
            "from xtos_ai_runtime.server import InferenceService, UnixInferenceServer",
            "path = Path(sys.argv[1])",
            "service = InferenceService(path.parent)",
            "before = set(os.listdir('/proc/self/fd'))",
            "print('starting', flush=True)",
            "try:",
            "    runtime = UnixInferenceServer(path, service)",
            "    runtime.server_close()",
            "except Exception as error:",
            "    result = {'error': type(error).__name__, 'message': str(error)}",
            "else:",
            "    result = {'error': None}",
            "finally:",
            "    service.close()",
            "result['fds_unchanged'] = set(os.listdir('/proc/self/fd')) == before",
            "fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)",
            "try:",
            "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)",
            "finally:",
            "    os.close(fd)",
            "print(json.dumps(result))",
        ]) + chr(10)
        compile(retry_code, "<fifo-state-recovery-probe>", "exec")
        environment = dict(os.environ, PYTHONPATH=str(ROOT / "tools/xtos-ai-runtime"),
                           PYTHONDONTWRITEBYTECODE="1")
        for stranded in (True, False):
            for phase in ("pending", "detached"):
                with self.subTest(stranded=stranded, phase=phase), \
                        tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    witness = -1
                    record = None
                    if stranded:
                        path, service, quarantine, state = self._strand_failed_constructor_socket(root)
                        self.assertTrue(state.name.endswith(".detached"))
                        if phase == "pending":
                            pending = state.with_name(state.name[:-9])
                            state.rename(pending)
                            state = pending
                        record = state.read_bytes()
                        witness = os.open(quarantine / "entry", os.O_PATH | os.O_CLOEXEC)
                        state.unlink()
                    else:
                        path = root / "runtime.sock"
                        state = root / (".xtos-socket-state-" + "a" * 32
                                        + (".detached" if phase == "detached" else ""))
                    public = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    try:
                        os.mkfifo(state, 0o600)
                        state.chmod(0o600)
                        (root / "caller-file").write_bytes(b"protected caller bytes")
                        public.bind(str(path))
                        public.listen(4)

                        def snapshot():
                            result = {}
                            for item in [root, *root.rglob("*")]:
                                info = item.lstat()
                                content = item.read_bytes() if stat.S_ISREG(info.st_mode) else None
                                result[str(item.relative_to(root))] = (
                                    info.st_dev, info.st_ino, info.st_mode, info.st_uid,
                                    info.st_nlink, content,
                                )
                            return result

                        before = snapshot()
                        process = subprocess.Popen(
                            [sys.executable, "-c", retry_code, str(path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment,
                        )
                        blocked = None
                        try:
                            try:
                                stdout, stderr = process.communicate(timeout=1)
                            except subprocess.TimeoutExpired:
                                blocked = Path(f"/proc/{process.pid}/wchan").read_text().strip()
                                process.terminate()
                                stdout, stderr = process.communicate(timeout=2)
                        finally:
                            if process.poll() is None:
                                process.kill()
                                process.communicate(timeout=2)
                        self.assertEqual(snapshot(), before, "FIFO rejection mutated the scene")
                        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                            probe.settimeout(1)
                            probe.connect(str(path))
                        self.assertEqual(stderr, "")
                        self.assertEqual(stdout.splitlines()[0], "starting")
                        self.assertIsNone(blocked, f"constructor blocked opening FIFO: wchan={blocked}")
                        self.assertEqual(process.returncode, 0)
                        document = json.loads(stdout.splitlines()[1])
                        self.assertEqual(document["error"], "RuntimeError")
                        self.assertIn("private service-owned file", document["message"])
                        self.assertTrue(document["fds_unchanged"], "rejected state leaked descriptors")

                        state.unlink()
                        if record is not None:
                            state.write_bytes(record)
                            state.chmod(0o600)
                        caller = root / "caller.sock"
                        path.rename(caller)
                        recovered = subprocess.run(
                            [sys.executable, "-c", retry_code, str(path)],
                            capture_output=True, text=True, timeout=2, env=environment,
                        )
                        self.assertEqual(recovered.returncode, 0, recovered.stderr)
                        self.assertEqual(recovered.stderr, "")
                        document = json.loads(recovered.stdout.splitlines()[1])
                        self.assertIsNone(document["error"], document)
                        self.assertTrue(document["fds_unchanged"])
                        self.assertEqual(list(root.glob(".xtos-socket-*")), [])
                        self.assertFalse(path.exists())
                        if witness >= 0:
                            self.assertEqual(os.fstat(witness).st_nlink, 0)
                        self.assertEqual((root / "caller-file").read_bytes(), b"protected caller bytes")
                        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                            probe.settimeout(1)
                            probe.connect(str(caller))
                    finally:
                        public.close()
                        if witness >= 0:
                            os.close(witness)

    def test_unix_server_constructor_recovery_preserves_untrusted_quarantine(self):
        for tamper in ("owner", "directory", "entry", "state", "duplicate_state", "unknown"):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path, service, quarantine, state = self._strand_failed_constructor_socket(root)
                protected_socket = None
                public = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                public.bind(str(path))
                public.listen(4)
                public_identity = (path.stat().st_dev, path.stat().st_ino)
                try:
                    if tamper == "owner":
                        (quarantine / "owner").unlink()
                        protected_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        protected_socket.bind(str(quarantine / "owner"))
                        protected_socket.listen(1)
                    elif tamper == "directory":
                        quarantine.rename(root / "preserved-quarantine")
                        quarantine.mkdir(mode=0o700)
                        (quarantine / "entry").write_text("caller directory sentinel")
                    elif tamper == "entry":
                        (quarantine / "entry").rename(root / "preserved-original")
                        (quarantine / "entry").write_text("caller entry sentinel")
                    elif tamper == "state":
                        state.write_text('{"invalid":"caller state sentinel"}')
                    elif tamper == "duplicate_state":
                        value = state.read_text()
                        state.write_text(value[:-1] + ',"target":"runtime.sock"}')
                    else:
                        (quarantine / "sentinel").write_text("caller unknown sentinel")
                    before = {str(p.relative_to(root)): p.lstat().st_ino for p in root.rglob("*")}
                    with self.assertRaises((RuntimeError, FileExistsError)):
                        UnixInferenceServer(path, service)
                    self.assertEqual({str(p.relative_to(root)): p.lstat().st_ino for p in root.rglob("*")}, before)
                    self.assertEqual((path.stat().st_dev, path.stat().st_ino), public_identity)
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                        probe.settimeout(1)
                        probe.connect(str(path))
                    self.assertTrue(state.exists())
                finally:
                    public.close()
                    if protected_socket is not None:
                        protected_socket.close()

    def test_detached_constructor_recovery_rejects_replaced_entry_without_mutation(self):
        retry_code = chr(10).join([
            "import json, sys",
            "from pathlib import Path",
            "from xtos_ai_runtime.server import InferenceService, UnixInferenceServer",
            "path = Path(sys.argv[1])",
            "service = InferenceService(path.parent)",
            "try:",
            "    runtime = UnixInferenceServer(path, service)",
            "    runtime.server_close()",
            "except Exception as error:",
            "    print(json.dumps({'error': type(error).__name__, 'message': str(error)}))",
            "else:",
            "    print(json.dumps({'error': None}))",
            "finally:",
            "    service.close()",
        ]) + chr(10)
        compile(retry_code, "<detached-recovery-probe>", "exec")
        environment = dict(os.environ, PYTHONPATH=str(ROOT / "tools/xtos-ai-runtime"),
                           PYTHONDONTWRITEBYTECODE="1")
        for replacement_kind in ("file", "symlink", "socket"):
            for public_present in (False, True):
                with self.subTest(replacement=replacement_kind, public_present=public_present), \
                        tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    path, service, quarantine, state = self._strand_failed_constructor_socket(root)
                    self.assertTrue(state.name.endswith(".detached"))
                    entry = quarantine / "entry"
                    entry.rename(root / "preserved-original")
                    sockets = []
                    try:
                        if replacement_kind == "file":
                            entry.write_bytes(b"untrusted entry sentinel")
                        elif replacement_kind == "symlink":
                            target = root / "caller-target"
                            target.write_bytes(b"protected referent")
                            entry.symlink_to(target)
                        else:
                            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                            sockets.append((connection, entry))
                            connection.bind(str(entry))
                            connection.listen(4)
                        if public_present:
                            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                            sockets.append((connection, path))
                            connection.bind(str(path))
                            connection.listen(4)

                        def snapshot():
                            result = {}
                            for item in root.rglob("*"):
                                info = item.lstat()
                                content = (os.readlink(item) if stat.S_ISLNK(info.st_mode)
                                           else item.read_bytes() if stat.S_ISREG(info.st_mode) else None)
                                result[str(item.relative_to(root))] = (
                                    info.st_dev, info.st_ino, info.st_mode, info.st_uid,
                                    info.st_nlink, content,
                                )
                            return result

                        before = snapshot()
                        result = subprocess.run(
                            [sys.executable, "-c", retry_code, str(path)],
                            capture_output=True, text=True, timeout=5, env=environment,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stderr, "")
                        self.assertEqual(snapshot(), before,
                                         "detached recovery mutated untrusted quarantine or public path")
                        document = json.loads(result.stdout)
                        self.assertEqual(document["error"], "RuntimeError")
                        self.assertIn("detached", document["message"])
                        self.assertEqual(path.exists(), public_present)
                        for _, address in sockets:
                            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                                probe.settimeout(1)
                                probe.connect(str(address))
                    finally:
                        for connection, _ in sockets:
                            connection.close()
                        service.close()

    def test_unix_server_startup_leaves_unrelated_cleanup_names_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = "a" * 32
            quarantine = root / (".xtos-socket-cleanup-" + token)
            quarantine.mkdir(mode=0o700)
            (quarantine / "entry").write_text("unrelated caller entry")
            marker = root / (".xtos-socket-moved-" + token)
            marker.write_text(token + "\n")
            marker.chmod(0o600)
            before = {str(p.relative_to(root)): p.lstat().st_ino for p in root.rglob("*")}
            service = bind_fixture_models(InferenceService(root, trust_policy=FixtureTrustPolicy()))
            try:
                runtime = UnixInferenceServer(root / "runtime.sock", service)
                runtime.server_close()
                self.assertEqual({str(p.relative_to(root)): p.lstat().st_ino for p in root.rglob("*")}, before)
                self.assertEqual((quarantine / "entry").read_text(), "unrelated caller entry")
                self.assertEqual(marker.read_text(), token + "\n")
            finally:
                service.close()

    def test_unix_server_constructor_recovery_retries_partial_cleanup_errors(self):
        import errno
        import xtos_ai_runtime.server as runtime_module
        for fault in ("entry", "owner", "quarantine", "state", "phase"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path, service, quarantine, state = self._strand_failed_constructor_socket(root)
                if fault == "phase" and state.name.endswith(".detached"):
                    pending_state = state.with_name(state.name[:-9])
                    state.rename(pending_state)
                    state = pending_state
                original_unlink, original_rmdir = runtime_module.os.unlink, runtime_module.os.rmdir
                original_restore = runtime_module._rename_noreplace
                attempts = []

                def fail_unlink(target, *args, **kwargs):
                    matches = (target == fault or fault == "state" and str(target).startswith(".xtos-socket-state-"))
                    if matches:
                        attempts.append(target)
                        raise OSError(errno.EIO, "fixture partial cleanup failure")
                    return original_unlink(target, *args, **kwargs)

                def fail_rmdir(target, *args, **kwargs):
                    if fault == "quarantine" and target == quarantine.name:
                        attempts.append(target)
                        raise OSError(errno.EIO, "fixture partial directory failure")
                    return original_rmdir(target, *args, **kwargs)

                def fail_phase(source, source_fd, target, target_fd):
                    if fault == "phase" and str(target).endswith(".detached"):
                        attempts.append(target)
                        raise OSError(errno.EIO, "fixture cleanup phase failure")
                    return original_restore(source, source_fd, target, target_fd)

                with mock.patch.object(runtime_module.os, "unlink", side_effect=fail_unlink), \
                        mock.patch.object(runtime_module.os, "rmdir", side_effect=fail_rmdir), \
                        mock.patch.object(runtime_module, "_rename_noreplace", side_effect=fail_phase):
                    with self.assertRaises(OSError) as caught:
                        UnixInferenceServer(path, service)
                self.assertEqual(caught.exception.errno, errno.EIO)
                self.assertEqual(len(attempts), 1)
                self.assertFalse(path.exists())
                replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    replacement.bind(str(path))
                    replacement.listen(4)
                    identity = (path.stat().st_dev, path.stat().st_ino)
                    with self.assertRaises(FileExistsError):
                        UnixInferenceServer(path, service)
                    self.assertEqual((path.stat().st_dev, path.stat().st_ino), identity)
                    self.assertEqual(list(root.glob(".xtos-socket-*")), [])
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                        probe.settimeout(1)
                        probe.connect(str(path))
                    path.rename(root / "caller.sock")
                    runtime = UnixInferenceServer(path, service)
                    runtime.server_close()
                    self.assertEqual((root / "caller.sock").stat().st_ino, identity[1])
                    self.assertFalse(path.exists())
                finally:
                    replacement.close()

    def test_unix_server_constructor_retains_path_when_stable_reference_is_unavailable(self):
        import errno
        import xtos_ai_runtime.server as runtime_module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "runtime.sock"
            service = bind_fixture_models(InferenceService(root, trust_policy=FixtureTrustPolicy()))
            original_open = runtime_module.os.open

            def refuse_stable_reference(target, flags, *args, **kwargs):
                if flags & getattr(os, "O_PATH", 0):
                    raise OSError(errno.ENOTSUP, "fixture O_PATH unavailable")
                return original_open(target, flags, *args, **kwargs)

            try:
                with mock.patch.object(runtime_module.os, "open", refuse_stable_reference):
                    with self.assertRaisesRegex(OSError, "fixture O_PATH unavailable"):
                        UnixInferenceServer(path, service)
                self.assertTrue(path.exists(), "unsafe cleanup removed a socket without stable identity")
                self.assertTrue(stat.S_ISSOCK(path.lstat().st_mode))
            finally:
                path.unlink(missing_ok=True)
                service.close()

    def test_concurrent_server_close_does_not_quarantine_replacement(self):
        import xtos_ai_runtime.server as runtime_module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "runtime.sock"
            service = bind_fixture_models(InferenceService(root, trust_policy=FixtureTrustPolicy()))
            runtime = UnixInferenceServer(path, service)
            replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            original_cleanup = runtime._unlink_owned_socket
            original_close_reference = runtime._close_socket_reference
            cleanup_calls = 0
            cleanup_lock = threading.Lock()
            first_cleanup_done = threading.Event()
            release_first_close = threading.Event()
            replacement_bound = threading.Event()
            reference_closed = threading.Event()
            second_cleanup_entered = threading.Event()
            second_close_lock_attempted = threading.Event()
            errors = []

            class TrackedLock:
                def __init__(self):
                    self.lock = threading.Lock()
                    self.calls = 0
                    self.calls_lock = threading.Lock()

                def acquire(self, blocking=True, timeout=-1):
                    with self.calls_lock:
                        self.calls += 1
                        if self.calls == 2:
                            second_close_lock_attempted.set()
                    return self.lock.acquire(blocking, timeout)

                def release(self):
                    self.lock.release()

                def __enter__(self):
                    self.acquire()
                    return self

                def __exit__(self, *_args):
                    self.release()

            def cleanup_once():
                nonlocal cleanup_calls
                with cleanup_lock:
                    cleanup_calls += 1
                    call = cleanup_calls
                if call == 1:
                    result = original_cleanup()
                    replacement.bind(str(path))
                    replacement.listen(1)
                    replacement_bound.set()
                    first_cleanup_done.set()
                    if not release_first_close.wait(1):
                        raise AssertionError("first close was not released")
                    return result
                second_cleanup_entered.set()
                if not reference_closed.wait(1):
                    raise AssertionError("second cleanup did not reach the reference-close boundary")
                return original_cleanup()

            def close_reference():
                try:
                    return original_close_reference()
                finally:
                    reference_closed.set()

            def close_runtime():
                try:
                    runtime.server_close()
                except BaseException as exc:
                    errors.append(exc)

            runtime._close_lock = TrackedLock()
            first = threading.Thread(target=close_runtime, daemon=True)
            second = threading.Thread(target=close_runtime, daemon=True)
            try:
                with mock.patch.object(runtime, "_unlink_owned_socket", side_effect=cleanup_once), \
                        mock.patch.object(runtime, "_close_socket_reference", side_effect=close_reference):
                    first.start()
                    self.assertTrue(first_cleanup_done.wait(1))
                    second.start()
                    deadline = time.monotonic() + 1
                    while time.monotonic() < deadline:
                        if second_close_lock_attempted.is_set() or second_cleanup_entered.is_set():
                            break
                        time.sleep(.001)
                    self.assertTrue(second_close_lock_attempted.is_set() or second_cleanup_entered.is_set(),
                                    "second close did not reach the serialization boundary")
                    release_first_close.set()
                    first.join(2)
                    second.join(2)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(cleanup_calls, 1)
                self.assertTrue(replacement_bound.is_set())
                self.assertTrue(path.exists(), "replacement socket disappeared during duplicate close")
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(1)
                    probe.connect(str(path))
                self.assertEqual(list(root.glob(".xtos-socket-cleanup-*")), [])
            finally:
                release_first_close.set()
                if first.ident is not None:
                    first.join(2)
                if second.ident is not None:
                    second.join(2)
                replacement.close()
                path.unlink(missing_ok=True)
                for entry in root.glob(".xtos-socket-cleanup-*"):
                    (entry / "entry").unlink(missing_ok=True)
                    entry.rmdir()
                service.close()

    def test_unix_server_close_is_bounded_when_cleanup_stalls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "runtime.sock"
            service = bind_fixture_models(InferenceService(root, trust_policy=FixtureTrustPolicy()))
            runtime = UnixInferenceServer(path, service)
            replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            original_cleanup = runtime._unlink_owned_socket
            cleanup_entered = threading.Event()
            release_cleanup = threading.Event()
            second_started = threading.Event()
            second_lock_attempted = threading.Event()
            cleanup_calls = 0
            cleanup_calls_lock = threading.Lock()
            errors = {"first": [], "second": []}
            reference_fd = runtime._socket_reference_fd

            class TrackedLock:
                def __init__(self):
                    self.lock = threading.Lock()
                    self.calls = 0
                    self.calls_lock = threading.Lock()

                def acquire(self, blocking=True, timeout=-1):
                    with self.calls_lock:
                        self.calls += 1
                        if self.calls == 2:
                            second_lock_attempted.set()
                    return self.lock.acquire(blocking, timeout)

                def release(self):
                    self.lock.release()

                def __enter__(self):
                    self.acquire()
                    return self

                def __exit__(self, *_args):
                    self.release()

            def stall_first_cleanup():
                nonlocal cleanup_calls
                with cleanup_calls_lock:
                    cleanup_calls += 1
                    call = cleanup_calls
                if call == 1:
                    cleanup_entered.set()
                    if not release_cleanup.wait(2):
                        raise AssertionError("stalled cleanup was not released")
                original_cleanup()
                if call == 1:
                    replacement.bind(str(path))
                    replacement.listen(1)
                    raise OSError("fixture transient cleanup failure")

            def close_runtime(name, started=None):
                if started is not None:
                    started.set()
                try:
                    runtime.server_close()
                except BaseException as exc:
                    errors[name].append(exc)

            first = threading.Thread(target=close_runtime, args=("first",))
            second = threading.Thread(target=close_runtime, args=("second", second_started))
            second_finished_while_stalled = False
            runtime._close_lock = TrackedLock()
            try:
                with mock.patch.object(runtime, "_unlink_owned_socket", side_effect=stall_first_cleanup):
                    first.start()
                    self.assertTrue(cleanup_entered.wait(1), "first close did not reach cleanup")
                    second.start()
                    self.assertTrue(second_started.wait(1), "second close did not start")
                    self.assertTrue(second_lock_attempted.wait(1), "second close did not attempt the close lock")
                    second.join(.5)
                    second_finished_while_stalled = not second.is_alive()
                    release_cleanup.set()
                    first.join(2)
                    second.join(2)

                self.assertTrue(second_finished_while_stalled,
                                "duplicate server_close waited without a bound for stalled cleanup")
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
                self.assertEqual(len(errors["first"]), 1)
                self.assertIsInstance(errors["first"][0], OSError)
                self.assertEqual(len(errors["second"]), 1)
                self.assertIsInstance(errors["second"][0], RuntimeError)
                self.assertRegex(str(errors["second"][0]), "in progress")
                self.assertEqual(runtime._close_state, "cleanup_failed")

                with mock.patch.object(runtime, "_unlink_owned_socket", side_effect=stall_first_cleanup):
                    runtime.server_close()
                self.assertEqual(cleanup_calls, 2, "retry did not recheck and preserve the replacement")
                self.assertEqual(runtime._close_state, "closed")
                self.assertEqual(runtime._socket_reference_fd, -1)
                with self.assertRaises(OSError):
                    os.fstat(reference_fd)
                self.assertTrue(path.exists(), "duplicate close removed the replacement socket")
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(1)
                    probe.connect(str(path))
                self.assertEqual(list(root.glob(".xtos-socket-cleanup-*")), [])
            finally:
                release_cleanup.set()
                if first.ident is not None:
                    first.join(2)
                if second.ident is not None:
                    second.join(2)
                replacement.close()
                path.unlink(missing_ok=True)
                for entry in root.glob(".xtos-socket-cleanup-*"):
                    (entry / "entry").unlink(missing_ok=True)
                    entry.rmdir()
                service.close()

    def test_base_exception_close_releases_gate_and_propagates_to_waiter(self):
        for exception_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(exception_type=exception_type.__name__):
                entered = threading.Event()
                release = threading.Event()
                class BaseExceptionWorker(ControlledWorker):
                    def __init__(self):
                        super().__init__()
                        self.close_calls = 0

                    def close(self):
                        self.close_calls += 1
                        entered.set()
                        release.wait(1)
                        raise exception_type("fixture base exception")

                service = bind_fixture_models(InferenceService(ROOT, trust_policy=FixtureTrustPolicy()))
                worker = BaseExceptionWorker()
                first_errors, second_errors = [], []

                def close_into(errors):
                    try:
                        service._safe_close(worker)
                    except BaseException as exc:
                        errors.append(exc)

                first = threading.Thread(target=close_into, args=(first_errors,), daemon=True)
                second = threading.Thread(target=close_into, args=(second_errors,), daemon=True)
                first.start()
                self.assertTrue(entered.wait(1))
                second.start()
                release.set()
                first.join(2)
                second.join(2)
                self.assertFalse(first.is_alive(), "first close did not propagate its base exception")
                self.assertFalse(second.is_alive(), "close waiter remained blocked after base exception")
                self.assertEqual(len(first_errors), 1)
                self.assertIsInstance(first_errors[0], exception_type)
                self.assertEqual(len(second_errors), 1)
                self.assertIsInstance(second_errors[0], exception_type)
                state = service._close_states[worker]
                self.assertTrue(state.done.is_set())
                self.assertEqual(worker.close_calls, 1)

    def test_shutdown_waits_for_recovery_before_closing_worker(self):
        class RecoveryBarrierWorker(ControlledWorker):
            def __init__(self):
                super().__init__()
                self.alive = False
                self.restart_entered = threading.Event()
                self.allow_restart = threading.Event()
                self.restart_exited = threading.Event()
                self.close_entered = threading.Event()
                self.close_during_restart = False
                self._restart_lock = threading.Lock()
                self._restarting = False

            def restart(self, cancel=None):
                with self._restart_lock:
                    self._restarting = True
                self.restart_entered.set()
                try:
                    self.allow_restart.wait(2)
                    self.alive = False if cancel is not None and cancel.is_set() else True
                    return {"status": "ready"}
                finally:
                    with self._restart_lock:
                        self._restarting = False
                    self.restart_exited.set()

            def close(self):
                with self._restart_lock:
                    self.close_during_restart = self._restarting
                self.close_entered.set()
                super().close()

        service = bind_fixture_models(InferenceService(ROOT, trust_policy=FixtureTrustPolicy()))
        worker = RecoveryBarrierWorker()
        service._models["controlled"] = worker
        worker.release.set()
        result = {}
        infer = threading.Thread(target=lambda: result.update(service.dispatch({
            "id": "recover", "op": "infer", "model": "controlled",
            "input": [[0]], "deadline_ms": 1000})), daemon=True)
        shutdown_errors = []
        shutdown = threading.Thread(target=lambda: _capture_exception(service.close, shutdown_errors), daemon=True)
        try:
            infer.start()
            self.assertTrue(worker.restart_entered.wait(1))
            infer.join(1)
            self.assertFalse(infer.is_alive())
            shutdown.start()
            time.sleep(.05)
            self.assertFalse(worker.close_entered.is_set(), "shutdown closed a worker during restart")
            worker.allow_restart.set()
            shutdown.join(2)
            self.assertFalse(shutdown.is_alive())
            self.assertEqual(shutdown_errors, [])
            self.assertTrue(worker.restart_exited.is_set())
            self.assertTrue(worker.close_entered.is_set())
            self.assertFalse(worker.close_during_restart)
        finally:
            worker.allow_restart.set()
            infer.join(2)
            shutdown.join(2)
            service.close()

    def test_shutdown_timeout_keeps_recovering_worker_unclosed(self):
        class StalledRecoveryWorker(ControlledWorker):
            def __init__(self):
                super().__init__()
                self.alive = False
                self.restart_entered = threading.Event()
                self.allow_restart = threading.Event()
                self.close_calls = 0

            def restart(self, cancel=None):
                self.restart_entered.set()
                self.allow_restart.wait(2)
                self.alive = False
                return {"status": "ready"}

            def close(self):
                self.close_calls += 1
                super().close()

        service = bind_fixture_models(InferenceService(ROOT, trust_policy=FixtureTrustPolicy()))
        worker = StalledRecoveryWorker()
        service._models["controlled"] = worker
        worker.release.set()
        infer = threading.Thread(target=lambda: service.dispatch({
            "id": "recover-timeout", "op": "infer", "model": "controlled",
            "input": [[0]], "deadline_ms": 1000}), daemon=True)
        try:
            infer.start()
            self.assertTrue(worker.restart_entered.wait(1))
            infer.join(1)
            self.assertFalse(infer.is_alive())
            with mock.patch("xtos_ai_runtime.server.SHUTDOWN_TIMEOUT", .1):
                with self.assertRaisesRegex(RuntimeError, "shutdown"):
                    service.close()
            self.assertEqual(worker.close_calls, 0, "timed-out recovery was closed concurrently")
            worker.allow_restart.set()
            service.close()
            self.assertEqual(worker.close_calls, 1)
        finally:
            worker.allow_restart.set()
            infer.join(2)
            service.close()

    def test_active_dispatch_does_not_wait_forever_for_stalled_shutdown_close(self):
        release_close = threading.Event()
        close_started = threading.Event()
        class StalledCloseWorker(ControlledWorker):
            def close(self):
                close_started.set()
                release_close.wait(2)
                super().close()

        service = bind_fixture_models(InferenceService(ROOT, trust_policy=FixtureTrustPolicy()))
        worker = StalledCloseWorker()
        service._models["controlled"] = worker
        result = {}
        infer = threading.Thread(target=lambda: result.update(service.dispatch({
            "id": "active-stalled-close", "op": "infer", "model": "controlled",
            "input": [[0]], "deadline_ms": 5000})), daemon=True)
        try:
            infer.start()
            self.assertTrue(worker.started.wait(1))
            with mock.patch("xtos_ai_runtime.server.SHUTDOWN_TIMEOUT", .1):
                with self.assertRaisesRegex(RuntimeError, "shutdown"):
                    service.close()
            self.assertTrue(close_started.is_set())
            infer.join(.5)
            self.assertFalse(infer.is_alive(), "active dispatch remained blocked on shutdown close gate")
            self.assertEqual(result["safe_action"], "no_action")
        finally:
            release_close.set()
            infer.join(2)
            service.close()


def _capture_exception(function, errors):
    try:
        function()
    except BaseException as exc:
        errors.append(exc)


if __name__ == "__main__":
    unittest.main()
