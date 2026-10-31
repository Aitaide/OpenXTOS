"""Explicit bounded TCP bytes; no discovery, authentication or device claims."""
import ipaddress
import os
import select
import socket
import threading
import time
import errno


class NetworkError(RuntimeError):
    """Transport or lifecycle failure."""


class NetworkTimeout(NetworkError):
    """The absolute operation deadline expired."""


class NetworkCancelled(NetworkError):
    """Cancellation is latched for this session."""


class NetworkDisconnected(NetworkError):
    """Terminal socket error or EOF."""


class TCPTransport:
    def __init__(self, address, port):
        if not isinstance(address, str) or '%' in address:
            raise ValueError('numeric IPv4 or IPv6 address without zone required')
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError('numeric IPv4 or IPv6 address required') from exc
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('port must be 1..65535')
        self._endpoint = (str(parsed), port)
        self._family = socket.AF_INET if parsed.version == 4 else socket.AF_INET6
        self._socket = None
        self._wake = None
        self._owner = None
        self._cancelled = False
        self._lifetime = threading.Lock()

    @property
    def connected(self):
        return self._socket is not None

    @staticmethod
    def _deadline(deadline_ns):
        if type(deadline_ns) is not int or deadline_ns <= 0:
            raise ValueError('deadline_ns must be a positive integer')
        if time.monotonic_ns() >= deadline_ns:
            raise NetworkTimeout('TCP deadline expired')

    def _check_owner(self):
        if self._owner is not None and self._owner != threading.get_ident():
            raise NetworkError('operation requires session owner')

    def open(self, *, deadline_ns):
        self._check_owner()
        if self.connected:
            raise NetworkError('already open')
        self._deadline(deadline_ns)
        sock, wake = None, None
        try:
            sock = socket.socket(self._family, socket.SOCK_STREAM)
            sock.setblocking(False)
            sock.set_inheritable(False)
            wake = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
            self._deadline(deadline_ns)
            result = sock.connect_ex(self._endpoint)
            if result not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR):
                raise NetworkError('TCP connect failed: %s' % result)
            while True:
                self._deadline(deadline_ns)
                poller = select.poll()
                poller.register(sock, select.POLLOUT)
                remaining = deadline_ns - time.monotonic_ns()
                try:
                    ready = poller.poll(min(2147483647, max(0, (remaining + 999999) // 1000000)))
                except InterruptedError:
                    continue
                self._deadline(deadline_ns)
                if not ready:
                    continue
                error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if error or any(flags & (select.POLLHUP | select.POLLERR | select.POLLNVAL) for _, flags in ready):
                    raise NetworkError('TCP connect failed: %s' % error)
                break
            self._deadline(deadline_ns)
            with self._lifetime:
                self._socket, self._wake = sock, wake
                self._owner = threading.get_ident()
                self._cancelled = False
            return self
        except (OSError, NetworkError) as exc:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            for fd in wake or ():
                try:
                    os.close(fd)
                except OSError:
                    pass
            if isinstance(exc, NetworkError):
                raise
            raise NetworkError('TCP open failed') from exc

    def request_cancel(self):
        # The descriptor and actual write share close's lifetime lock.
        with self._lifetime:
            if self._wake is None or self._cancelled:
                return
            self._cancelled = True
            for _ in range(3):
                try:
                    os.write(self._wake[1], b'!')
                    return
                except InterruptedError:
                    continue
                except OSError:
                    # Full pipe is already readable; any failed notification is
                    # covered by the latch and bounded poll fallback.
                    return

    def _check_cancel(self):
        with self._lifetime:
            if self._cancelled:
                raise NetworkCancelled('TCP session cancelled')

    def _ready(self, events, deadline_ns):
        while True:
            self._check_cancel()
            self._deadline(deadline_ns)
            poller = select.poll()
            poller.register(self._socket, events)
            poller.register(self._wake[0], select.POLLIN)
            remaining = deadline_ns - time.monotonic_ns()
            try:
                ready = poller.poll(min(50, max(0, (remaining + 999999) // 1000000)))
            except InterruptedError:
                continue
            except OSError as exc:
                raise NetworkDisconnected('TCP poll failed') from exc
            self._check_cancel()
            self._deadline(deadline_ns)
            if any(flags & (select.POLLHUP | select.POLLERR | select.POLLNVAL) for _, flags in ready):
                raise NetworkDisconnected('terminal TCP poll event')
            if ready:
                return

    def read(self, max_bytes, *, deadline_ns):
        self._check_owner()
        if not self.connected:
            raise NetworkError('not open')
        if type(max_bytes) is not int or not 1 <= max_bytes <= 132:
            raise ValueError('max_bytes must be 1..132')
        while True:
            self._ready(select.POLLIN, deadline_ns)
            try:
                data = self._socket.recv(max_bytes)
            except (InterruptedError, BlockingIOError):
                continue
            except OSError as exc:
                raise NetworkDisconnected('TCP recv failed') from exc
            if not data:
                raise NetworkDisconnected('TCP EOF')
            self._check_cancel()
            self._deadline(deadline_ns)
            return data

    def write_all(self, data, *, deadline_ns):
        self._check_owner()
        if not self.connected:
            raise NetworkError('not open')
        if not isinstance(data, bytes) or not 1 <= len(data) <= 8192:
            raise ValueError('data must be nonempty bytes of at most 8192 bytes')
        offset = 0
        while offset < len(data):
            self._ready(select.POLLOUT, deadline_ns)
            try:
                sent = self._socket.send(data[offset:])
            except (InterruptedError, BlockingIOError):
                continue
            except OSError as exc:
                raise NetworkDisconnected('TCP send failed') from exc
            if sent == 0:
                raise NetworkDisconnected('TCP send made no progress')
            offset += sent
        self._check_cancel()
        self._deadline(deadline_ns)

    def close(self):
        self._check_owner()
        with self._lifetime:
            sock, wake = self._socket, self._wake
            self._socket, self._wake, self._owner = None, None, None
            self._cancelled = False
        error = None
        if sock is not None:
            try:
                sock.close()
            except OSError as exc:
                error = exc
        for fd in wake or ():
            try:
                os.close(fd)
            except OSError as exc:
                error = error or exc
        if error is not None:
            raise NetworkError('TCP close failed') from error
