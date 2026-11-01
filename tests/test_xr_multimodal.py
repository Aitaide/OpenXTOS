"""Application simulation only: exact bounded immutable samples."""
from dataclasses import FrozenInstanceError
import importlib.util
from pathlib import Path
import sys
import unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools/xtos-xr'), str(ROOT / 'tools')]
if importlib.util.find_spec('xtos_xr'):
    import xtos_xr as xr
else:
    xr = None

class Clock:
    def __init__(self): self.now = 1000
    def __call__(self): return self.now

def sample(modality='eeg', **updates):
    d = dict(schema='xtos.multimodal.sample.v1', modality=modality,
             source_id=modality + '-fixture', source_kind='fixture_replay',
             clock_domain='openxr_time' if modality=='eye_gaze' else 'host_monotonic_ns', source_timestamp_ns=990,
             host_observed_ns=995, sequence=1, generation=1,
             quality=dict(valid=True, tracked=True, predicted=False), values=[1.25])
    d.update(updates)
    return d

def fixture_convert(domain,timestamp):
    """Explicit simulated point conversion, never a runtime/device calibration."""
    return dict(converted_host_ns=timestamp,uncertainty_ns=0,conversion_id='fixture-point')

class MultimodalTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(xr, 'MultimodalSession is not implemented')
        self.clock = Clock()
        self.session = xr.MultimodalSession(clock=self.clock, converter=fixture_convert,max_age_ns=100,
            max_skew_ns=10, max_uncertainty_ns=5, candidate_ttl_ns=50)
    def triple(self):
        for m in ('eeg', 'eye_gaze', 'emg'): self.session.ingest(sample(m))
        return self.session.join()
    def test_immutable_three_latest_join(self):
        data = sample()
        frozen = self.session.ingest(data)
        data['values'][0] = 999
        data['quality']['valid'] = False
        self.assertEqual(frozen.values, (1.25,))
        with self.assertRaises(FrozenInstanceError): frozen.sequence = 9
        self.session.ingest(sample('eye_gaze'))
        self.session.ingest(sample('emg'))
        candidate = self.session.join()
        self.assertEqual(len(candidate.samples), 3)
        self.assertEqual(candidate.expires_ns, 1050)
        self.assertEqual(candidate.samples[0].converted_host_ns, 990)
        with self.assertRaises(FrozenInstanceError): candidate.barrier = 99
        self.assertIs(self.session.join(), candidate)

    def test_exact_schema_and_float32_rejection(self):
        bad = [sample(extra=1), sample(sequence=True), sample(generation=0),
               sample(source_id='nonascii-眼'), sample(source_kind='other'),
               sample(values=[]), sample(values=[1.0] * 33), sample(values=[True]),
               sample(values=[float('nan')]), sample(values=[float('inf')]),
               sample(values=[3.5e38]), sample(values=[1e-50]),
               sample(quality=dict(valid=1, tracked=True, predicted=False)),
               sample(quality=dict(valid=True, tracked=True, predicted=False, extra=False)),
               sample(source_timestamp_ns=False), sample(host_observed_ns=0),
               sample(clock_domain='unknown'), sample(modality='other')]
        for data in bad:
            with self.subTest(data=data):
                with self.assertRaises(xr.XRError): self.session.ingest(data)
        for name in ('max_age_ns', 'max_skew_ns', 'max_uncertainty_ns', 'candidate_ttl_ns'):
            for value in (True, 0, 1.0, 2**63):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(xr.XRError):
                        xr.MultimodalSession(clock=self.clock, **{name: value})

    def test_conversion_freshness_and_barrier(self):
        unconverted=xr.MultimodalSession(clock=self.clock)
        with self.assertRaises(xr.XRError):
            unconverted.ingest(sample('eye_gaze', clock_domain='openxr_time'))
        calls = []
        def convert(domain, timestamp):
            calls.append((domain, timestamp))
            return dict(converted_host_ns=990, uncertainty_ns=5, conversion_id='fixture-map')
        session = xr.MultimodalSession(clock=self.clock, converter=convert,
            max_age_ns=100, max_skew_ns=10, max_uncertainty_ns=5, candidate_ttl_ns=50)
        for m in ('eeg', 'eye_gaze', 'emg'):
            session.ingest(sample(m, clock_domain='openxr_time'))
        self.assertEqual(len(calls), 3)
        candidate = session.join()
        self.assertEqual(candidate.samples[1].conversion_id, 'fixture-map')
        session.barrier('runtime fault')
        with self.assertRaises(xr.XRError): session.join()
        self.triple()
        self.clock.now = 1091
        with self.assertRaises(xr.XRError): self.session.join()
        self.clock.now = 1000
        with self.assertRaises(xr.XRError): self.session.ingest(sample())

    def test_converter_delay_rechecks_current_freshness(self):
        def convert(domain, timestamp):
            self.clock.now = 1101
            return dict(converted_host_ns=990, uncertainty_ns=0, conversion_id='delayed')
        session = xr.MultimodalSession(clock=self.clock, converter=convert, max_age_ns=100)
        with self.assertRaises(xr.XRError):
            session.ingest(sample('eye_gaze', clock_domain='openxr_time'))

    def test_age_skew_exact_boundaries(self):
        for age, skew, allowed in ((100, 10, True), (101, 10, False), (100, 11, False)):
            with self.subTest(age=age, skew=skew):
                session = xr.MultimodalSession(clock=self.clock,converter=fixture_convert,max_age_ns=100,max_skew_ns=10)
                def fill():
                    for m, t in zip(('eeg', 'eye_gaze', 'emg'), (1000-age, 1000-age+skew, 1000-age)):
                        session.ingest(sample(m, source_timestamp_ns=t))
                    return session.join()
                if allowed: self.assertEqual(len(fill().samples), 3)
                else:
                    with self.assertRaises(xr.XRError): fill()

    def test_continuity_quality_future_and_rebuild(self):
        for change in (dict(sequence=1), dict(sequence=3), dict(generation=0),
                       dict(source_timestamp_ns=989, sequence=2),
                       dict(host_observed_ns=1001, sequence=2),
                       dict(quality=dict(valid=False, tracked=True, predicted=False), sequence=2)):
            with self.subTest(change=change):
                session = xr.MultimodalSession(clock=self.clock,converter=fixture_convert)
                for m in ('eeg', 'eye_gaze', 'emg'): session.ingest(sample(m))
                old = session.join()
                with self.assertRaises(xr.XRError): session.ingest(sample(**change))
                with self.assertRaises(xr.XRError): session.approval_request(old)
                with self.assertRaises(xr.XRError): session.join()
        session = xr.MultimodalSession(clock=self.clock,converter=fixture_convert)
        for m in ('eeg', 'eye_gaze', 'emg'): session.ingest(sample(m))
        old = session.join()
        session.ingest(sample(source_id='replacement'))
        with self.assertRaises(xr.XRError): session.approval_request(old)
        for m in ('eye_gaze', 'emg'): session.ingest(sample(m, sequence=2))
        self.assertGreater(session.join().barrier, old.barrier)

    def test_malformed_conversion_outputs(self):
        for output in (None, {}, dict(converted_host_ns=990, uncertainty_ns=0, conversion_id=''),
                       dict(converted_host_ns=True, uncertainty_ns=0, conversion_id='x'),
                       dict(converted_host_ns=996, uncertainty_ns=0, conversion_id='x'),
                       dict(converted_host_ns=990, uncertainty_ns=6, conversion_id='x'),
                       dict(converted_host_ns=990, uncertainty_ns=False, conversion_id='x')):
            with self.subTest(output=output):
                session = xr.MultimodalSession(clock=self.clock, converter=lambda *args: output,
                                               max_uncertainty_ns=5)
                with self.assertRaises(xr.XRError):
                    session.ingest(sample('eye_gaze', clock_domain='openxr_time'))

    def test_schema_is_exact_builtin_string(self):
        class EqualSchema:
            def __eq__(self, other): return True
        with self.assertRaises(xr.XRError):
            self.session.ingest(sample(schema=EqualSchema()))

    def test_eye_gaze_requires_runtime_sample_domain_before_conversion(self):
        for domain in ('host_monotonic_ns','emg_device_time'):
            calls=[]
            session=xr.MultimodalSession(clock=self.clock,
                converter=lambda *args:(calls.append(args) or
                    dict(converted_host_ns=990,uncertainty_ns=0,conversion_id='probe')))
            with self.subTest(domain=domain):
                with self.assertRaises(xr.XRError):
                    session.ingest(sample('eye_gaze',clock_domain=domain))
                self.assertEqual(calls,[])
                with self.assertRaises(xr.XRError):session.join()
            session.close()

if __name__ == '__main__': unittest.main()
