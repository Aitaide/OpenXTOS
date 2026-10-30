"""Replay verified OpenBCI Cyton captures through the XTOS filter pipeline."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

from openbci_cyton import CytonPacketParser, MAX_FEED_BYTES, PACKET_SIZE
from cyton_capture import _rename_directory_noreplace

MAX_CAPTURE_BYTES = 16 * 1024 * 1024
# A capture may use the full raw bound. Keep the decoded bound and frame-count
# bound derived from that same contract so a valid capture is replayable.
MAX_FRAMES_BYTES = MAX_CAPTURE_BYTES * 16
MAX_FRAME_COUNT = MAX_CAPTURE_BYTES // PACKET_SIZE + 1
MAX_MANIFEST_BYTES = 64 * 1024
MAX_JSON_DEPTH = 64
SAMPLE_INTERVAL_NS = 4_000_000
MANIFEST_SCHEMA = "xtos-openbci-cyton-capture-manifest-v1"
FRAME_SCHEMA = "xtos-openbci-cyton-frame-v1"


class CaptureReplayError(ValueError):
    """A capture bundle failed structural or integrity validation."""


def _read_regular_at(directory_fd: int, name: str, limit: int) -> bytes:
    """Read a fixed bundle member through a pinned directory descriptor."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                     dir_fd=directory_fd)
    except OSError as exc:
        raise CaptureReplayError(f"cannot open capture member {name}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CaptureReplayError(f"capture input is not a regular file: {name}")
        if info.st_size > limit:
            raise CaptureReplayError(f"capture input exceeds capacity: {name}")
        chunks = []
        remaining = limit + 1
        while remaining:
            block = os.read(fd, min(1024 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        data = b"".join(chunks)
        if len(data) > limit:
            raise CaptureReplayError(f"capture input exceeds capacity: {name}")
        return data
    finally:
        os.close(fd)


def _open_directory_pinned(path: Path) -> int:
    """Open every directory component with O_NOFOLLOW and retain its final FD."""
    parts = Path(path).parts
    if ".." in parts:
        raise CaptureReplayError("capture bundle path must not contain '..'")
    absolute = Path(path).is_absolute()
    fd = -1
    try:
        fd = os.open("/" if absolute else ".",
                     os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for component in parts:
            if component in ("/", ""):
                continue
            next_fd = os.open(component,
                              os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=fd)
            os.close(fd)
            fd = next_fd
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise CaptureReplayError("capture bundle is not a directory")
        result = fd
        fd = -1
        return result
    except OSError as exc:
        raise CaptureReplayError(f"cannot open capture bundle: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _read_bundle_files(bundle: Path):
    directory_fd = _open_directory_pinned(bundle)
    try:
        return (
            _read_regular_at(directory_fd, "manifest.json", MAX_MANIFEST_BYTES),
            _read_regular_at(directory_fd, "capture.bin", MAX_CAPTURE_BYTES),
            _read_regular_at(directory_fd, "frames.jsonl", MAX_FRAMES_BYTES),
        )
    finally:
        os.close(directory_fd)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CaptureReplayError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise CaptureReplayError(f"non-finite JSON constant: {value}")


def _validate_json_depth(text: str, name: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise CaptureReplayError(f"maximum JSON nesting depth exceeded in {name}")
        elif char in "]}":
            depth -= 1


def _parse_json(data: bytes, name: str):
    try:
        text = data.decode("utf-8")
        _validate_json_depth(text, name)
        return json.loads(text, object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise CaptureReplayError(f"invalid JSON in {name}: {exc}") from exc


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_stream_state(manifest: dict) -> None:
    """Reject command/error combinations that cannot come from this capture."""
    start = manifest.get("stream_start_command")
    stop = manifest.get("stream_stop_command")
    stop_error = manifest.get("stream_stop_error")
    if start not in (None, "b") or stop not in (None, "s"):
        raise CaptureReplayError("stream command state is invalid")
    if stop_error is not None and not isinstance(stop_error, str):
        raise CaptureReplayError("stream stop error has invalid type")
    if start is None and (stop is not None or stop_error is not None):
        raise CaptureReplayError("stream stop is recorded without a started stream")
    if start == "b" and stop is None and stop_error is None:
        raise CaptureReplayError("stream started without a stop result")
    if stop is not None and stop_error is not None:
        raise CaptureReplayError("stream stop command and stop error conflict")


def _verify_bundle(bundle: Path):
    bundle = Path(bundle)
    if bundle.is_symlink():
        raise CaptureReplayError("capture bundle must be a real directory")
    manifest_bytes, raw, frame_data = _read_bundle_files(bundle)
    manifest = _parse_json(manifest_bytes, "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise CaptureReplayError("unsupported capture manifest schema")
    if manifest.get("transport") != "serial" or manifest.get("sample_contract") != {
        "channels": 8,
        "sample_rate_hz": 250,
        "unit": "nanovolt",
        "gain": 24,
        "gain_source": "OpenBCI Cyton default; verify actual board channel gain before interpreting nV values",
        "timestamp": "host_monotonic_ns",
        "sequence": "extended_cyton_sample_counter",
    }:
        raise CaptureReplayError("capture sample contract mismatch")
    if manifest.get("physical_device_evidence") is not False \
            or manifest.get("physical_device_evidence_verified") is not False:
        raise CaptureReplayError("capture physical evidence boundary is invalid")
    _validate_stream_state(manifest)
    if manifest.get("raw_bytes") != len(raw) or manifest.get("raw_capture_sha256") != _sha(raw):
        raise CaptureReplayError("raw capture hash or size mismatch")
    if manifest.get("frames_sha256") != _sha(frame_data):
        raise CaptureReplayError("frames JSONL hash mismatch")
    lines = frame_data.splitlines()
    if len(lines) < 2:
        raise CaptureReplayError("capture contains no decoded frames")
    metadata = _parse_json(lines[0], "frames.jsonl metadata")
    if not isinstance(metadata, dict) or metadata.get("schema") != MANIFEST_SCHEMA \
            or metadata.get("record_type") != "capture_metadata" \
            or metadata.get("channels") != 8 or metadata.get("sample_rate_hz") != 250 \
            or metadata.get("unit") != "nanovolt" \
            or metadata.get("timestamp_clock") != "host_monotonic" \
            or metadata.get("gain") != 24 \
            or metadata.get("gain_source") != "OpenBCI Cyton default; verify actual board channel gain before interpreting nV values":
        raise CaptureReplayError("capture frame metadata mismatch")
    declared_frames = [_parse_json(line, f"frames.jsonl:{index + 2}")
                       for index, line in enumerate(lines[1:])]
    if len(declared_frames) > MAX_FRAME_COUNT:
        raise CaptureReplayError("capture frame count exceeds capacity")
    times = []
    for index, frame in enumerate(declared_frames):
        if not isinstance(frame, dict) or frame.get("schema") != FRAME_SCHEMA:
            raise CaptureReplayError(f"invalid frame schema at index {index}")
        if frame.get("channels") != 8 or frame.get("sample_rate_hz") != 250 \
                or frame.get("unit") != "nanovolt" or frame.get("timestamp_clock") != "host_monotonic":
            raise CaptureReplayError(f"frame contract mismatch at index {index}")
        if type(frame.get("sequence")) is not int or frame["sequence"] < 0 \
                or type(frame.get("sample_number")) is not int \
                or frame["sequence"] & 0xFF != frame["sample_number"]:
            raise CaptureReplayError(f"invalid sample counter at index {index}")
        if type(frame.get("timestamp_ns")) is not int or frame["timestamp_ns"] <= 0:
            raise CaptureReplayError(f"invalid host timestamp at index {index}")
        channels = frame.get("channels_nv")
        if not isinstance(channels, list) or len(channels) != 8 \
                or any(type(value) is not int or not -(1 << 31) <= value < (1 << 31)
                       for value in channels):
            raise CaptureReplayError(f"invalid channel samples at index {index}")
        quality = frame.get("quality")
        if type(quality) is not str or quality not in {"ok", "gap"}:
            raise CaptureReplayError(f"invalid frame quality at index {index}")
        if index:
            previous = declared_frames[index - 1]["sequence"]
            if frame["sequence"] <= previous:
                raise CaptureReplayError(f"frame sequence is not increasing at index {index}")
            expected_quality = "gap" if frame["sequence"] > previous + 1 else "ok"
            if frame["quality"] != expected_quality:
                raise CaptureReplayError(f"frame quality contradicts sequence at index {index}")
        elif frame["quality"] != "ok":
            raise CaptureReplayError("first frame cannot be marked as a gap")
        times.append(frame["timestamp_ns"])
    if any(right <= left for left, right in zip(times, times[1:])):
        raise CaptureReplayError("host receive timestamps are not strictly increasing")
    time_iter = iter(times)
    parser = CytonPacketParser(monotonic_ns=time_iter.__next__)
    actual = []
    try:
        for offset in range(0, len(raw), MAX_FEED_BYTES):
            actual.extend(parser.feed(raw[offset:offset + MAX_FEED_BYTES]))
    except StopIteration as exc:
        raise CaptureReplayError("raw bytes and decoded frame count disagree: raw contains more frames") from exc
    if len(actual) != len(declared_frames):
        raise CaptureReplayError("raw bytes and decoded frame count disagree")
    for index, (observed, declared) in enumerate(zip(actual, declared_frames)):
        if (observed.sequence != declared["sequence"]
                or observed.sample_number != declared["sample_number"]
                or observed.host_receive_monotonic_ns != declared["timestamp_ns"]
                or list(observed.channels_nv) != declared["channels_nv"]
                or observed.auxiliary.hex() != declared.get("auxiliary_hex")
                or observed.footer != declared.get("footer")):
            raise CaptureReplayError(f"raw bytes disagree with decoded frame {index}")
    stats = manifest.get("stats")
    if not isinstance(stats, dict):
        raise CaptureReplayError("manifest parser stats are missing")
    actual_stats = vars(parser.stats)
    for key, value in actual_stats.items():
        if type(stats.get(key)) is not int or stats[key] != value:
            detail = " (serial noise observed)" if key in {"discarded_bytes", "invalid_packets"} \
                and value else ""
            raise CaptureReplayError(f"manifest parser stats disagree for {key}{detail}")
    if len(actual) != stats["accepted_packets"]:
        raise CaptureReplayError("manifest parser stats do not match decoded frames")
    if stats["bytes_received"] != len(raw):
        raise CaptureReplayError("manifest parser stats do not match raw bytes")
    if manifest.get("trailing_packet_bytes") != parser.buffered_bytes:
        raise CaptureReplayError("manifest trailing packet byte count does not match raw bytes")
    if parser.buffered_bytes:
        raise CaptureReplayError(f"partial packet remains at end of capture: {parser.buffered_bytes} bytes")
    missing = 0
    for left, right in zip(actual, actual[1:]):
        delta = right.sequence - left.sequence
        if delta != 1:
            missing += max(0, delta - 1)
    if missing:
        raise CaptureReplayError(f"sample gap detected: {missing} missing samples")
    if stats.get("missing_samples") != missing or stats.get("duplicate_packets") != 0 \
            or stats.get("out_of_order_packets") != 0:
        raise CaptureReplayError("manifest counter stats disagree with frames")
    if stats.get("invalid_packets") != 0 or stats.get("discarded_bytes") != 0:
        raise CaptureReplayError("capture contains invalid packets or discarded serial bytes")
    if parser.buffered_bytes:
        raise CaptureReplayError(f"partial packet remains at end of capture: {parser.buffered_bytes} bytes")
    if manifest.get("status") != "completed":
        if stats.get("missing_samples") or stats.get("duplicate_packets") \
                or stats.get("out_of_order_packets"):
            raise CaptureReplayError("sample gap or ordering error in capture")
        raise CaptureReplayError("capture status is not a clean completed run")
    if manifest.get("capture_error") is not None or manifest.get("stream_stop_error") is not None:
        raise CaptureReplayError("capture manifest records a device or stream error")
    return manifest, actual, _sha(raw), _sha(frame_data), _sha(manifest_bytes)


def _run_pair(pipeline: Path, samples, channel_left: int, channel_right: int,
              destination: Path):
    with tempfile.TemporaryDirectory(prefix="xtos-cyton-pair-") as temporary:
        source = Path(temporary) / "pair.csv"
        filtered = Path(temporary) / "filtered.csv"
        first_sequence = samples[0].sequence
        with source.open("w", encoding="ascii", newline="\n") as stream:
            stream.write("# xtos-eeg-v1,channels=2,sample_rate_hz=250,unit=nanovolt\n")
            for frame in samples:
                timestamp = (samples[0].host_receive_monotonic_ns
                             + (frame.sequence - first_sequence) * SAMPLE_INTERVAL_NS)
                stream.write(f"{timestamp},{frame.channels_nv[channel_left]},"
                             f"{frame.channels_nv[channel_right]}\n")
        try:
            result = subprocess.run(
                [str(pipeline), "--input", str(source), "--strict-quality",
                 "--bandpass", "5", "40", "--dump-filtered", str(filtered)],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CaptureReplayError(f"filter pipeline failed to run: {exc}") from exc
        if result.returncode != 0:
            raise CaptureReplayError(f"filter pipeline rejected channel pair: {result.stderr.strip()}")
        summary = _parse_json(result.stdout.encode(), "pipeline summary")
        if not isinstance(summary, dict):
            raise CaptureReplayError("pipeline summary must be a JSON object")
        output_data = filtered.read_bytes()
        if summary.get("status") != "ok" or summary.get("frames_in") != len(samples) \
                or summary.get("frames_out") != len(samples) or summary.get("quality_rejected") != 0:
            raise CaptureReplayError("filter pipeline summary does not match source frames")
        destination.write_bytes(source.read_bytes())
        destination.with_name(destination.stem + "-filtered.csv").write_bytes(output_data)
        return summary


def _default_pipeline() -> Path:
    """Find the C filter beside sources or in the staged XTOS rootfs."""
    module_dir = Path(__file__).resolve().parent
    candidates = [
        module_dir / "xtos-eeg-pipeline",
        module_dir.parents[2] / "bin" / "xtos-eeg-pipeline"
        if len(module_dir.parents) > 2 else Path("/nonexistent"),
        Path("/usr/bin/xtos-eeg-pipeline"),
    ]
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink() and os.access(candidate, os.X_OK):
            return candidate
    return candidates[0]


def replay_capture(bundle, output, *, pipeline=None):
    """Validate an immutable capture bundle and filter all eight channels.

    Since Cyton packets contain only a sample counter, the existing 2-channel
    periodic filter interface receives a nominal 250 Hz grid anchored at the
    first host receive timestamp. This grid is not device time or latency evidence.
    """
    bundle = Path(bundle)
    destination = Path(output)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"refusing to overwrite processed output {destination}")
    manifest, frames, raw_hash, frames_hash, manifest_hash = _verify_bundle(bundle)
    if len(frames) < 2:
        raise CaptureReplayError("at least two frames are required by the filter quality gate")
    if pipeline is None:
        pipeline = _default_pipeline()
    pipeline = Path(pipeline).expanduser().absolute()
    if pipeline.is_symlink() or not pipeline.is_file() or not os.access(pipeline, os.X_OK):
        raise CaptureReplayError("filter pipeline must be an executable regular file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-",
                                      dir=destination.parent))
    try:
        summaries = []
        for pair in range(4):
            summaries.append(_run_pair(
                pipeline, frames, pair * 2, pair * 2 + 1,
                temporary / f"pair-{pair}.csv"))
        report = {
            "schema": "xtos-openbci-cyton-replay-report-v1",
            "status": "processed",
            "source_capture": str(bundle),
            "raw_capture_sha256": raw_hash,
            "frames_sha256": frames_hash,
            "manifest_sha256": manifest_hash,
            "frames_processed": len(frames),
            "channel_count": 8,
            "channel_pairs_processed": len(summaries),
            "processing": "5-40Hz second-order bandpass; existing 2-channel XTOS pipeline",
            "timestamp_basis": "counter_derived_nominal_250Hz_grid_anchored_at_first_host_receive",
            "device_timestamp_interpreted": False,
            "device_timestamp_raw_preserved": True,
            "end_to_end_latency_evidence": False,
            "physical_device_evidence": False,
            "medical_or_hard_rt_acceptance": False,
            "capture_status": manifest["status"],
            "pair_summaries": summaries,
            "limitations": [
                "nominal processing grid is reconstructed from the 250Hz counter cadence, not a device clock",
                "host receive timestamps are retained in the source capture and are not used as device timestamps",
                "AUX/footer bytes are retained raw; optional board-time fields are not interpreted",
                "fixture or PTY captures do not establish physical device support",
                "filter output is not an EEG classifier or medical result",
            ],
        }
        report_path = temporary / "report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=True, sort_keys=True,
                                          indent=2, allow_nan=False) + "\n",
                               encoding="utf-8")
        with report_path.open("rb") as stream:
            os.fsync(stream.fileno())
        _rename_directory_noreplace(temporary, destination)
        return report
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pipeline", type=Path)
    args = parser.parse_args(argv)
    try:
        report = replay_capture(args.capture, args.output, pipeline=args.pipeline)
    except (OSError, ValueError, CaptureReplayError) as exc:
        print(f"cyton_replay_error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
