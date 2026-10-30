"""Build the actual signal SDK in disposable trees; never touch canonical outputs."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
WINDOW_EXPORTS={'xtos_eeg_window_init','xtos_eeg_window_push',
                'xtos_eeg_window_features','xtos_eeg_window_reset'}
LEGACY_EXPORTS={'xtos_signal_validate_timestamps','xtos_signal_correct_timestamps',
                'xtos_signal_fft_power','xtos_notch_init','xtos_notch_process',
                'xtos_bandpass_init','xtos_bandpass_process','xtos_signal_ssvep_feature',
                'xtos_signal_p300_quality_gate','xtos_signal_p300_feature'}
HEADERS={'signal.h','signal_version.h','eeg_window.h'}
CLIENT=r'''#include <xtos/eeg_window.h>
#include <xtos/signal.h>
#include <xtos/signal_version.h>
#include <stdio.h>
static struct xtos_eeg_window state;
int main(void) {
    struct xtos_eeg_window_options o={.version=1,.channels=1,.sample_rate_hz=250,
        .window_frames=2,.hop_frames=1,.max_receive_age_ns=1000};
    struct xtos_eeg_sample s={.version=1,.channels=1,.sample_rate_hz=250,
        .unit=XTOS_EEG_UNIT_NANOVOLT,.host_clock=XTOS_EEG_CLOCK_MONOTONIC,
        .quality_flags=XTOS_EEG_CONFIG_VERIFIED,.source_flags=XTOS_EEG_SOURCE_SYNTHETIC,
        .enabled_mask=1,.session_id=1,.stream_generation=1,.config_generation=1,
        .sequence=1,.host_rx_ns=100,.samples_nv={3000}};
    struct xtos_eeg_features f;
    if(xtos_eeg_window_init(&state,&o))return 1;
    if(xtos_eeg_window_push(&state,&s))return 2;
    s.sequence++;
    if(xtos_eeg_window_push(&state,&s))return 3;
    if(xtos_eeg_window_features(&state,1100,&f))return 4;
    if(f.feature_count!=2 || f.values_uv[0]!=3 || f.values_uv[1]!=3)return 5;
    if(xtos_notch_init(NULL,250,50,10)!=-1)return 6;
    xtos_eeg_window_reset(&state);
    puts("installed_eeg_window=pass synthetic_only=1");return 0;
}
'''


def clean_env():
    env=dict(os.environ,LC_ALL='C')
    for name in ('CPATH','C_INCLUDE_PATH','CPLUS_INCLUDE_PATH','LIBRARY_PATH',
                 'LD_LIBRARY_PATH','LD_PRELOAD','MAKEFLAGS','MFLAGS'):
        env.pop(name,None)
    return env


def copy_sdk(destination):
    source=ROOT/'sdk/libxtos-signal'
    shutil.copytree(source,destination,ignore=shutil.ignore_patterns(
        '*.o','*.d','*.a','*.so','*.so.*','test_signal','test_eeg_window','__pycache__'))


def command(args,**kwargs):
    return subprocess.run([str(x) for x in args],env=clean_env(),capture_output=True,
                          text=True,timeout=60,**kwargs)


class EegWindowInstallTests(unittest.TestCase):
    def test_archive_exports_legacy_and_window(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            sdk=Path(temporary)/'sdk';copy_sdk(sdk)
            build=command(['make','-C',sdk,'clean','all'])
            self.assertEqual(build.returncode,0,build.stdout+build.stderr)
            symbols=command(['nm','-g','--defined-only',sdk/'libxtos-signal.a'])
            self.assertEqual(symbols.returncode,0,symbols.stderr)
            exports={line.split()[-1] for line in symbols.stdout.splitlines()
                     if len(line.split())>=3 and line.split()[-2] in ('T','W')}
            self.assertTrue(WINDOW_EXPORTS<=exports,
                            'built archive lacks window exports: '+str(sorted(WINDOW_EXPORTS-exports)))
            self.assertIn('xtos_bandpass_process',exports)

    def test_shared_ABI_version_and_exact_export_sets(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            sdk=Path(temporary)/'sdk';copy_sdk(sdk)
            build=command(['make','-C',sdk,'all'])
            self.assertEqual(build.returncode,0,build.stdout+build.stderr)
            shared=sdk/'libxtos-signal.so.1.1'
            self.assertTrue(shared.is_file(),'added symbols require SDK minor version 1.1')
            self.assertEqual(os.readlink(sdk/'libxtos-signal.so.1'),'libxtos-signal.so.1.1')
            self.assertEqual(os.readlink(sdk/'libxtos-signal.so'),'libxtos-signal.so.1')
            dynamic=command(['readelf','-d',shared]);self.assertEqual(dynamic.returncode,0,dynamic.stderr)
            self.assertIn('Library soname: [libxtos-signal.so.1]',dynamic.stdout)
            for path,args in [(sdk/'libxtos-signal.a',['nm','-g','--defined-only']),
                              (shared,['nm','-D','--defined-only'])]:
                result=command([*args,path]);self.assertEqual(result.returncode,0,result.stderr)
                exports={line.split()[-1] for line in result.stdout.splitlines()
                         if len(line.split())>=3 and line.split()[-2] in ('T','W')}
                self.assertEqual(exports,LEGACY_EXPORTS|WINDOW_EXPORTS)
            version=(sdk/'include/xtos/signal_version.h').read_text()
            self.assertIn('#define XTOS_SIGNAL_VERSION_MINOR 1',version)
            self.assertIn('#define XTOS_SIGNAL_VERSION_STRING "1.1.0"',version)

    def test_make_runs_window_cases_and_tracks_public_headers(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            sdk=Path(temporary)/'sdk';copy_sdk(sdk)
            build=command(['make','-C',sdk,'clean','all','test'])
            self.assertEqual(build.returncode,0,build.stdout+build.stderr)
            self.assertTrue((sdk/'test_eeg_window').is_file(),'make test omits portable window cases')
            self.assertIn('PASS assertions=181',build.stdout)
            self.assertIn('PASS eeg_window_cases=9',build.stdout)
            header=sdk/'include/xtos/eeg_window.h'
            target_times=[(sdk/p).stat().st_mtime_ns for p in ('src/eeg_window.o','test_eeg_window')]
            os.utime(header,ns=(max(target_times)+1000000000,max(target_times)+1000000000))
            plan=command(['make','-C',sdk,'-n','all','test'])
            self.assertEqual(plan.returncode,0,plan.stdout+plan.stderr)
            for expected in ('-c src/eeg_window.c','rcs libxtos-signal.a',
                             '-shared','-o test_eeg_window','-o test_signal'):
                self.assertIn(expected,plan.stdout)

    def test_tool_overrides_and_clean_preserve_sources(self):
        import shlex
        import sys
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            directory=Path(temporary);sdk=directory/'sdk';copy_sdk(sdk)
            wrappers={};counters={}
            for name,tool in [('CC',shutil.which('cc')),('AR',shutil.which('ar'))]:
                self.assertIsNotNone(tool)
                wrapper=directory/(name.lower()+'-tool');counter=directory/(name.lower()+'.calls')
                wrapper.write_text('#!'+sys.executable+'\nimport os,sys\n'
                    +'with open('+repr(str(counter))+',"a") as stream:stream.write("called\\n")\n'
                    +'os.execv('+repr(tool)+',['+repr(tool)+',*sys.argv[1:]])\n')
                wrapper.chmod(0o700);wrappers[name]=wrapper;counters[name]=counter
            build=command(['make','-C',sdk,'all','test',
                           'CC='+str(wrappers['CC']),'AR='+str(wrappers['AR'])])
            self.assertEqual(build.returncode,0,build.stdout+build.stderr)
            for name in ('CC','AR'):
                self.assertTrue(counters[name].is_file(),'Makefile ignores '+name+' override')
                self.assertGreater(len(counters[name].read_text().splitlines()),0)
            sentinel=sdk/'caller-owned';sentinel.write_bytes(b'keep')
            result=command(['make','-C',sdk,'clean'])
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertFalse((sdk/'test_eeg_window').exists(),'make clean leaves new test binary')
            self.assertFalse(list((sdk/'src').glob('*.o')))
            self.assertFalse(list(sdk.glob('libxtos-signal*')))
            self.assertEqual(sentinel.read_bytes(),b'keep')
            self.assertTrue((sdk/'tests/test_eeg_window.c').is_file())
            self.assertTrue((sdk/'include/xtos/eeg_window.h').is_file())

    def test_version_upgrade_clean_removes_previous_owned_shared_file(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            sdk=Path(temporary)/'sdk';copy_sdk(sdk)
            old=sdk/'libxtos-signal.so.1.0';old.write_bytes(b'retained previous build placeholder')
            sentinel=sdk/'caller-owned';sentinel.write_bytes(b'keep')
            result=command(['make','-C',sdk,'clean','all'])
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertFalse(old.exists(),'version upgrade clean retains obsolete owned shared file')
            self.assertEqual({p.name for p in sdk.glob('libxtos-signal.so.*.*')},{'libxtos-signal.so.1.1'})
            self.assertEqual(sentinel.read_bytes(),b'keep')

    def test_installed_shared_archive_clients_and_uninstall(self):
        import shlex
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            directory=Path(temporary);sdk=directory/'sdk';stage=directory/'stage'
            copy_sdk(sdk)
            sentinel=stage/'usr/include/xtos/unrelated.h';sentinel.parent.mkdir(parents=True)
            sentinel.write_bytes(b'caller-owned sentinel\n')
            installed=command(['make','-C',sdk,'install','DESTDIR='+str(stage)])
            self.assertEqual(installed.returncode,0,installed.stdout+installed.stderr)
            include=stage/'usr/include';lib=stage/'usr/lib'
            for name in HEADERS:
                self.assertEqual((include/'xtos'/name).read_bytes(),(sdk/'include/xtos'/name).read_bytes())
            client=directory/'client.c';client.write_text(CLIENT)
            for mode in ('archive','shared'):
                binary=directory/('client-'+mode);deps=directory/(mode+'.d')
                linkage=[str(lib/'libxtos-signal.a')] if mode=='archive' else ['-L'+str(lib),'-Wl,-rpath,'+str(lib),'-lxtos-signal']
                built=command(['cc','-std=c11','-Wall','-Wextra','-Werror','-I'+str(include),
                               '-MD','-MF',deps,client,*linkage,'-lm','-o',binary])
                self.assertEqual(built.returncode,0,built.stdout+built.stderr)
                result=command([binary]);self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertEqual(result.stdout,'installed_eeg_window=pass synthetic_only=1\n')
                headers=[Path(p).resolve() for p in shlex.split(deps.read_text().replace('\\\n',' ').split(':',1)[1])
                         if p.endswith('.h') and Path(p).name in HEADERS]
                self.assertEqual({p.name for p in headers},HEADERS)
                self.assertTrue(all(p.is_relative_to(include.resolve()) for p in headers))
                self.assertNotIn(str(sdk/'include'),deps.read_text())
            removed=command(['make','-C',sdk,'uninstall','DESTDIR='+str(stage)])
            self.assertEqual(removed.returncode,0,removed.stdout+removed.stderr)
            self.assertEqual(sentinel.read_bytes(),b'caller-owned sentinel\n')
            self.assertFalse(any((include/'xtos'/name).exists() for name in HEADERS))
            self.assertFalse(list(lib.glob('libxtos-signal*')))
            self.assertTrue((sdk/'include/xtos/eeg_window.h').is_file())


if __name__=='__main__':unittest.main(verbosity=2)
