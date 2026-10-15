#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Fetch exact APT source packages listed by collect_distribution_notices.py."""
from __future__ import annotations

import csv
import hashlib
import subprocess
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(f"usage: {sys.argv[0]} SOURCE_PACKAGES.tsv OUTPUT_DIR")
    manifest, output = Path(sys.argv[1]), Path(sys.argv[2])
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise SystemExit(f"output directory must be empty: {output}")
    with manifest.open(newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    if not rows:
        raise SystemExit("source package manifest is empty")

    for row in rows:
        name = row["source_package"]
        version = row["source_version"]
        before = set(output.iterdir())
        print(f"fetch source package {name}={version}", flush=True)
        subprocess.run(
            ["apt-get", "source", "--download-only", f"{name}={version}"],
            cwd=output,
            check=True,
        )
        created = set(output.iterdir()) - before
        if not any(path.is_file() and path.suffix == ".dsc" for path in created):
            raise SystemExit(f"APT did not provide a source descriptor for {name}={version}")

    files = sorted(path for path in output.iterdir() if path.is_file() and path.name != "SHA256SUMS")
    with (output / "SHA256SUMS").open("w") as stream:
        for path in files:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            stream.write(f"{digest}  {path.name}\n")
    print(f"source_archives=pass packages={len(rows)} files={len(files)}")


if __name__ == "__main__":
    main()
