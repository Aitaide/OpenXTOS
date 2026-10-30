"""Actual C adapter + P2 SDK + window, mocked only at syscall transport boundary."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
CASES=('golden','malformed','continuity','freshness','empty_disabled','sdk_read',
       'rebind','source_mask_capacity','invalid_atomic','bad_quality')


class EegStreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work=tempfile.TemporaryDirectory(prefix='xtos-eeg-stream-',dir=os.environ.get('TMPDIR','/root/.hermes/cache/scratch'))
        cls.addClassCleanup(cls.work.cleanup)
        cls.binary=Path(cls.work.name)/'stream-test'
        mode=os.environ.get('XTOS_EEG_STREAM_SANITIZER','')
        if mode not in ('','address,undefined'):
            raise RuntimeError('Unsupported explicit EEG stream sanitizer')
        flags=['-O1','-g','-fno-omit-frame-pointer','-fno-pie','-no-pie','-fsanitize='+mode] if mode else ['-O2']
        command=['cc','-std=c11','-Wall','-Wextra','-Werror',*flags,
            '-I'+str(ROOT/'tools/xtos-eeg-stream'),
            '-I'+str(ROOT/'sdk/libxtos-bci/include'),'-I'+str(ROOT/'sdk/libxtos-signal/include'),
            str(ROOT/'tools/xtos-eeg-stream/tests/test_eeg_stream.c'),
            str(ROOT/'tools/xtos-eeg-stream/eeg_stream.c'),
            str(ROOT/'sdk/libxtos-bci/src/session.c'),
            str(ROOT/'sdk/libxtos-signal/src/eeg_window.c'),'-lm',
            '-Wl,--wrap=open','-Wl,--wrap=close','-Wl,--wrap=ioctl','-o',str(cls.binary)]
        result=subprocess.run(command,capture_output=True,text=True,timeout=40)
        if result.returncode:
            raise RuntimeError('C EEG stream fixture compile failed: '+result.stdout+result.stderr)

    def run_case(self,name):
        result=subprocess.run([str(self.binary),name],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


def _case(name):
    def test(self):
        self.run_case(name)
    test.__name__='test_'+name
    return test

for _name in CASES:
    setattr(EegStreamTests,'test_'+_name,_case(_name))

if __name__=='__main__':unittest.main(verbosity=2)
