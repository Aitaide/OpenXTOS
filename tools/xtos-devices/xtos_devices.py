"""Small synchronous device contract; fixture-only implementation, no real I/O.

The host provides ``openbci_cyton`` from tools/xtos-eeg-pipeline on sys.path.
Transport kinds describe an intended medium, not proof of physical access.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol
import time

from openbci_cyton import CHANNELS, PACKET_SIZE, SAMPLE_RATE_HZ, CytonFrame, CytonPacketParser

MAX_READ_BYTES = 4 * PACKET_SIZE


class TransportKind(str, Enum):
    SERIAL = "serial"
    USB = "usb"
    BLE = "ble"
    NETWORK = "network"


class Provenance(str, Enum):
    FIXTURE_REPLAY = "fixture_replay"
    LIVE_DEVICE = "live_device"


class Lifecycle(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    STREAMING = "streaming"
    FAILED = "failed"


class Authentication(str, Enum):
    NOT_CHECKED = "not_checked"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class DeviceIdentity:
    device_id: str
    model: str


@dataclass(frozen=True)
class Capabilities:
    channels: int
    sample_rate_hz: int
    unit: str


@dataclass(frozen=True)
class LifecycleEvent:
    state: Lifecycle
    generation: int
    reason: str
    authentication: Authentication
    provenance: Provenance


class PluginRegistry:
    """Explicit factories only: no import discovery or automatic device access."""

    def __init__(self):
        self._factories = {}

    def register(self, name: str, factory) -> None:
        if name in self._factories:
            raise ValueError(f"duplicate plugin: {name}")
        self._factories[name] = factory

    def lookup(self, name: str):
        if name not in self._factories:
            raise KeyError(f"unknown plugin: {name}")
        return self._factories[name]


class Transport(Protocol):
    """Nonblocking fixture boundary: at most max_bytes, b'' is terminal EOF.

    connect returns a simulated authentication result, not a credential proof.
    Implementations must not perform real I/O in this slice.
    """

    kind: TransportKind
    provenance: Provenance
    physical_device_evidence: bool

    def connect(self) -> Authentication: ...

    def read(self, max_bytes: int) -> bytes: ...

    def disconnect(self) -> None: ...


class SimulatedTransport:
    """Finite in-memory sessions. Each connect consumes the next supplied session."""

    provenance = Provenance.FIXTURE_REPLAY
    physical_device_evidence = False

    def __init__(self, sessions, *, kind=TransportKind.SERIAL):
        self.kind = TransportKind(kind)
        self._sessions = iter(sessions)
        self._chunks = iter(())
        self.connected = False
        self.authentication = Authentication.ACCEPTED

    def connect(self) -> Authentication:
        self._chunks = iter(next(self._sessions, ()))
        self.connected = True
        return self.authentication

    def read(self, max_bytes: int) -> bytes:
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_READ_BYTES:
            raise ValueError("read capacity must be within MAX_READ_BYTES")
        if not self.connected:
            raise RuntimeError("transport disconnected")
        chunk = next(self._chunks, b"")
        if isinstance(chunk, Exception):
            raise chunk
        if not isinstance(chunk, bytes):
            raise RuntimeError("malformed transport bytes")
        if len(chunk) > max_bytes:
            self.disconnect()
            raise RuntimeError("transport chunk exceeds capacity")
        return chunk

    def disconnect(self) -> None:
        self.connected = False
        self._chunks = iter(())


@dataclass(frozen=True)
class Sample:
    identity: DeviceIdentity
    transport_kind: TransportKind
    capabilities: Capabilities
    generation: int
    authentication: Authentication
    provenance: Provenance
    sequence: int
    sample_number: int
    host_receive_monotonic_ns: int
    timestamp_clock: str
    channels: tuple[int, ...]
    auxiliary: bytes
    footer: int
    action: str = "no_action"
    physical_device_evidence: bool = False


class CytonPlugin:
    """Adapt canonical Cyton frames; no commands are sent to any device."""

    provenance = Provenance.FIXTURE_REPLAY
    physical_device_evidence = False
    capabilities = Capabilities(CHANNELS, SAMPLE_RATE_HZ, "nanovolt")

    def __init__(self, identity: DeviceIdentity, transport: Transport,
                 *, monotonic_ns=time.monotonic_ns):
        if transport.provenance is not Provenance.FIXTURE_REPLAY or transport.physical_device_evidence is not False:
            raise ValueError("this slice accepts fixture_replay transports only")
        self.identity = identity
        self.transport = transport
        self.transport_kind = transport.kind
        self._clock = monotonic_ns
        self.parser = CytonPacketParser(monotonic_ns=self._clock)
        self.generation = 0
        self.authentication = Authentication.NOT_CHECKED
        self.state = Lifecycle.DISCONNECTED
        self.events = []
        self._event(Lifecycle.DISCONNECTED, "created")

    def _event(self, state: Lifecycle, reason: str) -> None:
        self.state = state
        self.events.append(LifecycleEvent(state, self.generation, reason,
                                          self.authentication, self.provenance))

    def _disconnect_after_failure(self):
        try:
            self.transport.disconnect()
        except Exception as error:
            self._event(Lifecycle.FAILED, "transport_cleanup_failed")
            return error
        self._event(Lifecycle.FAILED, "transport_connect_failed")
        return None

    def connect(self) -> int:
        if self.state not in (Lifecycle.DISCONNECTED, Lifecycle.FAILED):
            raise RuntimeError("disconnect before reconnect")
        self.generation += 1
        self.parser = CytonPacketParser(monotonic_ns=self._clock)
        self.authentication = Authentication.NOT_CHECKED
        try:
            self.authentication = self.transport.connect()
        except Exception as error:
            cleanup_error = self._disconnect_after_failure()
            if cleanup_error is not None:
                raise RuntimeError(f"transport connect failure: {error}; "
                                   f"cleanup failed: {cleanup_error}") from error
            raise RuntimeError(f"transport connect failure: {error}") from error
        if self.authentication is not Authentication.ACCEPTED:
            try:
                self.transport.disconnect()
            except Exception as error:
                self._event(Lifecycle.FAILED, "authentication_cleanup_failed")
                raise RuntimeError(f"authentication rejected; cleanup failed: {error}") from error
            self._event(Lifecycle.FAILED, "authentication_rejected")
            raise RuntimeError("authentication rejected")
        self._event(Lifecycle.CONNECTED, "connected")
        return self.generation

    def cancel(self) -> None:
        self.authentication = Authentication.NOT_CHECKED
        self.parser = CytonPacketParser(monotonic_ns=self._clock)
        try:
            self.transport.disconnect()
        except Exception as error:
            self._event(Lifecycle.FAILED, "cancel_cleanup_failed")
            raise RuntimeError(f"cancel cleanup failed: {error}") from error
        self._event(Lifecycle.DISCONNECTED, "cancelled")

    def _check_generation(self, generation: int) -> None:
        if type(generation) is not int or generation != self.generation:
            raise RuntimeError("stale or invalid generation")

    def start(self, generation: int) -> None:
        self._check_generation(generation)
        if self.authentication is not Authentication.ACCEPTED:
            raise RuntimeError("authentication required")
        if self.state is not Lifecycle.CONNECTED:
            raise RuntimeError("device must be connected before start")
        self._event(Lifecycle.STREAMING, "started")

    def read(self, generation: int) -> tuple[Sample, ...]:
        self._check_generation(generation)
        if self.state != Lifecycle.STREAMING:
            raise RuntimeError("device is not streaming")
        try:
            data = self.transport.read(MAX_READ_BYTES)
            if not isinstance(data, bytes):
                raise RuntimeError("malformed transport bytes")
            if len(data) > MAX_READ_BYTES:
                raise RuntimeError("transport chunk exceeds capacity")
            if not data:
                raise RuntimeError("eof transport")
            frames = self.parser.feed(data)
            stats = self.parser.stats
            if any((stats.discarded_bytes, stats.invalid_packets, stats.duplicate_packets,
                    stats.out_of_order_packets, stats.counter_gaps)):
                raise RuntimeError("malformed Cyton stream")
            return tuple(self._sample(frame) for frame in frames)
        except Exception as error:
            try:
                self.transport.disconnect()
            except Exception as cleanup_error:
                self._event(Lifecycle.FAILED, "transport_failure_cleanup_failed")
                raise RuntimeError(f"transport failure: {error}; cleanup failed: {cleanup_error}") from error
            self._event(Lifecycle.FAILED, "transport_failure")
            raise RuntimeError(f"transport failure: {error}") from error

    def _sample(self, frame: CytonFrame) -> Sample:
        return Sample(self.identity, self.transport_kind, self.capabilities,
                      self.generation, self.authentication, self.provenance,
                      frame.sequence, frame.sample_number, frame.host_receive_monotonic_ns,
                      frame.timestamp_clock, frame.channels_nv, frame.auxiliary, frame.footer)


class DevicePlugin(Protocol):
    """Session tokens bind operations to a particular connection generation."""

    identity: DeviceIdentity
    capabilities: Capabilities
    transport_kind: TransportKind
    provenance: Provenance
    state: Lifecycle
    authentication: Authentication
    events: list[LifecycleEvent]
    physical_device_evidence: bool
    generation: int

    def connect(self) -> int: ...

    def start(self, generation: int) -> None: ...

    def read(self, generation: int) -> tuple[Sample, ...]: ...

    def cancel(self) -> None: ...
