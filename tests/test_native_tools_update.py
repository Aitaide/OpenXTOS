#!/usr/bin/env python3
"""update tool tests: version compare, signed-index check, mirror probe/fallback,
download + hash verify, tamper and bad-signature rejection.

Runs the static binary as the current user against a local HTTP mirror
fixture; no network, no installed XTOS image required.
"""
import hashlib
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'tools' / 'xtos-native' / 'update' / 'update.c'
KEY = None
BUILD = None


def sign(key_path, target):
    sig = Path(str(target) + '.sig')
    subprocess.run(['openssl', 'pkeyutl', '-sign', '-inkey', str(key_path),
                    '-rawin', '-in', str(target), '-out', str(sig)], check=True)
    return sig


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


def serve(directory):
    handler = lambda *a, **kw: QuietHandler(*a, directory=str(directory), **kw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def serve_tls(directory, certfile, keyfile):
    handler = lambda *a, **kw: QuietHandler(*a, directory=str(directory), **kw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile, keyfile)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def write_index(directory, version='1.2.0', key=None, artifacts=None, name_suffix=''):
    artifacts = artifacts if artifacts is not None else [
        {'name': f'openxtos-{version}-x86_64-installer.iso', 'arch': 'x86_64', 'kind': 'installer-iso',
         'size': 1024, 'sha256': hashlib.sha256(b'iso-bytes').hexdigest(),
         'url': f'http://127.0.0.1/REPLACE/openxtos-{version}-x86_64-installer.iso'},
    ]
    spec = {'version': version, 'release_name': 'Fixture', 'channel': 'prerelease',
            'published': '2026-10-16', 'commit': 'a' * 40,
            'notes_url': 'https://github.com/Aitaide/OpenXTOS/releases/tag/fixture',
            'artifacts': artifacts}
    index = {'schema_version': 1, 'kind': 'xtos-release-index', 'latest': spec}
    path = directory / ('release-index.json' + name_suffix)
    path.write_bytes((json.dumps(index, indent=2, sort_keys=True) + '\n').encode())
    sign(key, path)
    return index


class UpdateToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global KEY, BUILD
        cls.tmp = tempfile.TemporaryDirectory(prefix='xtos-update-test-')
        cls.base = Path(cls.tmp.name)
        key_dir = cls.base / 'keys'
        key_dir.mkdir()
        KEY = key_dir / 'update-key.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'ed25519', '-out', str(KEY)], check=True)
        pub = key_dir / 'update-pub.pem'
        subprocess.run(['openssl', 'pkey', '-in', str(KEY), '-pubout', '-out', str(pub)], check=True)
        cls.pub = pub
        BUILD = cls.base / 'update'
        subprocess.run(['bash', str(ROOT / 'scripts' / 'build_mbedtls.sh')], check=True)
        mb = ROOT / 'tools' / 'xtos-native' / 'vendor' / 'mbedtls'
        subprocess.run(['gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-static',
                        '-DMBEDTLS_CONFIG_FILE="xtos_mbedtls_config.h"',
                        '-I', str(ROOT / 'tools' / 'xtos-native' / 'common'),
                        '-I', str(ROOT / 'tools' / 'xtos-native' / 'vendor' / 'monocypher'),
                        '-I', str(mb / 'include'), '-I', str(mb),
                        '-o', str(BUILD), str(SRC),
                        str(ROOT / 'tools' / 'xtos-native' / 'vendor' / 'monocypher' / 'monocypher.c'),
                        str(ROOT / 'tools' / 'xtos-native' / 'vendor' / 'monocypher' / 'monocypher-ed25519.c'),
                        str(ROOT / '.build' / 'mbedtls' / 'libmbedtls_xtos.a')],
                       check=True)
        cls.mirror = cls.base / 'mirror'
        (cls.mirror / 'pool').mkdir(parents=True)
        cls.local_release = cls.base / 'xtos-release'
        cls.server, cls.port = serve(cls.mirror)
        cls.mirror_url = f'http://127.0.0.1:{cls.port}'
        # local TLS fixture: self-signed CA + leaf for "localhost"
        tls_dir = cls.base / 'tls'
        tls_dir.mkdir()
        ca_key = tls_dir / 'ca.key'
        cls.ca_pem = tls_dir / 'ca.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(ca_key), '-out', str(cls.ca_pem),
                        '-subj', '/CN=XTOS test CA', '-days', '2'], check=True, capture_output=True)
        leaf_key = tls_dir / 'leaf.key'
        leaf_csr = tls_dir / 'leaf.csr'
        subprocess.run(['openssl', 'req', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(leaf_key), '-out', str(leaf_csr),
                        '-subj', '/CN=localhost'], check=True, capture_output=True)
        leaf_pem = tls_dir / 'leaf.pem'
        ext = tls_dir / 'leaf.ext'
        ext.write_text('subjectAltName=DNS:localhost,IP:127.0.0.1\n')
        subprocess.run(['openssl', 'x509', '-req', '-in', str(leaf_csr), '-CA', str(cls.ca_pem),
                        '-CAkey', str(ca_key), '-CAcreateserial', '-out', str(leaf_pem),
                        '-days', '2', '-extfile', str(ext)], check=True, capture_output=True)
        cls.other_ca_pem = tls_dir / 'other-ca.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(tls_dir / 'other-ca.key'), '-out', str(cls.other_ca_pem),
                        '-subj', '/CN=Other CA', '-days', '2'], check=True, capture_output=True)
        cls.tls_server, cls.tls_port = serve_tls(cls.mirror, str(leaf_pem), str(leaf_key))
        cls.tls_url = f'https://localhost:{cls.tls_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def run_update(self, *args, env_extra=None, installed: str | None = '1.2.0'):
        env = dict(os.environ)
        env['XTOS_UPDATE_LOCAL_RELEASE'] = str(self.local_release)
        env['XTOS_UPDATE_MIRRORS'] = self.mirror_url
        env['XTOS_UPDATE_PUBKEY'] = str(self.pub)
        env['XTOS_UPDATE_TIMEOUT_SEC'] = '3'
        if installed is not None:
            self.local_release.write_text(f'XTOS_VERSION={installed}\nXTOS_RELEASE_NAME=Fixture\n')
        if env_extra:
            env.update(env_extra)
        return subprocess.run([str(BUILD), *args], capture_output=True, text=True, env=env, timeout=120)

    def test_01_version_compare(self):
        cases = [
            ('1.0.0-beta.1', '1.0.0-beta.4', 'older'),
            ('1.0.0-beta.4', '1.0.0-beta.4', 'equal'),
            ('1.0.0-beta.4', '1.0.0', 'older'),
            ('1.0.0', '1.0.0-beta.4', 'newer'),
            ('1.2.0', '1.10.0', 'older'),
            ('2.0.0', '1.99.99', 'newer'),
            ('1.0.0-beta.10', '1.0.0-beta.9', 'newer'),
            ('1.0.0-rc.1', '1.0.0-beta.9', 'newer'),
        ]
        for a, b, want in cases:
            r = self.run_update('compare', a, b, installed=None)
            self.assertEqual(r.returncode, 0, f'compare {a} {b}: {r.stderr}')
            self.assertIn(want, r.stdout, f'compare {a} {b}: {r.stdout!r}')

    def test_02_check_up_to_date(self):
        write_index(self.mirror, version='1.2.0', key=KEY)
        r = self.run_update('check', installed='1.2.0')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('up-to-date', r.stdout)
        self.assertIn('Signature: verified', r.stdout)
        r = self.run_update('--json', 'check', installed='1.2.0')
        data = json.loads(r.stdout)
        self.assertEqual(data['up_to_date'], True)
        self.assertEqual(data['signature'], 'verified')

    def test_03_check_update_available(self):
        write_index(self.mirror, version='1.3.0', key=KEY)
        r = self.run_update('check', installed='1.2.0')
        self.assertEqual(r.returncode, 1, f'expected exit 1 for available update: {r.stdout}')
        self.assertIn('update available', r.stdout)
        self.assertIn('1.3.0', r.stdout)
        r = self.run_update('--json', 'check', installed='1.2.0')
        data = json.loads(r.stdout)
        self.assertEqual(data['up_to_date'], False)
        self.assertEqual(data['latest_version'], '1.3.0')

    def test_04_bad_signature_rejected(self):
        other_key = self.base / 'keys' / 'other.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'ed25519', '-out', str(other_key)], check=True)
        write_index(self.mirror, version='1.3.0', key=other_key)
        r = self.run_update('check', installed='1.2.0')
        self.assertEqual(r.returncode, 2, f'expected exit 2: {r.stdout}{r.stderr}')
        self.assertIn('signature', r.stdout + r.stderr)
        write_index(self.mirror, version='1.2.0', key=KEY)

    def test_05_tampered_index_rejected(self):
        write_index(self.mirror, version='1.2.0', key=KEY)
        path = self.mirror / 'release-index.json'
        raw = json.loads(path.read_text())
        raw['latest']['version'] = '9.9.9'
        path.write_bytes((json.dumps(raw, indent=2, sort_keys=True) + '\n').encode())
        r = self.run_update('check', installed='1.2.0')
        self.assertEqual(r.returncode, 2, f'tampered index accepted: {r.stdout}')
        write_index(self.mirror, version='1.2.0', key=KEY)

    def test_06_dead_mirrors(self):
        r = self.run_update('check', installed='1.2.0',
                            env_extra={'XTOS_UPDATE_MIRRORS': 'http://127.0.0.1:1'})
        self.assertEqual(r.returncode, 2)
        self.assertIn('unreachable', r.stdout + r.stderr)

    def test_07_mirror_fallback_order(self):
        write_index(self.mirror, version='1.3.0', key=KEY)
        r = self.run_update('check', installed='1.2.0',
                            env_extra={'XTOS_UPDATE_MIRRORS': f'http://127.0.0.1:1 {self.mirror_url}'})
        self.assertEqual(r.returncode, 1, f'fallback failed: {r.stdout}{r.stderr}')
        self.assertIn('update available', r.stdout)

    def test_08_offline_index_check(self):
        write_index(self.mirror, version='1.2.0', key=KEY)
        index_path = self.mirror / 'release-index.json'
        r = self.run_update('check', installed='1.2.0',
                            env_extra={'XTOS_UPDATE_MIRRORS': '', 'XTOS_UPDATE_OFFLINE_INDEX': str(index_path)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('up-to-date', r.stdout)
        # regression: offline mode must never delete the caller's index files
        self.assertTrue(index_path.is_file(), 'offline index deleted by check')
        self.assertTrue(Path(str(index_path) + '.sig').is_file(), 'offline index sig deleted by check')

    def test_09_download_verifies_hash(self):
        iso = self.mirror / 'openxtos-1.3.0-x86_64-installer.iso'
        iso.write_bytes(b'iso-bytes')
        write_index(self.mirror, version='1.3.0', key=KEY, artifacts=[
            {'name': 'openxtos-1.3.0-x86_64-installer.iso', 'arch': 'x86_64', 'kind': 'installer-iso',
             'size': len(b'iso-bytes'), 'sha256': hashlib.sha256(b'iso-bytes').hexdigest(),
             'url': f'{self.mirror_url}/openxtos-1.3.0-x86_64-installer.iso'},
        ])
        dest = self.base / 'dest'
        r = self.run_update('download', '--dest', str(dest), installed='1.2.0')
        self.assertEqual(r.returncode, 0, f'download failed: {r.stdout}{r.stderr}')
        got = dest / 'openxtos-1.3.0-x86_64-installer.iso'
        self.assertEqual(got.read_bytes(), b'iso-bytes')
        r = self.run_update('--json', 'download', '--dest', str(dest), installed='1.2.0')
        data = json.loads(r.stdout)
        self.assertEqual(data['downloaded'], 1)

    def test_10_download_rejects_bad_hash(self):
        iso = self.mirror / 'openxtos-1.3.0-x86_64-installer.iso'
        iso.write_bytes(b'tampered-bytes')
        write_index(self.mirror, version='1.3.0', key=KEY, artifacts=[
            {'name': 'openxtos-1.3.0-x86_64-installer.iso', 'arch': 'x86_64', 'kind': 'installer-iso',
             'size': len(b'tampered-bytes'), 'sha256': hashlib.sha256(b'iso-bytes').hexdigest(),
             'url': f'{self.mirror_url}/openxtos-1.3.0-x86_64-installer.iso'},
        ])
        dest = self.base / 'dest-bad'
        r = self.run_update('download', '--dest', str(dest), installed='1.2.0')
        self.assertEqual(r.returncode, 2, f'bad hash accepted: {r.stdout}')
        self.assertFalse(list(dest.glob('*.iso')) if dest.exists() else True)

    def test_11_arch_filter(self):
        write_index(self.mirror, version='1.3.0', key=KEY, artifacts=[
            {'name': 'x86.iso', 'arch': 'x86_64', 'kind': 'installer-iso',
             'size': 1, 'sha256': hashlib.sha256(b'x').hexdigest(), 'url': 'https://example.com/x86.iso'},
            {'name': 'arm.tar.gz', 'arch': 'arm64', 'kind': 'boot-bundle',
             'size': 1, 'sha256': hashlib.sha256(b'a').hexdigest(), 'url': 'https://example.com/arm.tar.gz'},
        ])
        r = self.run_update('--json', 'check', installed='1.2.0')
        data = json.loads(r.stdout)
        self.assertEqual([a['name'] for a in data['artifacts']], ['x86.iso'])
        self.assertEqual(data['arch'], 'x86_64')

    def test_12_https_fetch_and_download(self):
        # native mbedTLS transport: index check and artifact download over TLS
        (self.mirror / 'over-tls.iso').write_bytes(b'tls-bytes')
        write_index(self.mirror, version='1.4.0', key=KEY, artifacts=[
            {'name': 'over-tls.iso', 'arch': 'x86_64', 'kind': 'installer-iso',
             'size': len(b'tls-bytes'), 'sha256': hashlib.sha256(b'tls-bytes').hexdigest(),
             'url': f'{self.tls_url}/over-tls.iso'},
        ])
        r = self.run_update('check', installed='1.2.0',
                            env_extra={'XTOS_UPDATE_MIRRORS': self.tls_url,
                                       'XTOS_UPDATE_CA_BUNDLE': str(self.ca_pem)})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('Signature: verified', r.stdout)
        self.assertIn('1.4.0', r.stdout)
        dest = self.base / 'dest-tls'
        r = self.run_update('download', '--dest', str(dest), installed='1.2.0',
                            env_extra={'XTOS_UPDATE_MIRRORS': self.tls_url,
                                       'XTOS_UPDATE_CA_BUNDLE': str(self.ca_pem)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((dest / 'over-tls.iso').read_bytes(), b'tls-bytes')

    def test_13_https_wrong_ca_unreachable(self):
        write_index(self.mirror, version='1.4.0', key=KEY)
        r = self.run_update('check', installed='1.2.0',
                            env_extra={'XTOS_UPDATE_MIRRORS': self.tls_url,
                                       'XTOS_UPDATE_CA_BUNDLE': str(self.other_ca_pem)})
        self.assertEqual(r.returncode, 2)
        self.assertIn('unreachable', r.stdout + r.stderr)
        # and a hostname that does not match the certificate must fail too
        # (127.0.0.1 IS in the fixture SAN; 127.0.0.2 is not)
        r = self.run_update('check', installed='1.2.0',
                            env_extra={'XTOS_UPDATE_MIRRORS': f'https://127.0.0.2:{self.tls_port}',
                                       'XTOS_UPDATE_CA_BUNDLE': str(self.ca_pem)})
        self.assertEqual(r.returncode, 2)
        write_index(self.mirror, version='1.2.0', key=KEY)


if __name__ == '__main__':
    unittest.main(verbosity=2)
