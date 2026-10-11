#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'scripts/configure_userspace.py'

def fixture_manifest():
    package={'name':'fixture','sha256':'0'*64}
    return json.dumps({'schema_version':1,'architecture':'x86_64','lock_sha256':'0'*64,
      'lock':{'schema_version':1,'architecture':'x86_64','package_format':'deb','packages':[package]},
      'packages':[{'package':package,'files':[]}]})+'\n'

class ConfigureTests(unittest.TestCase):
    def test_rejects_unowned_root_before_writes(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root=Path(tmp)
            result=subprocess.run([sys.executable,str(SCRIPT),'--rootfs',str(root)],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('manifest',result.stderr)
            self.assertEqual(list(root.iterdir()),[])

    def test_owned_root_gets_literal_defaults_and_no_unlocked_accounts(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root=Path(tmp)
            (root/'usr/share/xtos').mkdir(parents=True)
            (root/'usr/share/xtos/userspace-manifest.json').write_text(fixture_manifest())
            (root/'usr/bin').mkdir(parents=True)
            for n in ['dash','vim.tiny','gawk']:(root/'usr/bin'/n).write_text('fixture executable\n')
            (root/'etc').mkdir()
            (root/'usr/share/ca-certificates/mozilla').mkdir(parents=True)
            (root/'usr/share/ca-certificates/mozilla/test.crt').write_text('fixture certificate\n')
            result=subprocess.run([sys.executable,str(SCRIPT),'--rootfs',str(root)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(os.readlink(root/'usr/bin/vi'),'vim.tiny')
            self.assertEqual(os.readlink(root/'usr/bin/sh'),'dash')
            self.assertIn('GOTOOLCHAIN=local',(root/'etc/profile.d/xtos-developer.sh').read_text())
            self.assertIn('GOROOT=/usr/lib/go',(root/'etc/profile.d/xtos-developer.sh').read_text())
            self.assertEqual((root/'etc/ssl/certs/ca-certificates.crt').read_text(),'fixture certificate\n')
            self.assertIn('root:!:',(root/'etc/shadow').read_text())
            self.assertEqual((root/'etc/shadow').stat().st_mode & 0o777,0o600)
            self.assertEqual((root/'root/.config/go/telemetry/mode').read_text(),'off\n')
            self.assertEqual((root/'etc/skel/.config/go/telemetry/mode').read_text(),'off\n')
            self.assertFalse((root/'etc/resolv.conf').exists())
            first=(root/'etc/profile.d/xtos-developer.sh').read_bytes()
            repeat=subprocess.run([sys.executable,str(SCRIPT),'--rootfs',str(root)],capture_output=True,text=True)
            self.assertEqual(repeat.returncode,0,repeat.stderr)
            self.assertEqual((root/'etc/profile.d/xtos-developer.sh').read_bytes(),first)

if __name__=='__main__':unittest.main()
