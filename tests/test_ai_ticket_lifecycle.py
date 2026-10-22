"""Bounded ticket ownership under controlled scheduler interleavings."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/xtos-ai-runtime"))
from xtos_ai_runtime.server import InferenceService
from ai_service_trust_fixtures import FixtureTrustPolicy
from test_ai_model_revocation import EventWorker
from ai_trust_fixtures import make_fixture, model_claims, signed_envelope
from unittest.mock import patch

RAW = json.loads((ROOT / "tools/xtos-ai-runtime/model-manifest.example.json").read_text())


class TicketLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.policy = FixtureTrustPolicy()
        self.created = []
        def factory(ticket):
            worker = EventWorker(ticket)
            self.created.append(worker)
            return worker
        self.service = InferenceService(ROOT, worker_factory=factory, trust_policy=self.policy)
        self.service._stop_trust_watcher()
        self.addCleanup(self.service.close)

    def load(self):
        return self.service.dispatch({"id": "load", "op": "reload", "manifest": RAW})

    def infer(self, request_id, deadline_ms=5000):
        return self.service.dispatch({"id": request_id, "op": "infer", "model": RAW["name"],
                                      "input": [[1., 0., 0., 0.]], "deadline_ms": deadline_ms})

    def test_queued_timeouts_remove_ticket_entries_while_active_is_blocked(self):
        self.assertEqual(self.load()["status"], "ok")
        worker = self.created[0]
        results = []
        active = threading.Thread(target=lambda: results.append(self.infer("active")))
        active.start()
        try:
            self.assertTrue(worker.entered.wait(1))
            for index in range(50):
                result = self.infer(f"expired-{index}", 1)
                self.assertEqual(result["status"], "timeout", result)
                self.assertEqual(result["safe_action"], "no_action")
            with self.service._condition:
                self.assertEqual(set(self.service._inflight), {"active"})
                self.assertEqual(set(self.service._inflight_tickets), {"active"})
                self.assertIsNone(self.service._queued_id)
            self.assertFalse(worker.ticket.deny_event.is_set())
        finally:
            worker.release.set()
            active.join(2)
        self.assertFalse(active.is_alive())
        self.assertEqual(results[0]["status"], "success", results)
        self.assertEqual(self.service._inflight_tickets, {})

    def test_reload_retires_worker_maps_without_closing_current_worker(self):
        for _ in range(50):
            self.assertEqual(self.load()["status"], "ok")
        current = self.created[-1]
        self.assertEqual(set(self.service._worker_tickets), {current})
        self.assertEqual(self.service._close_states, {})
        self.assertEqual(self.service._retired, set())
        self.assertTrue(current.alive)
        self.assertFalse(current.ticket.deny_event.is_set())
        self.assertTrue(all(worker.closed.is_set() for worker in self.created[:-1]))
        self.service.close()
        self.assertEqual(self.service._worker_tickets, {})
        self.assertEqual(self.service._close_states, {})

    def test_queue_full_rejections_remove_ticket_entries(self):
        self.assertEqual(self.load()["status"], "ok")
        worker = self.created[0]
        results = []
        active = threading.Thread(target=lambda: results.append(self.infer("active")))
        queued = threading.Thread(target=lambda: results.append(self.infer("queued")))
        active.start()
        try:
            self.assertTrue(worker.entered.wait(1))
            queued.start()
            deadline = time.monotonic() + 1
            with self.service._condition:
                while self.service._queued_id != "queued" and time.monotonic() < deadline:
                    self.service._condition.wait(.01)
                self.assertEqual(self.service._queued_id, "queued")
            for index in range(50):
                self.assertEqual(self.infer(f"full-{index}")["reason"], "queue_full")
            with self.service._condition:
                self.assertEqual(set(self.service._inflight_tickets), {"active", "queued"})
        finally:
            worker.release.set()
            active.join(2)
            if queued.ident is not None:
                queued.join(2)
        self.assertFalse(active.is_alive())
        self.assertFalse(queued.is_alive())
        self.assertEqual(self.service._inflight_tickets, {})


    def test_queued_model_unavailable_removes_ticket_entry(self):
        self.assertEqual(self.load()["status"], "ok")
        worker = self.created[0]
        results = {}
        active = threading.Thread(target=lambda: results.update(active=self.infer("active")))
        queued = threading.Thread(target=lambda: results.update(queued=self.infer("queued")))
        active.start()
        try:
            self.assertTrue(worker.entered.wait(1))
            queued.start()
            deadline = time.monotonic() + 1
            with self.service._condition:
                while self.service._queued_id != "queued" and time.monotonic() < deadline:
                    self.service._condition.wait(.01)
                self.assertEqual(self.service._queued_id, "queued")
            self.assertEqual(self.service.dispatch(
                {"id": "unload", "op": "unload", "model": RAW["name"]})["status"], "ok")
        finally:
            worker.release.set()
            active.join(2)
            if queued.ident is not None:
                queued.join(2)
        self.assertFalse(active.is_alive())
        self.assertFalse(queued.is_alive())
        self.assertEqual(results["queued"]["reason"], "model_unavailable", results)
        self.assertEqual(self.service._inflight, {})
        self.assertEqual(self.service._inflight_tickets, {})


    def test_shutdown_removes_queued_ticket_entry(self):
        self.assertEqual(self.load()["status"], "ok")
        worker = self.created[0]
        results = {}
        active = threading.Thread(target=lambda: results.update(active=self.infer("active")))
        queued = threading.Thread(target=lambda: results.update(queued=self.infer("queued")))
        active.start()
        try:
            self.assertTrue(worker.entered.wait(1))
            queued.start()
            deadline = time.monotonic() + 1
            with self.service._condition:
                while self.service._queued_id != "queued" and time.monotonic() < deadline:
                    self.service._condition.wait(.01)
                self.assertEqual(self.service._queued_id, "queued")
                self.service._closed = True
                self.service._condition.notify_all()
            queued.join(1)
            self.assertFalse(queued.is_alive())
            self.assertEqual(results["queued"]["reason"], "service_closed")
            with self.service._condition:
                self.assertEqual(set(self.service._inflight_tickets), {"active"})
        finally:
            with self.service._condition:
                self.service._closed = False
            worker.release.set()
            active.join(2)
            if queued.ident is not None:
                queued.join(2)
        self.assertFalse(active.is_alive())


    def _promotion_window_cancel_attempt(self, index):
        """One fresh-service interleaving attempt for the promotion-window cancel.

        While the service condition is held, the active request's worker is
        released (so its post-infer path blocks on the condition) and the
        queued request's event is set without notify -- exactly what cancel /
        worker denial do. The active request's finally then clears _active_id
        and notifies, so the queued thread exits its wait loop through the
        loop condition and meets the post-loop event check.
        """
        policy = FixtureTrustPolicy()
        created = []
        def factory(ticket):
            worker = EventWorker(ticket)
            created.append(worker)
            return worker
        service = InferenceService(ROOT, worker_factory=factory, trust_policy=policy)
        service._stop_trust_watcher()
        results = {}
        try:
            load = service.dispatch({"id": "load", "op": "reload", "manifest": RAW})
            assert load["status"] == "ok", load
            worker = created[0]
            def infer(rid):
                results[rid] = service.dispatch(
                    {"id": rid, "op": "infer", "model": RAW["name"],
                     "input": [[1., 0., 0., 0.]], "deadline_ms": 5000})
            active = threading.Thread(target=infer, args=("active",))
            active.start()
            assert worker.entered.wait(1), "active infer never entered the worker"
            queued = threading.Thread(target=infer, args=("queued",))
            queued.start()
            deadline = time.monotonic() + 1
            with service._condition:
                while service._queued_id != "queued" and time.monotonic() < deadline:
                    service._condition.wait(.01)
                assert service._queued_id == "queued", "request never queued"
                worker.release.set()
                time.sleep(.0025)
                service._inflight["queued"].set()
            active.join(2)
            queued.join(2)
            assert not active.is_alive() and not queued.is_alive(), "dispatch threads stuck"
            detail = {
                "attempt": index,
                "queued_status": results["queued"].get("status"),
                "active_status": results["active"].get("status"),
                "inflight_clean": "queued" not in service._inflight,
            }
            leaked = service._inflight_tickets.get("queued") is not None
            detail["inflight_tickets_retains"] = leaked
            return leaked, detail
        finally:
            try:
                service.close()
            except Exception:
                pass

    def test_promotion_window_cancel_removes_ticket_entry(self):
        leaks = []
        for index in range(20):
            leaked, detail = self._promotion_window_cancel_attempt(index)
            if leaked:
                leaks.append(detail)
                break
        self.assertEqual(leaks, [], f"queued cancel leaked ticket entries: {leaks}")

    def test_promotion_without_worker_ticket_removes_ticket_entry(self):
        self.assertEqual(self.load()["status"], "ok")
        worker = self.created[0]
        results = {}
        active = threading.Thread(target=lambda: results.update(active=self.infer("active")))
        queued = threading.Thread(target=lambda: results.update(queued=self.infer("queued")))
        active.start()
        try:
            self.assertTrue(worker.entered.wait(1))
            queued.start()
            deadline = time.monotonic() + 1
            with self.service._condition:
                while self.service._queued_id != "queued" and time.monotonic() < deadline:
                    self.service._condition.wait(.01)
                self.assertEqual(self.service._queued_id, "queued")
                # the queued entry recorded the worker's ticket at queue time
                self.assertIn("queued", self.service._inflight_tickets)
                worker.release.set()
                time.sleep(.0025)
                # simulate the stale window: the ticket disappears while the
                # request is still queued, so promotion finds no ticket
                self.service._worker_tickets.pop(worker, None)
        finally:
            worker.release.set()
            active.join(2)
            if queued.ident is not None:
                queued.join(2)
        self.assertFalse(active.is_alive())
        self.assertFalse(queued.is_alive())
        self.assertEqual(results["queued"]["status"], "unavailable", results)
        self.assertEqual(results["queued"]["reason"], "policy_unavailable", results)
        self.assertNotIn("queued", self.service._inflight)
        self.assertEqual(self.service._inflight_tickets, {})


class PolicyTicketLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xtos-ticket-lifecycle-")
        self.addCleanup(self.temp.cleanup)
        self.fx = make_fixture(Path(self.temp.name) / "fixture")
        self.created = []
        def factory(ticket):
            worker = EventWorker(ticket)
            self.created.append(worker)
            return worker
        self.service = InferenceService(self.fx.model_root, worker_factory=factory,
                                        trust_policy=self.fx.policy)
        self.service._stop_trust_watcher()
        self.addCleanup(self.service.close)
        self.envelope = signed_envelope(
            "xtos-ai-model-v1", hashlib.sha256(self.fx.publisher.public_raw).hexdigest(),
            model_claims(self.fx.now, "model.onnx", self.fx.model_bytes), self.fx.publisher,
            b"XTOS-AI-MODEL-v1\x00")

    def load(self):
        return self.service.dispatch({"id": "load", "op": "reload", "manifest": self.envelope})

    def test_fifty_reloads_keep_only_current_policy_ticket(self):
        for _ in range(50):
            self.assertEqual(self.load()["status"], "ok")
        current = self.created[-1]
        self.assertEqual(list(self.fx.policy._authorized.values()), [current.ticket])
        self.assertEqual(set(self.service._worker_tickets), {current})
        self.assertEqual(self.service._close_states, {})
        self.assertEqual(self.service._retirement_states, {})
        self.fx.policy.check(current.ticket)
        self.assertTrue(all(worker.ticket.deny_event.is_set() for worker in self.created[:-1]))
        self.service.close()
        self.assertEqual(self.fx.policy._authorized, {})
        self.assertEqual(self.fx.policy._deny_reasons, {})
        self.assertEqual(self.service._worker_tickets, {})

    def test_active_replacement_keeps_old_ticket_until_dispatch_finishes(self):
        self.assertEqual(self.load()["status"], "ok")
        old = self.created[0]
        results = []
        active = threading.Thread(target=lambda: results.append(self.service.dispatch(
            {"id": "active", "op": "infer", "model": old.ticket.manifest.name,
             "input": [[1., 0., 0., 0.]], "deadline_ms": 5000})))
        active.start()
        try:
            self.assertTrue(old.entered.wait(1))
            self.assertEqual(self.load()["status"], "ok")
            current = self.created[-1]
            self.assertEqual(set(self.fx.policy._authorized.values()), {old.ticket, current.ticket})
            self.fx.policy.check(old.ticket)
            self.assertFalse(old.closed.is_set())
        finally:
            old.release.set()
            active.join(2)
        self.assertFalse(active.is_alive())
        self.assertEqual(results[0]["status"], "success", results)
        self.assertEqual(list(self.fx.policy._authorized.values()), [current.ticket])
        self.assertTrue(old.closed.is_set())
        self.fx.policy.check(current.ticket)

    def test_registry_refusal_retires_new_ticket_without_reaching_factory(self):
        for index in range(4):
            envelope = signed_envelope(
                "xtos-ai-model-v1", hashlib.sha256(self.fx.publisher.public_raw).hexdigest(),
                model_claims(self.fx.now, "model.onnx", self.fx.model_bytes, name=f"model-{index}"),
                self.fx.publisher, b"XTOS-AI-MODEL-v1\x00")
            self.assertEqual(self.service.dispatch(
                {"id": "load", "op": "load", "manifest": envelope})["status"], "ok")
        self.assertEqual(self.load()["reason"], "model_registry_full")
        self.assertEqual(len(self.created), 4)
        self.assertEqual(len(self.fx.policy._authorized), 4)
        for worker in self.created:
            self.fx.policy.check(worker.ticket)

    def test_staging_denial_retires_ticket_after_owner_exits(self):
        entered, release = threading.Event(), threading.Event()
        check = self.fx.policy.check
        def blocked_check(ticket):
            check(ticket)
            if threading.current_thread().name == "stage-owner":
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("stage fixture was not released")
        self.fx.policy.check = blocked_check
        results = []
        stage = threading.Thread(target=lambda: results.append(self.load()), name="stage-owner")
        # Block the post-start service check, not authorize's internal check.
        def factory(ticket):
            worker = EventWorker(ticket)
            self.created.append(worker)
            self.fx.policy.check = blocked_check
            return worker
        self.fx.policy.check = check
        self.service._worker_factory = factory
        stage.start()
        try:
            self.assertTrue(entered.wait(1))
            worker = self.created[0]
            self.service._deny_workers({worker: "model_revoked"})
            self.assertIsNone(self.service._safe_close(worker))
            self.assertIn(id(worker.ticket), self.fx.policy._authorized)
        finally:
            release.set()
            stage.join(2)
        self.assertFalse(stage.is_alive())
        self.assertEqual(results[0]["reason"], "model_revoked", results)
        self.assertEqual(self.fx.policy._authorized, {})
        self.assertEqual(self.service._worker_tickets, {})

    def test_late_close_completion_retires_ticket_without_another_close_call(self):
        self.assertEqual(self.load()["status"], "ok")
        worker = self.created[0]
        self.service._models.clear()
        entered, release, retired = threading.Event(), threading.Event(), threading.Event()
        retire = self.fx.policy.retire
        def record_retirement(ticket):
            retire(ticket)
            retired.set()
        self.fx.policy.retire = record_retirement
        def close():
            entered.set()
            if not release.wait(2):
                raise RuntimeError("close fixture was not released")
            worker.alive = False
        worker.close = close
        try:
            with patch("xtos_ai_runtime.server.SHUTDOWN_TIMEOUT", .05):
                self.assertIsInstance(self.service._safe_close(worker), TimeoutError)
                self.assertTrue(entered.is_set())
            release.set()
            self.assertTrue(retired.wait(1))
            self.assertEqual(self.fx.policy._authorized, {})
        finally:
            release.set()
            self.service._safe_close(worker)

    def test_failed_worker_start_retires_policy_ticket(self):
        def factory(ticket):
            worker = EventWorker(ticket)
            def fail_start(cancel=None):
                raise RuntimeError("injected staged start failure")
            worker.start = fail_start
            self.created.append(worker)
            return worker
        self.service._worker_factory = factory
        for _ in range(50):
            self.assertEqual(self.load()["reason"], "model_load_failed")
        self.assertEqual(self.fx.policy._authorized, {})
        self.assertEqual(self.fx.policy._deny_reasons, {})
        self.assertEqual(self.service._worker_tickets, {})
        self.assertTrue(all(worker.closed.is_set() for worker in self.created))

    def test_unexpected_staging_exception_closes_worker_and_retires_ticket(self):
        def factory(ticket):
            worker = EventWorker(ticket)
            def fail_start(cancel=None):
                raise TypeError("injected unexpected staging failure")
            worker.start = fail_start
            self.created.append(worker)
            return worker
        self.service._worker_factory = factory
        with self.assertRaisesRegex(TypeError, "unexpected staging failure"):
            self.load()
        self.assertEqual(self.service._staging, {})
        self.assertEqual(self.fx.policy._authorized, {})
        self.assertEqual(self.service._worker_tickets, {})
        self.assertTrue(self.created[0].closed.is_set())

    def test_retirement_failure_is_retained_for_shutdown_retry(self):
        self.assertEqual(self.load()["status"], "ok")
        worker = self.created[0]
        self.service._models.clear()
        retire = self.fx.policy.retire
        calls = []
        def fail_retire(ticket):
            calls.append(ticket)
            raise OSError("injected retirement failure")
        self.fx.policy.retire = fail_retire
        result = self.service._safe_close(worker)
        self.assertIsInstance(result, OSError)
        self.assertIn(worker, self.service._retired)
        self.assertIn(worker, self.service._worker_tickets)
        self.fx.policy.retire = retire
        self.service.close()
        self.assertEqual(self.fx.policy._authorized, {})
        self.assertEqual(self.service._worker_tickets, {})
        self.assertEqual(self.service._retirement_states, {})
        self.assertEqual(self.service._retired, set())

    def test_failed_factory_retires_only_new_ticket(self):
        self.assertEqual(self.load()["status"], "ok")
        current = self.created[0]
        def fail_factory(ticket):
            raise RuntimeError("injected factory failure")
        self.service._worker_factory = fail_factory
        for _ in range(50):
            self.assertEqual(self.load()["reason"], "model_load_failed")
        self.assertEqual(list(self.fx.policy._authorized.values()), [current.ticket])
        self.fx.policy.check(current.ticket)
        self.assertTrue(current.alive)
        self.assertFalse(current.ticket.deny_event.is_set())

    def test_blocked_retirement_keeps_single_bounded_close_owner(self):
        self.assertEqual(self.load()["status"], "ok")
        worker = self.created[0]
        self.service._models.clear()
        entered, release = threading.Event(), threading.Event()
        retire = self.fx.policy.retire
        calls, results, errors = [], [], []
        def blocked_retire(ticket):
            self.assertFalse(self.service._condition._is_owned())
            calls.append(ticket)
            entered.set()
            if not release.wait(2):
                raise RuntimeError("retirement fixture was not released")
            retire(ticket)
        self.fx.policy.retire = blocked_retire
        def close():
            try:
                results.append(self.service._safe_close(worker))
            except BaseException as error:
                errors.append(error)
        first = threading.Thread(target=close)
        second = threading.Thread(target=close)
        try:
            with patch("xtos_ai_runtime.server.SHUTDOWN_TIMEOUT", .05):
                first.start()
                self.assertTrue(entered.wait(1))
                second.start()
                first.join(.3)
                second.join(.3)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
                self.assertEqual(len(calls), 1)
                self.assertEqual(len(results), 2)
                self.assertTrue(all(isinstance(result, TimeoutError) for result in results))
        finally:
            release.set()
            first.join(2)
            if second.ident is not None:
                second.join(2)
        self.assertEqual(errors, [])
        self.assertIsNone(self.service._safe_close(worker))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.fx.policy._authorized, {})
