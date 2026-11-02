"""Explicit verified TLS byte transport; no vendor or physical identity claim."""
import os
import select
import ssl
from typing import NamedTuple
from network_transport import (TCPTransport, NetworkError, NetworkTimeout,
                               NetworkCancelled, NetworkDisconnected)


class SecurityInfo(NamedTuple):
    protocol: str
    cipher: str
    server_hostname: str


class TLSTransport(TCPTransport):
    def __init__(self, address, port, *, server_hostname, cafile, certfile=None, keyfile=None):
        super().__init__(address, port)
        if (not isinstance(server_hostname, str) or not server_hostname
                or len(server_hostname) > 253
                or any(c.isspace() or c in '\x00/*\\' for c in server_hostname)):
            raise ValueError('explicit trusted server_hostname required')
        self._server_hostname = server_hostname
        self._cafile = self._explicit_path(cafile)
        if (certfile is None) != (keyfile is None):
            raise ValueError('certfile and keyfile must be supplied together')
        self._certfile = self._explicit_path(certfile) if certfile is not None else None
        self._keyfile = self._explicit_path(keyfile) if keyfile is not None else None
        self._security_info = None

    @staticmethod
    def _explicit_path(value):
        try:
            path = os.fspath(value)
        except TypeError as exc:
            raise ValueError('explicit certificate path required') from exc
        if not isinstance(path, str) or not path or chr(0) in path:
            raise ValueError('explicit certificate path required')
        return path

    @property
    def connected(self):
        return self._socket is not None and self._security_info is not None

    @property
    def security_info(self):
        return self._security_info

    def _context(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.hostname_checks_common_name = False
        context.load_verify_locations(cafile=self._cafile)
        if self._certfile is not None:
            # Refuse encrypted-key prompts; secrets never enter logs or metadata.
            def no_password():
                raise ValueError('interactive private key password is unsupported')
            context.load_cert_chain(self._certfile, self._keyfile, password=no_password)
        return context

    def open(self, *, deadline_ns):
        self._check_owner()
        if self._socket is not None:
            raise NetworkError('already open')
        self._deadline(deadline_ns)
        try:
            context = self._context()
            self._deadline(deadline_ns)
            super().open(deadline_ns=deadline_ns)
            self._check_cancel()
            self._socket = context.wrap_socket(
                self._socket, server_hostname=self._server_hostname,
                do_handshake_on_connect=False, suppress_ragged_eofs=False)
            while True:
                self._check_cancel()
                self._deadline(deadline_ns)
                try:
                    self._socket.do_handshake()
                    break
                except ssl.SSLWantReadError:
                    self._ready(select.POLLIN, deadline_ns)
                except ssl.SSLWantWriteError:
                    self._ready(select.POLLOUT, deadline_ns)
            self._check_cancel()
            self._deadline(deadline_ns)
            protocol, cipher = self._socket.version(), self._socket.cipher()
            if protocol != 'TLSv1.3' or not cipher or not self._socket.getpeercert():
                raise NetworkError('TLS verification incomplete')
            self._security_info = SecurityInfo(protocol, cipher[0], self._server_hostname)
            self._check_cancel()
            self._deadline(deadline_ns)
            return self
        except BaseException as exc:
            try:
                self.close()
            except NetworkError:
                pass
            if isinstance(exc, (OSError, ValueError)):
                raise NetworkError('TLS open failed') from exc
            raise

    def _start_io(self, deadline_ns):
        self._check_owner()
        if not self.connected:
            raise NetworkError('TLS not open')
        self._check_cancel()
        self._deadline(deadline_ns)

    def read(self, max_bytes, *, deadline_ns):
        self._check_owner()
        if not self.connected:
            raise NetworkError('TLS not open')
        if type(max_bytes) is not int or not 1 <= max_bytes <= 132:
            raise ValueError('max_bytes must be 1..132')
        while True:
            self._start_io(deadline_ns)
            try:
                data = self._socket.recv(max_bytes)
            except ssl.SSLWantReadError:
                self._ready(select.POLLIN, deadline_ns)
                continue
            except ssl.SSLWantWriteError:
                self._ready(select.POLLOUT, deadline_ns)
                continue
            except InterruptedError:
                continue
            except OSError as exc:
                raise NetworkDisconnected('TLS recv failed') from exc
            if not data:
                raise NetworkDisconnected('TLS EOF')
            self._check_cancel()
            self._deadline(deadline_ns)
            return data

    def write_all(self, data, *, deadline_ns):
        self._check_owner()
        if not self.connected:
            raise NetworkError('TLS not open')
        if not isinstance(data, bytes) or not 1 <= len(data) <= 8192:
            raise ValueError('data must be nonempty bytes of at most 8192 bytes')
        offset = 0
        while offset < len(data):
            self._start_io(deadline_ns)
            try:
                sent = self._socket.send(data[offset:])
            except ssl.SSLWantReadError:
                self._ready(select.POLLIN, deadline_ns)
                continue
            except ssl.SSLWantWriteError:
                self._ready(select.POLLOUT, deadline_ns)
                continue
            except InterruptedError:
                continue
            except OSError as exc:
                raise NetworkDisconnected('TLS send failed') from exc
            if sent == 0:
                raise NetworkDisconnected('TLS send made no progress')
            offset += sent
        self._check_cancel()
        self._deadline(deadline_ns)

    def close(self):
        self._check_owner()
        self._security_info = None
        super().close()
