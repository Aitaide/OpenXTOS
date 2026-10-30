"""Pure lifecycle evidence; synthetic streams grant no action authority."""
import dataclasses
import importlib
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from test_eeg_feature_binding import configuration, window
from xtos_eeg_loop.feature_binding import validate_binding


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.api = importlib.import_module('xtos_eeg_loop.lifecycle')
        self.fence = self.api.StreamFence(validate_binding(configuration()))

    def reserve(self, record=None, now=200, deadline=1, request='request-1'):
        return self.fence.reserve(window() if record is None else record, now, request, deadline)

    def test_exact_lease_is_one_use_and_always_no_action(self):
        lease = self.reserve()
        result = self.fence.complete(lease, 200)
        self.assertEqual(result.status, 'window_current')
        self.assertEqual(result.safe_action, 'no_action')
        self.assertIs(result.prepared, lease.prepared)
        self.assertEqual(self.fence.complete(lease, 200).status, 'rejected')
        self.assertEqual(self.fence.complete(lease, 200).safe_action, 'no_action')

    def test_busy_preserves_original_and_does_not_admit_replacement(self):
        original = self.reserve()
        later = dict(window(), window_id=6, first_sequence=1, last_sequence=2)
        with self.assertRaisesRegex(self.api.LifecycleError, 'busy'):
            self.reserve(later)
        self.assertEqual(self.fence.complete(original, 200).status, 'window_current')
        replacement = self.reserve(later)
        forged = dataclasses.replace(replacement)
        self.assertEqual(self.fence.complete(forged, 200).status, 'rejected')
        self.assertEqual(self.fence.complete(original, 200).status, 'rejected')
        self.assertEqual(self.fence.complete(replacement, 200).status, 'window_current')

    def test_replay_and_hop_fences_require_new_barrier(self):
        lease = self.reserve()
        self.fence.complete(lease, 200)
        with self.assertRaisesRegex(self.api.LifecycleError, 'replay'):
            self.reserve()
        with self.assertRaises(self.api.LifecycleError):
            self.reserve(dict(window(), window_id=6, first_sequence=1, last_sequence=2))
        # Invalid continuity cannot be resurrected at the same barrier.
        accepted = self.reserve(dict(window(), barrier_id=2, window_id=6,
                                     first_sequence=10, last_sequence=11))
        self.assertGreater(accepted.revision, lease.revision)
        self.fence.complete(accepted, 200)
        with self.assertRaisesRegex(self.api.LifecycleError, 'hop'):
            self.reserve(dict(window(), barrier_id=2, window_id=7,
                              first_sequence=12, last_sequence=13))
        with self.assertRaises(self.api.LifecycleError):
            self.reserve(dict(window(), barrier_id=2, window_id=7,
                              first_sequence=11, last_sequence=12))

    def test_malformed_reservation_revokes_pending_and_preserves_replay(self):
        old = self.reserve()
        for record, request in ((dict(window(), values=[]), 'ok'), (window(), 'bad/id')):
            with self.assertRaises(self.api.LifecycleError):
                self.reserve(record, request=request)
        self.assertEqual(self.fence.complete(old, 200).status, 'rejected')
        with self.assertRaises(self.api.LifecycleError):
            self.reserve(dict(window(), window_id=6, first_sequence=1, last_sequence=2))
        with self.assertRaisesRegex(self.api.LifecycleError, 'replay'):
            self.reserve(dict(window(), barrier_id=2))
        replacement = self.reserve(dict(window(), barrier_id=2, window_id=6))
        self.assertEqual(self.fence.complete(replacement, 200).status, 'window_current')

    def test_thread_invalidation_replacement_cannot_be_consumed_by_old_lease(self):
        old = self.reserve()
        start, done = threading.Event(), threading.Event()
        errors = []
        def handler():
            if not start.wait(2):
                errors.append('start timeout')
                return
            try:
                self.fence.invalidate(2, 2, 'gap')
            except Exception as exc:
                errors.append(repr(exc))
            finally:
                done.set()
        thread = threading.Thread(target=handler)
        thread.start()
        start.set()
        self.assertTrue(done.wait(2))
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        replacement = self.reserve(dict(window(), barrier_id=3, window_id=6))
        self.assertEqual(self.fence.complete(old, 200).status, 'rejected')
        self.assertEqual(self.fence.complete(replacement, 200).status, 'window_current')

    def test_complete_checks_oldest_age_monotonic_time_and_strict_deadline(self):
        for now, expected in ((200, 'window_current'), (201, 'rejected'),
                              (199, 'rejected'), (True, 'rejected'),
                              (200.0, 'rejected'), (1 << 64, 'rejected')):
            with self.subTest(now=now):
                self.fence = self.api.StreamFence(validate_binding(configuration()))
                lease = self.reserve()
                result = self.fence.complete(lease, now)
                self.assertEqual(result.status, expected)
                self.assertEqual(result.safe_action, 'no_action')
                self.assertEqual(self.fence.complete(lease, 200).status, 'rejected')
        raw = dict(configuration(), max_receive_age_ns=10_000_000)
        for elapsed, expected in ((999_999, 'window_current'), (1_000_000, 'rejected')):
            with self.subTest(elapsed=elapsed):
                self.fence = self.api.StreamFence(validate_binding(raw))
                lease = self.reserve(dict(window(), max_receive_age_ns=10_000_000))
                self.assertEqual(self.fence.complete(lease, 200 + elapsed).status, expected)

    def test_close_from_handler_is_terminal_and_idempotent(self):
        old = self.reserve()
        done = threading.Event()
        errors = []
        def handler():
            try:
                self.fence.close('shutdown')
                self.fence.close('duplicate')
            except Exception as exc:
                errors.append(repr(exc))
            finally:
                done.set()
        thread = threading.Thread(target=handler)
        thread.start()
        self.assertTrue(done.wait(2))
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.fence.complete(old, 200).status, 'rejected')
        self.fence.invalidate(3, 1, 'rebind')
        with self.assertRaisesRegex(self.api.LifecycleError, 'closed'):
            self.reserve(dict(window(), session_id=4))

    def test_revision_exhaustion_is_terminal_without_wrap(self):
        lease = self.reserve()
        self.fence._revision = (1 << 64) - 1
        with self.assertRaisesRegex(self.api.LifecycleError, 'exhausted'):
            self.fence.invalidate(2, 2, 'gap')
        self.assertEqual(self.fence._revision, (1 << 64) - 1)
        self.assertEqual(self.fence.complete(lease, 200).status, 'rejected')
        with self.assertRaisesRegex(self.api.LifecycleError, 'exhausted'):
            self.reserve(dict(window(), session_id=3))

    # Added-green probes below are regressions, not claimed behavioral RED.
    def test_duplicate_events_idempotent_and_stale_events_preserve_healthy_lease(self):
        self.fence.invalidate(2, 2, 'gap')
        revision = self.fence._revision
        self.fence.invalidate(2, 2, 'duplicate')
        self.assertEqual(self.fence._revision, revision)
        with self.assertRaises(self.api.LifecycleError):
            self.reserve(dict(window(), barrier_id=2, window_id=6))
        lease = self.reserve(dict(window(), barrier_id=3, window_id=6))
        for session, barrier in ((1, 99), (2, 2)):
            with self.subTest(session=session, barrier=barrier):
                with self.assertRaises(self.api.LifecycleError):
                    self.fence.invalidate(session, barrier, 'late')
        self.assertEqual(self.fence.complete(lease, 200).status, 'window_current')

    def test_malformed_events_revoke_without_resetting_window_fence(self):
        for session, barrier, reason in ((True, 2, 'gap'), (2.0, 2, 'gap'),
                (0, 2, 'gap'), (1 << 64, 2, 'gap'), (2, False, 'gap'),
                (2, 0, 'gap'), (2, 1 << 64, 'gap'), (2, 2, ''),
                (2, 2, 'x'*129), (2, 2, 'é'), (2, 2, None)):
            with self.subTest(session=session, barrier=barrier, reason=reason):
                self.fence = self.api.StreamFence(validate_binding(configuration()))
                old = self.reserve()
                with self.assertRaises(self.api.LifecycleError):
                    self.fence.invalidate(session, barrier, reason)
                self.assertEqual(self.fence.complete(old, 200).status, 'rejected')
                with self.assertRaisesRegex(self.api.LifecycleError, 'replay'):
                    self.reserve(dict(window(), barrier_id=2))
                lease = self.reserve(dict(window(), barrier_id=2, window_id=6))
                self.assertEqual(self.fence.complete(lease, 200).status, 'window_current')

    def test_generation_changes_need_barrier_and_new_session_resets_only_session_fence(self):
        for generation in ('stream_generation', 'config_generation'):
            with self.subTest(generation=generation):
                self.fence = self.api.StreamFence(validate_binding(configuration()))
                self.fence.complete(self.reserve(), 200)
                changed = dict(window(), window_id=6, first_sequence=1, last_sequence=2)
                changed[generation] += 1
                with self.assertRaisesRegex(self.api.LifecycleError, 'generation'):
                    self.reserve(changed)
                lease = self.reserve(dict(changed, barrier_id=2))
                self.fence.complete(lease, 200)
                newer = self.reserve(dict(window(), session_id=3, barrier_id=1, window_id=1))
                self.fence.complete(newer, 200)
                with self.assertRaises(self.api.LifecycleError):
                    self.reserve(dict(window(), session_id=2, barrier_id=99, window_id=99))

    def test_overlap_forward_backward_and_window_id_boundaries(self):
        for first in (0, 2):
            with self.subTest(first=first):
                self.fence = self.api.StreamFence(validate_binding(configuration()))
                self.fence.complete(self.reserve(), 200)
                with self.assertRaisesRegex(self.api.LifecycleError, 'hop'):
                    self.reserve(dict(window(), window_id=6, first_sequence=first, last_sequence=first+1))
        self.fence = self.api.StreamFence(validate_binding(configuration()))
        lease = self.reserve(dict(window(), window_id=(1 << 64)-1,
                                  first_sequence=(1 << 64)-2, last_sequence=(1 << 64)-1))
        self.fence.complete(lease, 200)
        with self.assertRaises(self.api.LifecycleError):
            self.reserve(dict(window(), barrier_id=2, window_id=1))
        lease = self.reserve(dict(window(), session_id=3, window_id=1))
        self.assertEqual(self.fence.complete(lease, 200).status, 'window_current')

    def test_snapshots_and_results_are_frozen_owned_no_action(self):
        record = window()
        lease = self.reserve(record)
        record['values'][0] = 999
        record['session_id'] = 999
        self.assertEqual(lease.prepared.values[0], 0.0)
        result = self.fence.complete(lease, 200)
        self.assertEqual(result.prepared.source_kind, 'synthetic')
        for target, field, value in ((lease, 'revision', 0),
                (lease.prepared, 'values', ()), (result, 'safe_action', 'allowed')):
            with self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(target, field, value)
        self.assertIsNone(self.fence.complete(lease, 200).prepared)

    def test_two_reserve_handlers_admit_at_most_one(self):
        barrier = threading.Barrier(3)
        admitted, denied, errors = [], [], []
        def handler():
            try:
                barrier.wait(2)
                admitted.append(self.reserve())
            except self.api.LifecycleError as exc:
                denied.append(str(exc))
            except Exception as exc:
                errors.append(repr(exc))
        threads = [threading.Thread(target=handler) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(2)
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(admitted), 1)
        self.assertEqual(denied, ['busy'])
        self.assertEqual(self.fence.complete(admitted[0], 200).status, 'window_current')

    def test_constructor_revalidates_binding_and_malformed_close_stays_terminal(self):
        good = validate_binding(configuration())
        for binding in (None, configuration(), dataclasses.replace(good, hop_frames=0),
                        dataclasses.replace(good, input_shape=[1, 16])):
            with self.subTest(binding=binding), self.assertRaises(self.api.LifecycleError):
                self.api.StreamFence(binding)
        old = self.reserve()
        with self.assertRaises(self.api.LifecycleError):
            self.fence.close('')
        self.assertEqual(self.fence.complete(old, 200).status, 'rejected')
        with self.assertRaises(self.api.LifecycleError):
            self.reserve()

    def test_generation_protocol_fault_revokes_active_and_new_barrier_replaces_it(self):
        old = self.reserve()
        with self.assertRaisesRegex(self.api.LifecycleError, 'generation'):
            self.reserve(dict(window(), window_id=6, stream_generation=4,
                              first_sequence=1, last_sequence=2))
        self.assertEqual(self.fence.complete(old, 200).status, 'rejected')
        replacement = self.reserve(dict(window(), window_id=6, barrier_id=2))
        latest = self.reserve(dict(window(), window_id=7, barrier_id=3))
        self.assertEqual(self.fence.complete(replacement, 200).status, 'rejected')
        self.assertEqual(self.fence.complete(latest, 200).status, 'window_current')


if __name__ == '__main__':
    unittest.main()
