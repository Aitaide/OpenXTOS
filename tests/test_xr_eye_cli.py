"""Headless CLI contract against the frozen OpenXR adapter and a copied ABI fixture."""
import difflib
import fcntl
import json
import os
import signal
import time
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
XR = ROOT / 'tools/xtos-xr'
EVIDENCE = Path(os.environ.get('XTOS_XR_TEST_OUTPUT',
                             ROOT / '.build/remaining-closure-v2/xr-eye-cli-implement'))


def fixture_copy(out):
    original = (XR/'tests/native/fixture_eye_runtime.c').read_text()
    changes = [
        ('if(!current_session || events>2) return XR_EVENT_UNAVAILABLE;',
         'if(!current_session || events>2 || (events==2 && !exit_requests) || (mode==18 && events==1 && !exit_requests) || (mode==21 && events==2)) return XR_EVENT_UNAVAILABLE;'),
        ('events==0?XR_SESSION_STATE_READY:(events==1?XR_SESSION_STATE_FOCUSED:XR_SESSION_STATE_STOPPING)',
         'events==0?XR_SESSION_STATE_READY:(events==1 && mode==18 && exit_requests?XR_SESSION_STATE_STOPPING:(events==1?XR_SESSION_STATE_FOCUSED:XR_SESSION_STATE_STOPPING))'),
        ('if(mode==8)return XR_ERROR_RUNTIME_FAILURE;',
         'if(mode==8)return XR_ERROR_RUNTIME_FAILURE;'),
        ('FAIL_IF(i!=H(XrInstance,1) || sessions!=session_destroys || spaces!=space_destroys || actions!=action_destroys || sets!=set_destroys);\n instance_destroys++;',
         'FAIL_IF(i!=H(XrInstance,1) || sessions!=session_destroys || spaces!=space_destroys || actions!=action_destroys || sets!=set_destroys);\n if(mode==20)return XR_ERROR_RUNTIME_FAILURE;\n instance_destroys++;'),
        ('conversion_calls++;out->tv_sec=0;out->tv_nsec=(long)(t+conversion_calls*1000);return XR_SUCCESS;',
         'conversion_calls++;out->tv_sec=(time_t)(t/1000000000);out->tv_nsec=(long)(t%1000000000);return XR_SUCCESS;'),
        ('locate_calls++;sample->time=mode==15?0:123456;',
         'if(mode==19)return XR_ERROR_RUNTIME_FAILURE;\n locate_calls++;sample->time=mode==15?0:time-1000;\n if(locate_calls==1){const char *marker=getenv("XTOS_XR_FIXTURE_MARKER");if(marker){FILE *f=fopen(marker,"w");if(f){fputs("located",f);fclose(f);}}}'),
    ]
    modified = original.replace('#include <math.h>', '#include <math.h>\n#include <stdio.h>')
    for old,new in changes:
        if modified.count(old) != 1:
            raise AssertionError('CLI fixture seam changed: ' + old)
        modified=modified.replace(old,new)
    modified += r'''
#include <stdio.h>
__attribute__((constructor)) static void cli_fixture_init(void) {
 const char *s=getenv("XTOS_XR_FIXTURE_MODE"); if(s && *s) eye_reset(atoi(s));
}
__attribute__((destructor)) static void cli_fixture_audit(void) {
 const char *p=getenv("XTOS_XR_FIXTURE_AUDIT"); if(!p || !*p)return;
 FILE *f=fopen(p,"w"); if(!f)return;
 fprintf(f,"{\"instances\":%d,\"instance_destroys\":%d,\"sessions\":%d,\"session_destroys\":%d,\"begin_calls\":%d,\"end_calls\":%d,\"exit_requests\":%d,\"locate_calls\":%d}\n",instances,instance_destroys,sessions,session_destroys,begin_calls,end_calls,exit_requests,locate_calls);
 fclose(f);
}
'''
    path=out/'fixture_eye_runtime_cli.c'
    path.write_text(modified)
    (out/'fixture.diff').write_text(''.join(difflib.unified_diff(original.splitlines(True),modified.splitlines(True),fromfile='frozen/fixture_eye_runtime.c',tofile='copy/fixture_eye_runtime_cli.c')))
    return path


class EyeCLITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        EVIDENCE.mkdir(parents=True,exist_ok=True)
        cls.out=Path(tempfile.mkdtemp(prefix='cli-test-',dir=EVIDENCE))
        cls.env=dict(os.environ,LC_ALL='C')
        flags=['-std=c11','-Wall','-Wextra','-Werror','-g','-D_POSIX_C_SOURCE=200809L','-DXR_NO_PROTOTYPES','-DXR_USE_TIMESPEC']
        if os.environ.get('XTOS_XR_OPTIMIZED')=='1': flags += ['-O2']
        if os.environ.get('XTOS_XR_SANITIZE')=='1':
            flags += ['-fsanitize=address,undefined','-fno-sanitize-recover=all','-fno-omit-frame-pointer','-no-pie']
            cls.env.update(ASAN_OPTIONS='detect_leaks=1:halt_on_error=1',UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1')
        includes=['-I'+str(XR/'include'),'-I'+str(XR/'vendor/openxr/include')]
        fixture=fixture_copy(cls.out)
        cls.fixture_cmd=['cc',*flags,*includes,'-shared','-fPIC',str(fixture),'-o',str(cls.out/'simulated.so')]
        cls.cli_cmd=['cc',*flags,*includes,str(XR/'src/runtime_adapter.c'),str(XR/'src/eye_session.c'),str(XR/'src/xr_eye_probe.c'),'-ldl','-lm','-o',str(cls.out/'xr-eye-probe')]
        cls.fixture_build=subprocess.run(cls.fixture_cmd,capture_output=True,text=True,env=cls.env)
        cls.cli_build=subprocess.run(cls.cli_cmd,capture_output=True,text=True,env=cls.env)
        for label,cmd,result in [('fixture',cls.fixture_cmd,cls.fixture_build),('cli',cls.cli_cmd,cls.cli_build)]:
            (cls.out/(label+'-build.json')).write_text(json.dumps({'command':cmd,'exit':result.returncode,'stdout':result.stdout,'stderr':result.stderr},indent=2))

    def test_entry_compiles(self):
        self.assertEqual(self.fixture_build.returncode,0,self.fixture_build.stderr)
        self.assertEqual(self.cli_build.returncode,0,self.cli_build.stderr)

    def test_requires_privacy_consent_before_loader(self):
        p=self.invoke('--loader',self.loader)
        self.assertNotEqual(p.returncode,0)
        self.assertEqual(p.stdout,'')
        self.assertIn('accept-eye-data',p.stderr)

    def test_duplicate_and_malformed_arguments_rejected_before_loader(self):
        for args in [('--loader',self.loader,'--loader',self.loader),
                     ('--loader',self.loader,'--max-samples','1x'),
                     ('--loader',self.loader,'--duration-ms','0'),
                     ('--loader','relative.so')]:
            with self.subTest(args=args):
                p=self.invoke('--accept-eye-data',*args)
                self.assertNotEqual(p.returncode,0)
                self.assertIn('invalid arguments',p.stderr)
                self.assertEqual(p.stdout,'')

    def test_simulated_headless_capture_has_no_action_and_separate_times(self):
        p=self.invoke('--loader',self.loader,'--accept-eye-data','--simulated',
                      '--max-samples','2','--duration-ms','100')
        self.assertEqual(p.returncode,0,p.stderr)
        result=json.loads(p.stdout)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['source_kind'],'simulated')
        self.assertEqual(result['safe_action'],'no_action')
        self.assertIs(result['physical_acceptance'],False)
        self.assertEqual(result['sample_count'],2)
        self.assertEqual(len(result['samples']),2)
        self.assertEqual(result['pose_time_provenance'],'runtime_reported_unknown_prediction_or_interpolation')
        for sample in result['samples']:
            self.assertGreater(sample['query_host_ns'],0)
            self.assertGreater(sample['query_xr_time'],0)
            self.assertGreater(sample['sample_xr_time'],0)
            self.assertGreater(sample['converted_host_ns'],0)
            self.assertNotEqual(sample['query_xr_time'],sample['sample_xr_time'])
            self.assertEqual(len(sample['pose']['position']),3)
            self.assertEqual(len(sample['pose']['orientation']),4)
        self.assertNotIn('predicted',p.stdout)
        self.assertEqual(p.stdout.count('\n'),1)

    def test_nonfocused_runtime_times_out_without_sample(self):
        p=self.invoke('--loader',self.loader,'--accept-eye-data',
                      '--duration-ms','20',mode='18')
        self.assertNotEqual(p.returncode,0)
        data=json.loads(p.stdout)
        self.assertEqual(data['status'],'no_sample')
        self.assertEqual(data['source_kind'],'runtime_unverified')
        self.assertEqual(data['sample_count'],0)
        self.assertEqual(data['samples'],[])
        self.assertEqual(data['safe_action'],'no_action')

    def test_inactive_and_invalid_pose_never_publish(self):
        for mode in ('12','13','14','15','16'):
            with self.subTest(mode=mode):
                p=self.invoke('--loader',self.loader,'--accept-eye-data',
                              '--duration-ms','15',mode=mode)
                self.assertNotEqual(p.returncode,0,p.stderr)
                data=json.loads(p.stdout)
                self.assertEqual(data['status'],'no_sample')
                self.assertEqual(data['sample_count'],0)
                self.assertEqual(data['samples'],[])

    def test_cleanup_failure_never_reports_complete(self):
        audit=self.out/'cleanup-failed-audit.json'
        p=self.invoke('--loader',self.loader,'--accept-eye-data','--simulated',
                      '--duration-ms','20',mode='8',audit=audit)
        self.assertNotEqual(p.returncode,0)
        data=json.loads(p.stdout)
        self.assertEqual(data['status'],'cleanup_failed')
        self.assertEqual(data['safe_action'],'no_action')
        counters=json.loads(audit.read_text())
        self.assertEqual(counters['session_destroys'],0)
        self.assertEqual(counters['instance_destroys'],0)
        self.assertEqual(counters['exit_requests'],1)
        self.assertEqual(counters['end_calls'],1)

    def test_success_and_runtime_fault_close_all_children(self):
        for mode in ('0','19'):
            with self.subTest(mode=mode):
                audit=self.out/('cleanup-'+mode+'.json')
                p=self.invoke('--loader',self.loader,'--accept-eye-data',
                              '--max-samples','1','--duration-ms','30',mode=mode,audit=audit)
                data=json.loads(p.stdout)
                self.assertEqual(p.returncode,0 if mode=='0' else 1)
                self.assertEqual(data['status'],'complete' if mode=='0' else 'runtime_error')
                self.assertEqual(data['safe_action'],'no_action')
                counters=json.loads(audit.read_text())
                self.assertEqual(counters['sessions'],counters['session_destroys'])
                self.assertEqual(counters['instances'],counters['instance_destroys'])
                self.assertEqual(counters['begin_calls'],1)
                self.assertEqual(counters['exit_requests'],0 if mode=='19' else 1)

    def test_missing_stopping_event_refuses_complete(self):
        audit=self.out/'missing-stopping-audit.json'
        p=self.invoke('--loader',self.loader,'--accept-eye-data',
                      '--max-samples','1','--duration-ms','30',mode='21',audit=audit)
        self.assertNotEqual(p.returncode,0)
        data=json.loads(p.stdout)
        self.assertEqual(data['status'],'cleanup_failed')
        counters=json.loads(audit.read_text())
        self.assertEqual(counters['exit_requests'],1)
        self.assertEqual(counters['session_destroys'],0)
        self.assertEqual(counters['instance_destroys'],0)

    def test_stdout_flush_failure_is_nonzero(self):
        with open('/dev/full','w') as sink:
            p=self.invoke('--loader',self.loader,'--accept-eye-data',
                          '--duration-ms','20',stdout=sink)
        self.assertNotEqual(p.returncode,0)
        self.assertIn('stdout write or flush failed',p.stderr)

    def test_full_unread_stderr_does_not_block_output_failure_exit(self):
        reader, writer = os.pipe()
        child = None
        audit = self.out / 'full-stderr-audit.json'
        try:
            capacity = fcntl.fcntl(writer, fcntl.F_SETPIPE_SZ, 4096)
            self.assertEqual(capacity, 4096)
            self.assertEqual(os.write(writer, b'x' * capacity), capacity)
            original_flags = fcntl.fcntl(writer, fcntl.F_GETFL)
            command = [str(self.out / 'xr-eye-probe'), '--loader', self.loader,
                       '--accept-eye-data', '--max-samples', '1', '--duration-ms', '30']
            started = time.monotonic()
            with open('/dev/full', 'wb') as sink:
                child = subprocess.Popen(command, stdout=sink, stderr=writer,
                    env=dict(self.env, XTOS_XR_FIXTURE_MODE='0',
                             XTOS_XR_FIXTURE_AUDIT=str(audit)))
            timed_out = False
            try:
                child.wait(timeout=.75)
            except subprocess.TimeoutExpired:
                timed_out = True
            counters = json.loads(audit.read_text()) if audit.exists() else None
            blocked_at = None
            if timed_out:
                try:
                    blocked_at = (Path('/proc') / str(child.pid) / 'wchan').read_text().strip()
                except OSError:
                    pass
            record = dict(command=command, stdout='/dev/full',
                stderr='full unread pipe', pipe_capacity=capacity, prefilled_bytes=capacity,
                elapsed_seconds=time.monotonic() - started, exit_before_intervention=child.poll(),
                timed_out=timed_out, blocked_at=blocked_at, cleanup_before_intervention=counters)
            (self.out / 'full-stderr-execution.json').write_text(json.dumps(record, indent=2))
            self.assertFalse(timed_out, 'full stderr blocked exit after runtime cleanup: ' + str(record))
            self.assertEqual(child.returncode, 1)
            if counters is None:
                self.fail('loader cleanup audit missing')
            self.assertEqual(counters['sessions'], counters['session_destroys'])
            self.assertEqual(counters['instances'], counters['instance_destroys'])
            self.assertEqual(counters['sessions'], 1)
            self.assertEqual(counters['locate_calls'], 1)
            self.assertEqual(fcntl.fcntl(writer, fcntl.F_GETFL), original_flags)
            self.assertEqual(os.read(reader, capacity), b'x' * capacity)
        finally:
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=2)
            os.close(writer)
            os.close(reader)

    def test_signal_after_sample_reports_interrupted_and_cleans(self):
        for sig in (signal.SIGTERM,signal.SIGINT):
            with self.subTest(sig=sig):
                marker=self.out/('marker-'+str(sig.value))
                audit=self.out/('signal-'+str(sig.value)+'.json')
                child=subprocess.Popen([str(self.out/'xr-eye-probe'),'--loader',self.loader,
                                        '--accept-eye-data','--max-samples','64','--duration-ms','5000'],
                                       stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                                       env=dict(self.env,XTOS_XR_FIXTURE_MODE='0',
                                                XTOS_XR_FIXTURE_MARKER=str(marker),XTOS_XR_FIXTURE_AUDIT=str(audit)))
                try:
                    deadline=time.monotonic()+2
                    while not marker.exists() and child.poll() is None and time.monotonic()<deadline:
                        time.sleep(.001)
                    self.assertTrue(marker.exists(),'sample locate never happened')
                    child.send_signal(sig)
                    out,err=child.communicate(timeout=3)
                    self.assertNotEqual(child.returncode,0,err)
                    data=json.loads(out)
                    self.assertEqual(data['status'],'interrupted')
                    self.assertGreater(data['sample_count'],0)
                    self.assertEqual(data['safe_action'],'no_action')
                    counters=json.loads(audit.read_text())
                    self.assertEqual(counters['sessions'],counters['session_destroys'])
                    self.assertEqual(counters['instances'],counters['instance_destroys'])
                finally:
                    if child.poll() is None: child.kill();child.communicate(timeout=3)

    def test_instance_destroy_failure_remains_nonzero(self):
        audit=self.out/'instance-destroy-failed-audit.json'
        p=self.invoke('--loader',self.loader,'--accept-eye-data',
                      '--max-samples','1','--duration-ms','30',mode='20',audit=audit)
        self.assertNotEqual(p.returncode,0)
        data=json.loads(p.stdout)
        self.assertEqual(data['status'],'cleanup_failed')
        self.assertEqual(data['safe_action'],'no_action')
        counters=json.loads(audit.read_text())
        self.assertEqual(counters['sessions'],counters['session_destroys'])
        self.assertEqual(counters['instance_destroys'],0)

    def test_maximum_sample_bound_is_64(self):
        p=self.invoke('--loader',self.loader,'--accept-eye-data','--simulated',
                      '--max-samples','64','--duration-ms','1000')
        self.assertEqual(p.returncode,0,p.stderr)
        data=json.loads(p.stdout)
        self.assertEqual(data['sample_count'],64)
        self.assertEqual(len(data['samples']),64)

    def test_partial_sample_count_is_not_complete(self):
        p = self.invoke('--loader', self.loader, '--accept-eye-data',
                        '--max-samples', '64', '--duration-ms', '10')
        self.assertNotEqual(p.returncode, 0, p.stdout)
        result = json.loads(p.stdout)
        self.assertGreater(result['sample_count'], 0)
        self.assertLess(result['sample_count'], 64)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['safe_action'], 'no_action')
        self.assertIs(result['physical_acceptance'], False)

    def test_unread_pipe_output_obeys_duration_and_cleans_before_exit(self):
        for prefilled in (False, True):
            with self.subTest(prefilled=prefilled):
                reader, writer = os.pipe()
                fcntl.fcntl(writer, fcntl.F_SETPIPE_SZ, 4096)
                if prefilled:
                    os.set_blocking(writer, False)
                    while True:
                        try:
                            os.write(writer, b'x' * 4096)
                        except BlockingIOError:
                            break
                    os.set_blocking(writer, True)
                audit = self.out / ('backpressure-' + str(prefilled) + '.json')
                child = None
                try:
                    child = subprocess.Popen([str(self.out/'xr-eye-probe'),
                        '--loader', self.loader, '--accept-eye-data', '--max-samples', '64',
                        '--duration-ms', '600'], stdout=writer, stderr=subprocess.PIPE,
                        env=dict(self.env, XTOS_XR_FIXTURE_MODE='0', XTOS_XR_FIXTURE_AUDIT=str(audit)))
                    try:
                        _, stderr = child.communicate(timeout=1.5)
                    except subprocess.TimeoutExpired:
                        self.fail('stdout backpressure escaped duration; child cleanup not complete')
                    self.assertNotEqual(child.returncode, 0, stderr)
                    counters = json.loads(audit.read_text())
                    self.assertEqual(counters['sessions'], counters['session_destroys'])
                    self.assertEqual(counters['instances'], counters['instance_destroys'])
                    self.assertIn(b'stdout', stderr)
                finally:
                    if child is not None and child.poll() is None:
                        child.kill()
                        child.communicate(timeout=2)
                    os.close(writer)
                    os.close(reader)

    def test_sigterm_during_blocked_output_exits_without_draining(self):
        reader, writer = os.pipe()
        fcntl.fcntl(writer, fcntl.F_SETPIPE_SZ, 4096)
        os.write(writer, b'x' * 4096)
        audit = self.out / 'blocked-sigterm-audit.json'
        child = subprocess.Popen([str(self.out/'xr-eye-probe'), '--loader', self.loader,
            '--accept-eye-data', '--max-samples', '1', '--duration-ms', '5000'],
            stdout=writer, stderr=subprocess.PIPE,
            env=dict(self.env, XTOS_XR_FIXTURE_MODE='0', XTOS_XR_FIXTURE_AUDIT=str(audit)))
        try:
            deadline = time.monotonic() + 2
            while not audit.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.001)
            self.assertTrue(audit.exists(), 'cleanup did not reach loader release before blocked output')
            self.assertIsNone(child.poll(), 'fixture did not hold the output path')
            child.send_signal(signal.SIGTERM)
            _, stderr = child.communicate(timeout=.75)
            self.assertNotEqual(child.returncode, 0)
            counters = json.loads(audit.read_text())
            self.assertEqual(counters['sessions'], counters['session_destroys'])
            self.assertEqual(counters['instances'], counters['instance_destroys'])
            self.assertIn(b'stdout', stderr)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=2)
            os.close(writer)
            os.close(reader)

    def test_slow_reader_resuming_before_deadline_gets_complete_json(self):
        reader, writer = os.pipe()
        fcntl.fcntl(writer, fcntl.F_SETPIPE_SZ, 4096)
        os.write(writer, b'x' * 4096)
        audit = self.out / 'slow-reader-audit.json'
        child = subprocess.Popen([str(self.out/'xr-eye-probe'), '--loader', self.loader,
            '--accept-eye-data', '--max-samples', '64', '--duration-ms', '1500'],
            stdout=writer, stderr=subprocess.PIPE,
            env=dict(self.env, XTOS_XR_FIXTURE_MODE='0', XTOS_XR_FIXTURE_AUDIT=str(audit)))
        os.close(writer)
        try:
            deadline = time.monotonic() + 2
            while not audit.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.001)
            self.assertTrue(audit.exists(), 'cleanup never reached summary output')
            self.assertIsNone(child.poll(), 'prefilled pipe did not exercise output backpressure')
            os.set_blocking(reader, False)
            data = bytearray()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    chunk = os.read(reader, 65536)
                except BlockingIOError:
                    time.sleep(.001)
                    continue
                if not chunk:
                    break
                data.extend(chunk)
            _, stderr = child.communicate(timeout=.5)
            self.assertEqual(child.returncode, 0, stderr)
            self.assertEqual(data[:4096], b'x' * 4096)
            result = json.loads(data[4096:])
            self.assertEqual(result['sample_count'], 64)
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(result['safe_action'], 'no_action')
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=2)
            os.close(reader)

    def test_closed_output_pipe_is_reported_after_device_cleanup(self):
        reader, writer = os.pipe()
        os.close(reader)
        audit = self.out / 'closed-output-audit.json'
        child = subprocess.Popen([str(self.out/'xr-eye-probe'), '--loader', self.loader,
            '--accept-eye-data', '--max-samples', '1', '--duration-ms', '50'],
            stdout=writer, stderr=subprocess.PIPE,
            env=dict(self.env, XTOS_XR_FIXTURE_MODE='0', XTOS_XR_FIXTURE_AUDIT=str(audit)))
        os.close(writer)
        try:
            _, stderr = child.communicate(timeout=2)
            self.assertNotEqual(child.returncode, 0)
            self.assertIn(b'stdout', stderr)
            counters = json.loads(audit.read_text())
            self.assertEqual(counters['sessions'], counters['session_destroys'])
            self.assertEqual(counters['instances'], counters['instance_destroys'])
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=2)

    def invoke(self,*args,mode='0',stdout=None,audit=None):
        self.assertEqual(self.cli_build.returncode,0,self.cli_build.stderr)
        argv=[str(self.out/'xr-eye-probe'),*args]
        return subprocess.run(argv,capture_output=stdout is None,text=True,stdout=stdout,stderr=subprocess.PIPE if stdout else None,
                              env=dict(self.env,XTOS_XR_FIXTURE_MODE=str(mode),
                                       **({'XTOS_XR_FIXTURE_AUDIT':str(audit)} if audit else {})),timeout=8)

    @property
    def loader(self): return str(self.out/'simulated.so')

if __name__=='__main__': unittest.main()
