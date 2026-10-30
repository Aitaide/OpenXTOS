"""Version-pinned offline codec for OpenBCI Ganglion firmware v2.0.1."""
from __future__ import annotations

from dataclasses import dataclass
import time

PROFILE = "openbci-ganglion-v2.0.1"
CHANNELS = 4
SAMPLE_RATE_HZ = 200
FRAME_BYTES = 20
RAW_PADDING = b"ABCDEFG"
SCALE_NUMERATOR_NV = 15_686_000
SCALE_DENOMINATOR_COUNTS = 8_388_607
MAX_NOTIFICATION_BYTES = 20
MAX_TEXT_BYTES = 4096


class GanglionProtocolError(ValueError):
    """Malformed, unsupported, discontinuous or unseeded Ganglion frame."""


@dataclass(frozen=True)
class GanglionSample:
    sequence: int
    epoch: int
    packet_id: int
    ring_index: int
    sample_index: int
    sample_rate_hz: int
    channels_counts: tuple[int, int, int, int]
    channels_nv: tuple[int, int, int, int]
    auxiliary_byte: int | None
    auxiliary_valid: bool
    host_receive_monotonic_ns: int
    timestamp_clock: str
    quality: tuple[str, ...]
    action: str = "no_action"
    physical_device_evidence: bool = False
    device_timestamp_ns: None = None


@dataclass(frozen=True)
class GanglionControl:
    packet_id: int
    kind: str
    payload: bytes
    text: str | None
    complete: bool
    numeric_value: None = None
    units: str = "unverified"
    action: str = "no_action"
    physical_device_evidence: bool = False


@dataclass(frozen=True)
class GanglionBatch:
    samples: tuple[GanglionSample, ...]
    controls: tuple[GanglionControl, ...]


def _signed24(data: bytes) -> tuple[int, int, int, int]:
    values = []
    for offset in range(0, 12, 3):
        value = int.from_bytes(data[offset:offset + 3], "big", signed=False)
        if value & 0x800000:
            value -= 1 << 24
        values.append(value)
    return tuple(values)


def _signed_nv(value: int) -> int:
    product = value * SCALE_NUMERATOR_NV
    magnitude = abs(product) // SCALE_DENOMINATOR_COUNTS
    return -magnitude if product < 0 else magnitude


def _unpack_deltas(data: bytes, width: int) -> tuple[int, ...]:
    bit_count = width * 8
    packed = int.from_bytes(data, "big")
    values = []
    for index in range(8):
        shift = bit_count - width * (index + 1)
        word = (packed >> shift) & ((1 << width) - 1)
        values.append(word - (1 << width) if word & 1 else word)
    return tuple(values)


class GanglionV2Decoder:
    """Decode complete v2 BLE notifications. No discovery, BLE or I/O occurs."""

    profile = PROFILE
    sample_rate_hz = SAMPLE_RATE_HZ
    scale_numerator_nv = SCALE_NUMERATOR_NV
    scale_denominator_counts = SCALE_DENOMINATOR_COUNTS
    physical_device_evidence = False

    def __init__(self, *, profile: str = PROFILE, monotonic_ns=time.monotonic_ns):
        if profile != PROFILE:
            raise GanglionProtocolError("explicit supported Ganglion v2.0.1 profile required")
        if not callable(monotonic_ns):
            raise GanglionProtocolError("monotonic_ns must be callable")
        self._clock = monotonic_ns
        self._epoch = 0
        self._sequence = 0
        self._host_time = None
        self._last_ring = 0
        self._seed = None
        self._control_buffer = bytearray()
        self._control_kind = None
        self._last_mode = None

    @property
    def seeded(self) -> bool:
        return self._seed is not None

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def events(self) -> tuple:
        return ()

    def reset(self) -> None:
        self._sequence = 0
        self._last_ring = 0
        self._seed = None
        self._control_buffer.clear()
        self._control_kind = None
        self._last_mode = None

    def _invalidate(self, message: str) -> None:
        self._seed = None
        self._last_ring = 0
        self._last_mode = None
        self._control_buffer.clear()
        self._control_kind = None
        raise GanglionProtocolError(message)

    def feed_notification(self, data: bytes, *, host_receive_monotonic_ns: int | None = None) -> GanglionBatch:
        if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_NOTIFICATION_BYTES:
            self._invalidate("notification must be 1..20 bounded bytes")
        if host_receive_monotonic_ns is None:
            try:
                host_receive_monotonic_ns = self._clock()
            except Exception as error:
                self._invalidate("host monotonic clock failed")
                raise GanglionProtocolError("host monotonic clock failed") from error
        if type(host_receive_monotonic_ns) is not int or host_receive_monotonic_ns <= 0:
            self._invalidate("positive host monotonic timestamp required")
        if self._host_time is not None and host_receive_monotonic_ns < self._host_time:
            self._invalidate("host receive timestamp regressed")
        self._host_time = host_receive_monotonic_ns
        packet_id = data[0]
        if self._control_buffer and packet_id not in (206, 207):
            self._invalidate("interleaved packet interrupted verbose ASCII message")
        if packet_id == 0:
            return self._raw(data, host_receive_monotonic_ns)
        if 1 <= packet_id <= 100:
            return self._compressed(data, host_receive_monotonic_ns, width=18,
                                    ring_index=packet_id, auxiliary_slot=packet_id % 10)
        if 101 <= packet_id <= 200:
            return self._compressed(data, host_receive_monotonic_ns, width=19,
                                    ring_index=packet_id - 100, auxiliary_slot=None)
        if 201 <= packet_id <= 205:
            return self._impedance(data)
        if packet_id in (206, 207):
            return self._multipacket_ascii(data)
        self._invalidate("unknown Ganglion v2 packet ID")

    def _raw(self, data: bytes, timestamp: int) -> GanglionBatch:
        if len(data) != FRAME_BYTES:
            self._invalidate("raw packet length must be 20 bytes")
        if data[13:] != RAW_PADDING:
            self._invalidate("raw packet padding must equal ASCII A..G")
        counts = _signed24(data[1:13])
        self._epoch += 1
        self._seed = counts
        self._last_ring = 0
        self._last_mode = None
        sample = self._make_sample(packet_id=0, ring_index=0, sample_index=0,
                                   counts=counts, timestamp=timestamp,
                                   quality=("raw_seed", "exact_counts", "nominal_scale"),
                                   auxiliary_byte=None, auxiliary_valid=False)
        self._sequence += 1
        return GanglionBatch((sample,), ())

    def _compressed(self, data: bytes, timestamp: int, *, width: int,
                    ring_index: int, auxiliary_slot: int | None) -> GanglionBatch:
        expected = FRAME_BYTES
        if len(data) != expected:
            self._invalidate("compressed packet length must be 20 bytes")
        if self._seed is None:
            self._invalidate("compressed packet requires raw seed; reseed required")
        if ring_index != self._last_ring + 1:
            self._invalidate("compressed packet ring gap or duplicate; reseed required")
        payload = data[1:19] if width == 18 else data[1:20]
        deltas = _unpack_deltas(payload, width)
        auxiliary_byte = data[19] if width == 18 else None
        auxiliary_valid = width == 18 and auxiliary_slot in (1, 2, 3)
        # The source leaves this byte untouched on other slots; never publish stale data.
        samples = []
        counts = self._seed
        mode = width
        for sample_index in range(2):
            prior = counts
            current = tuple(old - delta for old, delta in
                            zip(prior, deltas[4 * sample_index:4 * sample_index + 4]))
            quality = ["compressed", "delta_quantized", "nominal_scale"]
            if auxiliary_valid:
                quality.append("auxiliary_byte_present_unscaled")
            if any(value < -(1 << 23) or value >= (1 << 23) for value in current):
                quality.append("reconstructed_outside_signed24")
            if self._last_mode is not None and mode != self._last_mode:
                quality.append("codec_mode_transition")
            sample = self._make_sample(packet_id=data[0], ring_index=ring_index,
                                       sample_index=sample_index, counts=current,
                                       timestamp=timestamp, quality=tuple(quality),
                                       auxiliary_byte=auxiliary_byte if auxiliary_valid else None,
                                       auxiliary_valid=auxiliary_valid)
            samples.append(sample)
            self._sequence += 1
            counts = current
            self._last_mode = mode
        self._seed = counts
        self._last_ring = ring_index
        return GanglionBatch(tuple(samples), ())

    def _impedance(self, data: bytes) -> GanglionBatch:
        payload = data[1:]
        if len(data) < 2 or len(payload) > 11 or payload[-1:] != b"Z":
            self._invalidate("impedance control must contain bounded ASCII digits and Z")
        digits = payload[:-1]
        if any(byte < 48 or byte > 57 for byte in digits):
            self._invalidate("impedance control digits are malformed; units unverified")
        control = GanglionControl(data[0], "impedance_opaque", payload,
                                  payload.decode("ascii"), True)
        return GanglionBatch((), (control,))

    def _multipacket_ascii(self, data: bytes) -> GanglionBatch:
        piece = data[1:]
        if data[0] == 206 and len(data) != FRAME_BYTES:
            self._invalidate("non-final verbose ASCII fragment must be 20 bytes")
        if data[0] == 207 and not 1 <= len(data) <= FRAME_BYTES:
            self._invalidate("final verbose ASCII fragment must be 1..20 bytes")
        if any((byte != 10 and (byte < 32 or byte > 126)) for byte in piece):
            self._invalidate("verbose ASCII fragment must be nonempty printable bytes")
        if len(self._control_buffer) + len(piece) > MAX_TEXT_BYTES:
            self._invalidate("verbose ASCII aggregation capacity exceeded")
        if self._control_kind is None:
            self._control_kind = "verbose_ascii"
        self._control_buffer.extend(piece)
        complete = data[0] == 207
        if not complete:
            return GanglionBatch((), ())
        payload = bytes(self._control_buffer)
        control = GanglionControl(207, "verbose_ascii", payload,
                                  payload.decode("ascii"), True)
        self._control_buffer.clear()
        self._control_kind = None
        return GanglionBatch((), (control,))

    def _make_sample(self, *, packet_id: int, ring_index: int, sample_index: int,
                     counts: tuple[int, int, int, int], timestamp: int,
                     quality: tuple[str, ...], auxiliary_byte: int | None,
                     auxiliary_valid: bool) -> GanglionSample:
        return GanglionSample(
            sequence=self._sequence, epoch=self._epoch, packet_id=packet_id,
            ring_index=ring_index, sample_index=sample_index,
            sample_rate_hz=SAMPLE_RATE_HZ, channels_counts=counts,
            channels_nv=tuple(_signed_nv(value) for value in counts),
            auxiliary_byte=auxiliary_byte, auxiliary_valid=auxiliary_valid,
            host_receive_monotonic_ns=timestamp,
            timestamp_clock="host_notification_receive_monotonic",
            quality=quality)
