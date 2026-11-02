"""Compile the actual OpenXR ABI fixture and exercise opt-in headless gaze."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'tools/xtos-xr'

class EyeSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = Path(os.environ.get('XTOS_XR_TEST_OUTPUT', ROOT/'.build/remaining-closure-v2/xr-eye-implement/tests'))
        base.mkdir(parents=True,exist_ok=True)
        cls.out = Path(tempfile.mkdtemp(prefix='eye-',dir=base))
        cls.env = dict(os.environ,LC_ALL='C')
        flags = ['-std=c11','-Wall','-Wextra','-Werror','-g','-D_POSIX_C_SOURCE=200809L','-DXR_NO_PROTOTYPES','-DXR_USE_TIMESPEC']
        if os.environ.get('XTOS_XR_SANITIZE')=='1':
            flags += ['-fsanitize=address,undefined','-fno-sanitize-recover=all','-fno-omit-frame-pointer','-no-pie']
            cls.env.update(ASAN_OPTIONS='detect_leaks=1:halt_on_error=1',UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1')
        inc=['-I'+str(SRC/'include'),'-I'+str(SRC/'vendor/openxr/include')]
        fixture=SRC/'tests/native/fixture_eye_runtime.c'
        cls.build(['cc',*flags,*inc,'-shared','-fPIC',str(fixture),'-o',str(cls.out/'simulated.so')])
        sources=[SRC/'src/runtime_adapter.c']
        if (SRC/'src/eye_session.c').exists(): sources.append(SRC/'src/eye_session.c')
        cls.build(['cc',*flags,*inc,*(str(s) for s in sources),str(SRC/'tests/native/test_eye_session.c'),'-ldl','-lm','-o',str(cls.out/'eye-test')])
    @classmethod
    def build(cls,cmd):
        p=subprocess.run(cmd,capture_output=True,text=True,env=cls.env)
        with (cls.out/'build.log').open('a') as f: f.write(json.dumps(cmd)+'\n'+p.stdout+p.stderr+'\nexit='+str(p.returncode)+'\n')
        if p.returncode: raise AssertionError(p.stderr)
    def case(self,name):
        p=subprocess.run([str(self.out/'eye-test'),name,str(self.out/'simulated.so')],capture_output=True,text=True,env=self.env)
        (self.out/(name+'.log')).write_text(p.stdout+p.stderr+'\nexit='+str(p.returncode)+'\n')
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
    def test_headless_eye_requires_extensions_before_create(self): self.case('extensions')
    def test_real_headless_session_action_spaces_cleanup(self): self.case('create')
    def test_failed_open_without_children_is_retryable(self): self.case('open_retry')
    def test_failed_session_destroy_keeps_parent_and_retries(self): self.case('close_retry')
    def test_hmd_unavailable_refuses_session(self): self.case('missing_hmd')
    def test_ready_focused_stopping_dispatch(self): self.case('events')
    def test_wrong_session_event_fences_sample(self): self.case('wrong_session')
    def test_session_loss_destroys_without_end(self): self.case('loss')
    def test_running_session_requests_exit(self): self.case('request_exit')
    def test_invalid_pose_and_timestamp_never_publish(self): self.case('invalid_samples')
    def test_missing_dispatch_or_eye_support_unavailable(self): self.case('unsupported')
    def runtime_pose_time_is_distinct_from_query(self): self.case('sample')
    def test_sample_capture_time_is_distinct_from_query(self):
        """Legacy test-ID alias; runtime pose time does not imply raw capture."""
        self.runtime_pose_time_is_distinct_from_query()

if __name__=='__main__': unittest.main()
