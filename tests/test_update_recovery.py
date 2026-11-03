#!/usr/bin/env python3
"""Real native update/recover, with block identity simulated at the OS seam.

Every target open maps the single synthetic device to an owned regular file.
This establishes restart/retry logic, not physical block or power-loss safety.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'tools/xtos-native/update/update.c'
DEVICE = '/dev/loop938271'

WRAPPER = r'''
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/fs.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>
static unsigned char targets[4096];
static long written;
static const char *device = "/dev/loop938271";
int __real_open(const char *, int, ...);
int __wrap_open(const char *p, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) { va_list a; va_start(a, flags); mode = va_arg(a, int); va_end(a); }
    int target = strcmp(p, device) == 0;
    int fd = __real_open(target ? getenv("FIXTURE_TARGET") : p, flags, mode);
    if (fd >= 0 && fd < 4096) targets[fd] = target;
    return fd;
}
int __real_fstat(int, struct stat *);
int __real_close(int);
int __wrap_close(int fd) {
    if (fd >= 0 && fd < 4096) targets[fd] = 0;
    return __real_close(fd);
}
int __wrap_fstat(int fd, struct stat *st) {
    int rc = __real_fstat(fd, st);
    if (!rc && fd >= 0 && fd < 4096 && targets[fd]) {
        st->st_mode = (st->st_mode & 0777) | S_IFBLK; st->st_rdev = makedev(240, 255);
    }
    return rc;
}
int __real_stat(const char *, struct stat *);
int __wrap_stat(const char *p, struct stat *st) {
    if (strcmp(p, device) == 0) {
        int rc = __real_stat(getenv("FIXTURE_TARGET"), st);
        if (!rc) { st->st_mode = (st->st_mode & 0777) | S_IFBLK; st->st_rdev = makedev(240,255); }
        return rc;
    }
    if (strcmp(p, "/sys/class/block/loop938271/partition") == 0) { errno=ENOENT; return -1; }
    return __real_stat(p, st);
}
char *__real_realpath(const char *, char *);
char *__wrap_realpath(const char *p, char *out) {
    if (strcmp(p, device) == 0) { if (!out) out=malloc(strlen(device)+1); if (out) strcpy(out,device); return out; }
    return __real_realpath(p,out);
}
DIR *__real_opendir(const char *);
DIR *__wrap_opendir(const char *p) {
    return __real_opendir(strcmp(p,"/sys/class/block")==0 ? getenv("FIXTURE_SYSFS") : p);
}
FILE *__real_fopen(const char *, const char *);
FILE *__wrap_fopen(const char *p, const char *mode) {
    if (strcmp(p,"/sys/class/block/loop938271/dev")==0) {
        char b[4096]; snprintf(b,sizeof(b),"%s/loop938271/dev",getenv("FIXTURE_SYSFS"));
        return __real_fopen(b,mode);
    }
    return __real_fopen(p,mode);
}
int __real_ioctl(int, unsigned long, ...);
int __wrap_ioctl(int fd, unsigned long request, ...) {
    va_list a; va_start(a,request); void *p=va_arg(a,void *); va_end(a);
    if (fd>=0 && fd<4096 && targets[fd] && request==BLKGETSIZE64) {
        struct stat st; if (__real_fstat(fd,&st)) return -1;
        *(unsigned long long *)p=(unsigned long long)st.st_size;
        if (getenv("FIXTURE_REPLACE_TARGET")) {
            const char *target=getenv("FIXTURE_TARGET"); char moved[4096];
            snprintf(moved,sizeof(moved),"%s.retained",target);
            if (rename(target,moved)) _exit(95);
            int replacement=__real_open(target,O_CREAT|O_EXCL|O_WRONLY,0600);
            const char sentinel[]="CALLER_REPLACEMENT_MUST_SURVIVE";
            if (replacement<0 || write(replacement,sentinel,sizeof(sentinel)-1)!=(ssize_t)(sizeof(sentinel)-1)) _exit(96);
            __real_close(replacement);
            unsetenv("FIXTURE_REPLACE_TARGET");
        }
        return 0;
    }
    return __real_ioctl(fd,request,p);
}
ssize_t __real_write(int,const void *,size_t);
ssize_t __wrap_write(int fd,const void *p,size_t n) {
    const char *fail=getenv("FIXTURE_FAIL_AFTER");
    if (fail && fd>=0 && fd<4096 && targets[fd]) {
        long limit=strtol(fail,NULL,10);
        if (written>=limit) { errno=EIO; return -1; }
        if (n>(size_t)(limit-written)) n=(size_t)(limit-written);
        ssize_t rc=__real_write(fd,p,n); if (rc>0) written+=rc; return rc;
    }
    return __real_write(fd,p,n);
}
'''


class UpdateRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='update-recovery-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.base = Path(cls.temporary.name)
        cls.key = cls.base / 'test-key.pem'
        cls.pub = cls.base / 'test-pub.pem'
        for cmd in [
            ['openssl', 'genpkey', '-algorithm', 'ed25519', '-out', str(cls.key)],
            ['openssl', 'pkey', '-in', str(cls.key), '-pubout', '-out', str(cls.pub)],
        ]:
            subprocess.run(cmd, check=True, capture_output=True)
        spec = importlib.util.spec_from_file_location('recovery_compile', ROOT / 'tests/test_update_apply_security.py')
        if spec is None or spec.loader is None:
            raise RuntimeError('native update compiler helper missing')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        wrapper = cls.base / 'syscalls.c'
        wrapper.write_text(WRAPPER)
        cls.binary = cls.base / 'update'
        cls.compile_command = module.compile_update(cls.binary, [str(wrapper), *[
            '-Wl,--wrap=' + n for n in ['open', 'close', 'fstat', 'stat', 'realpath', 'opendir', 'fopen', 'ioctl', 'write']]])

    def setUp(self):
        self.scene = tempfile.TemporaryDirectory(dir=self.base)
        self.addCleanup(self.scene.cleanup)
        self.root = Path(self.scene.name)
        self.download = self.root / 'download'
        self.download.mkdir()
        self.payload = b'AUTHENTICATED-BOOT-IMAGE\0' * 4000
        (self.download / 'installer.iso').write_bytes(self.payload)
        self.hash = hashlib.sha256(self.payload).hexdigest()
        self.index = self.download / 'release-index.json'
        self.index.write_text(json.dumps({
            'schema_version': 1, 'kind': 'xtos-release-index',
            'latest': {'version': '1.2.0', 'channel': 'prerelease', 'artifacts': [{
                'arch': 'x86_64', 'kind': 'installer-iso', 'name': 'installer.iso',
                'size': len(self.payload), 'sha256': self.hash, 'url': 'https://example.invalid/image.iso'}]}
        }, indent=2, sort_keys=True) + '\n')
        subprocess.run(['openssl', 'pkeyutl', '-sign', '-inkey', str(self.key), '-rawin',
                        '-in', str(self.index), '-out', str(self.index) + '.sig'], check=True, capture_output=True)
        self.target = self.root / 'spare-media.regular'
        self.target.write_bytes(b'OLD_BOOTABLE_SPARE_' * 10000)
        self.original = self.target.read_bytes()
        self.sysfs = self.root / 'sysfs'
        (self.sysfs / 'loop938271').mkdir(parents=True)
        (self.sysfs / 'loop938271/dev').write_text('240:255\n')
        self.journal = self.download / 'apply-journal.json'
        self.environment = {**os.environ, 'XTOS_UPDATE_PUBKEY': str(self.pub),
            'XTOS_UPDATE_ARCH': 'x86_64', 'FIXTURE_TARGET': str(self.target),
            'FIXTURE_SYSFS': str(self.sysfs)}

    def run_update(self, command, fail_after=None, extra=None):
        env = dict(self.environment)
        if extra:
            env.update(extra)
        if fail_after is not None:
            env['FIXTURE_FAIL_AFTER'] = str(fail_after)
        return subprocess.run([str(self.binary), command, '--from', str(self.download), '--to', DEVICE,
                               '--confirm', 'ERASE-XTOS'], capture_output=True, text=True, env=env, timeout=10)

    def interrupted(self):
        result = self.run_update('apply', fail_after=2048)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('write to', result.stderr)
        self.assertEqual(json.loads(self.journal.read_bytes())['state'], 'write-failed')
        self.assertNotEqual(self.target.read_bytes(), self.original)
        self.assertEqual(self.target.read_bytes()[:2048], self.payload[:2048])
        return result

    def test_explicit_recovery_after_failed_process_reauthenticates_and_rewrites(self):
        self.interrupted()
        result = self.run_update('recover')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.target.read_bytes()[:len(self.payload)], self.payload)
        journal = json.loads(self.journal.read_bytes())
        self.assertEqual(journal['state'], 'verified')
        self.assertIs(journal['rollback_available'], False)

    def test_recovery_rejects_missing_journal_without_writing(self):
        result = self.run_update('recover')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('recovery journal', result.stderr)
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_recovery_rechecks_signature_before_writing(self):
        self.interrupted()
        prior = self.target.read_bytes()
        self.index.write_bytes(self.index.read_bytes() + b' ')
        result = self.run_update('recover')
        self.assertEqual(result.returncode, 2)
        self.assertIn('signature', result.stderr)
        self.assertEqual(self.target.read_bytes(), prior)

    def test_recovery_rejects_mismatched_journal_without_writing(self):
        self.interrupted()
        before = self.target.read_bytes()
        self.journal.write_text(self.journal.read_text().replace(self.hash, '0' * 64))
        result = self.run_update('recover')
        self.assertEqual(result.returncode, 2)
        self.assertIn('recovery journal', result.stderr)
        self.assertEqual(self.target.read_bytes(), before)

    def test_target_replacement_after_capacity_check_never_redirects_copy_or_readback(self):
        result = self.run_update('apply', extra={'FIXTURE_REPLACE_TARGET': '1'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.target.read_bytes(), b'CALLER_REPLACEMENT_MUST_SURVIVE')
        retained = Path(str(self.target) + '.retained')
        self.assertEqual(retained.read_bytes()[:len(self.payload)], self.payload)
        self.assertEqual(json.loads(self.journal.read_bytes())['state'], 'verified')

    def test_recovery_handles_prepared_verification_failed_and_verified_states(self):
        self.interrupted()
        receipt = json.loads(self.journal.read_bytes())
        for state in ['prepared', 'verification-failed', 'verified']:
            with self.subTest(state=state):
                receipt['state'] = state
                self.journal.write_text(json.dumps(receipt, separators=(',', ':')) + '\n')
                result = self.run_update('recover')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.target.read_bytes()[:len(self.payload)], self.payload)

    def test_recovery_rejects_nonregular_symlink_and_malformed_journal(self):
        self.interrupted()
        receipt = self.journal.read_bytes()
        before = self.target.read_bytes()
        for kind in ['fifo', 'symlink', 'malformed', 'oversized', 'unknown-state']:
            with self.subTest(kind=kind):
                self.journal.unlink()
                if kind == 'fifo':
                    os.mkfifo(self.journal)
                elif kind == 'symlink':
                    outside = self.root / 'receipt-copy'
                    outside.write_bytes(receipt)
                    self.journal.symlink_to(outside)
                elif kind == 'malformed':
                    self.journal.write_bytes(receipt + b'trailing')
                elif kind == 'oversized':
                    self.journal.write_bytes(b' ' * 8193)
                else:
                    self.journal.write_bytes(receipt.replace(b'write-failed', b'unknown-state'))
                result = self.run_update('recover')
                self.assertEqual(result.returncode, 2)
                self.assertIn('recovery journal', result.stderr)
                self.assertEqual(self.target.read_bytes(), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
