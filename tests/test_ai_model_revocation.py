"""Deterministic model trust/revocation linearization regressions."""
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/xtos-ai-runtime"))
from xtos_ai_runtime.server import InferenceService
from xtos_ai_runtime.protocol import success_result
from ai_service_trust_fixtures import FixtureTrustPolicy

RAW = json.loads((ROOT / "tools/xtos-ai-runtime/model-manifest.example.json").read_text())

class EventWorker:
    def __init__(self, ticket):
        self.ticket = ticket
        self.alive = True
        self.pid = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()
        self.stage_entered = threading.Event()
        self.stage_release = threading.Event()
        self.block_stage = False
        self.block_recovery = False
        self.recovery_entered = threading.Event()
        self.recovery_release = threading.Event()
        self.close_failures = 0
    def start(self, cancel=None):
        if self.block_stage:
            self.stage_entered.set()
            self.stage_release.wait(3)
        return {"status": "ready", "runtime_version": "fixture-only"}
    def infer(self, request_id, value, deadline_ms, cancel=None):
        self.entered.set()
        self.release.wait(3)
        return success_result(request_id, self.ticket.manifest, [[.8, .1, .1]], 1.)
    def restart(self, cancel=None):
        self.recovery_entered.set()
        if self.block_recovery:
            self.recovery_release.wait(3)
        self.alive = True
        return {"status": "ready"}
    def close(self):
        if self.close_failures:
            self.close_failures -= 1
            raise OSError("injected stop failure")
        self.alive = False
        self.closed.set()

class RevocationTests(unittest.TestCase):
    def setUp(self):
        self.policy = FixtureTrustPolicy()
        self.created = []
        def factory(ticket):
            worker = EventWorker(ticket)
            self.created.append(worker)
            return worker
        self.service = InferenceService(ROOT, worker_factory=factory, trust_policy=self.policy)
        self.addCleanup(self.service.close)
    def load(self, raw=RAW):
        return self.service.dispatch({"id": "load", "op": "load", "manifest": raw})
    def infer(self, request_id):
        return self.service.dispatch({"id": request_id, "op": "infer", "model": RAW["name"],
                                      "input": [[1., 0., 0., 0.]], "deadline_ms": 5000})
    def revoke(self):
        return self.service.refresh_trust(RAW["sha256"])
    def test_idle_revocation_stops_without_another_inference(self):
        self.assertEqual(self.load()["status"], "ok")
        worker = self.created[0]
        self.policy.revoked.add(RAW["sha256"])
        self.assertTrue(worker.closed.wait(2))
        self.assertTrue(worker.ticket.deny_event.is_set())
        self.assertNotIn(RAW["name"], self.service._models)
    def test_revocation_before_result_commit_discards_active_and_queued_output(self):
        self.assertEqual(self.load()["status"], "ok")
        worker = self.created[0]
        results = {}
        active = threading.Thread(target=lambda: results.update(active=self.infer("active")))
        queued = threading.Thread(target=lambda: results.update(queued=self.infer("queued")))
        active.start()
        try:
            self.assertTrue(worker.entered.wait(1))
            queued.start()
            deadline = time.monotonic()+1
            with self.service._condition:
                while self.service._queued_id != "queued" and time.monotonic() < deadline:
                    self.service._condition.wait(.01)
            self.assertEqual(self.service._queued_id, "queued")
            self.assertEqual(self.revoke()["status"], "ok")
        finally:
            worker.release.set()
            active.join(2)
            queued.join(2)
        self.assertFalse(active.is_alive())
        self.assertFalse(queued.is_alive())
        for result in results.values():
            self.assertEqual(result["reason"], "model_revoked", result)
            self.assertEqual(result["safe_action"], "no_action")
            self.assertIsNone(result["output"])
        self.assertEqual(len(results), 2)
    def test_staging_ready_after_revocation_cannot_become_active(self):
        def factory(ticket):
            worker = EventWorker(ticket)
            worker.block_stage = True
            self.created.append(worker)
            return worker
        self.service._worker_factory = factory
        results = []
        stage = threading.Thread(target=lambda: results.append(self.load()))
        stage.start()
        try:
            deadline = time.monotonic()+1
            while not self.created and time.monotonic() < deadline:
                time.sleep(.001)
            worker = self.created[0]
            self.assertTrue(worker.stage_entered.wait(1))
            self.assertEqual(self.revoke()["status"], "ok")
        finally:
            self.created[0].stage_release.set()
            stage.join(2)
        self.assertFalse(stage.is_alive())
        self.assertEqual(results[0]["reason"], "model_revoked", results)
        self.assertNotIn(RAW["name"], self.service._models)
        self.assertTrue(worker.closed.is_set())
    def test_recovery_ready_after_revocation_is_closed_not_resurrected(self):
        self.load()
        worker = self.created[0]
        worker.alive = False
        worker.block_recovery = True
        worker.release.set()
        self.infer("crash")
        self.assertTrue(worker.recovery_entered.wait(1))
        results = []
        refresh = threading.Thread(target=lambda: results.append(self.revoke()))
        refresh.start()
        try:
            self.assertTrue(worker.ticket.deny_event.wait(1))
            worker.recovery_release.set()
            refresh.join(2)
            self.assertFalse(refresh.is_alive())
            self.assertEqual(results[0]["status"], "ok", results)
            self.assertTrue(worker.closed.wait(1))
            self.assertFalse(worker.alive)
            self.assertNotIn(RAW["name"], self.service._models)
        finally:
            worker.recovery_release.set()
            refresh.join(2)
    def test_stop_failure_is_visible_and_retained_for_bounded_retry(self):
        self.load()
        worker = self.created[0]
        worker.close_failures = 1
        with patch("xtos_ai_runtime.server.TRUST_WATCH_INTERVAL", 60):
            self.service._watch_stop.set()
            self.service._watch_thread.join(1)
            failed = self.revoke()
            self.assertEqual(failed["reason"], "worker_stop_failed", failed)
            self.assertTrue(worker.ticket.deny_event.is_set())
            health = self.service.dispatch({"id": "h", "op": "health"})
            self.assertEqual(health["trust"]["status"], "worker_stop_failed")
            self.assertEqual(health["trust"]["revoked_workers_pending"], 1)
            self.assertEqual(self.revoke()["status"], "ok")
            self.assertTrue(worker.closed.is_set())
    def test_unsigned_default_factory_never_executes(self):
        created = []
        class Forbidden:
            def start(self, cancel=None):
                created.append("executed")
                raise RuntimeError("unsigned execution reached worker")
            def close(self):
                pass
        service = InferenceService(ROOT, worker_factory=lambda ticket: Forbidden())
        try:
            result = service.dispatch({"id": "u", "op": "load", "manifest": RAW})
            self.assertEqual(result["reason"], "policy_unavailable", result)
            self.assertEqual(created, [])
            health = service.dispatch({"id": "h", "op": "health"})
            self.assertEqual(health["service_runtime_version"], "1.0.1")
            self.assertEqual(health["trust"]["mode"], "production")
        finally:
            service.close()
    def test_blocked_policy_tick_does_not_block_health(self):
        self.load()
        self.service._watch_stop.set()
        self.service._watch_thread.join(1)
        entered, release, health_done = threading.Event(), threading.Event(), threading.Event()
        def tick():
            entered.set()
            release.wait(3)
        self.policy.tick = tick
        refresh = threading.Thread(target=self.service.refresh_trust)
        health = threading.Thread(target=lambda: (self.service.dispatch({"id": "h", "op": "health"}), health_done.set()))
        refresh.start()
        try:
            self.assertTrue(entered.wait(1))
            health.start()
            self.assertTrue(health_done.wait(.2), "policy tick holds service condition")
        finally:
            release.set()
            refresh.join(2)
            if health.ident is not None:
                health.join(2)

    def test_blocked_ticket_check_does_not_block_health(self):
        self.load()
        self.service._watch_stop.set()
        self.service._watch_thread.join(1)
        entered, release, health_done = threading.Event(), threading.Event(), threading.Event()
        original = self.policy.check
        def blocked(ticket):
            entered.set()
            release.wait(3)
            return original(ticket)
        self.policy.check = blocked
        infer = threading.Thread(target=lambda: self.infer("blocked-check"))
        health = threading.Thread(target=lambda: (self.service.dispatch({"id":"h", "op":"health"}), health_done.set()))
        infer.start()
        try:
            self.assertTrue(entered.wait(1))
            health.start()
            self.assertTrue(health_done.wait(.2), "ticket check holds service condition")
        finally:
            release.set()
            infer.join(2)
            if health.ident is not None: health.join(2)

    def test_recovering_worker_refusal_does_not_start_another_recovery(self):
        self.load()
        worker = self.created[0]
        worker.alive = False
        cancel = threading.Event()
        self.service._recovering[worker] = cancel
        result = self.infer("already-recovering")
        self.assertEqual(result["reason"], "worker_recovering", result)
        self.assertEqual(self.service._recovery_threads.get(worker), None)
        self.assertIs(self.service._recovering.get(worker), cancel)
        self.service._recovering.pop(worker)

    def test_unexpected_watcher_fault_globally_denies_and_stops(self):
        self.load()
        worker = self.created[0]
        def broken_refresh():
            raise ValueError("injected unexpected watcher fault")
        self.service.refresh_trust = broken_refresh
        self.assertTrue(worker.ticket.deny_event.wait(1), "watcher fault did not deny ticket")
        self.assertTrue(worker.closed.wait(1), "watcher fault did not stop worker")
        self.service._watch_thread.join(1)
        self.assertFalse(self.service._watch_thread.is_alive())

    def test_global_watcher_denial_refuses_staging_before_factory_or_start(self):
        entered_authorize = threading.Event()
        release_authorize = threading.Event()
        original_authorize = self.policy.authorize
        factory_calls = []
        start_calls = []

        def blocked_authorize(raw, model_root):
            entered_authorize.set()
            if not release_authorize.wait(2):
                raise AssertionError("authorize fixture was not released")
            return original_authorize(raw, model_root)

        class ForbiddenAfterGlobalDeny:
            def start(self, cancel=None):
                start_calls.append(True)
                return {"status": "ready", "runtime_version": "fixture-only"}
            def close(self):
                pass

        self.policy.authorize = blocked_authorize
        self.service._worker_factory = lambda ticket: (factory_calls.append(ticket), ForbiddenAfterGlobalDeny())[1]
        self.service.refresh_trust = lambda: (_ for _ in ()).throw(
            ValueError("injected fatal watcher failure"))
        results = []
        stage = threading.Thread(target=lambda: results.append(self.load()))
        stage.start()
        try:
            self.assertTrue(entered_authorize.wait(1), "staging did not reach trust authorization")
            self.service._watch_thread.join(2)
            self.assertFalse(self.service._watch_thread.is_alive(), "fatal watcher failure did not settle")
            self.assertEqual(self.service._trust_failure, "policy_unavailable")
        finally:
            release_authorize.set()
            stage.join(2)
        self.assertFalse(stage.is_alive(), "staging did not return after global refusal")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["reason"], "policy_unavailable", results[0])
        self.assertEqual(factory_calls, [], "global refusal reached the worker factory")
        self.assertEqual(start_calls, [], "global refusal started a child worker")

    def test_concurrent_refresh_close_owner_returns_bounded_then_retries(self):
        self.load()
        worker = self.created[0]
        self.service._watch_stop.set()
        self.service._watch_thread.join(1)
        self.assertFalse(self.service._watch_thread.is_alive())
        close_entered, release_close, close_finished = (
            threading.Event(), threading.Event(), threading.Event())
        close_calls = []

        def stalled_close():
            close_calls.append(threading.current_thread())
            close_entered.set()
            if not release_close.wait(2):
                raise AssertionError("close owner was not released")
            worker.alive = False
            worker.closed.set()
            close_finished.set()

        worker.close = stalled_close
        self.policy.revoked.add(RAW["sha256"])
        results, errors = {}, []

        def refresh(name):
            try:
                results[name] = self.service.refresh_trust()
            except BaseException as exc:
                errors.append(exc)

        owner = threading.Thread(target=refresh, args=("owner",), daemon=True)
        contender = threading.Thread(target=refresh, args=("contender",), daemon=True)
        try:
            with patch("xtos_ai_runtime.server.SHUTDOWN_TIMEOUT", .1):
                owner.start()
                self.assertTrue(close_entered.wait(1), "refresh did not acquire worker close ownership")
                contender.start()
                owner.join(.5)
                contender.join(.5)
                self.assertFalse(owner.is_alive(), "close owner caller exceeded its bounded wait")
                self.assertFalse(contender.is_alive(), "close contender exceeded its bounded wait")
                self.assertEqual(errors, [])
                self.assertEqual(len(close_calls), 1, "contender invoked close concurrently")
                for name in ("owner", "contender"):
                    self.assertEqual(results[name].get("reason"), "worker_stop_failed", results[name])
                close_state = self.service._close_states[worker]
                self.assertFalse(close_state.done.is_set())
        finally:
            release_close.set()
            if owner.ident is not None:
                owner.join(2)
            if contender.ident is not None:
                contender.join(2)
        self.assertTrue(close_finished.wait(1), "shared close owner did not finish cleanup")
        self.assertTrue(close_state.done.wait(1))
        self.assertNotIn(worker, self.service._close_states)
        close_owner_deadline = time.monotonic() + 1
        while time.monotonic() < close_owner_deadline:
            if not any(thread.name == "xtos-ai-worker-close" and thread.is_alive()
                       for thread in threading.enumerate()):
                break
            time.sleep(.005)
        self.assertFalse(any(thread.name == "xtos-ai-worker-close" and thread.is_alive()
                             for thread in threading.enumerate()), "worker close owner thread leaked")
        self.assertFalse(worker.alive)
        self.assertEqual(len(close_calls), 1)
        retried = self.service.refresh_trust()
        self.assertEqual(retried["status"], "ok", retried)
        self.assertNotIn(worker, self.service._retired)
        self.assertNotIn(worker, self.service._shutdown_close_errors)

    def test_trust_update_is_not_a_socket_operation(self):
        result = self.service.dispatch({"id": "write", "op": "refresh_trust", "bundle": RAW["sha256"]})
        self.assertEqual(result["reason"], "unknown_operation")
