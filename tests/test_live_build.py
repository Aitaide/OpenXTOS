#!/usr/bin/env python3
"""Live builder negative acceptance never changes existing rootfs or media."""
import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'scripts/build_live.py'

class LiveBuildTests(unittest.TestCase):
    def test_help_describes_non_installing_live_output(self):
        p=subprocess.run([sys.executable,str(SCRIPT),'--help'],capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertIn('--userspace-root',p.stdout)
        self.assertIn('--kernel-build',p.stdout)
        self.assertIn('--authorized-key',p.stdout)

    def test_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            out=Path(tmp)/'existing';out.mkdir();(out/'sentinel').write_text('keep')
            p=subprocess.run([sys.executable,str(SCRIPT),'--userspace-root',tmp,'--kernel-build',tmp,'--busybox',tmp,'--output-dir',str(out)],capture_output=True,text=True)
            self.assertNotEqual(p.returncode,0)
            self.assertIn('already exists',p.stderr)
            self.assertEqual((out/'sentinel').read_text(),'keep')

    def test_missing_input_creates_no_output(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            out=Path(tmp)/'new'
            p=subprocess.run([sys.executable,str(SCRIPT),'--userspace-root',str(Path(tmp)/'missing'),'--kernel-build',tmp,'--busybox',tmp,'--output-dir',str(out)],capture_output=True,text=True)
            self.assertNotEqual(p.returncode,0)
            self.assertFalse(out.exists())

    def test_private_key_input_is_rejected(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('live',SCRIPT)
        assert spec and spec.loader
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            key=Path(tmp)/'key';key.write_text('-----BEGIN OPENSSH PRIVATE KEY-----\n')
            with self.assertRaises(ValueError):m.validate_public_key(key)

    def test_source_provenance_reports_clean_and_changed_trees(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('live',SCRIPT)
        assert spec and spec.loader
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertTrue(hasattr(module,'source_provenance'), 'builder hardcodes dirty instead of reading source state')
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)
            subprocess.run(['git','init','-q',str(repo)],check=True)
            subprocess.run(['git','-C',str(repo),'config','user.name','XTOS Test'],check=True)
            subprocess.run(['git','-C',str(repo),'config','user.email','test@example.invalid'],check=True)
            (repo/'source').write_text('first\n')
            subprocess.run(['git','-C',str(repo),'add','source'],check=True)
            subprocess.run(['git','-C',str(repo),'commit','-qm','fixture'],check=True)
            sha=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
            self.assertEqual(module.source_provenance(repo), {'source_git_head':sha,'source_state':'clean'})
            (repo/'source').write_text('modified\n')
            self.assertEqual(module.source_provenance(repo)['source_state'],'dirty')
            subprocess.run(['git','-C',str(repo),'checkout','--','source'],check=True)
            (repo/'new-source').write_text('untracked\n')
            self.assertEqual(module.source_provenance(repo)['source_state'],'dirty')

    def test_developer_has_graphics_groups_but_not_root_group(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('live',SCRIPT)
        assert spec and spec.loader
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertTrue(hasattr(module, 'account_groups'), 'missing graphics group provisioning')
        groups={line.split(':')[0]:line.split(':') for line in module.account_groups().splitlines()}
        for name,gid in [('tty','5'),('dialout','20'),('video','44'),('render','1001')]:
            self.assertEqual(groups[name][2],gid)
        for name in ['dialout','video','render']:
            self.assertIn('xtos',groups[name][3].split(','))
        self.assertNotIn('xtos',groups['root'][3].split(','))

    def test_key_only_accounts_have_no_usable_password_or_locked_key_auth(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('live',SCRIPT)
        assert spec and spec.loader
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        shadow=module.account_shadow(key_login=True)
        for line in shadow.splitlines():
            value=line.split(':')[1]
            self.assertEqual(value,'NP')
            self.assertNotIn(value[0],'!*')
        self.assertIn('root:!:',module.account_shadow(key_login=False))

if __name__=='__main__':unittest.main()
