"""Strict, text-only parsers for the first Cyton command replies.

This module deliberately has no serial I/O and does not decode EEG samples.
The accepted strings are the formatter-shaped replies from the pinned MIT
OpenBCI firmware reference.
"""

from dataclasses import dataclass
import re


class CytonProtocolError(RuntimeError):
    """The reply is not an accepted Cyton protocol response."""


@dataclass(frozen=True)
class FirmwareInfo:
    firmware: str
    board_id: int
    accelerometer_id: int
    banner: str


@dataclass(frozen=True)
class CytonConfiguration:
    registers: tuple[int, ...]
    gains: tuple[int, ...]
    enabled_mask: int
    sample_rate_hz: int


_BANNER_FIRST_LINE = "OpenBCI V3 8-16 channel"
_BANNER_LIS_LINE = "LIS3DH Device ID: 0x33"
_BANNER_FIRMWARE_LINE = "Firmware: v3.1.2"
_ADS_NAMES = (
    "ADS_ID", "CONFIG1", "CONFIG2", "CONFIG3", "LOFF",
    "CH1SET", "CH2SET", "CH3SET", "CH4SET", "CH5SET", "CH6SET",
    "CH7SET", "CH8SET", "BIAS_SENSP", "BIAS_SENSN", "LOFF_SENSP",
    "LOFF_SENSN", "LOFF_FLIP", "LOFF_STATP", "LOFF_STATN", "GPIO",
    "MISC1", "MISC2", "CONFIG4",
)
_BANNER_ID = re.compile(r"(?:On Board )?ADS1299 Device ID: 0x([0-9A-Fa-f]{2})\Z")
_ADS_ROW = re.compile(
    r"([A-Z][A-Z0-9_]*)[ \t]*,[ \t]*([0-9A-Fa-f]{2})[ \t]*,[ \t]*"
    r"([0-9A-Fa-f]{2})[ \t]*,[ \t]*([01])"
    r"(?:[ \t]*,[ \t]*([01])){7}\Z"
)
_LIS_ROW = re.compile(r"0x([0-9A-Fa-f]{2})[ \t]+([0-9A-Fa-f]{2})\Z")
_LIS_RANGES = ((0x07, 0x0F), (0x1F, 0x33), (0x38, 0x3D))


def _fail(message: str) -> CytonProtocolError:
    return CytonProtocolError(message)


def _validate_bytes(body: bytes, limit: int) -> None:
    if not isinstance(body, bytes):
        raise _fail("reply must be bytes")
    if not 1 <= len(body) <= limit:
        raise _fail("reply length out of bounds")
    if any(byte not in (9, 10, 13) and not 32 <= byte <= 126 for byte in body):
        raise _fail("reply contains a non-printable byte")


def _lines(body: bytes) -> list[str]:
    lines = body.split(b"\r\n")
    if len(lines) > 1 and lines[-1] == b"":
        lines.pop()
    if not lines or any(b"\r" in line or b"\n" in line for line in lines):
        raise _fail("reply has invalid CRLF line framing")
    try:
        return [line.decode("ascii") for line in lines]
    except UnicodeDecodeError as exc:  # defensive after byte validation
        raise _fail("reply is not ASCII") from exc


def parse_banner(body: bytes) -> FirmwareInfo:
    """Parse the exact first-slice Cyton reset banner, without ``$$$``."""
    _validate_bytes(body, 255)
    lines = _lines(body)
    if len(lines) != 4 or lines[0] != _BANNER_FIRST_LINE:
        raise _fail("unexpected banner lines")
    ads_line, lis_line, firmware_line = lines[1:]
    if "Daisy" in ads_line:
        raise _fail("Daisy banner is unsupported")
    ads_match = _BANNER_ID.fullmatch(ads_line)
    if not ads_match or int(ads_match.group(1), 16) != 0x3E:
        raise _fail("unexpected ADS1299 device ID")
    if lis_line != _BANNER_LIS_LINE or firmware_line != _BANNER_FIRMWARE_LINE:
        raise _fail("banner fields are missing, duplicated, or out of order")
    return FirmwareInfo("v3.1.2", 0x3E, 0x33, body.decode("ascii"))


def _parse_ads_row(line: str, expected_index: int) -> int:
    fields = [field.strip(" \t") for field in line.split(",")]
    if len(fields) != 11 or not _ADS_ROW.fullmatch(line):
        raise _fail("malformed ADS register row")
    name, address_text, value_text = fields[:3]
    if name != _ADS_NAMES[expected_index] or int(address_text, 16) != expected_index:
        raise _fail("ADS register is missing, unknown, duplicated, or out of order")
    bits = [int(bit) for bit in fields[3:]]
    value = int(value_text, 16)
    expected_bits = [(value >> bit) & 1 for bit in range(7, -1, -1)]
    if bits != expected_bits:
        raise _fail("ADS register bit echo does not match its byte")
    return value


def _lis_group(address: int) -> int:
    for group, (start, end) in enumerate(_LIS_RANGES):
        if start <= address <= end:
            return group
    return -1


def parse_registers(body: bytes) -> CytonConfiguration:
    """Parse a complete Board ADS snapshot and optional LIS3DH rows."""
    _validate_bytes(body, 8192)
    lines = _lines(body)
    index = 0
    while index < len(lines) and lines[index] == "":
        index += 1
    if index == len(lines) or lines[index] != "Board ADS Registers":
        if any("Daisy" in line for line in lines):
            raise _fail("Daisy register section is unsupported")
        raise _fail("Board ADS Registers header is required")
    index += 1

    values = []
    for expected_index in range(len(_ADS_NAMES)):
        if index >= len(lines):
            raise _fail("Board ADS register snapshot is incomplete")
        if lines[index] == "":
            raise _fail("blank line inside Board ADS register snapshot")
        values.append(_parse_ads_row(lines[index], expected_index))
        index += 1
    if tuple(values)[:1] != (0x3E,) or values[1] != 0x96:
        raise _fail("ADS identity or sample-rate configuration is not default")
    if tuple(values[5:13]) != (0x68,) * 8:
        raise _fail("Cyton configuration is not the default eight-channel mode")

    while index < len(lines) and lines[index] == "":
        index += 1
    if index == len(lines):
        return CytonConfiguration(tuple(values), (24,) * 8, 0xFF, 250)
    if lines[index] != "LIS3DH Registers":
        if "Daisy" in lines[index]:
            raise _fail("Daisy register section is unsupported")
        raise _fail("unexpected trailing register text")
    index += 1

    lis_count = 0
    previous_address = -1
    previous_group = -1
    blank_between_groups = False
    while index < len(lines):
        line = lines[index]
        index += 1
        if line == "":
            if not lis_count:
                raise _fail("blank line before LIS3DH registers")
            blank_between_groups = True
            continue
        match = _LIS_ROW.fullmatch(line)
        if not match:
            raise _fail("malformed LIS3DH register row")
        address = int(match.group(1), 16)
        group = _lis_group(address)
        if group < 0 or address <= previous_address or group < previous_group:
            raise _fail("LIS3DH register address is invalid or out of order")
        if blank_between_groups and group == previous_group:
            raise _fail("blank line inside an LIS3DH register group")
        previous_address = address
        previous_group = group
        lis_count += 1
        blank_between_groups = False
    if not lis_count:
        raise _fail("LIS3DH Registers section is empty")
    return CytonConfiguration(tuple(values), (24,) * 8, 0xFF, 250)