import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "tools/xtos-ai-runtime"))
from xtos_ai_runtime.client import request as client_request
from xtos_ai_runtime.server import InferenceService, UnixInferenceServer  # noqa: E402
from ai_service_trust_fixtures import make_real_signed_service_fixture


class RealServiceTests(unittest.TestCase):
    def setUp(self):
        if not os.environ.get("XTOS_TEST_AI_PYTHON"):
            return
        directory = tempfile.TemporaryDirectory(prefix="xtos-real-service-")
        self.addCleanup(directory.cleanup)
        self.fx, self.envelope = make_real_signed_service_fixture(Path(directory.name))

    def make_service(self):
        return InferenceService(self.fx.model_root,
                                python_command=[os.environ["XTOS_TEST_AI_PYTHON"]],
                                trust_policy=self.fx.policy)

    @unittest.skipUnless(os.environ.get("XTOS_TEST_AI_PYTHON"), "explicit locked AI Python not selected")
    def test_load_infer_health_and_reload(self):
        manifest = json.loads(self.envelope)
        service = self.make_service()
        try:
            loaded = service.dispatch({"id": "load-1", "op": "load", "manifest": manifest})
            self.assertEqual(loaded["status"], "ok", loaded)
            self.assertEqual(loaded["safe_action"], "no_action")
            inferred = service.dispatch({
                "id": "infer-1", "op": "infer", "model": "fixed-classifier",
                "input": [[2.0, 0.0, -1.0, 0.0]], "deadline_ms": 3000,
            })
            self.assertEqual(inferred["status"], "success", inferred)
            self.assertEqual(inferred["safe_action"], "action_allowed")
            self.assertEqual(inferred["provider"], "CPUExecutionProvider")
            self.assertLessEqual(abs(inferred["output"][0][0] - 0.8437947034835815), 1e-5)
            health = service.dispatch({"id": "health-1", "op": "health"})
            self.assertTrue(health["models"]["fixed-classifier"]["alive"])
            reloaded = service.dispatch({"id": "reload-1", "op": "reload", "manifest": manifest})
            self.assertEqual(reloaded["status"], "ok", reloaded)
            listed = service.dispatch({"id": "list-1", "op": "list"})
            self.assertEqual(listed["models"], ["fixed-classifier"])
        finally:
            service.close()

    @unittest.skipUnless(os.environ.get("XTOS_TEST_AI_PYTHON"), "explicit locked AI Python not selected")
    def test_real_service_recovers_after_worker_deadline(self):
        manifest = json.loads(self.envelope)
        service = self.make_service()
        try:
            loaded = service.dispatch({"id": "recover-load", "op": "load", "manifest": manifest})
            self.assertEqual(loaded["status"], "ok", loaded)
            worker = service._models["fixed-classifier"]
            pid = worker.pid
            original_infer = worker.infer
            entered_worker = threading.Event()

            def stopped_infer(request_id, value, remaining_ms, cancel=None):
                # Inject after real trust validation: it can exceed 250ms under
                # QEMU. Keep real worker timeout/kill/reap/recovery semantics.
                entered_worker.set()
                os.kill(pid, signal.SIGSTOP)
                return original_infer(request_id, value, min(250, remaining_ms), cancel)

            with mock.patch.object(worker, "infer", side_effect=stopped_infer):
                result = service.dispatch({
                    "id": "recover-timeout", "op": "infer", "model": "fixed-classifier",
                    "input": [[2.0, 0.0, -1.0, 0.0]], "deadline_ms": 3000,
                })
            self.assertTrue(entered_worker.is_set(), "request expired before fault injection")
            self.assertEqual(result["status"], "timeout", result)
            self.assertEqual(result["safe_action"], "no_action")
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                health = service.dispatch({"id": "recover-health", "op": "health"})
                state = health["models"]["fixed-classifier"]
                if state["alive"] and state["pid"] != pid and not state["recovering"]:
                    break
                time.sleep(.05)
            self.assertTrue(state["alive"], health)
            self.assertNotEqual(state["pid"], pid, health)
            self.assertFalse(state["recovering"], health)
            recovered = service.dispatch({
                "id": "recover-infer", "op": "infer", "model": "fixed-classifier",
                "input": [[2.0, 0.0, -1.0, 0.0]], "deadline_ms": 3000,
            })
            self.assertEqual(recovered["status"], "success", recovered)
        finally:
            service.close()

    @unittest.skipUnless(os.environ.get("XTOS_TEST_AI_PYTHON"), "explicit locked AI Python not selected")
    def test_real_uds_cancels_inflight_and_recovers(self):
        manifest = json.loads(self.envelope)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cancel.sock"
            service = self.make_service()
            server = UnixInferenceServer(path, service)
            serving = threading.Thread(target=server.serve_forever, daemon=True)
            serving.start()
            response, errors = {}, []
            def infer():
                try:
                    response.update(client_request(path, {
                        "id": "cancel-active", "op": "infer", "model": "fixed-classifier",
                        "input": [[2.0, 0.0, -1.0, 0.0]], "deadline_ms": 5000,
                    }, timeout=10))
                except Exception as exc:
                    errors.append(repr(exc))
            active = threading.Thread(target=infer)
            try:
                loaded = client_request(path, {"id": "cancel-load", "op": "load", "manifest": manifest}, timeout=120)
                self.assertEqual(loaded["status"], "ok", loaded)
                old_pid = service._models["fixed-classifier"].pid
                os.kill(old_pid, signal.SIGSTOP)
                active.start()
                until = time.monotonic() + 2
                while time.monotonic() < until:
                    health = client_request(path, {"id": "waiting", "op": "health"}, timeout=1)
                    if health["inflight"] == 1:
                        break
                    time.sleep(.01)
                self.assertEqual(health["inflight"], 1, health)
                cancelled = client_request(path, {"id": "cancel-request", "op": "cancel", "target_id": "cancel-active"})
                self.assertEqual(cancelled["reason"], "cancel_requested", cancelled)
                active.join(2)
                self.assertFalse(active.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(response.get("status"), "cancelled", response)
                self.assertEqual(response["safe_action"], "no_action")
                with self.assertRaises(ProcessLookupError):
                    os.kill(old_pid, 0)
                until = time.monotonic() + 15
                while time.monotonic() < until:
                    state = client_request(path, {"id": "recovery", "op": "health"})["models"]["fixed-classifier"]
                    if state["alive"] and not state["recovering"]:
                        break
                    time.sleep(.05)
                self.assertTrue(state["alive"], state)
                self.assertFalse(state["recovering"], state)
                final = client_request(path, {"id": "after-cancel", "op": "infer", "model": "fixed-classifier",
                    "input": [[2.0, 0.0, -1.0, 0.0]], "deadline_ms": 3000}, timeout=10)
                self.assertEqual(final["status"], "success", final)
            finally:
                service.close()
                if active.ident is not None:
                    active.join(3)
                server.shutdown()
                server.server_close()
                serving.join(3)

    @unittest.skipUnless(os.environ.get("XTOS_TEST_AI_PYTHON"), "explicit locked AI Python not selected")
    def test_real_uds_load_infer_and_shutdown(self):
        manifest = json.loads(self.envelope)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sock"
            service = self.make_service()
            server = UnixInferenceServer(path, service)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 2
                while not path.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                loaded = client_request(path, {"id": "uds-load", "op": "load", "manifest": manifest}, timeout=120)
                self.assertEqual(loaded["status"], "ok", loaded)
                result = client_request(path, {
                    "id": "uds-infer", "op": "infer", "model": "fixed-classifier",
                    "input": [[2.0, 0.0, -1.0, 0.0]], "deadline_ms": 3000,
                }, timeout=10)
                self.assertEqual(result["safe_action"], "action_allowed", result)
                self.assertLessEqual(abs(result["output"][0][0] - 0.8437947034835815), 1e-5)
            finally:
                server.shutdown()
                server.server_close()
                service.close()
                thread.join(timeout=3)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
