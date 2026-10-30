"""Declared-contract fixtures only: no native/device/runtime acceptance."""
import dataclasses
import importlib
from pathlib import Path
import struct
import sys
from typing import Any
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))


def configuration() -> dict[str, Any]:
    return dict(schema='xtos.eeg.model-binding.v1', model='eeg-fixture',
                model_sha256='a' * 64, feature_mapping='xtos.eeg.mean-rms.v1',
                unit='microvolt', order='mean_then_rms', channels=8,
                sample_rate_hz=250, enabled_mask=255, window_frames=2,
                hop_frames=1, max_receive_age_ns=100, input_name='eeg_input',
                input_dtype='float32', input_shape=[1, 16])


def window() -> dict[str, Any]:
    return dict(schema='xtos.eeg.window.v1', feature_mapping='xtos.eeg.mean-rms.v1',
                unit='microvolt', order='mean_then_rms', channels=8, sample_rate_hz=250,
                enabled_mask=255, frame_count=2, window_frames=2, hop_frames=1,
                max_receive_age_ns=100, emitted_at_ns=180, source_flags=2,
                source_kind='synthetic', barrier_id=1, session_id=2,
                stream_generation=3, config_generation=4, window_id=5,
                first_sequence=0, last_sequence=1, first_host_rx_ns=100,
                last_host_rx_ns=150, values=[float(i) for i in range(8)] * 2,
                safe_action='no_action', physical_acceptance=False)


class FeatureBindingTests(unittest.TestCase):
    def setUp(self):
        self.api = importlib.import_module('xtos_eeg_loop.feature_binding')

    def prepare(self, raw=None, record=None, now: Any=200, request_id: Any='request-1', deadline: Any=3000):
        return self.api.prepare_inference(self.api.validate_binding(
            configuration() if raw is None else raw), window() if record is None else record,
            now, request_id, deadline)

    def test_valid_mapping_prepares_exact_digest_request(self):
        prepared = self.prepare()
        self.assertEqual(prepared.values, tuple(window()['values']))
        self.assertEqual(prepared.request_document(), {
            'id': 'request-1', 'op': 'infer', 'model': 'eeg-fixture',
            'expected_model_sha256': 'a' * 64, 'input': [window()['values']],
            'deadline_ms': 3000})
        for name in ('source_kind', 'barrier_id', 'session_id', 'stream_generation',
                     'config_generation', 'window_id', 'first_sequence', 'last_sequence',
                     'first_host_rx_ns', 'last_host_rx_ns', 'max_receive_age_ns'):
            self.assertEqual(getattr(prepared, name), window()[name])
        self.assertEqual(prepared.prepared_at_ns, 200)

    def test_binding_strict_schema_types_bounds(self):
        bad = [(name, None) for name in configuration()]
        bad += [('schema', 'other'), ('model', ''), ('model', 'a/b'), ('model', 'é'),
                ('model', 'x' * 129), ('model_sha256', 'A' * 64),
                ('model_sha256', 'a' * 63), ('feature_mapping', 'fixture-eeg-v1'),
                ('unit', 'nanovolt'), ('order', 'rms_then_mean'), ('channels', 7),
                ('sample_rate_hz', 249), ('enabled_mask', 0), ('enabled_mask', 256),
                ('window_frames', 1), ('window_frames', 4097), ('hop_frames', 0),
                ('hop_frames', 3), ('max_receive_age_ns', 0),
                ('max_receive_age_ns', 1 << 64), ('input_name', ''),
                ('input_name', 'é'), ('input_name', 'x' * 129),
                ('input_dtype', 'float64'), ('input_shape', [16]),
                ('input_shape', [1, 15]), ('input_shape', [True, 16]),
                ('input_shape', [1.0, 16]), ('input_shape', (1, 16))]
        for name in ('channels', 'sample_rate_hz', 'enabled_mask', 'window_frames',
                     'hop_frames', 'max_receive_age_ns'):
            bad += [(name, True), (name, float(configuration()[name]))]
        for name, value in bad:
            with self.subTest(name=name, value=value):
                with self.assertRaises(self.api.BindingError):
                    self.api.validate_binding(dict(configuration(), **{name: value}))
        for value in (None, [], dict(configuration(), extra=1)):
            with self.subTest(value=value), self.assertRaises(self.api.BindingError):
                self.api.validate_binding(value)
        for name in configuration():
            raw = configuration()
            del raw[name]
            with self.subTest(missing=name), self.assertRaises(self.api.BindingError):
                self.api.validate_binding(raw)
        for mask, frames, hop, age in ((1, 2, 1, 1), (255, 4096, 4096, (1 << 64)-1)):
            raw = dict(configuration(), enabled_mask=mask, window_frames=frames,
                       hop_frames=hop, max_receive_age_ns=age)
            self.assertEqual(self.api.validate_binding(raw).window_frames, frames)

    def test_record_schema_metadata_source_and_identity(self):
        cases = [('schema', 'other'), ('feature_mapping', 'fixture-eeg-v1'),
                 ('unit', 'nanovolt'), ('order', 'other'), ('channels', 7),
                 ('sample_rate_hz', 251), ('enabled_mask', 1), ('frame_count', 3),
                 ('window_frames', 3), ('hop_frames', 2), ('max_receive_age_ns', 99),
                 ('safe_action', 'action_allowed'), ('physical_acceptance', True),
                 ('physical_acceptance', 0), ('source_flags', 3), ('source_flags', 0),
                 ('source_kind', 'serdev'), ('source_kind', 'fixture')]
        for name, original in window().items():
            if type(original) is int:
                cases += [(name, True), (name, float(original)), (name, -1), (name, 1 << 64)]
            cases.append((name, None))
        for name in ('barrier_id', 'session_id', 'stream_generation', 'config_generation', 'window_id'):
            cases.append((name, 0))
        for name, value in cases:
            with self.subTest(name=name, value=value), self.assertRaises(self.api.BindingError):
                self.prepare(record=dict(window(), **{name: value}))
        for name in window():
            record = window()
            del record[name]
            with self.subTest(missing=name), self.assertRaises(self.api.BindingError):
                self.prepare(record=record)
        with self.assertRaises(self.api.BindingError):
            self.prepare(record=dict(window(), extra=1))
        for record in ([], 'window'):
            with self.assertRaises(self.api.BindingError):
                self.prepare(record=record)
        record = dict(window(), source_flags=1, source_kind='serdev', session_id=(1 << 64)-1)
        self.assertEqual(self.prepare(record=record).session_id, (1 << 64)-1)

    def test_whole_window_age_sequence_and_request_bounds(self):
        for changes in ({'first_host_rx_ns': 99}, {'first_host_rx_ns': 151},
                        {'last_host_rx_ns': 181}, {'emitted_at_ns': 201},
                        {'first_sequence': 2}, {'last_sequence': 2},
                        {'first_sequence': (1 << 64)-1, 'last_sequence': 0}):
            with self.subTest(changes=changes), self.assertRaises(self.api.BindingError):
                self.prepare(record=dict(window(), **changes))
        for now in (True, 200.0, -1, 1 << 64, 179, 201):
            with self.subTest(now=now), self.assertRaises(self.api.BindingError):
                self.prepare(now=now)
        for request_id in ('', 'é', 'x' * 129, 'a/b', 1, None):
            with self.subTest(request_id=request_id), self.assertRaises(self.api.BindingError):
                self.prepare(request_id=request_id)
        for deadline in (True, 1.0, 0, -1, 300001, None):
            with self.subTest(deadline=deadline), self.assertRaises(self.api.BindingError):
                self.prepare(deadline=deadline)
        equal = dict(window(), first_host_rx_ns=100, last_host_rx_ns=100, emitted_at_ns=200)
        self.assertEqual(self.prepare(record=equal, request_id='x'*128, deadline=300000).prepared_at_ns, 200)
        high = dict(window(), first_sequence=(1 << 64)-2, last_sequence=(1 << 64)-1)
        self.assertEqual(self.prepare(record=high, deadline=1).last_sequence, (1 << 64)-1)

    def test_values_float32_mask_and_rms(self):
        for value in (True, '1', None, float('nan'), float('inf'), -float('inf'), 3.5e38, 10**500):
            record = window()
            record['values'][0] = value
            with self.subTest(value=value), self.assertRaises(self.api.BindingError):
                self.prepare(record=record)
        for values in ([], [0]*15, [0]*17, tuple([0]*16)):
            with self.subTest(values=values), self.assertRaises(self.api.BindingError):
                self.prepare(record=dict(window(), values=values))
        for mean, rms in ((1.0, -1.0), (-2.0, 1.0), (2.0, 1.9), (1e-20, 0.0)):
            record = window()
            record['values'][0], record['values'][8] = mean, rms
            with self.subTest(mean=mean, rms=rms), self.assertRaises(self.api.BindingError):
                self.prepare(record=record)
        raw = dict(configuration(), enabled_mask=1)
        record = dict(window(), enabled_mask=1, values=[0]*16)
        record['values'][0] = record['values'][8] = 0.1
        expected = struct.unpack('!f', struct.pack('!f', 0.1))[0]
        prepared = self.prepare(raw=raw, record=record)
        self.assertEqual(prepared.values[0], expected)
        self.assertTrue(all(type(value) is float for value in prepared.values))
        for index in (1, 9):
            for value in (1, 1e-100):
                changed = dict(record, values=list(record['values']))
                changed['values'][index] = value
                with self.subTest(index=index, value=value), self.assertRaises(self.api.BindingError):
                    self.prepare(raw=raw, record=changed)
        record = window()
        record['values'][0], record['values'][8] = 1.0, 1.0-5e-7
        self.prepare(record=record)
        largest = struct.unpack('!f', bytes.fromhex('7f7fffff'))[0]
        self.prepare(record=dict(window(), values=[largest]*16))

    def test_prepare_revalidates_forged_binding(self):
        good = self.api.validate_binding(configuration())
        for binding in (None, configuration(), dataclasses.replace(good, enabled_mask=True),
                        dataclasses.replace(good, model_sha256='bad')):
            with self.subTest(binding=binding), self.assertRaises(self.api.BindingError):
                self.api.prepare_inference(binding, window(), 200, 'id', 1)

    def test_snapshots_are_owned_frozen_and_request_is_fresh(self):
        # Regression for copying/frozen behavior already green in the first slice.
        raw, record = configuration(), window()
        binding = self.api.validate_binding(raw)
        prepared = self.api.prepare_inference(binding, record, 200, 'id', 1)
        raw['model'] = 'changed'
        raw['input_shape'][1] = 4
        record['values'][0] = 999
        record['session_id'] = 999
        first = prepared.request_document()
        first['input'][0][0] = 999
        first['expected_model_sha256'] = 'bad'
        self.assertEqual(binding.input_shape, (1, 16))
        self.assertEqual(prepared.values[0], 0.0)
        self.assertEqual(prepared.session_id, 2)
        self.assertEqual(prepared.request_document()['input'][0][0], 0.0)
        self.assertEqual(prepared.request_document()['expected_model_sha256'], 'a'*64)
        for target, field, value in ((binding, 'model', 'bad'), (prepared, 'values', ())):
            with self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(target, field, value)

    def test_rms_consistency_is_checked_before_underflow(self):
        record = window()
        record['values'][0], record['values'][8] = 1e-100, 0.0
        with self.assertRaises(self.api.BindingError):
            self.prepare(record=record)

    def test_bounded_printable_ascii_tensor_names(self):
        for name in ('eeg/input:0', 'x'*128):
            self.assertEqual(self.api.validate_binding(dict(configuration(), input_name=name)).input_name, name)
        for name in ('a\n', '\x00', 'a b'):
            with self.subTest(name=name), self.assertRaises(self.api.BindingError):
                self.api.validate_binding(dict(configuration(), input_name=name))
    def test_tiny_negative_rms_is_rejected_before_underflow(self):
        record = window()
        record['values'][8] = -1e-100
        with self.assertRaises(self.api.BindingError):
            self.prepare(record=record)

    def test_forged_shape_requires_immutable_tuple(self):
        good = self.api.validate_binding(configuration())
        for shape in (None, 16, [1, 16]):
            with self.subTest(shape=shape), self.assertRaises(self.api.BindingError):
                self.api.prepare_inference(dataclasses.replace(good, input_shape=shape), window(), 200, 'id', 1)


if __name__ == '__main__':
    unittest.main()
