"""Bounded OpenBCI Cyton serial capture and evidence bundle writer."""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import select
import shutil
import signal
import stat
import tempfile
import termios
import time
from threading import Event
from typing import Callable

from openbci_cyton import CytonPacketParser, MAX_FEED_BYTES

SCHEMA = "xtos-openbci-cyton-capture-manifest-v1"
FRAME_SCHEMA = "xtos-openbci-cyton-frame-v1"
DEFAULT_BAUD = 115200
DEFAULT_MAX_CAPTURE_BYTES = 16 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
POLL_INTERVAL_S = 0.1


class CaptureError(RuntimeError):
    """A serial capture could not produce a bounded evidence bundle."""


def configure_serial_fd(fd: int) -> None:
    """Configure an already-open descriptor as raw 115200 8N1 serial."""
    try:
        attrs = termios.tcgetattr(fd)
    except termios.error as exc:
        raise OSError(f"cannot read serial settings: {exc}") from exc
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)
    attrs[2] |= termios.CS8 | termios.CLOCAL | termios.CREAD
    if hasattr(termios, "CRTSCTS"):
        attrs[2] &= ~termios.CRTSCTS
    attrs[3] = 0
    attrs[4] = termios.B115200
    attrs[5] = termios.B115200
    attrs[6][termios.VMIN] = 1
    attrs[6][termios.VTIME] = 0
    try:
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except termios.error as exc:
        raise OSError(f"cannot configure serial settings: {exc}") from exc


def open_serial_device(path: str | os.PathLike[str]) -> int:
    """Open a character serial device without following a final symlink."""
    device = os.fspath(path)
    flags = os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK | os.O_NOFOLLOW
    try:
        fd = os.open(device, flags)
    except OSError:
        raise
    try:
        information = os.fstat(fd)
        if not stat.S_ISCHR(information.st_mode):
            raise OSError(errno.ENOTTY, f"serial path is not a character device: {device}")
        configure_serial_fd(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _json_line(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_capture_options(duration_s: float, max_capture_bytes: int) -> None:
    if isinstance(duration_s, bool) or not isinstance(duration_s, (int, float)) \
            or not math.isfinite(duration_s) or duration_s <= 0:
        raise ValueError("duration_s must be a finite positive number")
    if isinstance(max_capture_bytes, bool) or not isinstance(max_capture_bytes, int) \
            or max_capture_bytes <= 0:
        raise ValueError("max_capture_bytes must be a positive integer")
    if max_capture_bytes > DEFAULT_MAX_CAPTURE_BYTES:
        raise ValueError("maximum capture capacity is 16 MiB")


def _rename_directory_noreplace(source: Path, destination: Path) -> None:
    """Atomically publish a directory only if the destination does not exist."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2(RENAME_NOREPLACE) is unavailable")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p,
                          ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))


def _write_command(fd: int, command: bytes) -> None:
    view = memoryview(command)
    deadline = time.monotonic() + 1.0
    while view:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CaptureError("serial command write timed out")
        try:
            _, writable, _ = select.select([], [fd], [], remaining)
        except (OSError, ValueError) as exc:
            raise CaptureError(f"serial command wait failed: {exc}") from exc
        if not writable:
            raise CaptureError("serial command write timed out")
        try:
            written = os.write(fd, view)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                continue
            raise CaptureError(f"serial command write failed: {exc}") from exc
        if written <= 0:
            raise CaptureError("serial command write returned no progress")
        view = view[written:]


def _write_frame(stream, frame, previous_sequence: int | None) -> int:
    quality = "ok"
    if previous_sequence is not None and frame.sequence > previous_sequence + 1:
        quality = "gap"
    stream.write(_json_line({
        "schema": FRAME_SCHEMA,
        "sequence": frame.sequence,
        "sample_number": frame.sample_number,
        "timestamp_ns": frame.host_receive_monotonic_ns,
        "timestamp_clock": frame.timestamp_clock,
        "channels": len(frame.channels_nv),
        "sample_rate_hz": frame.sample_rate_hz,
        "unit": frame.unit,
        "quality": quality,
        "channels_nv": list(frame.channels_nv),
        "auxiliary_hex": frame.auxiliary.hex(),
        "footer": frame.footer,
    }))
    return frame.sequence


def capture_fd(
    fd: int,
    output: str | os.PathLike[str],
    *,
    device_label: str,
    duration_s: float,
    stop_event: Event | None = None,
    max_capture_bytes: int = DEFAULT_MAX_CAPTURE_BYTES,
    monotonic: Callable[[], float] = time.monotonic,
    start_stream: bool = False,
) -> dict:
    """Capture one bounded serial session and atomically publish its bundle.

    The raw bytes and decoded frames are written to a temporary sibling and
    published together only after hashes and the manifest are complete. The
    bundle records host monotonic timestamps separately from the Cyton counter.
    """
    _validate_capture_options(duration_s, max_capture_bytes)
    if not isinstance(device_label, str) or not device_label:
        raise ValueError("device_label must be a nonempty string")
    if not isinstance(fd, int) or fd < 0:
        raise ValueError("fd must be a valid nonnegative descriptor")
    if type(start_stream) is not bool:
        raise ValueError("start_stream must be a boolean")
    if stop_event is None:
        stop_event = Event()
    destination = Path(output)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"refusing to overwrite capture output {destination}")
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=parent))
    parser = CytonPacketParser()
    raw_path = temporary / "capture.bin"
    frames_path = temporary / "frames.jsonl"
    try:
        raw_digest = hashlib.sha256()
        raw_bytes = 0
        start = monotonic()
        if not math.isfinite(start):
            raise ValueError("monotonic clock returned a non-finite value")
        status = "completed"
        capture_error = None
        stream_started = False
        stream_stop_sent = False
        stream_stop_error = None
        with raw_path.open("wb") as raw_stream, frames_path.open("wb") as frame_stream:
            frame_stream.write(_json_line({
                "schema": SCHEMA,
                "record_type": "capture_metadata",
                "device": device_label,
                "transport": "serial",
                "baudrate": DEFAULT_BAUD,
                "channels": 8,
                "sample_rate_hz": 250,
                "unit": "nanovolt",
                "timestamp_clock": "host_monotonic",
                "quality_flags": ["ok", "gap"],
                "gain": 24,
                "gain_source": "OpenBCI Cyton default; verify actual board channel gain before interpreting nV values",
            }))
            previous_sequence = None
            try:
                if start_stream:
                    _write_command(fd, b"b")
                    stream_started = True
                    time.sleep(0.01)
                while True:
                    now = monotonic()
                    if not math.isfinite(now):
                        raise ValueError("monotonic clock returned a non-finite value")
                    if stop_event.is_set():
                        status = "interrupted"
                        break
                    if now - start >= duration_s:
                        break
                    timeout = min(POLL_INTERVAL_S, max(0.0, duration_s - (now - start)))
                    try:
                        readable, _, _ = select.select([fd], [], [], timeout)
                    except (OSError, ValueError) as exc:
                        raise CaptureError(f"serial wait failed: {exc}") from exc
                    if not readable:
                        continue
                    try:
                        data = os.read(fd, min(READ_CHUNK_BYTES, MAX_FEED_BYTES))
                    except OSError as exc:
                        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                            continue
                        capture_error = f"serial read failed: {exc}"
                        status = "device_read_failed"
                        break
                    if not data:
                        capture_error = "serial device returned EOF"
                        status = "device_read_failed"
                        break
                    remaining = max_capture_bytes - raw_bytes
                    accepted = data[:remaining]
                    if accepted:
                        raw_stream.write(accepted)
                        raw_digest.update(accepted)
                        raw_bytes += len(accepted)
                        for frame in parser.feed(accepted):
                            previous_sequence = _write_frame(frame_stream, frame, previous_sequence)
                    if len(accepted) < len(data):
                        status = "capacity_exceeded"
                        capture_error = "capture exceeds configured capacity"
                        break
            finally:
                if stream_started:
                    try:
                        _write_command(fd, b"s")
                        stream_stop_sent = True
                    except CaptureError as exc:
                        stream_stop_error = str(exc)
            raw_stream.flush()
            frame_stream.flush()
            os.fsync(raw_stream.fileno())
            os.fsync(frame_stream.fileno())
        if status != "capacity_exceeded":
            if parser.buffered_bytes:
                status = "incomplete_packet"
                if capture_error is None:
                    capture_error = f"partial packet remains: {parser.buffered_bytes} bytes"
                elif "partial packet" not in capture_error:
                    capture_error += f"; partial packet remains: {parser.buffered_bytes} bytes"
            elif parser.stats.invalid_packets or parser.stats.discarded_bytes:
                status = "serial_noise"
                if capture_error is None:
                    capture_error = "serial noise or malformed packets were observed"
            elif parser.stats.missing_samples or parser.stats.duplicate_packets \
                    or parser.stats.out_of_order_packets:
                status = "sample_gaps"
                if capture_error is None:
                    capture_error = "missing samples or counter ordering error"
            elif stream_stop_error is not None:
                status = "stream_stop_failed"
            elif capture_error is None and raw_bytes == 0:
                status = "no_bytes"
            elif capture_error is None and parser.stats.accepted_packets == 0:
                status = "no_valid_frames"
        frame_digest = _hash_file(frames_path)
        manifest = {
            "schema": SCHEMA,
            "status": status,
            "capture_error": capture_error,
            "device": device_label,
            "transport": "serial",
            "serial": {"baudrate": DEFAULT_BAUD, "data_bits": 8,
                       "parity": "none", "stop_bits": 1, "flow_control": "none"},
            "sample_contract": {"channels": 8, "sample_rate_hz": 250,
                                "unit": "nanovolt", "gain": 24,
                                "gain_source": "OpenBCI Cyton default; verify actual board channel gain before interpreting nV values",
                                "timestamp": "host_monotonic_ns",
                                "sequence": "extended_cyton_sample_counter"},
            "capture_duration_s": duration_s,
            "raw_bytes": raw_bytes,
            "trailing_packet_bytes": parser.buffered_bytes,
            "stream_start_command": "b" if stream_started else None,
            "stream_stop_command": "s" if stream_stop_sent else None,
            "stream_stop_error": stream_stop_error,
            "capture_error": capture_error,
            "raw_capture_sha256": raw_digest.hexdigest(),
            "frames_sha256": frame_digest,
            "stats": vars(parser.stats),
            "physical_device_evidence": False,
            "physical_device_evidence_verified": False,
            "limitations": [
                "Cyton packets provide a sample counter; timestamps are host receive CLOCK_MONOTONIC values",
                "raw capture and parser output require separate device identity and reviewer evidence",
                "this bundle is not medical, hard-real-time, or physical-device acceptance",
            ],
        }
        manifest_path = temporary / "manifest.json"
        manifest_path.write_bytes(_json_line(manifest))
        with manifest_path.open("rb") as stream:
            os.fsync(stream.fileno())
        try:
            _rename_directory_noreplace(temporary, destination)
        except FileExistsError as exc:
            raise FileExistsError(f"capture output appeared during publish: {destination}") from exc
        return manifest
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def capture_serial_device(device: str, output: str | os.PathLike[str], **kwargs) -> dict:
    fd = open_serial_device(device)
    try:
        return capture_fd(fd, output, device_label=device, **kwargs)
    finally:
        os.close(fd)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True, help="serial character device")
    parser.add_argument("--output", required=True, type=Path, help="new capture bundle directory")
    parser.add_argument("--duration-s", type=float, default=10.0)
    parser.add_argument("--max-capture-bytes", type=int, default=DEFAULT_MAX_CAPTURE_BYTES)
    parser.add_argument("--start-stream", action="store_true",
                        help="send the Cyton 'b' command and send 's' when capture ends")
    args = parser.parse_args(argv)
    stop_event = Event()
    previous = {}

    def request_stop(signum, _frame):
        previous[signum] = signal.getsignal(signum)
        stop_event.set()

    old_int = signal.signal(signal.SIGINT, request_stop)
    old_term = signal.signal(signal.SIGTERM, request_stop)
    try:
        manifest = capture_serial_device(
            args.device, args.output, duration_s=args.duration_s,
            stop_event=stop_event, max_capture_bytes=args.max_capture_bytes,
            start_stream=args.start_stream)
    except (OSError, ValueError, CaptureError) as exc:
        print(f"cyton_capture_error: {exc}", file=os.sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
    print(json.dumps(manifest, ensure_ascii=True, sort_keys=True))
    return 1 if manifest["status"] != "completed" or manifest["stream_stop_error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
