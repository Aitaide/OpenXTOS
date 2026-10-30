"""Linux PTY fixture evidence, not physical-device or hard-RT evidence."""
import errno
import fcntl
import importlib.util
import os
from pathlib import Path
import pty
import select
import sys
import termios
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('xtos_serial_transport', ROOT / 'tools/xtos-devices/serial_transport.py')


def deadline(ms=500):
    return time.monotonic_ns() + ms * 1_000_000


class SerialTransportTests(unittest.TestCase):
    def setUp(self):
        self.master, self.slave = pty.openpty()
        self.original = termios.tcgetattr(self.slave)
        self.module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(self.module)
        self.t = self.module.LinuxSerialTransport(os.ttyname(self.slave))

    def tearDown(self):
        try:
            self.t.close()
        except self.module.SerialError:
            pass  # HUP can prevent tty restoration; tested explicitly below.
        for fd in (self.master, self.slave):
            if fd is not None:
                os.close(fd)

    def test_open_raw_115200_restore(self):
        self.assertFalse(self.t.connected)
        self.assertIs(self.t.open(deadline_ns=deadline()), self.t)
        self.assertTrue(self.t.connected)
        attrs = termios.tcgetattr(self.slave)
        self.assertEqual(attrs[4:6], [termios.B115200, termios.B115200])
        self.assertEqual(attrs[0] & (termios.IXON | termios.IXOFF | termios.ICRNL), 0)
        self.assertEqual(attrs[1] & termios.OPOST, 0)
        self.assertEqual(attrs[2] & (termios.PARENB | termios.CSTOPB | termios.CRTSCTS), 0)
        self.assertEqual(attrs[2] & termios.CSIZE, termios.CS8)
        self.assertEqual(attrs[3] & (termios.ICANON | termios.ECHO | termios.ISIG), 0)
        # CAP_SYS_ADMIN can bypass open refusal; query the kernel flag directly.
        flag = bytearray(4)
        fcntl.ioctl(self.slave, 0x80045440, flag, True)  # Linux TIOCGEXCL
        self.assertEqual(int.from_bytes(flag, sys.byteorder), 1)
        self.t.close()
        self.assertFalse(self.t.connected)
        self.assertEqual(termios.tcgetattr(self.slave), self.original)
        fd = os.open(os.ttyname(self.slave), os.O_RDWR | os.O_NOCTTY)
        os.close(fd)
        self.t.close()


    def test_open_guards_and_owner(self):
        for bad in (True, 0, -1, 1.2, None):
            with self.subTest(deadline=bad), self.assertRaises(ValueError):
                self.t.open(deadline_ns=bad)
        with self.assertRaises(self.module.SerialTimeout):
            self.t.open(deadline_ns=1)
        for path in ('relative', '/etc/passwd', '/dev/stdin'):
            other = self.module.LinuxSerialTransport(path)
            with self.subTest(path=path), self.assertRaises((ValueError, self.module.SerialError)):
                other.open(deadline_ns=deadline())
            self.assertFalse(other.connected)
        self.t.open(deadline_ns=deadline())
        with self.assertRaises(self.module.SerialError):
            self.t.open(deadline_ns=deadline())
        errors = []
        def foreign():
            for operation in (lambda: self.t.open(deadline_ns=deadline()), self.t.close):
                try:
                    operation()
                except self.module.SerialError:
                    errors.append(True)
        worker = threading.Thread(target=foreign)
        worker.start()
        worker.join(1)
        self.assertEqual(errors, [True, True])
        self.assertTrue(self.t.connected)


    def test_read_raw_bytes_idle_timeout_hup(self):
        self.t.open(deadline_ns=deadline())
        payload = bytes(range(132))
        os.write(self.master, payload)
        self.assertEqual(self.t.read(132, deadline_ns=deadline()), payload)
        started = time.monotonic()
        with self.assertRaises(self.module.SerialTimeout):
            self.t.read(1, deadline_ns=deadline(25))
        self.assertLess(time.monotonic() - started, .5)
        self.assertTrue(self.t.connected)
        for value in (b'a', b'b', b'c'):
            os.write(self.master, value)
            self.assertEqual(self.t.read(1, deadline_ns=deadline()), value)
        os.write(self.master, b'undeliverable')
        os.close(self.master)
        self.master = None
        with self.assertRaises(self.module.SerialDisconnected):
            self.t.read(132, deadline_ns=deadline())


    def test_cancel_wakes_owner_and_reopen_is_fresh(self):
        self.t.open(deadline_ns=deadline())
        owner_fd = self.t._fd
        thread = threading.Thread(target=lambda: (time.sleep(.02), self.t.request_cancel()))
        thread.start()
        start = time.monotonic()
        try:
            with self.assertRaises(self.module.SerialCancelled):
                self.t.read(1, deadline_ns=deadline(1500))
        finally:
            thread.join(1)
        self.assertLess(time.monotonic() - start, .5)
        os.fstat(owner_fd)
        self.assertTrue(self.t.connected)
        with self.assertRaises(self.module.SerialCancelled):
            self.t.read(1, deadline_ns=deadline())
        self.t.close()
        self.t.request_cancel()
        self.t.open(deadline_ns=deadline())
        os.write(self.master, b'fresh')
        self.assertEqual(self.t.read(5, deadline_ns=deadline()), b'fresh')


    def test_write_short_retry_and_cancel_insensitive_cleanup(self):
        self.t.open(deadline_ns=deadline())
        real_write = os.write
        calls = []
        def short(fd, data):
            if fd == self.t._fd:
                calls.append(len(data))
                if len(calls) == 1:
                    raise InterruptedError(errno.EINTR, 'injected')
                if len(calls) == 2:
                    raise BlockingIOError(errno.EAGAIN, 'injected')
                if len(calls) == 3:
                    return 0
                return real_write(fd, data[:2])
            return real_write(fd, data)
        with patch.object(os, 'write', short):
            self.assertIsNone(self.t.write_all(b'abcdef', deadline_ns=deadline()))
        self.assertEqual(os.read(self.master, 6), b'abcdef')
        self.assertGreater(len(calls), 3)
        self.t.request_cancel()
        with self.assertRaises(self.module.SerialCancelled):
            self.t.write_all(b'blocked', deadline_ns=deadline())
        self.t.write_all(b's', deadline_ns=deadline(), cancel_sensitive=False)
        self.assertEqual(os.read(self.master, 1), b's')


    def test_close_closes_every_fd_even_if_one_close_reports_error(self):
        self.t.open(deadline_ns=deadline())
        owned = [self.t._fd, *self.t._wake]
        real_close = os.close
        failed = []
        def close_then_error(fd):
            real_close(fd)
            if fd == owned[0] and not failed:
                failed.append(fd)
                raise OSError(errno.EIO, 'injected after real close')
        with patch.object(os, 'close', close_then_error):
            with self.assertRaises(self.module.SerialError):
                self.t.close()
        self.assertFalse(self.t.connected)
        for fd in owned:
            with self.subTest(fd=fd), self.assertRaises(OSError):
                os.fstat(fd)
        self.assertEqual(termios.tcgetattr(self.slave), self.original)
        self.t.open(deadline_ns=deadline())


    def test_open_expiry_rollback_closes_tty_when_pipe_close_errors(self):
        real_pipe, real_close = os.pipe2, os.close
        captured = []
        opened = []
        real_open = os.open
        def observe_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd
        def slow_pipe(flags):
            pair = real_pipe(flags)
            captured.extend(pair)
            time.sleep(.03)
            return pair
        def close_then_error(fd):
            real_close(fd)
            if captured and fd == captured[0]:
                raise OSError(errno.EIO, 'injected rollback close error')
        with patch.object(os, 'pipe2', slow_pipe), patch.object(os, 'open', observe_open), patch.object(os, 'close', close_then_error):
            with self.assertRaises(self.module.SerialError):
                self.t.open(deadline_ns=deadline(10))
        self.assertFalse(self.t.connected)
        for fd in opened + captured:
            with self.subTest(fd=fd), self.assertRaises(OSError):
                os.fstat(fd)
        self.assertEqual(termios.tcgetattr(self.slave), self.original)
        flag = bytearray(4)
        fcntl.ioctl(self.slave, 0x80045440, flag, True)
        self.assertEqual(int.from_bytes(flag, sys.byteorder), 0)


    def test_poll_os_error_is_typed_disconnect(self):
        self.t.open(deadline_ns=deadline())
        real_poll = select.poll
        class BrokenPoll:
            def __init__(inner):
                inner.real = real_poll()
            def register(inner, *args):
                return inner.real.register(*args)
            def poll(inner, timeout):
                inner.real.poll(0)
                raise OSError(errno.EIO, 'injected tty poll failure')
        with patch.object(select, 'poll', BrokenPoll):
            with self.assertRaises(self.module.SerialDisconnected):
                self.t.read(1, deadline_ns=deadline())


class SerialCompletionTests(unittest.TestCase):
    # Reuse fixture helpers without inheriting/replacing the original 8 methods.
    setUp = SerialTransportTests.setUp
    tearDown = SerialTransportTests.tearDown

    def assert_closed(self, fds):
        for fd in fds:
            with self.subTest(closed_fd=fd), self.assertRaises(OSError) as caught:
                os.fstat(fd)
            self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_group4_parameter_boundaries_and_valid_extremes(self):
        self.t.request_cancel()
        for path in (None, 1, b'/dev/null', Path('/dev/null'), '', 'relative', '/dev/\x00null'):
            with self.subTest(path=repr(path)):
                other = self.module.LinuxSerialTransport(path)
                with self.assertRaises(ValueError):
                    other.open(deadline_ns=deadline())
                self.assertFalse(other.connected)
        for operation in (lambda: self.t.read(1, deadline_ns=deadline()),
                          lambda: self.t.write_all(b'x', deadline_ns=deadline())):
            with self.assertRaises(self.module.SerialError):
                operation()
        self.t.open(deadline_ns=deadline())
        with self.assertRaises(AttributeError):
            self.t.connected = False
        for fd in (self.t._fd, *self.t._wake):
            self.assertTrue(fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC)
            self.assertTrue(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_NONBLOCK)
        for capacity in (True, False, 0, -1, 133, 1.0, None, '1', b'1'):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                self.t.read(capacity, deadline_ns=deadline())
        for data in (b'', b'x' * 8193, bytearray(b'x'), memoryview(b'x'), 'x', 1, None):
            with self.subTest(data_type=type(data).__name__), self.assertRaises(ValueError):
                self.t.write_all(data, deadline_ns=deadline())
        for sensitive in (None, 0, 1, 'true', b'false'):
            with self.subTest(sensitive=sensitive), self.assertRaises(ValueError):
                self.t.write_all(b'x', deadline_ns=deadline(), cancel_sensitive=sensitive)
        for bad in (True, False, 0, -1, 1.5, None, '123'):
            for operation in (lambda: self.t.read(1, deadline_ns=bad),
                              lambda: self.t.write_all(b'x', deadline_ns=bad),
                              lambda: self.t.write_all(b's', deadline_ns=bad, cancel_sensitive=False)):
                with self.subTest(deadline=bad), self.assertRaises(ValueError):
                    operation()
        for operation in (lambda: self.t.read(1, deadline_ns=1),
                          lambda: self.t.write_all(b'x', deadline_ns=1),
                          lambda: self.t.write_all(b's', deadline_ns=1, cancel_sensitive=False)):
            with self.assertRaises(self.module.SerialTimeout):
                operation()
        for capacity in (1, 132):
            os.write(self.master, b'r' * capacity)
            self.assertEqual(self.t.read(capacity, deadline_ns=deadline()), b'r' * capacity)
        for length in (1, 8192):
            self.assertIsNone(self.t.write_all(b'w' * length, deadline_ns=deadline()))
            received = bytearray()
            poller = select.poll()
            poller.register(self.master, select.POLLIN)
            while len(received) < length:
                self.assertTrue(poller.poll(500))
                received.extend(os.read(self.master, length - len(received)))
            self.assertEqual(received, b'w' * length)
        self.t.close()
        for bad in (True, False, 0, -1, 1.5, None, '123'):
            with self.subTest(open_deadline=bad), self.assertRaises(ValueError):
                self.t.open(deadline_ns=bad)
        self.t.request_cancel()
        self.t.open(deadline_ns=deadline())
        os.write(self.master, b'n')
        self.assertEqual(self.t.read(1, deadline_ns=deadline()), b'n')

    def test_group4_final_symlink_and_non_tty_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temp:
            link = Path(temp) / 'tty'
            link.symlink_to(os.ttyname(self.slave))
            regular = Path(temp) / 'regular'
            regular.write_bytes(b'fixture')
            for path in (str(link), str(regular), '/dev/null', str(Path(temp) / 'missing')):
                with self.subTest(path=path):
                    other = self.module.LinuxSerialTransport(path)
                    with self.assertRaises(self.module.SerialError):
                        other.open(deadline_ns=deadline())
                    self.assertFalse(other.connected)
                    self.assertIsNone(other._wake)
        self.assertEqual(termios.tcgetattr(self.slave), self.original)

    def test_group4_open_faults_restore_termios_exclusivity_and_fds(self):
        from contextlib import ExitStack
        real_open, real_fstat = os.open, os.fstat
        real_get, real_set, real_ioctl, real_pipe = termios.tcgetattr, termios.tcsetattr, fcntl.ioctl, os.pipe2
        for fault in ('fstat', 'tcgetattr-1', 'tcgetattr-2', 'tcgetattr-3',
                      'tcsetattr-1', 'tcsetattr-2', 'ioctl', 'pipe'):
            with self.subTest(fault=fault):
                opened, calls, fired = [], {'get': 0, 'set': 0}, []
                def observe_open(*args, **kwargs):
                    fd = real_open(*args, **kwargs)
                    opened.append(fd)
                    return fd
                def fail():
                    fired.append(fault)
                    raise OSError(errno.EIO, fault)
                def fault_stat(fd):
                    value = real_fstat(fd)
                    if fault == 'fstat':
                        fail()
                    return value
                def fault_get(fd):
                    calls['get'] += 1
                    if fault == 'tcgetattr-' + str(calls['get']):
                        fail()
                    return real_get(fd)
                def fault_set(fd, when, attrs):
                    calls['set'] += 1
                    if fault == 'tcsetattr-' + str(calls['set']):
                        fail()
                    return real_set(fd, when, attrs)
                def fault_ioctl(fd, request, *args):
                    if fault == 'ioctl' and request == termios.TIOCEXCL:
                        fail()
                    return real_ioctl(fd, request, *args)
                def fault_pipe(flags):
                    if fault == 'pipe':
                        fail()
                    return real_pipe(flags)
                with ExitStack() as stack:
                    for target, name, hook in ((os, 'open', observe_open), (os, 'fstat', fault_stat),
                                               (termios, 'tcgetattr', fault_get), (termios, 'tcsetattr', fault_set),
                                               (self.module.tty, 'tcgetattr', fault_get),
                                               (self.module.tty, 'tcsetattr', fault_set),
                                               (fcntl, 'ioctl', fault_ioctl), (os, 'pipe2', fault_pipe)):
                        stack.enter_context(patch.object(target, name, hook))
                    with self.assertRaises(self.module.SerialError) as caught:
                        self.t.open(deadline_ns=deadline())
                self.assertEqual(fired, [fault])
                self.assertEqual(caught.exception.__cause__.errno, errno.EIO)
                self.assertFalse(self.t.connected)
                self.assertIsNone(self.t._wake)
                self.assertIsNone(self.t._owner)
                self.assertEqual(len(opened), 1)
                self.assert_closed(opened)
                self.assertEqual(real_get(self.slave), self.original)
                flag = bytearray(4)
                real_ioctl(self.slave, 0x80045440, flag, True)
                self.assertEqual(int.from_bytes(flag, sys.byteorder), 0)
                self.t.open(deadline_ns=deadline())
                self.t.close()

    def test_group4_close_restore_error_releases_all_and_reopens(self):
        self.t.open(deadline_ns=deadline())
        owned = [self.t._fd, *self.t._wake]
        real_set = termios.tcsetattr
        def fail_restore(fd, when, attrs):
            if fd == owned[0]:
                raise termios.error(errno.EIO, 'restore unavailable')
            return real_set(fd, when, attrs)
        with patch.object(termios, 'tcsetattr', fail_restore):
            with self.assertRaises(self.module.SerialError) as caught:
                self.t.close()
        self.assertEqual(caught.exception.__cause__.args[0], errno.EIO)
        self.assert_closed(owned)
        self.assertFalse(self.t.connected)
        self.assertIsNone(self.t._owner)
        self.assertIsNone(self.t._wake)
        flag = bytearray(4)
        fcntl.ioctl(self.slave, 0x80045440, flag, True)
        self.assertEqual(int.from_bytes(flag, sys.byteorder), 0)
        self.t.close()
        self.t.open(deadline_ns=deadline())
        os.write(self.master, b'x')
        self.assertEqual(self.t.read(1, deadline_ns=deadline()), b'x')
        self.t.close()
        # Failed restoration cannot restore bytes; restore the test fixture explicitly.
        real_set(self.slave, termios.TCSANOW, self.original)

    def test_group3_successful_syscalls_recheck_cancel_and_expiry(self):
        real_read, real_write = os.read, os.write
        for operation in ('read', 'write'):
            for boundary in ('cancel', 'expiry'):
                with self.subTest(operation=operation, boundary=boundary):
                    self.t.open(deadline_ns=deadline())
                    fd = self.t._fd
                    now = [1_000_000_000]
                    end = now[0] + 1_000_000_000
                    completed = []
                    if operation == 'read':
                        real_write(self.master, b'x')

                    def after_syscall():
                        completed.append(True)
                        if boundary == 'cancel':
                            self.t.request_cancel()
                        else:
                            now[0] = end

                    def read_then_boundary(target, size):
                        data = real_read(target, size)
                        if target == fd:
                            after_syscall()
                        return data

                    def write_then_boundary(target, data):
                        count = real_write(target, data)
                        if target == fd:
                            after_syscall()
                        return count

                    expected = self.module.SerialCancelled if boundary == 'cancel' else self.module.SerialTimeout
                    with patch.object(time, 'monotonic_ns', lambda: now[0]), \
                         patch.object(os, 'read', read_then_boundary), patch.object(os, 'write', write_then_boundary):
                        with self.assertRaises(expected):
                            if operation == 'read':
                                self.t.read(1, deadline_ns=end)
                            else:
                                self.t.write_all(b'x', deadline_ns=end)
                    self.assertEqual(completed, [True])
                    if operation == 'write':
                        self.assertEqual(real_read(self.master, 1), b'x')
                    self.t.close()

    def test_group3_terminal_flags_refuse_before_read(self):
        self.t.open(deadline_ns=deadline())
        os.write(self.master, b'x')
        real_poll, real_read = select.poll, os.read
        for terminal in (select.POLLHUP, select.POLLERR, select.POLLNVAL):
            with self.subTest(terminal=terminal):
                class TerminalPoll:
                    def __init__(inner):
                        inner.real = real_poll()
                    def register(inner, *args):
                        return inner.real.register(*args)
                    def poll(inner, timeout):
                        ready = inner.real.poll(0)
                        self.assertIn((self.t._fd, select.POLLIN), ready)
                        return [(self.t._fd, select.POLLIN | terminal)]
                with patch.object(select, 'poll', TerminalPoll), patch.object(os, 'read', wraps=real_read) as observed:
                    with self.assertRaises(self.module.SerialDisconnected):
                        self.t.read(1, deadline_ns=deadline())
                    observed.assert_not_called()
        self.assertEqual(self.t.read(1, deadline_ns=deadline()), b'x')

    def test_group3_zero_read_and_syscall_errors_disconnect(self):
        self.t.open(deadline_ns=deadline())
        os.write(self.master, b'x')
        real_read, real_write = os.read, os.write
        for mode in ('read-zero', 'read-eio', 'write-eio'):
            with self.subTest(mode=mode):
                calls = []
                def read_fault(fd, size):
                    calls.append(fd)
                    empty = real_read(fd, 0)
                    if mode == 'read-zero':
                        return empty
                    raise OSError(errno.EIO, mode)
                def write_fault(fd, data):
                    calls.append(fd)
                    real_write(fd, b'')
                    raise OSError(errno.EIO, mode)
                with patch.object(os, 'read', read_fault), patch.object(os, 'write', write_fault):
                    with self.assertRaises(self.module.SerialDisconnected):
                        if mode.startswith('read'):
                            self.t.read(1, deadline_ns=deadline())
                        else:
                            self.t.write_all(b'x', deadline_ns=deadline())
                self.assertEqual(calls, [self.t._fd])
        self.assertEqual(self.t.read(1, deadline_ns=deadline()), b'x')

    def test_group3_foreign_io_refuses_without_fd_access(self):
        self.t.open(deadline_ns=deadline())
        errors, unexpected = [], []
        def foreign():
            for operation in (lambda: self.t.read(1, deadline_ns=deadline()),
                              lambda: self.t.write_all(b'x', deadline_ns=deadline()),
                              lambda: self.t.write_all(b's', deadline_ns=deadline(), cancel_sensitive=False)):
                try:
                    operation()
                except self.module.SerialError as exc:
                    errors.append(str(exc))
                except BaseException as exc:
                    unexpected.append(repr(exc))
        worker = threading.Thread(target=foreign)
        try:
            with patch.object(os, 'read', wraps=os.read) as reads, \
                 patch.object(os, 'write', wraps=os.write) as writes, \
                 patch.object(select, 'poll', wraps=select.poll) as polls:
                worker.start()
                worker.join(2)
                reads.assert_not_called()
                writes.assert_not_called()
                polls.assert_not_called()
        finally:
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(unexpected, [])
        self.assertEqual(errors, ['operation requires session owner'] * 3)
        os.write(self.master, b'x')
        self.assertEqual(self.t.read(1, deadline_ns=deadline()), b'x')

    def test_group2_retries_keep_original_absolute_deadline(self):
        self.t.open(deadline_ns=deadline())
        real_poll, real_read, real_write = select.poll, os.read, os.write
        for mode in ('poll-eintr', 'poll-empty', 'read-eintr', 'read-eagain',
                     'write-eintr', 'write-eagain', 'write-zero'):
            with self.subTest(mode=mode):
                now, calls, budgets = [1_000_000_000], [], []
                end = now[0] + 30_000_000
                if mode.startswith('read'):
                    real_write(self.master, b'x')

                class RetryPoll:
                    def __init__(inner):
                        inner.real = real_poll()
                    def register(inner, *args):
                        return inner.real.register(*args)
                    def poll(inner, timeout):
                        budgets.append(timeout)
                        result = inner.real.poll(0)
                        if mode.startswith('poll'):
                            calls.append(mode)
                            now[0] += 5_000_000
                            if mode == 'poll-eintr':
                                raise InterruptedError(errno.EINTR, 'repeat poll')
                            return []
                        return result

                def fault_read(fd, size):
                    if fd != self.t._fd:
                        return real_read(fd, size)
                    real_read(fd, 0)
                    calls.append(mode)
                    now[0] += 5_000_000
                    if mode == 'read-eintr':
                        raise InterruptedError(errno.EINTR, mode)
                    raise BlockingIOError(errno.EAGAIN, mode)

                def fault_write(fd, data):
                    if fd != self.t._fd:
                        return real_write(fd, data)
                    real_write(fd, b'')
                    calls.append(mode)
                    now[0] += 5_000_000
                    if mode == 'write-eintr':
                        raise InterruptedError(errno.EINTR, mode)
                    if mode == 'write-eagain':
                        raise BlockingIOError(errno.EAGAIN, mode)
                    return 0

                with patch.object(time, 'monotonic_ns', lambda: now[0]), \
                     patch.object(select, 'poll', RetryPoll), \
                     patch.object(os, 'read', fault_read), patch.object(os, 'write', fault_write):
                    with self.assertRaises(self.module.SerialTimeout):
                        if mode.startswith('write'):
                            self.t.write_all(b'x', deadline_ns=end)
                        else:
                            self.t.read(1, deadline_ns=end)
                self.assertEqual(len(calls), 6)
                self.assertEqual(now[0], end)
                self.assertEqual(budgets, [30, 25, 20, 15, 10, 5])
                if mode.startswith('read'):
                    self.assertEqual(real_read(self.t._fd, 1), b'x')

    def test_group2_failed_cancel_wake_still_observed_by_poll(self):
        self.t.open(deadline_ns=deadline())
        entered = threading.Event()
        errors, timeouts = [], []
        real_write, real_poll = os.write, select.poll
        wake_fd = self.t._wake[1]

        class ObservedPoll:
            def __init__(inner):
                inner.real = real_poll()
            def register(inner, *args):
                return inner.real.register(*args)
            def poll(inner, timeout):
                timeouts.append(timeout)
                entered.set()
                return inner.real.poll(timeout)

        def fault(fd, data):
            if fd == wake_fd:
                raise InterruptedError(errno.EINTR, 'wake unavailable')
            return real_write(fd, data)

        def cancel():
            try:
                if not entered.wait(1):
                    raise RuntimeError('owner never polled')
                self.t.request_cancel()
            except BaseException as exc:
                errors.append(repr(exc))

        worker = threading.Thread(target=cancel)
        try:
            with patch.object(select, 'poll', ObservedPoll), patch.object(os, 'write', fault):
                worker.start()
                with self.assertRaises(self.module.SerialCancelled):
                    self.t.read(1, deadline_ns=deadline(300))
        finally:
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(timeouts)
        self.assertLessEqual(max(timeouts), 50, 'failed wake must not defer cancellation to caller deadline')

    def test_group2_repeated_cancel_eintr_releases_lock(self):
        self.t.open(deadline_ns=deadline())
        entered, release, done = (threading.Event() for _ in range(3))
        attempts, errors = [], []
        real_write = os.write

        def interrupted(fd, data):
            if fd == self.t._wake[1] and not release.is_set():
                attempts.append(fd)
                if len(attempts) >= 2:
                    entered.set()
                raise InterruptedError(errno.EINTR, 'repeat cancel interrupt')
            return real_write(fd, data)

        def cancel():
            try:
                self.t.request_cancel()
            except BaseException as exc:
                errors.append(repr(exc))
            finally:
                done.set()

        worker = threading.Thread(target=cancel)
        try:
            with patch.object(os, 'write', interrupted):
                worker.start()
                self.assertTrue(entered.wait(1))
                self.assertTrue(done.wait(.25), 'cancel retains lock in unbounded EINTR retry')
                self.assertLessEqual(len(attempts), 3)
                with self.assertRaises(self.module.SerialCancelled):
                    self.t.read(1, deadline_ns=deadline())
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        owned = [self.t._fd, *self.t._wake]
        self.t.close()
        self.assert_closed(owned)

    def test_group1_cancel_write_close_reopen_fd_reuse(self):
        opened, write_entered, release_write = (threading.Event() for _ in range(3))
        begin_close, close_attempted, reopened, finish = (threading.Event() for _ in range(4))
        errors, writes, old, sentinels, closed, fresh = [], [], [], [], [], []
        real_write, real_lock = os.write, self.t._cancel_lock

        class ObservedLock:
            def __enter__(inner):
                if threading.current_thread().name == 'serial-owner' and begin_close.is_set():
                    close_attempted.set()
                real_lock.acquire()
            def __exit__(inner, *unused):
                real_lock.release()

        self.t._cancel_lock = ObservedLock()

        def coordinated_write(fd, data):
            if old and fd == old[2] and not release_write.is_set():
                write_entered.set()
                if not release_write.wait(2):
                    raise RuntimeError('write release missing')
            result = real_write(fd, data)
            writes.append((fd, data))
            return result

        def owner():
            try:
                self.t.open(deadline_ns=deadline())
                old.extend([self.t._fd, *self.t._wake])
                opened.set()
                if not begin_close.wait(2):
                    raise RuntimeError('close release missing')
                self.t.close()
                for fd in old:
                    try:
                        os.fstat(fd)
                    except OSError as exc:
                        closed.append((fd, exc.errno))
                read_fd, write_fd = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
                sentinels.extend([read_fd, write_fd])
                # Deliberately reuse the old cancellation write descriptor.
                if write_fd != old[2]:
                    os.dup2(write_fd, old[2], inheritable=False)
                    sentinels.append(old[2])
                self.t.open(deadline_ns=deadline())
                fresh.extend([self.t._fd, *self.t._wake])
                reopened.set()
                if not finish.wait(2):
                    raise RuntimeError('finish release missing')
                with self.assertRaises(self.module.SerialCancelled):
                    self.t.read(1, deadline_ns=deadline())
            except BaseException as exc:
                errors.append(('owner', repr(exc)))
            finally:
                try:
                    self.t.close()
                except BaseException as exc:
                    errors.append(('owner-close', repr(exc)))
                for fd in set(sentinels):
                    os.close(fd)

        def canceller():
            try:
                self.t.request_cancel()
            except BaseException as exc:
                errors.append(('cancel', repr(exc)))

        owner_thread = threading.Thread(target=owner, name='serial-owner')
        cancel_thread = threading.Thread(target=canceller, name='serial-cancel')
        try:
            with patch.object(os, 'write', coordinated_write):
                owner_thread.start()
                self.assertTrue(opened.wait(2))
                cancel_thread.start()
                self.assertTrue(write_entered.wait(2))
                begin_close.set()
                self.assertTrue(close_attempted.wait(2))
                self.assertFalse(reopened.is_set())
                for fd in old:
                    os.fstat(fd)  # close cannot retire the pipe during os.write.
                release_write.set()
                self.assertTrue(reopened.wait(2))
                cancel_thread.join(2)
                self.assertFalse(cancel_thread.is_alive())
                self.assertEqual(closed, [(fd, errno.EBADF) for fd in old])
                self.assertEqual(writes, [(old[2], b'!')])
                self.t.request_cancel()  # reaches only the new session's pipe.
                with self.assertRaises(BlockingIOError):
                    os.read(sentinels[0], 1)
        finally:
            release_write.set()
            begin_close.set()
            finish.set()
            if cancel_thread.ident is not None:
                cancel_thread.join(3)
            owner_thread.join(3)
        self.assertFalse(owner_thread.is_alive())
        self.assertFalse(cancel_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(self.t.connected)
        self.assert_closed(set(old + sentinels + fresh))
        self.assertEqual(termios.tcgetattr(self.slave), self.original)


if __name__ == '__main__':
    unittest.main()
