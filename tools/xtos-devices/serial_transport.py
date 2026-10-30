"""Bounded Linux tty transport; no discovery, protocol or physical claims.

Deadlines bound user-space retries, not arbitrary kernel ioctls or scheduling.
"""
import fcntl
import os
import select
import stat
import termios
import threading
import time
import tty


class SerialError(RuntimeError):
    """Transport or lifecycle failure."""


class SerialTimeout(SerialError):
    """Absolute operation budget expired."""


class SerialDisconnected(SerialError):
    """Terminal tty error or EOF."""


class SerialCancelled(SerialError):
    """Cancellation latched for this session."""


class LinuxSerialTransport:
    def __init__(self, path):
        self.path = path
        self._fd = None
        self._original = None
        self._owner = None
        self._cancel_lock = threading.Lock()
        self._wake = None
        self._cancelled = False

    def _check_owner(self):
        if self._owner is not None and self._owner != threading.get_ident():
            raise SerialError('operation requires session owner')

    @staticmethod
    def _deadline(deadline_ns):
        if type(deadline_ns) is not int or deadline_ns <= 0:
            raise ValueError('deadline_ns must be a positive integer')
        if time.monotonic_ns() >= deadline_ns:
            raise SerialTimeout('serial deadline expired')

    @property
    def connected(self):
        return self._fd is not None

    def open(self, *, deadline_ns):
        self._check_owner()
        if self.connected:
            raise SerialError('already open')
        self._deadline(deadline_ns)
        if not isinstance(self.path, str) or not os.path.isabs(self.path) or '\x00' in self.path:
            raise ValueError('explicit absolute device path required')
        fd = None
        original = None
        exclusive = False
        wake = None
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK |
                         os.O_CLOEXEC | os.O_NOFOLLOW)
            if not stat.S_ISCHR(os.fstat(fd).st_mode):
                raise SerialError('not a character device')
            original = termios.tcgetattr(fd)
            fcntl.ioctl(fd, termios.TIOCEXCL)
            exclusive = True
            tty.setraw(fd, termios.TCSANOW)
            attrs = termios.tcgetattr(fd)
            attrs[0] &= ~(termios.IXON | termios.IXOFF | termios.IXANY)
            attrs[2] &= ~(termios.PARENB | termios.CSTOPB | termios.CRTSCTS | termios.CSIZE)
            attrs[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[4] = attrs[5] = termios.B115200
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            wake = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
            self._deadline(deadline_ns)
            with self._cancel_lock:
                self._fd, self._original = fd, original
                self._owner = threading.get_ident()
                self._wake, self._cancelled = wake, False
            return self
        except (OSError, termios.error, SerialError) as exc:
            # Roll back every resource independently; keep the initiating error.
            if fd is not None:
                if original is not None:
                    try:
                        termios.tcsetattr(fd, termios.TCSANOW, original)
                    except (OSError, termios.error):
                        pass
                if exclusive:
                    try:
                        fcntl.ioctl(fd, termios.TIOCNXCL)
                    except OSError:
                        pass
            for owned_fd in ([fd] if fd is not None else []) + list(wake or ()):
                try:
                    os.close(owned_fd)
                except OSError:
                    pass
            if isinstance(exc, SerialError):
                raise
            raise SerialError('serial open failed') from exc

    def request_cancel(self):
        # Keep the write and pipe lifetime under one lock: never write a reused fd.
        with self._cancel_lock:
            if self._wake is None or self._cancelled:
                return
            self._cancelled = True
            # A signal storm must not retain the lifetime lock indefinitely.
            # _ready also checks the latch periodically if all wake writes fail.
            for _ in range(3):
                try:
                    os.write(self._wake[1], b'!')
                    return
                except InterruptedError:
                    continue
                except BlockingIOError:
                    return  # An already-full pipe is already a wakeup.

    def _check_cancel(self):
        with self._cancel_lock:
            if self._cancelled:
                raise SerialCancelled('serial session cancelled')

    def _ready(self, events, deadline_ns, cancel_sensitive=True):
        while True:
            if cancel_sensitive:
                self._check_cancel()
            self._deadline(deadline_ns)
            poller = select.poll()
            poller.register(self._fd, events)
            if cancel_sensitive:
                poller.register(self._wake[0], select.POLLIN)
            remaining = deadline_ns - time.monotonic_ns()
            timeout_ms = min(2147483647, max(0, (remaining + 999999) // 1000000))
            if cancel_sensitive:
                timeout_ms = min(timeout_ms, 50)  # Fallback for interrupted wake writes.
            try:
                ready = poller.poll(timeout_ms)
            except InterruptedError:
                continue
            except OSError as exc:
                raise SerialDisconnected('tty poll failed') from exc
            if cancel_sensitive:
                self._check_cancel()
            self._deadline(deadline_ns)
            for fd, flags in ready:
                if flags & (select.POLLHUP | select.POLLERR | select.POLLNVAL):
                    raise SerialDisconnected('terminal tty poll event')
            if ready:
                return

    def read(self, max_bytes, *, deadline_ns):
        self._check_owner()
        if not self.connected:
            raise SerialError('not open')
        if type(max_bytes) is not int or not 1 <= max_bytes <= 132:
            raise ValueError('max_bytes must be 1..132')
        while True:
            self._ready(select.POLLIN, deadline_ns)
            try:
                data = os.read(self._fd, max_bytes)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError as exc:
                raise SerialDisconnected('tty read failed') from exc
            if not data:
                raise SerialDisconnected('tty EOF')
            self._check_cancel()
            self._deadline(deadline_ns)
            return data

    def write_all(self, data, *, deadline_ns, cancel_sensitive=True):
        self._check_owner()
        if not self.connected:
            raise SerialError('not open')
        if not isinstance(data, bytes) or not 1 <= len(data) <= 8192:
            raise ValueError('data must be nonempty bytes of at most 8192 bytes')
        if type(cancel_sensitive) is not bool:
            raise ValueError('cancel_sensitive must be bool')
        offset = 0
        while offset < len(data):
            self._ready(select.POLLOUT, deadline_ns, cancel_sensitive)
            try:
                count = os.write(self._fd, data[offset:])
            except (BlockingIOError, InterruptedError):
                continue
            except OSError as exc:
                raise SerialDisconnected('tty write failed') from exc
            offset += count
        if cancel_sensitive:
            self._check_cancel()
        self._deadline(deadline_ns)

    def close(self):
        self._check_owner()
        if self._fd is None:
            return
        with self._cancel_lock:
            fd, original, wake = self._fd, self._original, self._wake
            self._fd, self._original, self._owner = None, None, None
            self._wake, self._cancelled = None, False
        error = None
        try:
            termios.tcsetattr(fd, termios.TCSANOW, original)
        except (OSError, termios.error) as exc:
            error = exc
        finally:
            try:
                fcntl.ioctl(fd, termios.TIOCNXCL)
            except OSError as exc:
                error = error or exc
            finally:
                for owned_fd in (fd, *wake):
                    try:
                        os.close(owned_fd)
                    except OSError as exc:
                        error = error or exc
        if error is not None:
            raise SerialError('serial close restoration failed') from error
