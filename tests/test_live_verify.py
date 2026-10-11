#!/usr/bin/env python3
"""Tests for the Live verifier's bounded, file-only entrypoints."""
import os,subprocess,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'scripts/verify_live.py'

class LiveVerifyTests(unittest.TestCase):
    def test_missing_candidate_does_not_create_disks_or_evidence(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            evidence=Path(tmp)/'evidence'
            p=subprocess.run([sys.executable,str(SCRIPT),'--candidate',str(Path(tmp)/'missing'),'--key',str(Path(tmp)/'missing-key'),'--evidence',str(evidence),'--modes','bios-cd'],capture_output=True,text=True)
            self.assertNotEqual(p.returncode,0)
            self.assertIn('candidate',p.stderr)
            self.assertFalse(evidence.exists())

    def test_help_exposes_file_only_validation_modes(self):
        p=subprocess.run([sys.executable,str(SCRIPT),'--help'],capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertIn('--modes',p.stdout)
        self.assertIn('--evidence',p.stdout)

if __name__=='__main__':unittest.main()
