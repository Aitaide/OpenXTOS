#!/usr/bin/env python3
"""Real native apply entry; only temporary regular files, never host block writes."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'tools/xtos-native/update/update.c'


def compile_update(output, extra=(), source=SRC):
    native = ROOT / 'tools/xtos-native'
    archive = ROOT / '.build/mbedtls/libmbedtls_xtos.a'
    if not archive.is_file():
        raise RuntimeError('prebuilt mbedTLS archive required; no shared rebuild allowed')
    cmd = ['gcc', '-O2', '-Wall', '-Wextra', '-Werror',
           '-DMBEDTLS_CONFIG_FILE="xtos_mbedtls_config.h"',
           '-I', str(native / 'common'), '-I', str(native / 'vendor/monocypher'),
           '-I', str(native / 'vendor/mbedtls/include'), '-I', str(native / 'vendor/mbedtls'),
           '-o', str(output), str(source), str(native / 'vendor/monocypher/monocypher.c'),
           str(native / 'vendor/monocypher/monocypher-ed25519.c'), str(archive), *extra]
    subprocess.run(cmd, check=True, env={**os.environ, 'LC_ALL': 'C'})
    return cmd


class ApplySecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='apply-security-')
        cls.base = Path(cls.tmp.name)
        cls.binary = cls.base / 'update'
        compile_update(cls.binary)
        cls.key = cls.base / 'fixture-key.pem'
        cls.pub = cls.base / 'pub.pem'
        cls.race_binary = cls.base / 'update-race'
        wrapper = cls.base / 'race.c'
        wrapper.write_text(r'''
#define main real_update_entry
#include "UPDATE_SOURCE"
#undef main
int main(int argc, char **argv) { return real_update_entry(argc, argv); }
char *__real_realpath(const char *, char *);
char *__wrap_realpath(const char *path, char *out) {
    const char *iso = getenv("REPLACE_ISO");
    if (iso) {
        unlink(iso);
        int evil = open(iso, O_CREAT | O_WRONLY, 0600);
        if (evil < 0 || write(evil, "EVIL", 4) != 4) _exit(93);
        close(evil);
        int found = 0;
        for (int fd = 3; fd < 128; fd++) {
            struct stat st;
            if (fstat(fd, &st) == 0 && S_ISREG(st.st_mode) && st.st_nlink == 0) {
                int out = open(getenv("SNAPSHOT_OUTPUT"), O_WRONLY);
                if (out < 0 || copy_iso_to_fd(fd, out, st.st_size) != 0 || close(out) != 0) _exit(94);
                found++;
            }
        }
        if (found != 1) _exit(95);
        fprintf(stderr, "ISO_RACE_FIRED\\n");
    }
    return __real_realpath(path, out);
}
FILE *__real_fmemopen(void *, size_t, const char *);
FILE *__wrap_fmemopen(void *buf, size_t n, const char *mode) {
    const char *path = getenv("REPLACE_INDEX");
    if (path) { int fd = open(path, O_WRONLY | O_TRUNC); if (fd < 0) _exit(91);
        const char evil[] = "{ invalid changed index }";
        if (write(fd, evil, sizeof(evil)-1) != sizeof(evil)-1) _exit(92);
        close(fd); fprintf(stderr, "INDEX_RACE_FIRED\\n"); }
    return __real_fmemopen(buf, n, mode);
}
'''.replace('UPDATE_SOURCE', str(SRC)))
        compile_update(cls.race_binary, extra=('-Wl,--wrap=fmemopen', '-Wl,--wrap=realpath'), source=wrapper)
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'ed25519', '-out', str(cls.key)], check=True)
        subprocess.run(['openssl', 'pkey', '-in', str(cls.key), '-pubout', '-out', str(cls.pub)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.case = Path(tempfile.mkdtemp(dir=self.base))
        self.payload = b'authentic signed ISO' * 4096
        self.art = {'name': 'mini.iso', 'arch': 'x86_64', 'kind': 'installer-iso',
                    'size': len(self.payload), 'sha256': hashlib.sha256(self.payload).hexdigest(),
                    'url': 'http://127.0.0.1:1/mini.iso'}
        self.index = {'schema_version': 1, 'kind': 'xtos-release-index',
                      'latest': {'version': '1.4.0', 'channel': 'prerelease', 'artifacts': [self.art]}}
        (self.case / 'mini.iso').write_bytes(self.payload)
        self.publish()
        self.manifest = {'schema_version': 1, 'kind': 'xtos-download-manifest',
                         'version': '1.4.0', 'channel': 'prerelease', 'arch': 'x86_64',
                         'artifacts': [dict(self.art)]}
        self.write_manifest()
        self.target = self.case / 'target-sentinel'
        self.target.write_bytes(b'UNTOUCHED')

    def publish(self, size_token=None):
        p = self.case / 'release-index.json'
        text = json.dumps(self.index, indent=2, sort_keys=True) + '\n'
        if size_token is not None:
            old = '"size": ' + str(self.art['size'])
            self.assertEqual(text.count(old), 1)
            text = text.replace(old, '"size": ' + size_token)
        p.write_text(text)
        subprocess.run(['openssl', 'pkeyutl', '-sign', '-inkey', str(self.key), '-rawin',
                        '-in', str(p), '-out', str(p) + '.sig'], check=True)
        if size_token is not None:
            # Invalid JSON tokens are still authentic bytes, not signature failures.
            subprocess.run(['openssl', 'pkeyutl', '-verify', '-pubin', '-inkey', str(self.pub),
                            '-rawin', '-in', str(p), '-sigfile', str(p) + '.sig'],
                           check=True, capture_output=True)

    def write_manifest(self):
        (self.case / 'download-manifest.json').write_text(json.dumps(self.manifest, indent=2))

    def apply(self, binary=None, env_extra=None):
        r = subprocess.run([str(binary or self.binary), 'apply', '--from', str(self.case),
                            '--to', str(self.target), '--confirm', 'ERASE-XTOS'],
                           env={**os.environ, 'XTOS_UPDATE_PUBKEY': str(self.pub),
                                'XTOS_UPDATE_ARCH': 'x86_64', **(env_extra or {})},
                           capture_output=True, text=True, timeout=5)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self.target.read_bytes(), b'UNTOUCHED')
        return r.stderr

    def test_unsigned_manifest_and_iso_replacement_rejected_before_target(self):
        evil = b'forged payload'
        (self.case / 'mini.iso').write_bytes(evil)
        self.manifest['artifacts'][0].update(size=len(evil), sha256=hashlib.sha256(evil).hexdigest())
        self.write_manifest()
        error = self.apply()
        self.assertIn('corrupt artifact', error)
        self.assertNotIn('canonical whole disk', error)
    def test_verified_iso_path_replacement_copies_only_retained_snapshot(self):
        output = self.case / 'race-copy-output'
        output.write_bytes(b'_' * len(self.payload))
        error = self.apply(binary=self.race_binary,
                           env_extra={'REPLACE_ISO': str(self.case / 'mini.iso'),
                                      'SNAPSHOT_OUTPUT': str(output)})
        self.assertIn('ISO_RACE_FIRED', error)
        self.assertIn('canonical whole disk', error)
        self.assertEqual(output.read_bytes(), self.payload)
        self.assertEqual((self.case / 'mini.iso').read_bytes(), b'EVIL')

    def test_verified_metadata_path_replacement_cannot_change_parsed_bytes(self):
        error = self.apply(binary=self.race_binary,
                           env_extra={'REPLACE_INDEX': str(self.case / 'release-index.json')})
        self.assertIn('INDEX_RACE_FIRED', error)
        self.assertIn('canonical whole disk', error)

    def test_signed_size_mismatch_rejected_before_target(self):
        self.art['size'] += 1
        self.publish()
        self.assertIn('corrupt artifact', self.apply())

    def test_signed_size_requires_complete_positive_json_integer(self):
        size = str(len(self.payload))
        # Match the native long storage bound on the compiler's target ABI.
        import ctypes
        overflow = str(1 << (ctypes.sizeof(ctypes.c_long) * 8 - 1))
        tokens = [size + '.75', size + '.0', size + 'e0', size + 'E+0',
                  '8.192e4', '"' + size + '"', '-' + size, '-0', '0',
                  overflow, '9' * 100, size + 'junk', size + ' junk',
                  size + ' .75', '+' + size, '0' + size, '0x14000',
                  'true', 'false', 'null', '[]', '{}', '']
        for token in tokens:
            with self.subTest(size_token=token):
                self.publish(size_token=token)
                error = self.apply()
                self.assertNotIn('canonical whole disk', error)
                self.assertNotIn('cannot resolve', error)
                self.assertNotIn('corrupt artifact', error)
                self.assertTrue('trusted signed metadata' in error or
                                'invalid signed installer metadata' in error, error)

    def test_minimum_signed_size_reaches_real_target_refusal(self):
        self.payload = b'x'
        (self.case / 'mini.iso').write_bytes(self.payload)
        self.art.update(size=1, sha256=hashlib.sha256(self.payload).hexdigest())
        for token in ('1', ' 1 \t'):
            with self.subTest(size_token=token):
                self.publish(size_token=token)
                self.assertIn('canonical whole disk', self.apply())

    def test_missing_channel_rejected(self):
        del self.index['latest']['channel']
        self.publish()
        self.assertIn('invalid signed installer metadata', self.apply())

    def test_invalid_signed_version_rejected(self):
        self.index['latest']['version'] = 'not-a-version'
        self.publish()
        self.assertIn('invalid signed installer metadata', self.apply())

    def test_valid_signed_input_reaches_real_target_refusal(self):
        self.assertIn('canonical whole disk', self.apply())

    def test_missing_signature_rejected_before_target(self):
        (self.case / 'release-index.json.sig').unlink()
        self.assertIn('signature verification failed', self.apply())

    def test_wrong_key_rejected_before_target(self):
        other = self.case / 'other-key.pem'
        pub = self.case / 'other-pub.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'ed25519', '-out', str(other)], check=True)
        subprocess.run(['openssl', 'pkey', '-in', str(other), '-pubout', '-out', str(pub)], check=True)
        self.assertIn('signature verification failed', self.apply(env_extra={'XTOS_UPDATE_PUBKEY': str(pub)}))

    def test_tampered_signed_version_rejected(self):
        p = self.case / 'release-index.json'
        p.write_text(p.read_text().replace('1.4.0', '9.9.9'))
        self.assertIn('signature verification failed', self.apply())

    def test_multiple_iso_rejected(self):
        self.index['latest']['artifacts'].append(dict(self.art, name='other.iso'))
        self.publish()
        self.assertIn('invalid signed installer metadata', self.apply())

    def test_wrong_architecture_rejected(self):
        self.art['arch'] = 'arm64'
        self.publish()
        self.assertIn('invalid signed installer metadata', self.apply())

    def test_signed_path_escape_rejected(self):
        self.art['name'] = '../mini.iso'
        self.publish()
        self.assertIn('invalid signed installer metadata', self.apply())

    def test_iso_symlink_rejected(self):
        (self.case / 'mini.iso').unlink()
        outside = self.base / 'outside.iso'
        outside.write_bytes(self.payload)
        (self.case / 'mini.iso').symlink_to(outside)
        self.assertIn('corrupt artifact', self.apply())

    def test_signed_metadata_symlink_rejected(self):
        p = self.case / 'release-index.json'
        outside = self.base / 'outside-index.json'
        p.rename(outside)
        p.symlink_to(outside)
        self.assertIn('signature verification failed', self.apply())

    def test_legacy_unsigned_directory_rejected(self):
        (self.case / 'release-index.json').unlink()
        (self.case / 'release-index.json.sig').unlink()
        self.assertIn('signature verification failed', self.apply())

    def test_oversized_signed_metadata_rejected(self):
        (self.case / 'release-index.json').write_bytes(b' ' * (4 * 1024 * 1024 + 1))
        self.assertIn('signature verification failed', self.apply())


class CopyFaultTests(unittest.TestCase):
    """Compile production C into a syscall-controlled regular-file harness."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='apply-copy-fault-')
        cls.base = Path(cls.tmp.name)
        cls.binary = cls.base / 'copy-harness'
        harness = cls.base / 'harness.c'
        harness.write_text(r'''
#define main update_entry
#include "UPDATE_SOURCE"
#undef main
static int calls;
ssize_t __real_write(int, const void *, size_t);
ssize_t __wrap_write(int fd, const void *p, size_t n) {
    const char *mode = getenv("FAULT");
    calls++;
    if (!strcmp(mode, "zero")) return 0;
    if (!strcmp(mode, "interrupts")) { errno = EINTR; return -1; }
    if (!strcmp(mode, "error")) { errno = EIO; return -1; }
    if (!strcmp(mode, "short") && n > 3) n = 3;
    if (!strcmp(mode, "once") && calls == 1) { errno = EINTR; return -1; }
    return __real_write(fd, p, n);
}
int main(int argc, char **argv) {
    if (argc != 4) return 99;
    int in = open(argv[1], O_RDONLY);
    if (in < 0) return 98;
    int rc;
    if (!strcmp(getenv("FAULT"), "journal")) {
        rc = write_apply_journal(argv[2], "prepared", "1.4.0", "/dev/loop-fixture", "abcd", atol(argv[3]));
    } else {
        int out = open(argv[2], O_WRONLY);
        if (out < 0) return 97;
        rc = copy_iso_to_fd(COPY_INPUT, out, atol(argv[3]));
        if (close(out) < 0) rc = -1;
    }
    close(in);
    printf("rc=%d calls=%d\\n", rc, calls);
    return rc ? 2 : 0;
}
'''.replace('UPDATE_SOURCE', str(SRC)).replace('COPY_INPUT', 'in'))
        compile_update(cls.binary, extra=('-Wl,--wrap=write',), source=harness)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_fault(self, mode, expected):
        source = self.base / ('input-' + mode)
        target = self.base / ('target-' + mode)
        data = b'input payload' * 8
        source.write_bytes(data)
        target.write_bytes(b'_' * len(data))
        r = subprocess.run([str(self.binary), str(source), str(target), str(len(data))],
                           env={**os.environ, 'FAULT': mode}, capture_output=True, text=True, timeout=2)
        self.assertEqual(r.returncode, expected, r.stdout + r.stderr)
        if expected == 0:
            self.assertEqual(target.read_bytes(), data)
        return r

    def test_prepared_journal_is_durable_json_without_rollback_claim(self):
        source = self.base / 'journal-input'
        source.write_bytes(b'x')
        journal = self.base / 'apply-journal.json'
        r = subprocess.run([str(self.binary), str(source), str(journal), '1'],
                           env={**os.environ, 'FAULT': 'journal'}, capture_output=True, text=True, timeout=2)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(journal.read_text())
        self.assertEqual(data['state'], 'prepared')
        self.assertEqual(data['rollback_available'], False)

    def test_zero_write_is_bounded_failure(self):
        self.run_fault('zero', 2)

    def test_persistent_eintr_is_bounded_failure(self):
        self.run_fault('interrupts', 2)

    def test_short_writes_copy_exact_bytes(self):
        self.run_fault('short', 0)

    def test_transient_eintr_retries_successfully(self):
        self.run_fault('once', 0)

    def test_write_error_propagates(self):
        self.run_fault('error', 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
