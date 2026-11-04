#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Collect project notices and copyright metadata for host binaries in rootfs."""
from __future__ import annotations

import csv
import re
import shutil
import subprocess
import sys
from pathlib import Path

from install_project_notices import install as install_project_notices


def add_host_paths(path: Path, required: dict[str, set[str]], ancestors: frozenset[Path] = frozenset()) -> None:
    try:
        resolved = path.resolve(strict=True)
        aliases = {str(path), str(resolved)}
        # dpkg may record either side of Debian/Ubuntu's usrmerge symlinks.
        for alias in tuple(aliases):
            relative = alias.removeprefix("/usr") if alias.startswith("/usr/") else alias
            if relative.split("/")[1] in {"bin", "sbin", "lib", "lib32", "lib64", "libx32"}:
                candidate = Path(relative if alias.startswith("/usr/") else "/usr" + alias)
                if candidate.exists() and candidate.resolve(strict=True) == resolved:
                    aliases.add(str(candidate))
        required.setdefault(str(resolved), set()).update(aliases)
        if path.is_dir():
            if resolved in ancestors:
                raise SystemExit(f"directory symlink cycle in explicit host path {path}")
            for child in sorted(path.iterdir()):
                add_host_paths(child, required, ancestors | {resolved})
    except (OSError, RuntimeError) as error:
        raise SystemExit(f"could not read explicit host path {path}: {error}") from error


def built_using_sources(value: str, field: str, owner: str) -> list[tuple[str, str]]:
    if not value:
        return []
    sources = []
    for entry in value.split(","):
        match = re.fullmatch(r"\s*([a-z0-9][a-z0-9+.-]+)\s+\(=\s*([0-9][A-Za-z0-9.+:~_-]*)\)\s*", entry)
        if match is None:
            raise SystemExit(f"malformed {field} metadata for {owner!r}: {entry!r}")
        sources.append((match[1], match[2]))
    return sources


def main() -> None:
    if len(sys.argv) not in (3, 4, 5):
        raise SystemExit(f"usage: {sys.argv[0]} SOURCE_ROOT ROOTFS [HOST_FILES [ARTIFACT]]")
    source_root, rootfs = map(Path, sys.argv[1:3])
    host_files = Path(sys.argv[3]) if len(sys.argv) >= 4 else None
    artifact = sys.argv[4] if len(sys.argv) == 5 else "x86_64 installer"
    docroot = install_project_notices(source_root, rootfs, artifact)
    third_party = docroot / "third-party-copyright"
    third_party.mkdir(parents=True, exist_ok=True)

    paths = []
    required_paths: dict[str, set[str]] = {}
    if host_files is None:
        for path in rootfs.rglob("*"):
            if path.is_file() or path.is_symlink():
                paths.append("/" + path.relative_to(rootfs).as_posix())
    else:
        for line in host_files.read_text().splitlines():
            if not line.strip():
                continue
            path = Path(line.strip())
            if not path.is_absolute():
                raise SystemExit(f"explicit host path must be absolute: {path}")
            add_host_paths(path, required_paths)
        for aliases in required_paths.values():
            paths.extend(aliases)

    owners: set[str] = set()
    owned_paths: set[str] = set()
    ordered_paths = sorted(set(paths))
    for offset in range(0, len(ordered_paths), 80):
        result = subprocess.run(
            ["dpkg-query", "-S", "--", *ordered_paths[offset : offset + 80]],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode not in (0, 1):
            raise SystemExit(f"dpkg ownership query error: {result.stderr.strip()}")
        for line in result.stdout.splitlines():
            if ": " not in line:
                continue
            owner_field, owned_path = line.split(": ", 1)
            if owner_field.startswith("diversion by "):
                continue
            if owned_path not in ordered_paths[offset : offset + 80]:
                continue
            path_owners = {owner.strip() for owner in owner_field.split(",") if owner.strip()}
            if path_owners:
                owners.update(path_owners)
                owned_paths.add(owned_path)

    for path, aliases in required_paths.items():
        if not aliases.intersection(owned_paths):
            raise SystemExit(f"no dpkg ownership found for explicit host path {path}")

    packages = []
    sources: dict[tuple[str, str], set[str]] = {}
    missing_copyright = []
    for owner in sorted(owners):
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${binary:Package}\t${source:Package}\t${source:Version}\t${Version}\t${Built-Using}\t${Static-Built-Using}\n", owner],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise SystemExit(f"could not resolve dpkg owner {owner!r} to package metadata: {result.stderr.strip()}")
        fields = result.stdout.rstrip("\n").split("\t")
        if len(fields) != 6:
            raise SystemExit(f"malformed package metadata for {owner!r}: expected six fields")
        binary, source, source_version, binary_version, built_using, static_built_using = fields
        binary_base = binary.split(":", 1)[0]
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9+.-]+(?::[a-z0-9][a-z0-9-]*)?", binary)
            or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]+", source)
            or not re.fullmatch(r"[0-9][A-Za-z0-9.+:~_-]*", binary_version)
            or not re.fullmatch(r"[0-9][A-Za-z0-9.+:~_-]*", source_version)
            or binary != owner
        ):
            raise SystemExit(f"malformed package metadata for {owner!r}")
        extra_sources = built_using_sources(built_using, "Built-Using", owner)
        extra_sources += built_using_sources(static_built_using, "Static-Built-Using", owner)
        for name, version in [(source, source_version), *extra_sources]:
            sources.setdefault((name, version), set()).add(binary)
        copyright = Path("/usr/share/doc") / binary_base / "copyright"
        if not copyright.is_file():
            missing_copyright.append(binary)
            continue
        safe_name = re.sub(r"[^A-Za-z0-9.+_-]+", "_", binary)
        shutil.copy2(copyright, third_party / f"{safe_name}.copyright")
        packages.append((binary, binary_version, source, source_version, f"third-party-copyright/{safe_name}.copyright"))

    if missing_copyright:
        raise SystemExit("missing copyright files for: " + ", ".join(sorted(missing_copyright)))
    if not packages:
        raise SystemExit("no host packages owning shipped rootfs files were identified")

    with (docroot / "HOST_PACKAGES.tsv").open("w", newline="") as output:
        writer = csv.writer(output, delimiter="\t", lineterminator="\n")
        writer.writerow(("binary_package", "binary_version", "source_package", "source_version", "copyright_file"))
        writer.writerows(packages)

    with (docroot / "SOURCE_PACKAGES.tsv").open("w", newline="") as output:
        writer = csv.writer(output, delimiter="\t", lineterminator="\n")
        writer.writerow(("source_package", "source_version", "binary_packages"))
        for (source, version), binaries in sorted(sources.items()):
            writer.writerow((source, version, ",".join(sorted(binaries))))
    print(f"distribution_notices=pass packages={len(packages)} sources={len(sources)}")


if __name__ == "__main__":
    main()
