"""OpenBCI Cyton 33-byte serial stream adapter.

This module decodes the Cyton binary protocol. Receive timestamps are host
CLOCK_MONOTONIC timestamps. This slice preserves the six AUX bytes as raw data
and does not interpret optional board-time fields in protocol footer variants.
"""
from __future__ import annotations

from dataclasses import dataclass
import time

PACKET_SIZE = 33
START_BYTE = 0xA0
CHANNELS = 8
SAMPLE_RATE_HZ = 250
VREF_NV = 4_500_000_000
GAIN = 24
ADC_SCALE_DENOMINATOR = GAIN * ((1 << 23) - 1)
MAX_FEED_BYTES = 1 << 20


def signed24(data: bytes) -> int:
    """Decode one big-endian two's-complement 24-bit ADS1299 value."""
    if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) != 3:
        raise ValueError("24-bit sample must contain exactly three bytes")
    value = (data[0] << 16) | (data[1] << 8) | data[2]
    return value - (1 << 24) if value & (1 << 23) else value


def nanovolts_from_raw24(value: int) -> int:
    """Convert ADS1299 counts using the positive full-scale denominator."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("raw sample must be an integer")
    if not -(1 << 23) <= value < (1 << 23):
        raise ValueError("raw sample is outside signed 24-bit range")
    denominator = ADC_SCALE_DENOMINATOR
    numerator = value * VREF_NV
    magnitude = abs(numerator) // denominator
    return -magnitude if numerator < 0 else magnitude


@dataclass(frozen=True)
class CytonFrame:
    sequence: int
    sample_number: int
    host_receive_monotonic_ns: int
    channels_nv: tuple[int, ...]
    auxiliary: bytes
    footer: int
    sample_rate_hz: int = SAMPLE_RATE_HZ
    unit: str = "nanovolt"
    timestamp_clock: str = "host_monotonic"


@dataclass
class CytonParserStats:
    bytes_received: int = 0
    discarded_bytes: int = 0
    invalid_packets: int = 0
    accepted_packets: int = 0
    duplicate_packets: int = 0
    out_of_order_packets: int = 0
    counter_gaps: int = 0
    missing_samples: int = 0


class CytonPacketParser:
    """Bounded incremental parser for arbitrarily chunked 33-byte Cyton packets."""

    def __init__(self, monotonic_ns=time.monotonic_ns):
        if not callable(monotonic_ns):
            raise TypeError("monotonic_ns must be callable")
        self._monotonic_ns = monotonic_ns
        self._buffer = bytearray()
        self._last_sample_number: int | None = None
        self._last_sequence: int | None = None
        self._last_receive_ns: int | None = None
        self.stats = CytonParserStats()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes | bytearray | memoryview) -> list[CytonFrame]:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("serial input must be bytes-like")
        if len(data) > MAX_FEED_BYTES:
            raise ValueError("serial input chunk exceeds capacity")
        frames = []
        for byte in data:
            self.stats.bytes_received += 1
            if not self._buffer:
                if byte == START_BYTE:
                    self._buffer.append(byte)
                else:
                    self.stats.discarded_bytes += 1
                continue
            self._buffer.append(byte)
            if len(self._buffer) < PACKET_SIZE:
                continue
            if self._buffer[-1] & 0xF0 != 0xC0:
                self.stats.invalid_packets += 1
                self._resynchronize()
                continue
            packet = bytes(self._buffer)
            self._buffer.clear()
            sample_number = packet[1]
            sequence = self._extend_counter(sample_number)
            if sequence is None:
                continue
            receive_ns = self._monotonic_ns()
            if isinstance(receive_ns, bool) or not isinstance(receive_ns, int) or receive_ns <= 0:
                raise ValueError("receive clock must return a positive integer nanosecond value")
            if self._last_receive_ns is not None and receive_ns <= self._last_receive_ns:
                raise ValueError("receive clock must be strictly monotonic")
            self._last_receive_ns = receive_ns
            channel_values = tuple(
                nanovolts_from_raw24(signed24(packet[offset:offset + 3]))
                for offset in range(2, 2 + CHANNELS * 3, 3)
            )
            frames.append(CytonFrame(
                sequence=sequence,
                sample_number=sample_number,
                host_receive_monotonic_ns=receive_ns,
                channels_nv=channel_values,
                auxiliary=packet[26:32],
                footer=packet[32],
            ))
            self.stats.accepted_packets += 1
        return frames

    def _extend_counter(self, sample_number: int) -> int | None:
        if self._last_sample_number is None:
            self._last_sample_number = sample_number
            self._last_sequence = sample_number
            return sample_number
        delta = (sample_number - self._last_sample_number) & 0xFF
        if delta == 0:
            self.stats.duplicate_packets += 1
            return None
        if delta >= 0x80:
            self.stats.out_of_order_packets += 1
            return None
        if delta > 1:
            self.stats.counter_gaps += 1
            self.stats.missing_samples += delta - 1
        if self._last_sequence is None:
            raise RuntimeError("sample counter state is incomplete")
        sequence = self._last_sequence + delta
        self._last_sample_number = sample_number
        self._last_sequence = sequence
        return sequence

    def _resynchronize(self) -> None:
        next_start = self._buffer.find(bytes((START_BYTE,)), 1)
        if next_start < 0:
            self.stats.discarded_bytes += len(self._buffer)
            self._buffer.clear()
            return
        self.stats.discarded_bytes += next_start
        del self._buffer[:next_start]


__all__ = [
    "CHANNELS", "MAX_FEED_BYTES", "PACKET_SIZE", "SAMPLE_RATE_HZ",
    "CytonFrame", "CytonPacketParser", "CytonParserStats",
    "nanovolts_from_raw24", "signed24",
]
