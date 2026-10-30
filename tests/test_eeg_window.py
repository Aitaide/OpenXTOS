"""Portable C streaming-window behavior cases; every build owns a fresh directory."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
CASES=('golden','boundaries','quality','freshness','rolling','bounds','invalid','duplicate','oldest_stale')

class EegWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work=tempfile.TemporaryDirectory(prefix='xtos-eeg-window-',dir=os.environ.get('TMPDIR','/root/.hermes/cache/scratch'))
        cls.addClassCleanup(cls.work.cleanup)
        cls.binary=Path(cls.work.name)/'window-test'
        mode=os.environ.get('XTOS_EEG_WINDOW_SANITIZER','')
        if mode not in ('','address,undefined'):
            raise RuntimeError('Unsupported explicit EEG window sanitizer')
        flags=['-O1','-g','-fno-omit-frame-pointer','-fno-pie','-no-pie','-fsanitize='+mode] if mode else ['-O2']
        command=['cc','-std=c11','-Wall','-Wextra','-Werror',*flags,
                 '-I'+str(ROOT/'sdk/libxtos-signal/include'),
                 str(ROOT/'sdk/libxtos-signal/tests/test_eeg_window.c'),
                 str(ROOT/'sdk/libxtos-signal/src/eeg_window.c'),'-lm','-o',str(cls.binary)]
        result=subprocess.run(command,capture_output=True,text=True,timeout=40)
        if result.returncode:
            raise RuntimeError('C window fixture compile failed: '+result.stdout+result.stderr)

    def run_case(self,name):
        result=subprocess.run([str(self.binary),name],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


def _case(name):
    def test(self):
        self.run_case(name)
    test.__name__='test_'+name
    return test

for _name in CASES:
    setattr(EegWindowTests,'test_'+_name,_case(_name))

if __name__=='__main__':
    unittest.main(verbosity=2)
