"""Real signed CPU/UDS revocation and kill/reap lifecycle."""
import json
import os
from pathlib import Path
import signal
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "tools/xtos-ai-runtime"))
from xtos_ai_runtime.client import request
from xtos_ai_runtime.server import InferenceService, UnixInferenceServer
from ai_trust_fixtures import policy_claims, signed_envelope
from ai_service_trust_fixtures import make_real_signed_service_fixture


class SignedLifecycleTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("XTOS_TEST_AI_PYTHON"), "explicit locked AI Python not selected")
    def test_signed_policy_revocation_kills_stopped_cpu_worker_and_denies_active_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            fx, model_envelope = make_real_signed_service_fixture(Path(directory))
            path = Path(directory) / "signed.sock"
            service = InferenceService(fx.model_root,
                python_command=[os.environ["XTOS_TEST_AI_PYTHON"]], trust_policy=fx.policy)
            server = UnixInferenceServer(path, service)
            owner = threading.Thread(target=server.serve_forever, daemon=True)
            owner.start()
            active_result, queued_result, errors = {}, {}, []
            active = threading.Thread(target=lambda: active_result.update(request(path, {
                "id":"active-signed", "op":"infer", "model":"fixed-classifier",
                "input":[[2.0,0.0,-1.0,0.0]], "deadline_ms":20000}, timeout=25)))
            queued = threading.Thread(target=lambda: queued_result.update(request(path, {
                "id":"queued-signed", "op":"infer", "model":"fixed-classifier",
                "input":[[2.0,0.0,-1.0,0.0]], "deadline_ms":20000}, timeout=25)))
            try:
                loaded = request(path, {"id":"load-signed", "op":"load", "manifest":json.loads(model_envelope)}, timeout=120)
                self.assertEqual(loaded["status"], "ok", loaded)
                success = request(path, {"id":"success-signed", "op":"infer", "model":"fixed-classifier",
                    "input":[[2.0,0.0,-1.0,0.0]], "deadline_ms":3000}, timeout=10)
                self.assertEqual(success["status"], "success", success)
                self.assertEqual(success["provider"], "CPUExecutionProvider", success)
                self.assertEqual(success["safe_action"], "action_allowed", success)
                self.assertLessEqual(abs(success["output"][0][0] - .8437947034835815), 1e-5)
                worker = service._models["fixed-classifier"]
                pid = worker.pid
                os.kill(pid, signal.SIGSTOP)
                active.start()
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    health = request(path, {"id":"health-active", "op":"health"}, timeout=3)
                    if health["inflight"] == 1: break
                    time.sleep(.02)
                self.assertEqual(health["inflight"], 1, health)
                queued.start()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    health = request(path, {"id":"health-queued", "op":"health"}, timeout=3)
                    if health["queued"] == 1: break
                    time.sleep(.02)
                self.assertEqual(health["queued"], 1, health)
                revoked = policy_claims(fx.now, fx.publisher, sequence=2,
                                        revoked_models=[fx.model_bytes and service._models["fixed-classifier"].manifest.sha256])
                root_id = __import__("hashlib").sha256(fx.root_signer.public_raw).hexdigest()
                bundle = signed_envelope("xtos-ai-policy-v1", root_id, revoked, fx.root_signer,
                                         b"XTOS-AI-POLICY-v1\x00")
                stopped = service.refresh_trust(bundle)
                self.assertEqual(stopped["status"], "ok", stopped)
                active.join(8); queued.join(8)
                self.assertFalse(active.is_alive()); self.assertFalse(queued.is_alive())
                for result in (active_result, queued_result):
                    self.assertEqual(result.get("safe_action"), "no_action", result)
                    self.assertIsNone(result.get("output"), result)
                with self.assertRaises(ProcessLookupError): os.kill(pid, 0)
                self.assertNotIn("fixed-classifier", service._models)
                with self.assertRaises(Exception): worker.start()
                with self.assertRaises(Exception): worker.restart()
                denied = worker.infer("denied", [[2.0,0.0,-1.0,0.0]], 1000)
                self.assertEqual(denied["safe_action"], "no_action", denied)
                self.assertIsNone(denied["output"], denied)
            finally:
                if active.ident is not None: active.join(3)
                if queued.ident is not None: queued.join(3)
                try: service.close()
                finally:
                    server.shutdown(); server.server_close(); owner.join(3)


if __name__ == "__main__":
    unittest.main()
