"""Single-owner Cyton serial session; protocol match is not device authentication."""
from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import PurePosixPath

from cyton_protocol import parse_banner, parse_registers
from openbci_cyton import CHANNELS, SAMPLE_RATE_HZ, CytonPacketParser
from serial_transport import SerialCancelled, SerialTimeout
from xtos_devices import (Authentication, Capabilities, DeviceIdentity, Lifecycle,
                          LifecycleEvent, Provenance, Sample, TransportKind)

COMMAND_BUDGET_NS = 3_000_000_000
CLEANUP_BUDGET_NS = 100_000_000
REPLY_QUIET_NS = 20_000_000


class CytonSessionError(RuntimeError):
    """Retain a primary session failure and any distinct cleanup failure."""

    def __init__(self, message, *, primary=None, cleanup_error=None):
        super().__init__(message)
        self.primary = primary
        self.cleanup_error = cleanup_error


class CytonSerialPlugin:
    physical_device_evidence = False

    def __init__(self, identity, transport, *, reset_defaults, provenance):
        if not isinstance(identity, DeviceIdentity):
            raise ValueError('explicit DeviceIdentity required')
        if reset_defaults is not True:
            raise ValueError('reset_defaults=True is required because v resets board settings')
        if not isinstance(provenance, Provenance):
            raise ValueError('explicit input provenance required')
        path = getattr(transport, 'path', '')
        if provenance is Provenance.LIVE_DEVICE and (not isinstance(path, str) or
                PurePosixPath(path).parent != PurePosixPath('/dev') or
                not PurePosixPath(path).name.startswith(('ttyUSB', 'ttyACM', 'ttyS', 'ttyAMA'))):
            raise ValueError('live_device requires an explicitly selected supported hardware tty; PTY is fixture_replay')
        self.identity = identity
        self.transport = transport
        self.provenance = provenance
        self.authentication = Authentication.NOT_CHECKED
        self.protocol_match = False
        self.configuration = None
        self.firmware = None
        self.state = Lifecycle.DISCONNECTED
        self.generation = 0
        self._owner = None
        self._cancel = threading.Event()
        self._lifecycle_lock = threading.RLock()
        self.parser = CytonPacketParser()
        self.capabilities = Capabilities(CHANNELS, SAMPLE_RATE_HZ, 'nanovolt')
        self._events = deque(maxlen=64)
        self._event(Lifecycle.DISCONNECTED, 'created')

    @property
    def events(self):
        return tuple(self._events)

    def _event(self, state, reason):
        self.state = state
        self._events.append(LifecycleEvent(state, self.generation, reason,
                                           self.authentication, self.provenance))

    def _check_owner(self):
        if self._owner is not None and self._owner != threading.get_ident():
            raise CytonSessionError('session operation requires owner thread')

    @staticmethod
    def _deadline(deadline_ns):
        if type(deadline_ns) is not int or deadline_ns <= 0:
            raise ValueError('positive integer absolute deadline_ns required')
        if time.monotonic_ns() >= deadline_ns:
            raise CytonSessionError('session deadline expired')
        return deadline_ns

    def _reply(self, *, deadline_ns, capacity):
        data = bytearray()
        while True:
            self._deadline(deadline_ns)
            chunk = self.transport.read(132, deadline_ns=deadline_ns)
            if not isinstance(chunk, bytes) or not chunk or len(chunk) > 132:
                raise CytonSessionError('invalid serial reply bytes')
            data.extend(chunk)
            offset = data.find(b'$$$')
            if offset >= 0:
                if offset > capacity or offset+3 != len(data):
                    raise CytonSessionError('oversized reply or trailing bytes')
                self._quiet(deadline_ns=deadline_ns)
                return bytes(data[:offset])
            if len(data) > capacity+2:
                raise CytonSessionError('serial reply capacity exceeded')

    def _quiet(self, *, deadline_ns):
        """Require 20 ms with no bytes, within the caller's absolute budget.

        This rejects pending/nearby trailing bytes, not indefinitely future input.
        Run again before start to cover bytes arriving after profile admission.
        """
        self._deadline(deadline_ns)
        quiet_end = time.monotonic_ns()+REPLY_QUIET_NS
        try:
            self.transport.read(132, deadline_ns=min(deadline_ns, quiet_end))
        except SerialTimeout:
            self._deadline(deadline_ns)
            if self._cancel.is_set():
                raise SerialCancelled('session cancelled during reply guard')
            if time.monotonic_ns() < quiet_end:
                raise
            return
        raise CytonSessionError('trailing bytes after serial reply')

    def connect(self, *, deadline_ns):
        self._check_owner()
        self._deadline(deadline_ns)
        if self.state not in (Lifecycle.DISCONNECTED, Lifecycle.FAILED):
            raise CytonSessionError('close current session before reconnect')
        self._owner = threading.get_ident()
        self.parser = CytonPacketParser()
        self.protocol_match = False
        self.configuration = None
        self.firmware = None
        self.authentication = Authentication.NOT_CHECKED
        try:
            # Serialize only connection turnover with the complete cancel latch.
            # Handshake reads remain outside this lock so query cancellation works.
            with self._lifecycle_lock:
                self.generation += 1
                self._cancel.clear()
                self.transport.open(deadline_ns=deadline_ns)
            transaction = min(deadline_ns, time.monotonic_ns()+COMMAND_BUDGET_NS)
            self.transport.write_all(b's', deadline_ns=transaction)
            self.transport.write_all(b'v', deadline_ns=transaction)
            firmware = parse_banner(self._reply(deadline_ns=transaction, capacity=255))
            transaction = min(deadline_ns, time.monotonic_ns()+COMMAND_BUDGET_NS)
            self.transport.write_all(b'?', deadline_ns=transaction)
            configuration = parse_registers(self._reply(deadline_ns=transaction, capacity=8192))
            self._deadline(deadline_ns)
            if self._cancel.is_set():
                raise SerialCancelled('session cancelled before profile publication')
            self.firmware = firmware
            self.configuration = configuration
            self.protocol_match = True
            self._event(Lifecycle.CONNECTED, 'profile_verified')
            return self.generation
        except Exception as error:
            cleanup = self._cleanup()
            self._event(Lifecycle.FAILED, 'session_failed')
            raise CytonSessionError('Cyton connect failed', primary=error, cleanup_error=cleanup) from error

    def start(self, generation, *, deadline_ns):
        self._check_owner()
        if type(generation) is not int or generation != self.generation:
            raise CytonSessionError('stale or invalid generation')
        if self.state is not Lifecycle.CONNECTED or not self.protocol_match:
            raise CytonSessionError('verified connected profile required')
        try:
            self._deadline(deadline_ns)
            self._quiet(deadline_ns=deadline_ns)
            self.transport.write_all(b'b', deadline_ns=deadline_ns)
            self._event(Lifecycle.STREAMING, 'start_written')
        except Exception as error:
            cleanup = self._cleanup()
            self._event(Lifecycle.FAILED, 'session_failed')
            raise CytonSessionError('Cyton start failed', primary=error, cleanup_error=cleanup) from error

    def read(self, generation, *, deadline_ns):
        self._check_owner()
        if type(generation) is not int or generation != self.generation:
            raise CytonSessionError('stale or invalid generation')
        if self.state is not Lifecycle.STREAMING:
            raise CytonSessionError('streaming session required')
        try:
            self._deadline(deadline_ns)
            data = self.transport.read(132, deadline_ns=deadline_ns)
            if not isinstance(data, bytes) or not data or len(data) > 132:
                raise CytonSessionError('invalid bounded stream bytes')
            frames = self.parser.feed(data)
            stats = self.parser.stats
            if any((stats.discarded_bytes, stats.invalid_packets, stats.duplicate_packets,
                    stats.out_of_order_packets, stats.counter_gaps)):
                raise CytonSessionError('malformed Cyton stream')
            samples = tuple(Sample(
                identity=self.identity, transport_kind=TransportKind.SERIAL,
                capabilities=self.capabilities, generation=self.generation,
                authentication=Authentication.NOT_CHECKED, provenance=self.provenance,
                sequence=frame.sequence, sample_number=frame.sample_number,
                host_receive_monotonic_ns=frame.host_receive_monotonic_ns,
                timestamp_clock='host_parser_emission_monotonic', channels=frame.channels_nv,
                auxiliary=frame.auxiliary, footer=frame.footer) for frame in frames)
            self._deadline(deadline_ns)
            if self._cancel.is_set():
                raise SerialCancelled('session cancelled before sample publication')
            return samples
        except Exception as error:
            cleanup = self._cleanup()
            self._event(Lifecycle.FAILED, 'session_failed')
            raise CytonSessionError('Cyton read failed', primary=error, cleanup_error=cleanup) from error

    def request_cancel(self):
        """Ask the transport owner to stop; never close the tty here."""
        with self._lifecycle_lock:
            self._cancel.set()
            self.transport.request_cancel()

    def _cleanup(self):
        with self._lifecycle_lock:
            return self._cleanup_locked()

    def _cleanup_locked(self):
        errors = []
        try:
            if self.transport.connected:
                self.transport.write_all(b's', deadline_ns=time.monotonic_ns()+CLEANUP_BUDGET_NS,
                                         cancel_sensitive=False)
        except Exception as error:
            errors.append(error)
        finally:
            try:
                self.transport.close()
            except Exception as error:
                errors.append(error)
        self.protocol_match = False
        self.configuration = None
        self.firmware = None
        self.parser = CytonPacketParser()
        return tuple(errors) if errors else None

    def close(self):
        self._check_owner()
        errors = self._cleanup()
        self._event(Lifecycle.FAILED if errors else Lifecycle.DISCONNECTED,
                    'cleanup_failed' if errors else 'closed')
        if errors:
            raise CytonSessionError('Cyton cleanup failed', cleanup_error=errors)
