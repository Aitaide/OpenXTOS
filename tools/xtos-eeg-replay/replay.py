#!/usr/bin/env python3
"""Validate and summarize an XTOS virtual EEG replay CSV."""

import argparse
import csv
import json
import math
import sys
from pathlib import Path


EXPECTED_HEADER = "# xtos-eeg-v1,channels=2,sample_rate_hz=250,unit=nanovolt"
EXPECTED_COLUMNS = ("timestamp_ns", "ch0_nv", "ch1_nv")
EXPECTED_INTERVAL_NS = 4_000_000
INTERVAL_TOLERANCE = 0.01


class ReplayError(ValueError):
    """A user-facing replay input validation error."""


def validate_csv(input_path: Path) -> dict:
    """Validate one replay CSV and return a JSON-serializable summary."""
    timestamps = []
    intervals = []

    try:
        stream = input_path.open("r", encoding="utf-8", newline="")
    except OSError as exc:
        raise ReplayError(f"cannot open {input_path}: {exc}") from exc

    with stream:
        header = stream.readline()
        if not header:
            raise ReplayError("missing format header")
        if header.rstrip("\r\n") != EXPECTED_HEADER:
            raise ReplayError("invalid format header")

        reader = csv.reader(stream)
        for line_number, row in enumerate(reader, start=2):
            if len(row) != len(EXPECTED_COLUMNS):
                raise ReplayError(
                    f"line {line_number}: expected 3 columns, got {len(row)}"
                )
            try:
                timestamp_ns = int(row[0])
            except ValueError as exc:
                raise ReplayError(
                    f"line {line_number}: timestamp_ns must be an integer"
                ) from exc
            try:
                channel_values = (float(row[1]), float(row[2]))
            except ValueError as exc:
                raise ReplayError(
                    f"line {line_number}: channel values must be numbers"
                ) from exc
            if not all(math.isfinite(value) for value in channel_values):
                raise ReplayError(
                    f"line {line_number}: channel values must be finite"
                )

            if timestamps and timestamp_ns <= timestamps[-1]:
                raise ReplayError(
                    f"line {line_number}: timestamps must be strictly increasing"
                )
            if timestamps:
                interval_ns = timestamp_ns - timestamps[-1]
                intervals.append(interval_ns)
                lower = EXPECTED_INTERVAL_NS * (1 - INTERVAL_TOLERANCE)
                upper = EXPECTED_INTERVAL_NS * (1 + INTERVAL_TOLERANCE)
                if not lower <= interval_ns <= upper:
                    raise ReplayError(
                        f"line {line_number}: sampling interval {interval_ns} ns "
                        f"is outside 1% of {EXPECTED_INTERVAL_NS} ns"
                    )
            timestamps.append(timestamp_ns)

    if not timestamps:
        raise ReplayError("input contains no data rows")

    summary = {
        "status": "ok",
        "format": "xtos-eeg-v1",
        "channels": 2,
        "sample_rate_hz": 250,
        "unit": "nanovolt",
        "sample_count": len(timestamps),
        "first_timestamp_ns": timestamps[0],
        "last_timestamp_ns": timestamps[-1],
        "expected_interval_ns": EXPECTED_INTERVAL_NS,
        "interval_count": len(intervals),
    }
    if intervals:
        summary["min_interval_ns"] = min(intervals)
        summary["max_interval_ns"] = max(intervals)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Validate an XTOS EEG replay CSV")
    parser.add_argument("--input", required=True, type=Path, help="CSV input path")
    parser.add_argument(
        "--check", action="store_true", help="validate input and print a JSON summary"
    )
    args = parser.parse_args(argv)

    if not args.check:
        parser.error("--check is required")

    try:
        summary = validate_csv(args.input)
    except ReplayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
