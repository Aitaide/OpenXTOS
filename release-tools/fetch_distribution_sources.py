#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Fetch exact APT source packages listed by collect_distribution_notices.py."""
from __future__ import annotations

import csv
import hashlib
import os
import re
import subprocess
import sys
from email.parser import Parser
from pathlib import Path
from urllib.parse import quote


def source_fields(descriptor: Path):
    text = descriptor.read_text()
    if text.startswith("-----BEGIN PGP SIGNED MESSAGE-----"):
        text = text.split("\n\n", 1)[1].split("-----BEGIN PGP SIGNATURE-----", 1)[0]
        text = "\n".join(line[2:] if line.startswith("- ") else line for line in text.splitlines())
    return Parser().parsestr(text)


def source_members(descriptor: Path):
    checksums = source_fields(descriptor).get("Checksums-Sha256", "")
    members = []
    for line in checksums.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 3:
            raise SystemExit(f"invalid source checksum entry in {descriptor.name}")
        digest, size, name = fields
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not size.isdigit() or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+~_-]*", name):
            raise SystemExit(f"unsafe or malformed source member in {descriptor.name}")
        members.append((digest, int(size), name))
    if not members:
        raise SystemExit(f"missing source checksums in {descriptor.name}")
    return members


def validate_source(name: str, version: str, descriptor: Path):
    fields = source_fields(descriptor)
    if fields.get("Source") != name or fields.get("Version") != version:
        raise SystemExit(f"source descriptor does not match {name}={version}: {descriptor.name}")
    for digest, size, filename in source_members(descriptor):
        path = descriptor.parent / filename
        if not path.is_file() or path.stat().st_size != size:
            raise SystemExit(f"missing or wrong-sized source archive {filename}")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise SystemExit(f"source archive checksum mismatch: {filename}")


def download(url: str, destination: Path):
    temporary = destination.with_name(destination.name + ".partial")
    subprocess.run(["curl", "-4", "--fail", "--location", "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "30", "--max-time", "600", "--retry", "2", "--output", str(temporary), url], check=True)
    temporary.replace(destination)


def fetch_ubuntu_source(name: str, version: str, output: Path):
    # Ubuntu's archive API retains immutable source files after APT supersedes them.
    base = "https://api.launchpad.net/1.0/ubuntu/+archive/primary/+files/"
    filename = f"{name}_{version.split(':', 1)[-1]}.dsc"
    descriptor = output / filename
    download(base + quote(filename, safe=""), descriptor)
    fields = source_fields(descriptor)
    if fields.get("Source") != name or fields.get("Version") != version:
        raise SystemExit(f"historical source descriptor does not match {name}={version}")
    for digest, size, member in source_members(descriptor):
        path = output / member
        if path.is_file() and path.stat().st_size == size:
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() == digest:
                    continue
        download(base + quote(member, safe=""), path)
    validate_source(name, version, descriptor)
    return descriptor


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
        result = subprocess.run(
            ["apt-get", "source", "--download-only", f"{name}={version}"],
            cwd=output,
            check=False,
        )
        if result.returncode:
            if os.environ.get("XTOS_SOURCE_ARCHIVE") != "ubuntu":
                result.check_returncode()
            print(f"fetch official Ubuntu historical source {name}={version}", flush=True)
            descriptor = fetch_ubuntu_source(name, version, output)
        else:
            created = set(output.iterdir()) - before
            descriptors = [path for path in created if path.is_file() and path.suffix == ".dsc"]
            if len(descriptors) != 1:
                raise SystemExit(f"APT did not provide exactly one source descriptor for {name}={version}")
            descriptor = descriptors[0]
        validate_source(name, version, descriptor)

    files = sorted(path for path in output.iterdir() if path.is_file() and path.name != "SHA256SUMS")
    with (output / "SHA256SUMS").open("w") as stream:
        for path in files:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            stream.write(f"{digest}  {path.name}\n")
    print(f"source_archives=pass packages={len(rows)} files={len(files)}")


if __name__ == "__main__":
    main()
