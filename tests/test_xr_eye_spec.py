"""Security regressions, distinct from the immutable BUG-detecting probes."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import test_xr_eye_session as eye_tests
SRC = eye_tests.SRC

class EyeSpecTests(unittest.TestCase):
    out: Path
    env: dict
    build = classmethod(eye_tests.EyeSessionTests.build.__func__)
    @classmethod
    def setUpClass(cls):
        # Reuse build helpers without inheriting the original twelve test methods.
        eye_tests.EyeSessionTests.setUpClass.__func__(cls)
        flags = ['cc','-std=c11','-Wall','-Wextra','-Werror','-g','-D_POSIX_C_SOURCE=200809L','-DXR_NO_PROTOTYPES','-DXR_USE_TIMESPEC']
        if os.environ.get('XTOS_XR_SANITIZE') == '1':
            flags += ['-fsanitize=address,undefined','-fno-sanitize-recover=all','-fno-omit-frame-pointer','-no-pie']
        cls.build([*flags,'-I'+str(SRC/'include'),'-I'+str(SRC/'vendor/openxr/include'),str(SRC/'src/runtime_adapter.c'),str(SRC/'src/eye_session.c'),str(SRC/'tests/native/test_eye_spec.c'),'-ldl','-lm','-o',str(cls.out/'spec-test')])
    def case(self, name):
        p = subprocess.run([str(self.out/'spec-test'),name,str(self.out/'simulated.so')],env=self.env,capture_output=True,text=True)
        (self.out/(name+'-spec.log')).write_text(p.stdout+p.stderr+'\nexit='+str(p.returncode)+'\n')
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
    def test_event_uncertainty_staleness_and_counter_fences(self): self.case('events')
    def test_each_child_cleanup_retry_restores_only_child_failure(self): self.case('cleanup')
    def test_terminal_loss_dispatch_and_live_parent_reopen(self): self.case('loss')
    def test_runtime_pose_time_contract(self):
        text = (SRC/'include/xtos_xr/eye_session.h').read_text()
        self.assertNotIn('Capture derives only from sampleTimeEXT', text)
        for token in ('predicted','interpolated','clamped','not raw sensor capture time','query_host_ns,query_xr_time,sample_xr_time,sample_host_ns'):
            self.assertIn(token,text)

if __name__ == '__main__': unittest.main()
