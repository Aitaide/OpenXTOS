"""Native export contract: actual main/SDK/adapter, syscall-only transport."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'tools/xtos-eeg-stream'
OWNED = ['eeg_export.c', 'eeg_export.h', 'main.c', 'Makefile', 'tests/test_eeg_export.c']
DEPS = [TOOL/'eeg_stream.c', TOOL/'eeg_stream.h', ROOT/'tests/test_eeg_stream.py', TOOL/'tests/test_eeg_stream.c']
DEPS += list((ROOT/'sdk/libxtos-bci/include/xtos').glob('*.h'))
DEPS += list((ROOT/'sdk/libxtos-signal/include/xtos').glob('*.h'))
DEPS += [ROOT/'sdk/libxtos-bci/src/session.c', ROOT/'sdk/libxtos-signal/src/eeg_window.c',
         ROOT/'sdk/libxtos-signal/src/signal.c', ROOT/'sdk/libxtos-bci/Makefile',
         ROOT/'sdk/libxtos-signal/Makefile', ROOT/'docs/eeg-stream-export-v1.md',
         ROOT/'docs/eeg-stream-adapter-v1.md']

def run_logged(command, work, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, timeout=40, **kwargs)
    with (work/'commands.jsonl').open('a') as log:
        log.write(json.dumps({'command': command, 'returncode': result.returncode,
                              'stdout': result.stdout, 'stderr': result.stderr,
                              'environment': {k:v for k,v in kwargs.get('env',{}).items()
                                              if k.startswith(('EXPORT_', 'XTOS_EEG_')) or k in ('LC_ALL','ASAN_OPTIONS','UBSAN_OPTIONS')}})+'\n')
    return result

class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        evidence = os.environ.get('XTOS_EXPORT_EVIDENCE')
        if evidence:
            cls.work = Path(evidence)
            cls.work.mkdir(parents=True, exist_ok=False)
        else:
            cls.temp = tempfile.TemporaryDirectory(prefix='export-', dir=os.environ.get('TMPDIR', '/root/.hermes/cache/scratch'))
            cls.addClassCleanup(cls.temp.cleanup)
            cls.work = Path(cls.temp.name)
        paths = [TOOL/p for p in OWNED] + [Path(__file__)] + DEPS
        hashes = {}
        for p in paths:
            if p.is_file():
                data = p.read_bytes()
                rel = p.relative_to(ROOT)
                target = cls.work/'source'/rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                hashes[str(rel)] = hashlib.sha256(data).hexdigest()
        (cls.work/'source-sha256.json').write_text(json.dumps(hashes, indent=2)+'\n')
        mode = os.environ.get('XTOS_EEG_EXPORT_SANITIZER', '')
        if mode not in ('', 'address,undefined'):
            raise RuntimeError('unsupported sanitizer')
        flags = ['-O1','-g','-fno-pie','-no-pie','-fno-omit-frame-pointer','-fsanitize='+mode,'-fno-sanitize-recover=all'] if mode else ['-O2']
        base = ['cc','-std=c11','-Wall','-Wextra','-Werror',*flags,'-I'+str(TOOL),
                '-I'+str(ROOT/'sdk/libxtos-bci/include'),'-I'+str(ROOT/'sdk/libxtos-signal/include')]
        cls.serializer = cls.work/'serializer'
        cmd = base + ['-DEXPORT_SERIALIZER_TEST',str(TOOL/'tests/test_eeg_export.c'),str(TOOL/'eeg_export.c'),'-lm','-o',str(cls.serializer)]
        result = run_logged(cmd, cls.work, env={**os.environ, 'LC_ALL':'C'})
        if result.returncode:
            raise RuntimeError(result.stderr)
        cls.binary = cls.work/'eeg-stream'
        if (TOOL/'main.c').exists():
            cmd = base + ['-DEXPORT_DELAY_FEATURES','-Wl,--wrap=xtos_eeg_window_features'] + [str(TOOL/p) for p in ('main.c','eeg_export.c','eeg_stream.c','tests/test_eeg_export.c')]
            cmd += [str(ROOT/'sdk/libxtos-bci/src/session.c'),str(ROOT/'sdk/libxtos-signal/src/eeg_window.c'),'-lm','-Wl,--wrap=open','-Wl,--wrap=close','-Wl,--wrap=ioctl','-o',str(cls.binary)]
            result = run_logged(cmd, cls.work)
            if result.returncode:
                raise RuntimeError(result.stderr)
        cls.pipe_binary = cls.work/'eeg-stream-osonly'
        cmd = base + [str(TOOL/p) for p in ('main.c','eeg_export.c','eeg_stream.c','tests/test_eeg_export.c')]
        cmd += [str(ROOT/'sdk/libxtos-bci/src/session.c'),str(ROOT/'sdk/libxtos-signal/src/eeg_window.c'),'-lm','-Wl,--wrap=open','-Wl,--wrap=close','-Wl,--wrap=ioctl','-o',str(cls.pipe_binary)]
        result = run_logged(cmd, cls.work, env={**os.environ,'LC_ALL':'C'})
        if result.returncode:
            raise RuntimeError(result.stderr)
        (cls.work/'binary-sha256.json').write_text(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (cls.serializer,cls.binary,cls.pipe_binary) if p.exists()}, indent=2)+'\n')

    def command(self, scenario='complete', args=None, full=False):
        args = args if args is not None else ['--device','/dev/xtos-export-fixture','--session-id','18446744073709551615','--window','4','--hop','2','--max-age-ns','1000000000','--duration-ms','30','--max-windows','2']
        env = {**os.environ, 'EXPORT_SCENARIO':scenario, 'EXPORT_TRACE':str(self.work/(scenario+'.trace'))}
        if full:
            with open('/dev/full','w') as out:
                r = subprocess.run([str(self.binary),*args],stdout=out,stderr=subprocess.PIPE,text=True,env=env,timeout=4)
            with (self.work/'commands.jsonl').open('a') as log:
                log.write(json.dumps({'command':[str(self.binary),*args], 'stdout':'/dev/full','stderr':r.stderr,'returncode':r.returncode,'scenario':scenario})+'\n')
        else:
            r = run_logged([str(self.binary),*args],self.work,env=env)
        return r

    def test_final_delayed_read_cannot_publish_after_duration(self):
        args=['--device','/dev/xtos-export-fixture','--session-id','9',
              '--window','4','--hop','2','--max-age-ns','1000000000',
              '--duration-ms','30','--max-windows','1']
        r=self.command('late_read',args)
        self.assertEqual(r.returncode,4,r.stdout+r.stderr)
        rows=[json.loads(line) for line in r.stdout.splitlines()]
        self.assertFalse(any(row['schema']=='xtos.eeg.window.v1' for row in rows))
        self.assertEqual(rows[-1]['status'],'degraded')
        self.assertEqual(rows[-1]['reason'],'duration_exhausted')
        self.assertEqual(rows[-1]['windows_emitted'],0)
        trace=(self.work/'late_read.trace').read_text().splitlines()
        self.assertEqual(trace.count('frame'),4)
        self.assertIn('stop',trace)
        self.assertEqual(trace[-1],'close')

    def test_delayed_feature_extraction_cannot_publish_after_duration(self):
        args=['--device','/dev/xtos-export-fixture','--session-id','9',
              '--window','4','--hop','2','--max-age-ns','1000000000',
              '--duration-ms','30','--max-windows','1']
        r=self.command('late_features',args)
        self.assertEqual(r.returncode,4,r.stdout+r.stderr)
        rows=[json.loads(line) for line in r.stdout.splitlines()]
        self.assertFalse(any(row['schema']=='xtos.eeg.window.v1' for row in rows))
        self.assertEqual(rows[-1]['reason'],'duration_exhausted')
        self.assertEqual(rows[-1]['windows_emitted'],0)

    def test_cancel_preserves_window_and_all_barriers(self):
        for scenario in ('sigint_window','sigterm_window'):
            r=self.command(scenario)
            self.assertEqual(r.returncode,4,r.stderr)
            rows=[json.loads(line) for line in r.stdout.splitlines()]
            self.assertEqual(rows[0]['schema'],'xtos.eeg.window.v1')
            self.assertEqual(rows[-1]['status'],'interrupted')
            self.assertEqual(rows[-1]['windows_emitted'],1)
            barriers=[row['barrier_id'] for row in rows if row['schema']=='xtos.eeg.barrier.v1']
            self.assertEqual(barriers,list(range(2,rows[-1]['barrier_id']+1)))

    def test_cancel_during_features_blocks_new_window(self):
        args=['--device','/dev/xtos-export-fixture','--session-id','9',
              '--window','4','--hop','2','--max-age-ns','1000000000',
              '--duration-ms','200','--max-windows','2']
        for scenario in ('feature_sigint','feature_sigterm',
                         'feature_sigint_preserve','feature_sigterm_preserve'):
            with self.subTest(scenario=scenario):
                r=self.command(scenario,args)
                self.assertEqual(r.returncode,4,r.stdout+r.stderr)
                rows=[json.loads(line) for line in r.stdout.splitlines()]
                windows=[row for row in rows if row['schema']=='xtos.eeg.window.v1']
                expected=1 if scenario.endswith('_preserve') else 0
                self.assertEqual(len(windows),expected,r.stdout)
                self.assertEqual(rows[-1]['status'],'interrupted')
                self.assertEqual(rows[-1]['reason'],'cancelled')
                self.assertEqual(rows[-1]['windows_emitted'],expected)
                if expected:
                    self.assertEqual(windows[0]['first_sequence'],1)
                    self.assertEqual(windows[0]['last_sequence'],4)
                    self.assertEqual(windows[0]['values'],list(range(1,9))*2)
                self.assertTrue(any(row['schema']=='xtos.eeg.barrier.v1' and
                                    row['reason']=='cancelled' for row in rows))
                trace=(self.work/(scenario+'.trace')).read_text().splitlines()
                self.assertEqual(trace.count('cancel_before_publish'),1)
                self.assertEqual(trace.count('frame'),6 if expected else 4)
                self.assertEqual(trace.count('stop'),1)
                self.assertEqual(trace[-1],'close')
                self.assertLess(trace.index('cancel_before_publish'),trace.index('stop'))

    def test_packaging(self):
        import shutil
        source=self.work/'disposable'
        for name in ('libxtos-bci','libxtos-signal'):
            src=ROOT/'sdk'/name;dst=source/'sdk'/name
            shutil.copytree(src,dst,ignore=shutil.ignore_patterns('*.o','*.a','*.so*','__pycache__'))
        tool=source/'tools/xtos-eeg-stream'
        shutil.copytree(TOOL,tool,ignore=shutil.ignore_patterns('*.o','eeg-stream','__pycache__'))
        (source/'tests').mkdir()
        shutil.copy2(Path(__file__),source/'tests/test_eeg_stream_export.py')
        stage=self.work/'installed'
        for name in ('libxtos-bci','libxtos-signal'):
            r=run_logged(['make','-C',str(source/'sdk'/name),'all','install','DESTDIR='+str(stage.resolve())],self.work)
            self.assertEqual(r.returncode,0,r.stderr)
        r=run_logged(['make','-C',str(tool),'all','test','install','DESTDIR='+str(stage.resolve()),
                      'XTOS_EXPORT_TEST_EVIDENCE='+str((self.work/'packaging-tests').resolve())],self.work)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertTrue((stage/'usr/bin/eeg-stream').is_file())
        install_build=self.work/'installed-client'
        r=run_logged(['make','-C',str(tool),'all','BUILD_DIR='+str(install_build.resolve()),
                     'CPPFLAGS=-I'+str((stage/'usr/include').resolve()),
                     'SDK_LIBS='+str((stage/'usr/lib/libxtos-bci.a').resolve())+' '+str((stage/'usr/lib/libxtos-signal.a').resolve())],self.work,
                     env={k:v for k,v in os.environ.items() if k not in ('CPATH','C_INCLUDE_PATH','LIBRARY_PATH','CPLUS_INCLUDE_PATH')})
        self.assertEqual(r.returncode,0,r.stderr)
        dep=(install_build/'main.d').read_text().replace('\\\n','')
        project=[Path(x) for x in dep.split() if '/xtos/' in x and x.endswith('.h')]
        self.assertTrue(project)
        self.assertTrue(all(p.is_relative_to(stage.resolve()/'usr/include') for p in project),dep)
        r=run_logged([str(install_build/'eeg-stream')],self.work)
        self.assertEqual(r.returncode,2)

    def test_lifecycle_failures(self):
        cases={'timeout':(4,'degraded','duration_exhausted'), 'insufficient':(4,'degraded','duration_exhausted'),
               'bad_tail':(4,'degraded','read_failed'),'disconnect':(4,'degraded','read_failed'),
               'stale':(4,'degraded','stale_or_invalid_window'),'disabled':(4,'degraded','disabled_configuration'),
               'stop_fail':(4,'degraded','stop_failed'),'start_fail':(3,'error','start_failed'),
               'disconnect_stop':(4,'degraded','read_failed'),
               'sigint':(4,'interrupted','cancelled'),'sigterm':(4,'interrupted','cancelled')}
        for scenario,(code,status,reason) in cases.items():
            with self.subTest(scenario=scenario):
                r=self.command(scenario)
                self.assertEqual(r.returncode,code,r.stderr)
                rows=[json.loads(line) for line in r.stdout.splitlines()]
                self.assertEqual(rows[-1]['status'],status)
                self.assertEqual(rows[-1]['reason'],reason)
                self.assertEqual(rows[-1]['windows_emitted'],2 if scenario=='stop_fail' else 0)
                trace=(self.work/(scenario+'.trace')).read_text().splitlines()
                self.assertIn('stop',trace)
                self.assertEqual(trace[-1],'close')
                self.assertTrue(r.stderr)
                if scenario in ('sigint','sigterm','bad_tail','disconnect','disconnect_stop','stale','disabled'):
                    self.assertTrue(any(row['schema']=='xtos.eeg.barrier.v1' for row in rows))
                if scenario=='disconnect_stop':self.assertIn('STOP failed',r.stderr)

    def test_real_broken_pipe_still_stops_and_closes(self):
        args=['--device','/dev/xtos-export-fixture','--session-id','9',
              '--window','4','--hop','2','--max-age-ns','1000000000',
              '--duration-ms','200','--max-windows','1']
        trace_path=self.work/'broken_pipe.trace'
        env={**os.environ,'EXPORT_SCENARIO':'complete','EXPORT_TRACE':str(trace_path)}
        reader,writer=os.pipe()
        os.close(reader)
        try:
            r=subprocess.run([str(self.pipe_binary),*args],stdout=writer,
                             stderr=subprocess.PIPE,text=True,env=env,timeout=4,
                             restore_signals=True)
        finally:
            os.close(writer)
        with (self.work/'commands.jsonl').open('a') as log:
            log.write(json.dumps({'command':[str(self.pipe_binary),*args],
                                  'stdout':'real pipe with read end closed before spawn',
                                  'environment':{k:v for k,v in env.items() if k.startswith(('EXPORT_','ASAN_','UBSAN_'))},
                                  'restore_signals':True,'returncode':r.returncode,
                                  'stderr':r.stderr})+'\n')
        self.assertEqual(r.returncode,5,r.stderr)
        self.assertIn('output_failed',r.stderr)
        trace=trace_path.read_text().splitlines()
        self.assertEqual(trace.count('start'),1)
        self.assertEqual(trace.count('frame'),4)
        self.assertEqual(trace.count('stop'),1)
        self.assertEqual(trace[-1],'close')

    def test_unread_pipe_obeys_duration_and_cancellation(self):
        import array
        import fcntl
        import signal
        import termios
        import time

        # Hold the actual read end open without consuming anything. Observe
        # exit and owned STOP/close BEFORE test cleanup can unblock the writer.
        for cancel,prefilled in ((False,False),(True,False),(False,True),(True,True)):
            with self.subTest(cancel=cancel,prefilled=prefilled):
                name=('full_' if prefilled else '')+('unread_cancel' if cancel else 'unread_duration')
                trace_path=self.work/(name+'.trace')
                env={**os.environ,'EXPORT_SCENARIO':'complete','EXPORT_TRACE':str(trace_path)}
                args=['--device','/dev/xtos-export-fixture','--session-id','9',
                      '--window','2','--hop','1','--max-age-ns','1000000000',
                      '--duration-ms','5000' if cancel else '100',
                      '--max-windows','100000']
                reader,writer=os.pipe()
                capacity=fcntl.fcntl(writer,fcntl.F_SETPIPE_SZ,4096)
                if prefilled:
                    self.assertEqual(os.write(writer,b'x'*capacity),capacity)
                started=time.monotonic()
                process=subprocess.Popen([str(self.pipe_binary),*args],stdout=writer,
                                         stderr=subprocess.PIPE,env=env,restore_signals=True)
                assert process.stderr is not None
                os.close(writer)
                exited=False
                try:
                    # Synchronize SIGTERM with a genuinely stalled pipe. Linux
                    # refuses another <= PIPE_BUF record with < PIPE_BUF space.
                    queued=array.array('i',[0])
                    ready_by=time.monotonic()+.5
                    events=[]
                    while time.monotonic()<ready_by:
                        fcntl.ioctl(reader,termios.FIONREAD,queued,True)
                        events=trace_path.read_text().splitlines() if trace_path.exists() else []
                        if queued[0]>=capacity-1024 and events.count('frame')>=(2 if prefilled else 8):
                            break
                        if process.poll() is not None:
                            break
                        time.sleep(.001)
                    self.assertGreaterEqual(queued[0],capacity-1024,'fixture never filled the real pipe')
                    self.assertGreaterEqual(events.count('frame'),2 if prefilled else 8)
                    if prefilled:
                        self.assertEqual(queued[0],capacity)
                    if cancel:
                        process.send_signal(signal.SIGTERM)
                    try:
                        process.wait(timeout=.75)
                        exited=True
                    except subprocess.TimeoutExpired:
                        pass
                    events=trace_path.read_text().splitlines()
                    observed={'command':[str(self.pipe_binary),*args], 'cancel':cancel,'prefilled':prefilled,
                              'pipe_capacity':capacity,'queued_before_signal':queued[0],
                              'exited_before_test_cleanup':exited,
                              'returncode':process.poll(),'elapsed_s':time.monotonic()-started,
                              'trace_before_test_cleanup':events}
                    with (self.work/'commands.jsonl').open('a') as log:
                        log.write(json.dumps(observed)+'\n')
                    self.assertTrue(exited,'unread full pipe exceeded duration/cancel bound before cleanup')
                    self.assertNotEqual(process.returncode,0)
                    self.assertEqual(events.count('stop'),1)
                    self.assertEqual(events[-1],'close')
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=2)
                    stderr=process.stderr.read().decode()
                    process.stderr.close()
                    os.close(reader)
                    with (self.work/'commands.jsonl').open('a') as log:
                        log.write(json.dumps({'scenario':name,'stderr':stderr,
                                              'cleanup_kill_required':not exited})+'\n')

    def test_stdout_full(self):
        r=self.command('full',full=True)
        self.assertEqual(r.returncode,5,r.stderr)
        self.assertIn('output',r.stderr)
        trace=(self.work/'full.trace').read_text().splitlines()
        self.assertIn('stop',trace)
        self.assertEqual(trace[-1],'close')

    def test_main_complete(self):
        for scenario in ('complete','serdev','eagain','mask','gap'):
            with self.subTest(scenario=scenario):
                r=self.command(scenario)
                self.assertEqual(r.returncode,0,r.stderr)
                rows=[json.loads(line) for line in r.stdout.splitlines()]
                windows=[f for f in rows if f['schema']=='xtos.eeg.window.v1']
                self.assertEqual(len(windows),2)
                self.assertEqual(rows[-1]['status'],'complete')
                self.assertEqual(windows[0]['session_id'],2**64-1)
                self.assertEqual(windows[0]['source_kind'],'serdev' if scenario=='serdev' else 'synthetic')
                self.assertEqual(windows[0]['values'],([0]*7+[8])*2 if scenario=='mask' else list(range(1,9))*2)
                self.assertEqual(windows[1]['first_sequence']-windows[0]['first_sequence'],2)
                for row in rows:
                    self.assertEqual(row['safe_action'],'no_action')
                    self.assertIs(row['physical_acceptance'],False)
                trace=(self.work/(scenario+'.trace')).read_text().splitlines()
                self.assertEqual(trace.count('open'),1)
                self.assertEqual(trace.count('start'),1)
                self.assertEqual(trace.count('stop'),1)
                self.assertEqual(trace[-1],'close')
                if scenario=='gap':
                    self.assertEqual(rows[0]['schema'],'xtos.eeg.barrier.v1')

    def test_options_no_open(self):
        valid=['--device','/dev/xtos-export-fixture','--session-id','1','--window','4','--hop','2','--max-age-ns','1000000000','--duration-ms','30','--max-windows','2']
        bad=[[],valid+['--extra','1'],valid+['--window','4'],valid[:-2]]
        for i,values in ((1,('relative','')), (3,('0','-1','+1',' 1','1x','18446744073709551616')), (5,('1','4097')), (7,('0','5')), (9,('0',)),(11,('0','600001')), (13,('0','100001'))):
            for value in values:
                args=valid.copy();args[i]=value;bad.append(args)
        for args in bad:
            r=self.command('options',args)
            self.assertEqual(r.returncode,2,r.stderr)
            self.assertEqual(r.stdout,'')
        self.assertFalse((self.work/'options.trace').exists())

    def test_serializer_invalid_atomic(self):
        r = run_logged([str(self.serializer),'invalid'],self.work)
        self.assertEqual(r.returncode,0,r.stderr)

    def test_serializer_golden(self):
        r = run_logged([str(self.serializer),'golden'],self.work)
        self.assertEqual(r.returncode,0,r.stderr)
        f = json.loads(r.stdout)
        self.assertEqual(f['session_id'],2**64-1)
        self.assertEqual(f['values'],list(range(1,9))*2)
        self.assertEqual(f['source_kind'],'synthetic')
        self.assertEqual(f['safe_action'],'no_action')
        self.assertIs(f['physical_acceptance'],False)

if __name__ == '__main__':
    unittest.main(verbosity=2)
