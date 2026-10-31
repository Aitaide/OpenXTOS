"""Real loopback software evidence, not identity, hardware or RT evidence."""
import errno
import importlib.util
import os
from pathlib import Path
import select
import socket
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('xtos_network_transport', ROOT / 'tools/xtos-devices/network_transport.py')


def deadline(ms=600):
    return time.monotonic_ns() + ms * 1_000_000


class NetworkTransportTests(unittest.TestCase):
    def setUp(self):
        self.m = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(self.m)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(4)
        self.listener.settimeout(1)
        self.t = self.m.TCPTransport('127.0.0.1', self.listener.getsockname()[1])
        self.peers = []

    def tearDown(self):
        self.t.close()
        for peer in self.peers:
            peer.close()
        self.listener.close()

    def connect(self):
        self.t.open(deadline_ns=deadline())
        peer, _ = self.listener.accept()
        peer.settimeout(1)
        self.peers.append(peer)
        return peer

    def test_numeric_constructor_has_no_io(self):
        with patch.object(socket, 'socket', side_effect=AssertionError('socket in constructor')), \
             patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS')):
            for address in ('127.0.0.1', '::1', '2001:db8::1'):
                self.assertFalse(self.m.TCPTransport(address, 65535).connected)
            for address in ('localhost', '', ' 127.0.0.1', '127.0.0.1 ', '127.0.0.1\x00', 'fe80::1%lo', None, 1):
                with self.subTest(address=address), self.assertRaises(ValueError):
                    self.m.TCPTransport(address, 80)
            for port in (True, False, 0, -1, 65536, 1.0, '80', None):
                with self.subTest(port=port), self.assertRaises(ValueError):
                    self.m.TCPTransport('127.0.0.1', port)
        with self.assertRaises(AttributeError):
            self.t.connected = True


    def test_open_publishes_nonblocking_session_then_releases_all(self):
        peer = self.connect()
        self.assertTrue(self.t.connected)
        self.assertFalse(self.t._socket.getblocking())
        owned = [self.t._socket.fileno(), *self.t._wake]
        for fd in owned:
            self.assertFalse(os.get_inheritable(fd))
        self.assertFalse(os.get_blocking(self.t._wake[0]))
        self.assertFalse(os.get_blocking(self.t._wake[1]))
        with self.assertRaises(self.m.NetworkError):
            self.t.open(deadline_ns=deadline())
        self.t.close()
        self.assertFalse(self.t.connected)
        for fd in owned:
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.assertEqual(peer.recv(1), b'')
        self.t.close()
        self.connect()

    def test_open_deadline_validation_precedes_io(self):
        with patch.object(socket, 'socket', side_effect=AssertionError('unexpected socket')):
            for bad in (True, False, 0, -1, 1.5, None):
                with self.subTest(value=bad), self.assertRaises(ValueError):
                    self.t.open(deadline_ns=bad)
            with self.assertRaises(self.m.NetworkTimeout):
                self.t.open(deadline_ns=1)


    def test_read_fragments_capacity_idle_and_eof(self):
        peer = self.connect()
        payload = bytes(range(132))
        peer.sendall(payload)
        self.assertEqual(self.t.read(132, deadline_ns=deadline()), payload)
        for fragment in (b'a', b'bc', b'd'):
            peer.sendall(fragment)
            self.assertEqual(self.t.read(len(fragment), deadline_ns=deadline()), fragment)
        with self.assertRaises(self.m.NetworkTimeout):
            self.t.read(1, deadline_ns=deadline(15))
        self.assertTrue(self.t.connected)
        peer.shutdown(socket.SHUT_WR)
        with self.assertRaises(self.m.NetworkDisconnected):
            self.t.read(1, deadline_ns=deadline())
        self.assertTrue(self.t.connected)

    def test_write_full_capacity_and_input_bounds(self):
        peer = self.connect()
        data = b'z' * 8192
        self.assertIsNone(self.t.write_all(data, deadline_ns=deadline()))
        received = b''
        while len(received) < len(data):
            received += peer.recv(8192)
        self.assertEqual(received, data)
        for bad in (True, 0, -1, 133, 1.0, None):
            with self.subTest(read=bad), self.assertRaises(ValueError):
                self.t.read(bad, deadline_ns=deadline())
        for bad in (b'', b'x' * 8193, bytearray(b'x'), 'x', None):
            with self.subTest(write=type(bad)), self.assertRaises(ValueError):
                self.t.write_all(bad, deadline_ns=deadline())
        for bad in (True, 0, -1, None, 1.0):
            with self.assertRaises(ValueError):
                self.t.read(1, deadline_ns=bad)
            with self.assertRaises(ValueError):
                self.t.write_all(b'x', deadline_ns=bad)


    def test_cancel_latched_wakes_read_and_reopen_isolated(self):
        self.t.request_cancel()
        peer = self.connect()
        errors = []
        def cancel():
            try:
                time.sleep(.015)
                self.t.request_cancel()
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=cancel)
        worker.start()
        start = time.monotonic()
        try:
            with self.assertRaises(self.m.NetworkCancelled):
                self.t.read(1, deadline_ns=deadline(1200))
        finally:
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertLess(time.monotonic()-start, .4)
        peer.sendall(b'old')
        for op in (lambda: self.t.read(1, deadline_ns=deadline()),
                   lambda: self.t.write_all(b'x', deadline_ns=deadline())):
            with self.assertRaises(self.m.NetworkCancelled):
                op()
        self.t.close()
        self.t.request_cancel()
        peer = self.connect()
        peer.sendall(b'new')
        self.assertEqual(self.t.read(3, deadline_ns=deadline()), b'new')


    def test_cancel_eintr_is_bounded_and_poll_fallback_observes_latch(self):
        self.connect()
        calls, errors, timeouts = [], [], []
        real_poll = select.poll
        entered = threading.Event()
        class Poll:
            def __init__(inner):
                inner.p = real_poll()
            def register(inner, *args):
                return inner.p.register(*args)
            def poll(inner, ms):
                timeouts.append(ms)
                entered.set()
                return inner.p.poll(ms)
        def interrupted(fd, data):
            calls.append(fd)
            raise InterruptedError(errno.EINTR, 'injected')
        def cancel():
            try:
                if not entered.wait(1):
                    raise AssertionError('owner did not poll')
                self.t.request_cancel()
            except BaseException as exc:
                errors.append(exc)
        with patch.object(os, 'write', interrupted), patch.object(select, 'poll', Poll):
            worker = threading.Thread(target=cancel)
            worker.start()
            start = time.monotonic()
            try:
                with self.assertRaises(self.m.NetworkCancelled):
                    self.t.read(1, deadline_ns=deadline(700))
            finally:
                worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 3)
        self.assertTrue(timeouts)
        self.assertLessEqual(max(timeouts), 50)
        self.assertLess(time.monotonic()-start, .4)

    def test_cancel_write_lifetime_blocks_close_and_fd_reuse(self):
        entered, release, do_close, closed, done = [threading.Event() for _ in range(5)]
        opened = threading.Event()
        errors, owned, observations = [], [], []
        sentinel_r, sentinel_w = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        real_write = os.write
        def held_write(fd, data):
            entered.set()
            if not release.wait(2):
                raise AssertionError('write barrier timeout')
            return real_write(fd, data)
        def owner():
            duplicate = None
            try:
                self.t.open(deadline_ns=deadline())
                owned.extend([self.t._socket.fileno(), *self.t._wake])
                opened.set()
                if not do_close.wait(1):
                    raise AssertionError('close not requested')
                self.t.close()
                closed.set()
                for fd in owned:
                    try:
                        os.fstat(fd)
                        raise AssertionError('transport resource leaked')
                    except OSError:
                        pass
                duplicate = owned[-1]
                os.dup2(sentinel_w, duplicate)
                if not done.wait(2):
                    raise AssertionError('cancel did not finish')
                try:
                    observations.append(os.read(sentinel_r, 1))
                except BlockingIOError:
                    observations.append(b'')
            except BaseException as exc:
                errors.append(exc)
            finally:
                self.t.close()
                if duplicate is not None:
                    os.close(duplicate)
        def cancel():
            try:
                self.t.request_cancel()
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()
        owner_thread = threading.Thread(target=owner)
        notifier = threading.Thread(target=cancel)
        try:
            owner_thread.start()
            self.assertTrue(opened.wait(1))
            peer, _ = self.listener.accept()
            self.peers.append(peer)
            with patch.object(os, 'write', held_write):
                notifier.start()
                self.assertTrue(entered.wait(1))
                do_close.set()
                premature = closed.wait(.04)
                release.set()
                notifier.join(1)
                owner_thread.join(1)
            self.assertFalse(premature, 'close retired fd while cancellation write was in flight')
            self.assertFalse(owner_thread.is_alive())
            self.assertFalse(notifier.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(observations, [b''])
        finally:
            release.set()
            do_close.set()
            done.set()
            owner_thread.join(2)
            if notifier.ident is not None:
                notifier.join(2)
            os.close(sentinel_r)
            os.close(sentinel_w)


    def test_post_syscall_cancel_never_publishes_success(self):
        peer = self.connect()
        peer.sendall(b'x')
        real_recv = socket.socket.recv
        def recv(sock, n):
            data = real_recv(sock, n)
            self.t.request_cancel()
            return data
        with patch.object(socket.socket, 'recv', recv):
            with self.assertRaises(self.m.NetworkCancelled):
                self.t.read(1, deadline_ns=deadline())
        self.t.close()
        self.connect()
        real_send = socket.socket.send
        def send(sock, data):
            n = real_send(sock, data)
            self.t.request_cancel()
            return n
        with patch.object(socket.socket, 'send', send):
            with self.assertRaises(self.m.NetworkCancelled):
                self.t.write_all(b'x', deadline_ns=deadline())

    def test_open_oserror_is_transport_error(self):
        with patch.object(socket, 'socket', side_effect=OSError(errno.EMFILE, 'injected')):
            with self.assertRaises(self.m.NetworkError):
                self.t.open(deadline_ns=deadline())
        self.assertFalse(self.t.connected)

    def test_owner_rejection_preserves_session(self):
        self.connect()
        errors = []
        def foreign():
            for op in (lambda: self.t.open(deadline_ns=deadline()), self.t.close,
                       lambda: self.t.read(1, deadline_ns=deadline()),
                       lambda: self.t.write_all(b'x', deadline_ns=deadline())):
                try:
                    op()
                except self.m.NetworkError:
                    errors.append(True)
        worker = threading.Thread(target=foreign)
        worker.start()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [True]*4)
        self.assertTrue(self.t.connected)

    def test_short_write_eintr_eagain_and_zero(self):
        peer = self.connect()
        real_send = socket.socket.send
        calls = []
        def send(sock, data):
            calls.append(len(data))
            if len(calls) == 1:
                raise InterruptedError(errno.EINTR, 'injected')
            if len(calls) == 2:
                raise BlockingIOError(errno.EAGAIN, 'injected')
            return real_send(sock, data[:2])
        with patch.object(socket.socket, 'send', send):
            self.t.write_all(b'abcdef', deadline_ns=deadline())
        self.assertEqual(peer.recv(6), b'abcdef')
        self.assertEqual(len(calls), 5)
        with patch.object(socket.socket, 'send', return_value=0):
            with self.assertRaises(self.m.NetworkDisconnected):
                self.t.write_all(b'x', deadline_ns=deadline())
        self.assertTrue(self.t.connected)

    def test_terminal_poll_with_data_is_fail_closed(self):
        peer = self.connect()
        peer.sendall(b'x')
        class Poll:
            def register(inner, *args):
                pass
            def poll(inner, ms):
                return [(self.t._socket.fileno(), select.POLLIN | select.POLLERR)]
        with patch.object(select, 'poll', Poll), patch.object(socket.socket, 'recv', side_effect=AssertionError('must not recv')):
            with self.assertRaises(self.m.NetworkDisconnected):
                self.t.read(1, deadline_ns=deadline())

    def test_close_continues_after_pipe_close_error(self):
        self.connect()
        owned = [self.t._socket.fileno(), *self.t._wake]
        real_close = os.close
        def close(fd):
            real_close(fd)
            if fd == owned[1]:
                raise OSError(errno.EIO, 'after real close')
        with patch.object(os, 'close', close):
            with self.assertRaises(self.m.NetworkError):
                self.t.close()
        for fd in owned:
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.assertFalse(self.t.connected)

    def test_ipv6_loopback(self):
        try:
            listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            listener.bind(('::1', 0))
        except OSError as exc:
            if 'listener' in locals():
                listener.close()
            self.skipTest('IPv6 unavailable: %s' % exc)
        listener.listen(1)
        listener.settimeout(1)
        transport = self.m.TCPTransport('::1', listener.getsockname()[1])
        peer = None
        try:
            transport.open(deadline_ns=deadline())
            peer, _ = listener.accept()
            peer.sendall(b'v6')
            self.assertEqual(transport.read(2, deadline_ns=deadline()), b'v6')
        finally:
            transport.close()
            if peer is not None:
                peer.close()
            listener.close()


    def test_so_error_failure_rolls_back_socket_and_pipe(self):
        before = set(os.listdir('/proc/self/fd'))
        real_get = socket.socket.getsockopt
        calls = []
        def get(sock, level, option, *args):
            if option == socket.SO_ERROR:
                calls.append(True)
                return errno.ECONNREFUSED
            return real_get(sock, level, option, *args)
        with patch.object(socket.socket, 'getsockopt', get):
            with self.assertRaises(self.m.NetworkError):
                self.t.open(deadline_ns=deadline())
        self.assertEqual(calls, [True])
        self.assertFalse(self.t.connected)
        self.assertEqual(set(os.listdir('/proc/self/fd')), before)

    def test_recv_retries_and_post_recv_deadline(self):
        peer = self.connect()
        peer.sendall(b'xy')
        real_recv = socket.socket.recv
        calls = []
        def recv(sock, n):
            calls.append(True)
            if len(calls) == 1:
                raise InterruptedError(errno.EINTR, 'injected')
            if len(calls) == 2:
                raise BlockingIOError(errno.EAGAIN, 'injected')
            return real_recv(sock, n)
        with patch.object(socket.socket, 'recv', recv):
            self.assertEqual(self.t.read(1, deadline_ns=deadline()), b'x')
        self.assertEqual(len(calls), 3)
        def late(sock, n):
            data = real_recv(sock, n)
            time.sleep(.02)
            return data
        with patch.object(socket.socket, 'recv', late):
            with self.assertRaises(self.m.NetworkTimeout):
                self.t.read(1, deadline_ns=deadline(10))
        self.assertTrue(self.t.connected)


if __name__ == '__main__':
    unittest.main()
