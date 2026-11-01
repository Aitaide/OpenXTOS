"""Application fence for native probe records; never grants device authority."""
import sys
from pathlib import Path
import unittest
import subprocess
import tempfile
import json
import os
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'tools/xtos-xr'),
              str(Path(__file__).resolve().parents[1]/'tools')]
from xtos_xr import MultimodalSession, XRError

class RuntimeFenceTests(unittest.TestCase):
    def session(self):
        session=MultimodalSession(clock=lambda:10000,converter=lambda domain,t:
            dict(converted_host_ns=t,uncertainty_ns=0,conversion_id='fixture-point'))
        for modality in ('eeg','eye_gaze','emg'):
            session.ingest(dict(schema='xtos.multimodal.sample.v1',modality=modality,
                source_id=modality,source_kind='simulated_runtime',
                clock_domain='openxr_time' if modality=='eye_gaze' else 'host_monotonic_ns',source_timestamp_ns=10000,
                host_observed_ns=10000,sequence=1,generation=1,
                quality=dict(valid=True,tracked=True,predicted=False),values=[0.0]))
        return session,session.join()

    def test_native_event_revokes_pending_multimodal_candidate(self):
        from xtos_xr.runtime_events import RuntimeFence
        session,candidate=self.session();fence=RuntimeFence(session)
        record=dict(source_kind='simulated_runtime',safe_action='no_action',
            physical_acceptance=False,generation=1,events=[dict(type=49,sequence=1,barrier=1,lost_count=2)],
            event_limit_reached=False,barrier=1,status='ok')
        self.assertEqual(fence.accept(record),'barrier')
        with self.assertRaises(XRError):session.approval_request(candidate)
        self.assertEqual(fence.accept(record),'rejected')
        session.close()

    def test_native_fault_or_malformed_record_never_preserves_candidate(self):
        from xtos_xr.runtime_events import RuntimeFence
        for record in ({},dict(status='unavailable',safe_action='no_action',physical_acceptance=False),
                       dict(status='ok',safe_action='action_allowed',physical_acceptance=True)):
            with self.subTest(record=record):
                session,candidate=self.session();fence=RuntimeFence(session)
                self.assertEqual(fence.accept(record),'rejected')
                with self.assertRaises(XRError):session.approval_request(candidate)
                session.close()

    def test_present_null_conversion_revokes_candidate(self):
        from xtos_xr.runtime_events import RuntimeFence
        base=dict(source_kind='simulated_runtime',safe_action='no_action',
            physical_acceptance=False,generation=1,events=[],
            event_limit_reached=False,barrier=0,status='ok')
        valid=dict(raw_xr_time=10000,host_ns=10000,runtime_result=0,status=0)
        cases=[('omitted',dict(base),'current'),
               ('valid',dict(base,conversion=valid),'current'),
               ('null',dict(base,conversion=None),'rejected'),
               ('malformed',dict(base,conversion={}),'rejected')]
        for label,record,expected in cases:
            with self.subTest(conversion=label):
                session,candidate=self.session()
                try:
                    record=json.loads(json.dumps(record))
                    self.assertEqual(RuntimeFence(session).accept(record),expected)
                    if expected=='rejected':
                        with self.assertRaises(XRError):session.approval_request(candidate)
                    else:
                        session.approval_request(candidate)
                finally:
                    session.close()

    def test_compiled_native_event_limit_revokes_pending_candidate(self):
        from xtos_xr.runtime_events import RuntimeFence
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='xr-native-fence-') as directory:
            output=Path(directory)
            subprocess.run(['make','-C',str(root/'tools/xtos-xr'),
                'BUILD_DIR='+str(output),'all'],capture_output=True,check=True)
            fixture=output/'fixture.so'
            subprocess.run(['cc','-shared','-fPIC','-DXR_NO_PROTOTYPES','-DXR_USE_TIMESPEC',
                '-I'+str(root/'tools/xtos-xr/vendor/openxr/include'),
                str(root/'tools/xtos-xr/tests/native/fixture_runtime.c'),
                '-o',str(fixture)],capture_output=True,check=True)
            run=subprocess.run([str(output/'xr-probe'),'--loader',str(fixture),'--simulated'],
                env=dict(os.environ,XTOS_XR_FIXTURE_MODE='11'),capture_output=True,text=True,timeout=10)
            self.assertEqual(run.returncode,0,run.stderr)
            report=json.loads(run.stdout)
            self.assertEqual(len(report['events']),256)
            session,candidate=self.session()
            self.assertEqual(RuntimeFence(session).accept(report),'barrier')
            with self.assertRaises(XRError):session.approval_request(candidate)
            session.close()

if __name__=='__main__':unittest.main()
