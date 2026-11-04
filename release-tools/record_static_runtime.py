#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Record static runtime archives selected by the actual build compiler."""
from pathlib import Path
import subprocess
import sys


def main():
    if len(sys.argv) != 3:
        raise SystemExit(f"usage: {sys.argv[0]} COMPILER OUTPUT_FILE")
    compiler, output = sys.argv[1:]
    paths = []
    for name in ("libc.a", "libgcc.a"):
        value = subprocess.check_output([compiler, f"-print-file-name={name}"], text=True).strip()
        archive = Path(value)
        if not archive.is_absolute() or not archive.is_file():
            raise SystemExit(f"{compiler} did not resolve {name} to an existing absolute archive: {value}")
        paths.append(str(archive.resolve(strict=True)))
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("".join(path + "\n" for path in paths))
    print(f"static_runtime_files=pass compiler={compiler} files={len(paths)}")


if __name__ == "__main__":
    main()
