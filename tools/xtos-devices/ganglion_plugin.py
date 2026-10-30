"""Fixture-only Ganglion BLE adapter using the version-pinned offline codec."""
from __future__ import annotations

from dataclasses import dataclass
from collections import deque

from ganglion_protocol import GanglionSample, GanglionV2Decoder
from xtos_devices import (
    Authentication,
    Capabilities,
    DeviceIdentity,
    Lifecycle,
    LifecycleEvent,
    Provenance,
    Sample,
    TransportKind,
)


@dataclass(frozen=True)
class GanglionSampleView:
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
    packet_id: int
    ring_index: int
    sample_index: int
    quality: tuple[str, ...]
    action: str = "no_action"
    physical_device_evidence: bool = False


class GanglionPlugin:
    """Adapt bounded fixture notifications; no discovery or real BLE I/O."""

    physical_device_evidence = False
    provenance = Provenance.FIXTURE_REPLAY
    capabilities = Capabilities(4, 200, "nanovolt_nominal")

    def __init__(self, identity: DeviceIdentity, transport, *, provenance: Provenance):
        if not isinstance(identity, DeviceIdentity):
            raise ValueError("explicit DeviceIdentity required")
        if provenance is not Provenance.FIXTURE_REPLAY:
            raise ValueError("Ganglion fixture slice accepts fixture_replay only")
        if transport.provenance is not Provenance.FIXTURE_REPLAY or transport.physical_device_evidence is not False:
            raise ValueError("fixture transport provenance required")
        if transport.kind is not TransportKind.BLE:
            raise ValueError("Ganglion adapter requires BLE transport kind")
        self.identity = identity
        self.transport = transport
        self.transport_kind = transport.kind
        self.provenance = provenance
        self.authentication = Authentication.NOT_CHECKED
        self.state = Lifecycle.DISCONNECTED
        self.generation = 0
        self.decoder = GanglionV2Decoder()
        self._events = deque(maxlen=64)
        self._controls = deque(maxlen=64)
        self._event(Lifecycle.DISCONNECTED, "created")

    @property
    def events(self):
        return tuple(self._events)

    @property
    def controls(self):
        return tuple(self._controls)

    def _event(self, state, reason):
        self.state = state
        self._events.append(LifecycleEvent(state, self.generation, reason,
                                           self.authentication, self.provenance))

    def connect(self) -> int:
        if self.state not in (Lifecycle.DISCONNECTED, Lifecycle.FAILED):
            raise RuntimeError("disconnect before reconnect")
        self.generation += 1
        self.decoder = GanglionV2Decoder()
        self._controls.clear()
        self.authentication = Authentication.NOT_CHECKED
        try:
            self.transport.connect()
        except Exception as error:
            try:
                self.transport.disconnect()
            except Exception as cleanup_error:
                self._event(Lifecycle.FAILED, "transport_connect_cleanup_failed")
                raise RuntimeError(f"Ganglion connect failed: {error}; cleanup failed: {cleanup_error}") from error
            self._event(Lifecycle.FAILED, "transport_connect_failed")
            raise RuntimeError(f"Ganglion connect failed: {error}") from error
        self._event(Lifecycle.CONNECTED, "fixture_transport_connected")
        return self.generation

    def start(self, generation: int) -> None:
        self._check_generation(generation)
        if self.state is not Lifecycle.CONNECTED:
            raise RuntimeError("device must be connected before start")
        self._event(Lifecycle.STREAMING, "started")

    def _check_generation(self, generation: int) -> None:
        if type(generation) is not int or generation != self.generation:
            raise RuntimeError("stale or invalid generation")

    def read(self, generation: int) -> tuple[GanglionSampleView, ...]:
        self._check_generation(generation)
        if self.state is not Lifecycle.STREAMING:
            raise RuntimeError("device is not streaming")
        try:
            data = self.transport.read(20)
            if not data:
                raise RuntimeError("eof transport")
            batch = self.decoder.feed_notification(data)
            self._controls.extend(batch.controls)
            return tuple(self._view(sample) for sample in batch.samples)
        except Exception as error:
            self._controls.clear()
            try:
                self.transport.disconnect()
            except Exception as cleanup_error:
                self._event(Lifecycle.FAILED, "transport_failure_cleanup_failed")
                raise RuntimeError(f"Ganglion read failed: {error}; cleanup failed: {cleanup_error}") from error
            self._event(Lifecycle.FAILED, "transport_failure")
            raise

    def _view(self, sample: GanglionSample) -> GanglionSampleView:
        return GanglionSampleView(
            identity=self.identity, transport_kind=self.transport_kind,
            capabilities=self.capabilities, generation=self.generation,
            authentication=self.authentication, provenance=self.provenance,
            sequence=sample.sequence, sample_number=sample.sequence,
            host_receive_monotonic_ns=sample.host_receive_monotonic_ns,
            timestamp_clock=sample.timestamp_clock, channels=sample.channels_nv,
            auxiliary=(bytes([sample.auxiliary_byte]) if sample.auxiliary_valid else b""),
            footer=sample.packet_id, packet_id=sample.packet_id,
            ring_index=sample.ring_index, sample_index=sample.sample_index,
            quality=sample.quality)

    def cancel(self) -> None:
        self._controls.clear()
        try:
            self.transport.disconnect()
        except Exception as error:
            self._event(Lifecycle.FAILED, "cancel_cleanup_failed")
            raise RuntimeError(f"Ganglion cancel failed: {error}") from error
        self.decoder.reset()
        self._controls.clear()
        self.authentication = Authentication.NOT_CHECKED
        self._event(Lifecycle.DISCONNECTED, "cancelled")
