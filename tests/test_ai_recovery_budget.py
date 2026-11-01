"""Controlled worker scheduler evidence only; no GPU inference claims."""
import json
from dataclasses import replace
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools/xtos-ai-runtime'))
from xtos_ai_runtime.server import InferenceService
from xtos_ai_runtime.protocol import safe_result
from ai_service_trust_fixtures import FixtureTrustPolicy, fixture_ticket


class BudgetWorker:
    def __init__(self, fail_restart=False):
        self.alive = False
        self.pid = os.getpid()
        self.calls = 0
        self.restarts = 0
        self.fail_restart = fail_restart

    def start(self, cancel=None):
        self.alive = True
        return {'status': 'ready', 'runtime_version': 'fixture-only'}

    def infer(self, request_id, value, deadline_ms, cancel=None):
        self.calls += 1
        self.alive = False
        return safe_result(request_id, 'unavailable', 'worker_exited')

    def restart(self, cancel=None):
        self.restarts += 1
        if self.fail_restart:
            raise RuntimeError('fixture_restart_failed')
        self.alive = True
        return {'status': 'ready'}

    def close(self):
        self.alive = False


class RecoveryBudgetTests(unittest.TestCase):
    def setUp(self):
        self.service = InferenceService(ROOT, trust_policy=FixtureTrustPolicy())
        self.worker = BudgetWorker()
        self.service._models['controlled'] = self.worker
        self.service._worker_tickets[self.worker] = fixture_ticket(name='controlled')
        self.addCleanup(self.service.close)

    def infer(self, number=0, **extra):
        return self.service.dispatch(dict(id='request-' + str(number), op='infer',
                                          model='controlled', input=[[0]], **extra))

    def settle(self):
        with self.service._condition:
            self.assertTrue(self.service._condition.wait_for(
                lambda: not self.service._recovering and not self.service._recovery_threads, 2))

    def health(self):
        return self.service.dispatch({'id': 'health', 'op': 'health'})['models']['controlled']

    def test_two_reservations_then_exhausted_including_dead_finally_path(self):
        for number in range(4):
            result = self.infer(number)
            self.settle()
        self.assertEqual(self.worker.restarts, 2)
        self.assertEqual(result['reason'], 'recovery_budget_exhausted')
        self.assertEqual(result['safe_action'], 'no_action')
        health = self.health()
        self.assertEqual((health['recovery_limit'], health['recovery_used'],
                          health['recovery_remaining']), (2, 2, 0))
        self.assertEqual(health['recovery_last_reason'], 'recovery_budget_exhausted')
        self.assertIn('diagnostics', health)
        self.assertIn('pid', health)


    def test_failed_restarts_consume_budget_and_preserve_diagnostics(self):
        self.worker.fail_restart = True
        for number in range(4):
            result = self.infer(number)
            self.settle()
        self.assertEqual(self.worker.restarts, 2)
        self.assertEqual(result['reason'], 'recovery_budget_exhausted')
        self.assertEqual(self.health()['diagnostics'], 'fixture_restart_failed')
        self.assertEqual(self.health()['recovery_used'], 2)

    def test_atomic_reservation_during_stalled_recovery(self):
        entered, release = threading.Event(), threading.Event()
        original_restart = self.worker.restart
        def stalled(cancel=None):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('fixture barrier timeout')
            return original_restart(cancel)
        self.worker.restart = stalled
        self.addCleanup(release.set)
        try:
            self.infer(0)
            self.assertTrue(entered.wait(1))
            barrier = threading.Barrier(9)
            results, errors = [], []
            def competing(number):
                try:
                    barrier.wait(1)
                    results.append(self.infer(number))
                except BaseException as exc:
                    errors.append(exc)
            threads = [threading.Thread(target=competing, args=(n,)) for n in range(1, 9)]
            for thread in threads:
                thread.start()
            barrier.wait(1)
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 8)
            self.assertEqual(self.health()['recovery_used'], 1)
            self.assertEqual(self.worker.calls, 1)
        finally:
            release.set()
            self.settle()
        self.infer(10)
        self.settle()
        self.infer(11)
        self.settle()
        self.assertEqual(self.worker.restarts, 2)

    def test_reload_new_generation_resets_budget_but_initial_load_is_free(self):
        for number in range(3):
            self.infer(number)
            self.settle()
        manifest = json.loads((ROOT / 'tools/xtos-ai-runtime/model-manifest.example.json').read_text())
        manifest['name'] = 'controlled'
        candidate = BudgetWorker()
        self.service._worker_factory = lambda ticket: candidate
        result = self.service.dispatch(dict(id='reload', op='reload', manifest=manifest))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(self.health()['recovery_used'], 0)
        self.assertEqual(self.health()['recovery_remaining'], 2)
        self.assertIsNone(self.health()['recovery_last_reason'])
        for number in range(3, 6):
            self.infer(number)
            self.settle()
        self.assertEqual(candidate.restarts, 2)
        self.assertEqual(self.worker.restarts, 2)

    def test_shutdown_and_revocation_do_not_start_recovery(self):
        self.service._worker_tickets[self.worker].deny_event.set()
        result = self.infer()
        self.settle()
        self.assertEqual(result['safe_action'], 'no_action')
        self.assertEqual(self.worker.restarts, 0)
        self.service.close()
        result = self.infer(1)
        self.assertEqual(result['reason'], 'service_closed')
        self.assertEqual(self.worker.restarts, 0)

    def test_queue_timeout_and_queue_full_do_not_reserve(self):
        with self.service._condition:
            self.service._active_id = 'fixture-active'
        try:
            result = self.infer(0, deadline_ms=1)
            self.assertEqual(result['reason'], 'timeout')
            with self.service._condition:
                self.service._queued_id = 'fixture-queued'
            result = self.infer(1)
            self.assertEqual(result['reason'], 'queue_full')
            self.assertEqual(self.health()['recovery_used'], 0)
            self.assertEqual(self.worker.restarts, 0)
        finally:
            with self.service._condition:
                self.service._active_id = self.service._queued_id = None
                self.service._condition.notify_all()

    def test_provider_pin_validation_and_mismatch_before_worker(self):
        for bad in ('ROCMExecutionProvider', '', None, True, [], {}, 3):
            with self.subTest(bad=bad):
                result = self.infer(expected_provider=bad)
                self.assertEqual(result['reason'], 'invalid_provider')
        for provider in ('CUDAExecutionProvider', 'MIGraphXExecutionProvider'):
            result = self.infer(expected_provider=provider)
            self.assertEqual((result['status'], result['reason'], result['safe_action']),
                             ('unavailable', 'provider_mismatch', 'no_action'))
        self.assertEqual(self.worker.calls, 0)
        self.assertEqual(self.worker.restarts, 0)
        result = self.infer(expected_provider='CPUExecutionProvider')
        self.assertEqual(result['reason'], 'worker_exited')
        self.settle()

    def test_unpinned_gpu_is_rejected_before_fixture_worker(self):
        ticket=self.service._worker_tickets[self.worker]
        self.service._worker_tickets[self.worker]=replace(ticket,
            manifest=replace(ticket.manifest,provider='CUDAExecutionProvider'))
        result=self.infer()
        self.assertEqual(result['reason'],'provider_mismatch',result)
        self.assertEqual(result['safe_action'],'no_action')
        self.assertEqual(self.worker.calls,0)
        self.assertEqual(self.worker.restarts,0)
        result=self.infer(1,expected_provider='CUDAExecutionProvider')
        self.assertEqual(result['reason'],'worker_exited',result)
        self.settle()

    def test_queued_revocation_then_real_close_clears_paired_maps(self):
        service=self.service
        original_wait=service._condition.wait
        wait_returned,release=threading.Event(),threading.Event()
        response,errors={},[]
        def held_wait(timeout=None):
            result=original_wait(timeout)
            if threading.current_thread().name=='recovery-queued':
                saved=service._condition._release_save()
                wait_returned.set()
                try:
                    if not release.wait(3):raise RuntimeError('queue release timeout')
                finally:service._condition._acquire_restore(saved)
            return result
        def queued():
            try:response.update(self.infer(77))
            except BaseException as exc:errors.append(exc)
        def close():
            try:service.close()
            except BaseException as exc:errors.append(exc)
        waiting=threading.Thread(target=queued,name='recovery-queued')
        closing=threading.Thread(target=close,name='recovery-close')
        try:
            with service._condition:service._active_id='fixture-active'
            with patch.object(service._condition,'wait',side_effect=held_wait):
                waiting.start()
                self.assertTrue(wait_returned.wait(1))
                service._deny_workers({self.worker:'model_revoked'})
                closing.start()
                with service._condition:
                    self.assertTrue(service._condition.wait_for(lambda:service._closed,1))
                release.set();waiting.join(2);closing.join(2)
            self.assertFalse(waiting.is_alive());self.assertFalse(closing.is_alive())
            self.assertEqual(errors,[])
            self.assertEqual(response['reason'],'service_closed',response)
            self.assertEqual(response['safe_action'],'no_action')
            self.assertIsNone(response['output'])
            self.assertEqual(service._inflight,{})
            self.assertEqual(service._inflight_tickets,{})
            self.assertEqual(service._request_denials,{})
            self.assertEqual(self.worker.restarts,0)
        finally:
            release.set()
            if waiting.ident is not None:waiting.join(3)
            if closing.ident is not None:closing.join(3)

    def test_pin_on_every_non_infer_op_is_invalid_request(self):
        for op in ('list', 'health', 'load', 'reload', 'unload', 'cancel', 'unknown'):
            with self.subTest(op=op):
                result = self.service.dispatch(dict(id='pin-op', op=op,
                    expected_provider='CPUExecutionProvider', model='controlled', target_id='x'))
                self.assertEqual(result['reason'], 'invalid_request')
        self.assertEqual(self.worker.calls, 0)


if __name__ == '__main__':
    unittest.main()
