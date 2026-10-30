"""Window SDK maintenance/packaging closure; synthetic CPU checks only."""
from pathlib import Path
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class EegWindowIntegrationTests(unittest.TestCase):
    def test_maintenance_and_each_system_header_install_path(self):
        text=(ROOT/'scripts/test.sh').read_text()
        for name in ('test_eeg_window.py','test_eeg_window_install.py','test_eeg_window_integration.py'):
            self.assertIn(name,text,'maintenance omits '+name)
        system=(ROOT/'scripts/build_system.sh').read_text()
        self.assertIn('"$STAGING/sdk/libxtos-$library/include/xtos/."',system)
        self.assertIn('test_eeg_window',system)
        for name in ('build_system_arm64.sh','build_system_riscv64.sh'):
            self.assertIn('libxtos-signal/include/xtos/eeg_window.h',(ROOT/'scripts'/name).read_text(),
                          name+' omits window public header')

    def test_SBOM_binds_versioned_window_source_and_all_signal_headers(self):
        spec=importlib.util.spec_from_file_location('window_sbom',ROOT/'tools/xtos-sbom/gen_sbom.py')
        sbom=importlib.util.module_from_spec(spec);spec.loader.exec_module(sbom)
        component=next(c for c in sbom.COMPONENTS if c['name']=='libxtos-signal')
        self.assertEqual(component['version'],'1.1.0')
        expected={'src/signal.c','src/eeg_window.c','include/xtos/signal.h',
                  'include/xtos/signal_version.h','include/xtos/eeg_window.h'}
        self.assertEqual(set(component['hash_globs']),expected)
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            output=Path(temporary)/'bom.json'
            result=subprocess.run(['python3','-B',str(ROOT/'tools/xtos-sbom/gen_sbom.py'),
                                   '--out',str(output)],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            doc=json.loads(output.read_text())
        actual=next(c for c in doc['components'] if c['name']=='libxtos-signal')
        self.assertEqual(actual['version'],'1.1.0')
        self.assertEqual({h['content'] for h in actual['hashes']},
                         {hashlib.sha256((ROOT/'sdk/libxtos-signal'/name).read_bytes()).hexdigest() for name in expected})
        checked=json.loads((ROOT/'docs/sbom.json').read_text())
        saved=next(c for c in checked['components'] if c['name']=='libxtos-signal')
        self.assertEqual(saved['version'],actual['version'])
        self.assertEqual(saved['hashes'],actual['hashes'])

    def test_actual_Live_installs_window_archive_headers_and_static_fixture(self):
        live_spec=importlib.util.spec_from_file_location('window_live',ROOT/'scripts/build_live.py')
        live=importlib.util.module_from_spec(live_spec);live_spec.loader.exec_module(live)
        client_spec=importlib.util.spec_from_file_location('window_client',ROOT/'tests/test_eeg_window_install.py')
        client=importlib.util.module_from_spec(client_spec);client_spec.loader.exec_module(client)
        import shlex
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            directory=Path(temporary);stage=directory/'stage';work=directory/'work'
            (stage/'usr/bin').mkdir(parents=True);work.mkdir()
            live.install_sdk(stage,work)
            fixture=stage/'usr/libexec/xtos/test_eeg_window'
            self.assertTrue(fixture.is_file(),'Live SDK installer omits static window fixture')
            result=client.command(['readelf','-l',fixture]);self.assertEqual(result.returncode,0,result.stderr)
            self.assertNotIn('INTERP',result.stdout)
            for case in ('golden','boundaries','quality','freshness','rolling','bounds','invalid','duplicate','oldest_stale'):
                done=client.command([fixture,case]);self.assertEqual(done.returncode,0,done.stdout+done.stderr)
            include=stage/'usr/include'
            for name in client.HEADERS:
                self.assertEqual((include/'xtos'/name).read_bytes(),(ROOT/'sdk/libxtos-signal/include/xtos'/name).read_bytes())
            source=directory/'client.c';source.write_text(client.CLIENT)
            binary=directory/'installed-client';dependency=directory/'installed-client.d'
            compiled=client.command(['cc','-std=c11','-Wall','-Wextra','-Werror','-static',
                '-I'+str(include),'-MD','-MF',dependency,source,stage/'usr/lib/libxtos-signal.a','-lm','-o',binary])
            self.assertEqual(compiled.returncode,0,compiled.stdout+compiled.stderr)
            ran=client.command([binary]);self.assertEqual(ran.returncode,0,ran.stdout+ran.stderr)
            self.assertEqual(ran.stdout,'installed_eeg_window=pass synthetic_only=1\n')
            headers=[Path(p).resolve() for p in shlex.split(dependency.read_text().replace('\\\n',' ').split(':',1)[1])
                     if p.endswith('.h') and Path(p).name in client.HEADERS]
            self.assertEqual({p.name for p in headers},client.HEADERS)
            self.assertTrue(all(p.is_relative_to(include.resolve()) for p in headers))
            self.assertNotIn(str(ROOT/'sdk/libxtos-signal/include'),dependency.read_text())
            # Execute the real System SDK-install shell slice on the same built
            # disposable source. Full kernel/rootfs generation remains separate.
            system=(ROOT/'scripts/build_system.sh').read_text()
            compile_line=next(line for line in system.splitlines()
                              if line.startswith('make ') and 'libxtos-signal' in line and 'test_eeg_window' in line)
            start=system.index('for library in bci rt signal; do')
            end=system.index('\ndone',start)+len('\ndone')
            install_lines=[line for line in system.splitlines()
                           if line.startswith('install ') and 'libxtos-signal/test_' in line]
            script='\n'.join([compile_line,system[start:end],*install_lines])+'\n'
            system_root=directory/'system-root'
            for path in ('usr/lib','usr/include/xtos','usr/libexec/xtos'):
                (system_root/path).mkdir(parents=True,exist_ok=True)
            env=client.clean_env();env.update(STAGING=str(work/'sdk-source'),ROOTFS=str(system_root))
            installed=subprocess.run(['bash','-eu','-c',script],env=env,capture_output=True,text=True,timeout=30)
            self.assertEqual(installed.returncode,0,installed.stdout+installed.stderr)
            system_fixture=system_root/'usr/libexec/xtos/test_eeg_window'
            self.assertTrue(system_fixture.is_file())
            for name in client.HEADERS:
                self.assertEqual((system_root/'usr/include/xtos'/name).read_bytes(),(include/'xtos'/name).read_bytes())
            self.assertEqual((system_root/'usr/lib/libxtos-signal.a').read_bytes(),(stage/'usr/lib/libxtos-signal.a').read_bytes())
            for case in ('golden','boundaries','quality','freshness','rolling','bounds','invalid','duplicate','oldest_stale'):
                done=client.command([system_fixture,case]);self.assertEqual(done.returncode,0,done.stdout+done.stderr)


if __name__=='__main__':unittest.main(verbosity=2)
