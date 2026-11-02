"""Local TLS software regressions; no physical identity or vendor acceptance."""
import importlib
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools/xtos-devices'))

def deadline(ms=1200):
    return time.monotonic_ns() + ms * 1_000_000


class Peer:
    def __init__(self, context, action):
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.listener.settimeout(2)
        self.port = self.listener.getsockname()[1]
        self.errors = []
        self.accepted = threading.Event()
        self.release = threading.Event()
        self.done = threading.Event()
        def serve():
            conn = None
            try:
                conn, _ = self.listener.accept()
                conn.settimeout(2)
                self.accepted.set()
                if context is not None:
                    conn = context.wrap_socket(conn, server_side=True)
                action(conn, self)
            except (ssl.SSLError, ConnectionError) as exc:
                self.errors.append(type(exc).__name__)
            except BaseException as exc:
                self.errors.append(repr(exc))
            finally:
                if conn is not None:
                    conn.close()
                self.done.set()
        self.thread = threading.Thread(target=serve)
        self.thread.start()

    def close(self):
        self.release.set()
        self.listener.close()
        self.thread.join(3)
        if self.thread.is_alive():
            raise AssertionError('TLS fixture thread leaked')
        unexpected = [e for e in self.errors if e not in {'SSLError', 'SSLEOFError', 'SSLCertVerificationError', 'ConnectionResetError', 'BrokenPipeError'}]
        if unexpected:
            raise AssertionError('TLS fixture errors: ' + repr(unexpected))

class TLSTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='xtos-tls-', dir=os.environ.get('TMPDIR', '/root/.hermes/cache/scratch'))
        cls.certdir = Path(cls.tmp.name)
        def openssl(*args):
            subprocess.run(['openssl', *args], cwd=cls.certdir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=XTOS-local-test-CA', '-keyout', 'ca.key', '-out', 'ca.pem', '-addext', 'basicConstraints=critical,CA:TRUE')
        openssl('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=localhost', '-keyout', 'server.key', '-out', 'server.csr')
        (cls.certdir/'ext.cnf').write_text('subjectAltName=DNS:localhost\nextendedKeyUsage=serverAuth,clientAuth\n')
        openssl('x509', '-req', '-in', 'server.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key', '-CAcreateserial', '-days', '1', '-out', 'server.pem', '-extfile', 'ext.cnf')
        openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=wrong-CA', '-keyout', 'wrong.key', '-out', 'wrong.pem')
        cls.m = importlib.import_module('tls_transport')
        evidence = os.environ.get('XTOS_TLS_CERT_METADATA')
        if evidence:
            import json
            meta = subprocess.run(['openssl', 'x509', '-in', str(cls.certdir/'server.pem'), '-noout', '-subject', '-issuer', '-dates', '-ext', 'subjectAltName', '-fingerprint', '-sha256'], capture_output=True, text=True, check=True).stdout
            Path(evidence).write_text(json.dumps({'server_certificate': meta, 'openssl': ssl.OPENSSL_VERSION})+'\n')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def context(self, mtls=False):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.load_cert_chain(self.certdir/'server.pem', self.certdir/'server.key')
        if mtls:
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(cafile=self.certdir/'ca.pem')
        return ctx

    def transport(self, peer, **kw):
        opts = dict(server_hostname='localhost', cafile=str(self.certdir/'ca.pem'))
        opts.update(kw)
        t = self.m.TLSTransport('127.0.0.1', peer.port, **opts)
        self.addCleanup(t.close)
        return t

    def peer(self, action, context: object = True):
        p = Peer(self.context() if context is True else context, action)
        self.addCleanup(p.close)
        return p

    def test_verified_handshake_transfer_and_immutable_metadata(self):
        received = []
        def action(conn, p):
            data = b''
            while len(data) < 8192:
                data += conn.recv(8192-len(data))
            received.append(data)
            conn.sendall(bytes(range(132)))
            p.release.wait(2)
        p = self.peer(action)
        t = self.transport(p)
        self.assertFalse(t.connected)
        self.assertIs(t.open(deadline_ns=deadline()), t)
        self.assertTrue(t.connected)
        self.assertIsInstance(t._socket, ssl.SSLSocket)
        self.assertFalse(t._socket.getblocking())
        self.assertEqual(t.security_info.protocol, 'TLSv1.3')
        self.assertEqual(t.security_info.server_hostname, 'localhost')
        self.assertTrue(t.security_info.cipher)
        with self.assertRaises(AttributeError):
            t.security_info.protocol = 'plain'
        t.write_all(b'x'*8192, deadline_ns=deadline())
        self.assertEqual(t.read(132, deadline_ns=deadline()), bytes(range(132)))
        self.assertEqual(received, [b'x'*8192])
        fds = [t._socket.fileno(), *t._wake]
        t.close()
        self.assertFalse(t.connected)
        self.assertIsNone(t.security_info)
        for fd in fds:
            with self.assertRaises(OSError):
                os.fstat(fd)
        t.close()


    def test_wrong_hostname_untrusted_ca_and_plaintext_refused(self):
        for opts in ({'server_hostname': 'wrong.invalid'}, {'cafile': str(self.certdir/'wrong.pem')}):
            with self.subTest(opts=opts):
                p = self.peer(lambda conn, p: p.release.wait(2))
                t = self.transport(p, **opts)
                with self.assertRaises(self.m.NetworkError):
                    t.open(deadline_ns=deadline())
                self.assertFalse(t.connected)
                self.assertIsNone(t.security_info)
                self.assertIsNone(t._socket)
                self.assertIsNone(t._wake)
        def plain(conn, p):
            conn.recv(8192)
            conn.sendall(b'HTTP/1.1 200 OK\r\n\r\n')
        p = self.peer(plain, context=None)
        t = self.transport(p)
        with self.assertRaises(self.m.NetworkError):
            t.open(deadline_ns=deadline())
        self.assertFalse(t.connected)
        self.assertIsNone(t._socket)

    def test_handshake_deadline_and_cancel_release_descriptors(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                p = self.peer(lambda conn, p: p.release.wait(2), context=None)
                t = self.transport(p)
                entered = threading.Event()
                owned, errors = [], []
                real_ready = t._ready
                def ready(events, d):
                    if not owned:
                        owned.extend([t._socket.fileno(), *t._wake])
                    self.assertFalse(t.connected)
                    self.assertIsNone(t.security_info)
                    entered.set()
                    return real_ready(events, d)
                def notifier():
                    try:
                        if not entered.wait(1):
                            raise AssertionError('handshake poll not entered')
                        t.request_cancel()
                    except BaseException as exc:
                        errors.append(exc)
                worker = threading.Thread(target=notifier) if cancel else None
                with patch.object(t, '_ready', ready):
                    if worker:
                        worker.start()
                    start = time.monotonic()
                    try:
                        with self.assertRaises(self.m.NetworkCancelled if cancel else self.m.NetworkTimeout):
                            t.open(deadline_ns=deadline(1200 if cancel else 40))
                    finally:
                        if worker:
                            worker.join(2)
                self.assertLess(time.monotonic()-start, .7)
                self.assertEqual(errors, [])
                if worker:
                    self.assertFalse(worker.is_alive())
                self.assertTrue(owned)
                for fd in owned:
                    with self.assertRaises(OSError):
                        os.fstat(fd)
                self.assertFalse(t.connected)
                self.assertIsNone(t._wake)

    def test_mtls_explicit_certificate_key_pair(self):
        received = []
        def action(conn, p):
            received.append(bool(conn.getpeercert()))
            conn.sendall(b'm')
            p.release.wait(2)
        p = self.peer(action, context=self.context(mtls=True))
        t = self.transport(p, certfile=str(self.certdir/'server.pem'), keyfile=str(self.certdir/'server.key'))
        t.open(deadline_ns=deadline())
        self.assertEqual(t.read(1, deadline_ns=deadline()), b'm')
        self.assertEqual(received, [True])
        for kw in ({'certfile': str(self.certdir/'server.pem')}, {'keyfile': str(self.certdir/'server.key')}):
            with self.assertRaises(ValueError):
                self.transport(p, **kw)


    def test_explicit_configuration_validation_without_network(self):
        with patch.object(socket, 'socket', side_effect=AssertionError('constructor network')), patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS')):
            for host in ('x.example', 'localhost'):
                self.m.TLSTransport('127.0.0.1', 443, server_hostname=host, cafile=str(self.certdir/'ca.pem'))
            for host in ('', 'local'+chr(0)+'host', 'local'+chr(10)+'host', '*', None):
                with self.subTest(host=host), self.assertRaises(ValueError):
                    self.m.TLSTransport('127.0.0.1', 443, server_hostname=host, cafile=str(self.certdir/'ca.pem'))
            for path in ('', None):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    self.m.TLSTransport('127.0.0.1', 443, server_hostname='localhost', cafile=path)
            with self.assertRaises(ValueError):
                self.m.TLSTransport('localhost', 443, server_hostname='localhost', cafile=str(self.certdir/'ca.pem'))
        t = self.m.TLSTransport('127.0.0.1', 443, server_hostname='localhost', cafile=str(self.certdir/'ca.pem'))
        c = t._context()
        self.assertEqual(c.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(c.check_hostname)
        self.assertFalse(c.hostname_checks_common_name)
        self.assertEqual(c.minimum_version, ssl.TLSVersion.TLSv1_3)


    def test_post_handshake_eof_idle_deadline_and_cancel_reopen(self):
        p = self.peer(lambda conn, p: p.release.wait(2))
        t = self.transport(p)
        t.open(deadline_ns=deadline())
        with self.assertRaises(self.m.NetworkTimeout):
            t.read(1, deadline_ns=deadline(30))
        entered = threading.Event()
        errors = []
        real_ready = t._ready
        def ready(events, d):
            entered.set()
            return real_ready(events, d)
        def cancel():
            try:
                if not entered.wait(1):
                    raise AssertionError('read never waited')
                t.request_cancel()
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=cancel)
        with patch.object(t, '_ready', ready):
            worker.start()
            try:
                with self.assertRaises(self.m.NetworkCancelled):
                    t.read(1, deadline_ns=deadline())
            finally:
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        with self.assertRaises(self.m.NetworkCancelled):
            t.write_all(b'x', deadline_ns=deadline())
        t.close()
        t.request_cancel()
        p.release.set()
        self.assertTrue(p.done.wait(1))
        p2 = self.peer(lambda conn, p: conn.sendall(b'n'))
        t._endpoint = ('127.0.0.1', p2.port)
        t.open(deadline_ns=deadline())
        self.assertEqual(t.read(1, deadline_ns=deadline()), b'n')
        self.assertTrue(p2.done.wait(1))
        with self.assertRaises(self.m.NetworkDisconnected):
            t.read(1, deadline_ns=deadline())

    def test_ssl_wants_cross_directions_short_writes_and_pending_reads(self):
        received = []
        def action(conn, p):
            conn.sendall(b'abc')
            data = b''
            while len(data) < 6:
                data += conn.recv(6-len(data))
            received.append(data)
            conn.sendall(b'!')
            p.release.wait(2)
        p = self.peer(action)
        t = self.transport(p)
        t.open(deadline_ns=deadline())
        real_recv, real_send = ssl.SSLSocket.recv, ssl.SSLSocket.send
        recv_calls, send_calls, waits = [], [], []
        real_ready = t._ready
        def ready(events, d):
            waits.append(events)
            return real_ready(events, d)
        def recv(sock, size, *args):
            if sock is t._socket:
                recv_calls.append(size)
                if len(recv_calls) == 1:
                    raise ssl.SSLWantWriteError()
            return real_recv(sock, size, *args)
        with patch.object(ssl.SSLSocket, 'recv', recv), patch.object(t, '_ready', ready):
            self.assertEqual(t.read(1, deadline_ns=deadline()), b'a')
        self.assertIn(__import__('select').POLLOUT, waits)
        self.assertEqual(t.read(1, deadline_ns=deadline()), b'b')
        self.assertEqual(t.read(1, deadline_ns=deadline()), b'c')
        def send(sock, data, *args):
            if sock is t._socket:
                send_calls.append(len(data))
                if len(send_calls) == 1:
                    raise ssl.SSLWantWriteError()
                return real_send(sock, data[:2], *args)
            return real_send(sock, data, *args)
        with patch.object(ssl.SSLSocket, 'send', send):
            t.write_all(b'123456', deadline_ns=deadline())
        self.assertEqual(t.read(1, deadline_ns=deadline()), b'!')
        self.assertEqual(received, [b'123456'])
        self.assertEqual(len(send_calls), 4)

    def test_input_limits_and_owner_preserve_verified_session(self):
        p = self.peer(lambda conn, p: p.release.wait(2))
        t = self.transport(p)
        t.open(deadline_ns=deadline())
        for bad in (True, 0, -1, 133, 1.0, None):
            with self.assertRaises(ValueError):
                t.read(bad, deadline_ns=deadline())
        for bad in (b'', b'x'*8193, bytearray(b'x'), 'x', None):
            with self.assertRaises(ValueError):
                t.write_all(bad, deadline_ns=deadline())
        for bad in (True, 0, -1, 1.0, None):
            with self.assertRaises(ValueError):
                t.read(1, deadline_ns=bad)
            with self.assertRaises(ValueError):
                t.write_all(b'x', deadline_ns=bad)
        errors = []
        def foreign():
            for op in (t.close, lambda: t.read(1, deadline_ns=deadline()),
                       lambda: t.write_all(b'x', deadline_ns=deadline()),
                       lambda: t.open(deadline_ns=deadline())):
                try:
                    op()
                except self.m.NetworkError:
                    errors.append(True)
        worker = threading.Thread(target=foreign)
        worker.start()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [True]*4)
        self.assertTrue(t.connected)

    def test_tls12_peer_refused(self):
        ctx = self.context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        p = self.peer(lambda conn, p: p.release.wait(2), context=ctx)
        t = self.transport(p)
        with self.assertRaises(self.m.NetworkError):
            t.open(deadline_ns=deadline())
        self.assertFalse(t.connected)
        self.assertIsNone(t._socket)


    def test_write_want_read_and_handshake_want_write(self):
        data_sent = threading.Event()
        send_readable = threading.Event()
        received = []
        def action(conn, p):
            conn.sendall(b'r')
            data_sent.set()
            if not send_readable.wait(1):
                raise AssertionError('send did not request readable peer')
            conn.sendall(b'q')
            received.append(conn.recv(1))
            p.release.wait(2)
        p = self.peer(action)
        t = self.transport(p)
        real_hs = ssl.SSLSocket.do_handshake
        calls = []
        def handshake(sock, *a, **kw):
            if not sock.server_side:
                calls.append(True)
                if len(calls) == 1:
                    raise ssl.SSLWantWriteError()
            return real_hs(sock, *a, **kw)
        with patch.object(ssl.SSLSocket, 'do_handshake', handshake):
            t.open(deadline_ns=deadline())
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(data_sent.wait(1))
        # Drain TLS 1.3 tickets while retaining the one-byte application record.
        self.assertEqual(t.read(1, deadline_ns=deadline()), b'r')
        real_send = ssl.SSLSocket.send
        wants = []
        def send(sock, data, *args):
            if sock is t._socket and not wants:
                wants.append(True)
                send_readable.set()
                raise ssl.SSLWantReadError()
            return real_send(sock, data, *args)
        real_ready = t._ready
        events = []
        def ready(ev, d):
            events.append(ev)
            return real_ready(ev, d)
        with patch.object(ssl.SSLSocket, 'send', send), patch.object(t, '_ready', ready):
            t.write_all(b'w', deadline_ns=deadline())
        self.assertEqual(events, [__import__('select').POLLIN])
        t.close()
        p.release.set()
        self.assertTrue(p.done.wait(1))
        self.assertEqual(received, [b'w'])

    def test_post_syscall_cancel_never_returns_data(self):
        p = self.peer(lambda conn, p: (conn.sendall(b'x'), p.release.wait(2)))
        t = self.transport(p)
        t.open(deadline_ns=deadline())
        real_recv = ssl.SSLSocket.recv
        def recv(sock, size, *args):
            data = real_recv(sock, size, *args)
            if sock is t._socket:
                t.request_cancel()
            return data
        with patch.object(ssl.SSLSocket, 'recv', recv):
            with self.assertRaises(self.m.NetworkCancelled):
                t.read(1, deadline_ns=deadline())


    def test_cancel_at_handshake_publication_cannot_expose_session(self):
        p = self.peer(lambda conn, p: p.release.wait(2))
        t = self.transport(p)
        real_hs = ssl.SSLSocket.do_handshake
        owned = []
        def handshake(sock, *a, **kw):
            result = real_hs(sock, *a, **kw)
            if sock is t._socket:
                owned.extend([sock.fileno(), *t._wake])
                t.request_cancel()
            return result
        with patch.object(ssl.SSLSocket, 'do_handshake', handshake):
            with self.assertRaises(self.m.NetworkCancelled):
                t.open(deadline_ns=deadline())
        self.assertTrue(owned)
        self.assertFalse(t.connected)
        self.assertIsNone(t.security_info)
        for fd in owned:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_wrap_failure_cleans_tcp_socket_and_wake_pipe(self):
        p = self.peer(lambda conn, p: p.release.wait(2), context=None)
        t = self.transport(p)
        owned = []
        def fail(context, sock, *a, **kw):
            # Publish the peer FD before retiring the client FDs. Otherwise
            # accept() may legally reuse a just-closed numeric FD and make
            # fstat() mistake the peer socket for a leaked client resource.
            if not p.accepted.wait(1):
                raise AssertionError('server did not acquire peer before wrapper failure')
            owned.extend([sock.fileno(), *t._wake])
            raise ssl.SSLError('injected TLS wrapper failure')
        with patch.object(ssl.SSLContext, 'wrap_socket', fail):
            with self.assertRaises(self.m.NetworkError):
                t.open(deadline_ns=deadline())
        self.assertTrue(owned)
        for fd in owned:
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.assertFalse(t.connected)
        self.assertIsNone(t.security_info)

    def test_entry_requires_explicit_trust(self):
        m = importlib.import_module('tls_transport')
        with self.assertRaises(TypeError):
            m.TLSTransport('127.0.0.1', 443)
        self.assertTrue(issubclass(m.TLSTransport, m.TCPTransport))

if __name__ == '__main__':
    unittest.main()
