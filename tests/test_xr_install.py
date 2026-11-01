"""Source-only XR application installation; no headset/runtime bundled."""
from pathlib import Path
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import shutil
import re
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]

def installer():
    spec=importlib.util.spec_from_file_location('xr_source_installer',ROOT/'scripts/install_xr.py')
    if spec is None or spec.loader is None:raise RuntimeError('XR installer missing')
    module=importlib.util.module_from_spec(spec)
    with patch.object(sys,'path',[str(ROOT/'scripts'),*sys.path]):
        spec.loader.exec_module(module)
    return module

class XrInstallTests(unittest.TestCase):
    def test_fresh_install_is_source_bound_and_demo_is_harmless(self):
        with tempfile.TemporaryDirectory(prefix='xr-install-') as directory:
            stage=Path(directory)
            module=installer();record=module.install(stage,ROOT)
            self.assertEqual(record['schema'],'xtos.xr.source-inventory.v1')
            self.assertFalse(record['physical_acceptance'])
            self.assertFalse(record['vendor_runtime_bundled'])
            package=stage/'usr/lib/xtos/xtos_xr'
            self.assertEqual({p.name for p in package.iterdir()},set(module.PYTHON_NAMES))
            for name in module.PYTHON_NAMES:
                self.assertEqual((package/name).read_bytes(),
                    (ROOT/'tools/xtos-xr/xtos_xr'/name).read_bytes())
            for name in ('test','fixture','vendor'):
                self.assertFalse(any(name in p.name for p in package.rglob('*')))
            env=dict(os.environ,PYTHONPATH=str(package.parent))
            run=subprocess.run([sys.executable,'-B','-m','xtos_xr','--demo'],
                cwd=stage,env=env,capture_output=True,text=True,timeout=15)
            self.assertEqual(run.returncode,0,run.stderr)
            output=json.loads(run.stdout)
            self.assertEqual(output['safe_action'],'no_action')
            self.assertFalse(output['physical_acceptance'])
            self.assertEqual(output['runtime_mode'],'simulated')
            self.assertTrue((stage/'usr/bin/xr-feedback-demo').is_file())
            self.assertTrue((stage/'usr/bin/xr-probe').is_file())
            self.assertFalse((stage/'var/lib/xtos/action-gate').exists())

    def test_identical_retry_and_conflict_preserve_caller_files(self):
        with tempfile.TemporaryDirectory(prefix='xr-install-reuse-') as directory:
            stage=Path(directory);module=installer();first=module.install(stage,ROOT)
            self.assertEqual(module.install(stage,ROOT),first)
            victim=stage/'usr/lib/xtos/xtos_xr/__init__.py'
            victim.write_bytes(b'caller-owned change')
            with self.assertRaises((ValueError,OSError)):
                module.install(stage,ROOT)
            self.assertEqual(victim.read_bytes(),b'caller-owned change')
            manifest=json.loads((stage/'usr/share/xtos/xr-sources.json').read_bytes())
            self.assertEqual(manifest,first)

    @unittest.skipUnless(os.geteuid()==0,'launcher chroot requires root')
    def test_exact_launcher_selects_target_interpreter_and_preserves_arguments(self):
        with tempfile.TemporaryDirectory(prefix='xr-launcher-') as directory:
            stage=Path(directory);installer().install(stage,ROOT)
            (stage/'bin').mkdir()
            shell=Path('/bin/sh').resolve()
            shutil.copy2(shell,stage/'bin/sh')
            linked=subprocess.run(['ldd',str(shell)],capture_output=True,text=True,check=True)
            for dependency in set(re.findall(r'(/[^\s()]+)',linked.stdout)):
                source=Path(dependency)
                target=stage/str(source).lstrip('/')
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(source,target)
            source=stage/'interpreter-sentinel.c'
            source.write_text('#include <stdio.h>\nint main(int n,char **v){for(int i=0;i<n;i++) puts(v[i]);return 0;}\n')
            subprocess.run(['cc','-static',str(source),'-o',str(stage/'usr/bin/python3')],check=True,capture_output=True)
            command=['chroot',str(stage),'/usr/bin/xr-feedback-demo','--demo','literal space','$HOME']
            run=subprocess.run(command,capture_output=True,text=True,timeout=10)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertEqual(run.stdout.splitlines(),['/usr/bin/python3',
                '/usr/libexec/xtos/xr-feedback-entry.py','--demo','literal space','$HOME'])
            (stage/'usr/bin/python3').unlink()
            missing=subprocess.run(command,capture_output=True,text=True,timeout=10)
            self.assertNotEqual(missing.returncode,0)
            self.assertEqual(missing.stdout,'')

    def test_existing_fifo_refusal_is_nonblocking_and_preserves_identity(self):
        with tempfile.TemporaryDirectory(prefix='xr-install-fifo-') as directory:
            stage=Path(directory);module=installer();module.install(stage,ROOT)
            for name in ('usr/lib/xtos/xtos_xr/__init__.py','usr/bin/xr-feedback-demo'):
                target=stage/name;before=target.read_bytes();mode=target.stat().st_mode&0o7777
                target.unlink();os.mkfifo(target)
                identity=(target.stat().st_dev,target.stat().st_ino)
                try:
                    with self.assertRaises((ValueError,OSError)):
                        module.install(stage,ROOT)
                    self.assertEqual((target.stat().st_dev,target.stat().st_ino),identity)
                finally:
                    target.unlink();target.write_bytes(before);target.chmod(mode)

    def test_racing_destination_is_not_replaced(self):
        with tempfile.TemporaryDirectory(prefix='xr-install-race-') as directory:
            stage=Path(directory);module=installer();original=module._rename_noreplace
            raced=stage/'usr/bin/xr-feedback-demo'
            def publish(source,target):
                if target==raced:
                    target.write_bytes(b'caller-race-sentinel')
                return original(source,target)
            with patch.object(module,'_rename_noreplace',side_effect=publish):
                with self.assertRaises(FileExistsError):module.install(stage,ROOT)
            self.assertEqual(raced.read_bytes(),b'caller-race-sentinel')
            self.assertFalse((stage/'usr/share/xtos/xr-sources.json').exists())

if __name__=='__main__':unittest.main()
