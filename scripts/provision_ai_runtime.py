#!/usr/bin/env python3
"""Create a pinned descriptor for, or safely install, a private CPU AI runtime bundle.

No download or package resolution is performed. `describe` records the complete
archive inventory; `install` accepts explicit bundle, descriptor and lock files,
checks every byte before extraction, and writes only to a nonexistent output root.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

SCHEMA = "xtos-ai-runtime-bundle-v1"
MAX_ARCHIVE_BYTES = 1_000_000_000
MAX_UNPACKED_BYTES = 600_000_000
MAX_MEMBERS = 20_000
PREFIX = "clean"
REQUIRED_RUNTIME = {"CPython": "3.11.15", "onnxruntime": "1.23.2", "numpy": "1.26.4"}
ELF_SYSTEM_SONAMES = [
    "ld-linux-x86-64.so.2", "libc.so.6", "libcrypt.so.1", "libdl.so.2",
    "libgcc_s.so.1", "libm.so.6", "libpthread.so.0", "librt.so.1",
    "libstdc++.so.6", "libutil.so.1", "libz.so.1",
]
WRAPPER = b'''#!/bin/sh\n# Private CPython entrypoint: intentionally no system-python fallback.\nset -eu\nHERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)\nPYTHON="$HERE/clean/python/bin/python3.11"\nif [ ! -x "$PYTHON" ]; then echo "private XTOS Python missing: $PYTHON" >&2; exit 127; fi\nexport LD_LIBRARY_PATH="$HERE/clean/python/lib:$HERE/clean/python/lib/python3.11/site-packages/numpy.libs:$HERE/clean/python/lib/python3.11/site-packages/onnxruntime/capi"\nexec "$PYTHON" -I -B "$@"\n'''


class BundleError(ValueError):
    """Invalid descriptor, archive, or installation request."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _safe_member_path(name: str, *, directory: bool = False) -> str:
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise BundleError(f"invalid archive path: {name!r}")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise BundleError(f"absolute archive path rejected: {name!r}")
    raw = name[:-1] if directory and name.endswith("/") else name
    parts = raw.split("/")
    if not raw or any(part in ("", ".", "..") for part in parts):
        raise BundleError(f"non-canonical archive path rejected: {name!r}")
    if parts[0] != PREFIX:
        raise BundleError(f"archive member outside {PREFIX}/ rejected: {name!r}")
    return "/".join(parts[1:])


def _resolved_link(member_path: str, target: str) -> str:
    if not isinstance(target, str) or not target or "\x00" in target or "\\" in target or target.startswith("/"):
        raise BundleError(f"unsafe symlink target for {member_path!r}")
    # Preserve component order: an intermediate symlink must expand before '..'.
    # Only the complete archive graph can establish whether this stays in root.
    return posixpath.join(posixpath.dirname(member_path), target)


def _validate_symlink_graph(entries: list[dict]) -> None:
    by_path = {entry["path"]: entry for entry in entries}
    for entry in entries:
        path = entry["path"]
        for parent in PurePosixPath(path).parents:
            parent_name = str(parent)
            if parent_name == ".":
                continue
            if parent_name not in by_path or by_path[parent_name]["type"] != "dir":
                raise BundleError(f"archive parent directory missing or shadowed: {parent_name!r}")
    links = {e["path"]: e["target"] for e in entries if e["type"] == "symlink"}
    for path, target in links.items():
        followed = 1

        def walk(parts: list[str], active: set[str]) -> list[str]:
            nonlocal followed
            resolved: list[str] = []
            for part in parts:
                # Even 'file/..', 'file/.' and 'file/' require a directory.
                if resolved and by_path["/".join(resolved)]["type"] != "dir":
                    raise BundleError(f"non-directory symlink prefix: {path!r}")
                if part in ("", "."):
                    continue
                if part == "..":
                    if not resolved:
                        raise BundleError(f"symlink escapes bundle root: {path!r}")
                    resolved.pop()
                    continue
                candidate = "/".join(resolved + [part])
                if candidate not in by_path:
                    raise BundleError(f"dangling symlink rejected: {path!r} -> {target!r}")
                if candidate in links:
                    if candidate in active:
                        raise BundleError(f"symlink cycle at {path!r}")
                    followed += 1
                    if followed > 64:
                        raise BundleError(f"symlink resolution depth exceeded: {path!r}")
                    resolved = walk(_resolved_link(candidate, links[candidate]).split("/"),
                                    active | {candidate})
                else:
                    resolved.append(part)
            return resolved

        if not walk(_resolved_link(path, target).split("/"), {path}):
            raise BundleError(f"symlink resolves to bundle root: {path!r}")


def _member_kind(member: tarfile.TarInfo) -> str:
    if member.isdir():
        return "dir"
    if member.type in (tarfile.REGTYPE, tarfile.AREGTYPE):
        return "file"
    if member.issym():
        return "symlink"
    raise BundleError(f"unsupported tar member type: {member.name!r}")


def inspect_archive(bundle: Path) -> list[dict]:
    size = bundle.stat().st_size
    if size <= 0 or size > MAX_ARCHIVE_BYTES:
        raise BundleError(f"archive size outside limits: {size}")
    entries: list[dict] = []
    seen: set[str] = set()
    total = 0
    try:
        with tarfile.open(bundle, mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_MEMBERS:
                raise BundleError(f"too many archive members: {len(members)}")
            for member in members:
                kind = _member_kind(member)
                if kind == "dir":
                    path = _safe_member_path(member.name, directory=True)
                elif kind == "file":
                    path = _safe_member_path(member.name)
                    if member.size < 0:
                        raise BundleError(f"negative file size: {member.name!r}")
                    total += member.size
                    if total > MAX_UNPACKED_BYTES:
                        raise BundleError("unpacked archive exceeds size limit")
                else:
                    path = _safe_member_path(member.name)
                    if not path.startswith("python/"):
                        raise BundleError(f"only controlled Python symlinks are allowed: {member.name!r}")
                if getattr(member, "sparse", None) is not None:
                    raise BundleError(f"sparse tar member rejected: {member.name!r}")
                if kind != "file" and member.size != 0:
                    raise BundleError(f"non-file member has data: {member.name!r}")
                if path in seen:
                    raise BundleError(f"duplicate archive member: {path!r}")
                seen.add(path)
                if member.mode & ~0o777:
                    raise BundleError(f"special permission bits rejected: {member.name!r}")
                row = {"path": path or ".", "type": kind, "mode": member.mode & 0o777}
                if kind == "file":
                    row["size"] = member.size
                    source = archive.extractfile(member)
                    if source is None:
                        raise BundleError(f"unreadable regular file: {member.name!r}")
                    h = hashlib.sha256()
                    read = 0
                    with source:
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            h.update(block)
                            read += len(block)
                    if read != member.size:
                        raise BundleError(f"truncated archive member: {member.name!r}")
                    row["sha256"] = h.hexdigest()
                elif kind == "symlink":
                    row["target"] = member.linkname
                    _resolved_link(path, member.linkname)
                entries.append(row)
    except (tarfile.TarError, OSError, EOFError) as exc:
        if isinstance(exc, BundleError):
            raise
        raise BundleError(f"cannot inspect gzip tar archive: {exc}") from exc
    root_entries = [e for e in entries if e["path"] == "."]
    if len(root_entries) != 1 or root_entries[0]["type"] != "dir":
        raise BundleError("archive must contain exactly one clean/ directory root")
    _validate_symlink_graph(entries)
    entries.sort(key=lambda e: (e["path"], e["type"]))
    return entries


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BundleError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _publish_noreplace(source: Path, destination: Path) -> None:
    # Linux renameat2 keeps an empty directory created by a racing caller intact.
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise BundleError("atomic no-replace publication requires Linux renameat2")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"invalid JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BundleError(f"JSON root must be an object: {path}")
    return value


def _verify_descriptor(bundle: Path, descriptor_path: Path, lock_path: Path) -> tuple[dict, list[dict]]:
    if descriptor_path.is_symlink() or not descriptor_path.is_file():
        raise BundleError("descriptor must be a regular non-symlink file")
    if lock_path.is_symlink() or not lock_path.is_file():
        raise BundleError("dependency lock must be a regular non-symlink file")
    desc = _read_json(descriptor_path)
    if desc.get("schema") != SCHEMA:
        raise BundleError("unsupported descriptor schema")
    expected = desc.get("bundle")
    if not isinstance(expected, dict):
        raise BundleError("descriptor has no bundle hash/size")
    if not bundle.is_file() or bundle.is_symlink():
        raise BundleError("bundle must be a regular non-symlink file")
    # Verify the complete compressed payload before parsing or extracting it.
    actual_size = bundle.stat().st_size
    if actual_size != expected.get("bytes"):
        raise BundleError("bundle byte size does not match descriptor")
    actual_hash = sha256_file(bundle)
    if actual_hash != expected.get("sha256"):
        raise BundleError("bundle SHA-256 does not match descriptor")
    lock_hash = sha256_file(lock_path)
    if lock_hash != desc.get("lock", {}).get("sha256"):
        raise BundleError("dependency lock SHA-256 does not match descriptor")
    # Only now inspect tar metadata and file bytes; still do not execute payloads.
    entries = inspect_archive(bundle)
    if entries != desc.get("members"):
        raise BundleError("archive file inventory does not exactly match descriptor")
    if desc.get("elf_system_sonames") != ELF_SYSTEM_SONAMES:
        raise BundleError("ELF system dependency allowlist does not match the pinned target policy")
    provenance = desc.get("provenance", {})
    if provenance.get("classification") == "reused_interpreter_fresh_locked_wheels":
        _verify_fresh_evidence(bundle, entries, provenance, lock_path)
    elif provenance.get("classification") != "reused_verified_artifact" or provenance.get("fresh_wheel_install") is not False:
        raise BundleError("descriptor provenance must identify reused verified inputs or bound fresh assembly evidence")
    with tarfile.open(bundle, mode="r:gz") as archive:
        actual_licenses = _metadata_from_archive(archive, entries)
    if actual_licenses != desc.get("package_licenses"):
        raise BundleError("package metadata/license-source inventory does not match the archive")
    if desc.get("runtime") != REQUIRED_RUNTIME:
        raise BundleError("descriptor runtime versions are not the pinned CPython/ORT/NumPy set")
    if desc.get("architecture") != "x86_64":
        raise BundleError("only x86_64 runtime bundles are supported")
    return desc, entries


def _verify_fresh_evidence(bundle: Path, entries: list[dict], provenance: dict, lock: Path) -> None:
    """A fresh claim is valid only with exact, in-bundle evidence and locked wheels."""
    by_path = {e["path"]: e for e in entries}
    evidence_path = provenance.get("assembly_evidence_path")
    if evidence_path != "notices/assembly.json" or provenance.get("fresh_wheel_install") is not True:
        raise BundleError("fresh assembly evidence path/claim invalid")
    row = by_path.get(evidence_path, {})
    if row.get("type") != "file" or row.get("sha256") != provenance.get("assembly_evidence_sha256"):
        raise BundleError("fresh assembly evidence digest mismatch")
    with tarfile.open(bundle, "r:gz") as archive:
        evidence = json.loads(archive.extractfile("clean/" + evidence_path).read(), object_pairs_hook=_unique_object)
    if (evidence.get("schema") != "xtos-ai-fresh-assembly-v1"
            or evidence.get("acquisition_sha256") != "a2fb0554c65b1daec82a22ad94d99d4c538ef069d1b827dc0a1cc28f27c057ab"
            or evidence.get("lock_sha256") != sha256_file(lock)
            or evidence.get("install_rc") != 0
            or any(evidence.get(k) is not True for k in ("fresh_wheel_install", "no_index", "require_hashes", "no_deps", "no_compile"))):
        raise BundleError("fresh assembly evidence contract mismatch")
    interpreter = evidence.get("interpreter", {})
    if (interpreter.get("classification") != "reused_verified_artifact" or interpreter.get("fresh_build") is not False
            or interpreter.get("bundle_sha256") != "2d5322a229875416717500c9b29609e204ff1c73e6fa35e9ba669313e849a58d"):
        raise BundleError("fresh assembly evidence must disclose reused interpreter")
    required = {"coloredlogs":"15.0.1", "flatbuffers":"25.12.19", "humanfriendly":"10.0",
                "mpmath":"1.3.0", "numpy":"1.26.4", "onnxruntime":"1.23.2",
                "packaging":"26.3", "protobuf":"5.29.5", "sympy":"1.14.0"}
    wheels = evidence.get("wheels", [])
    if not isinstance(wheels, list) or len(wheels) != 9 or {w.get("name"):w.get("version") for w in wheels} != required:
        raise BundleError("fresh assembly evidence must name exact nine wheels")
    lock_text = lock.read_text()
    for wheel in wheels:
        match = re.search(r"^" + re.escape(wheel["name"]) + r"==" + re.escape(wheel["version"]) + r"[^\n]*(?:\n[ \t]+[^\n]*)*", lock_text, re.M)
        if not match or "--hash=sha256:" + wheel.get("sha256", "invalid") not in match.group():
            raise BundleError("fresh assembly wheel hash not in exact dependency lock")
        record = by_path.get(wheel.get("record_path"), {})
        if (record.get("type") != "file" or record.get("sha256") != wheel.get("record_sha256")
                or type(wheel.get("verified_members")) is not int or wheel["verified_members"] < 1
                or wheel.get("bad") != [] or wheel.get("missing") != []):
            raise BundleError("fresh assembly wheel RECORD evidence invalid")
    if by_path.get(evidence.get("source_manifest_path"), {}).get("type") != "file":
        raise BundleError("fresh assembly source manifest evidence missing")


def _metadata_from_archive(archive: tarfile.TarFile, members: list[dict]) -> dict:
    files = {m["path"] for m in members if m["type"] == "file"}
    prefixes = "python/lib/python3.11/site-packages/"
    packages = []
    for name, version in [("onnxruntime", "1.23.2"), ("numpy", "1.26.4"),
                          ("coloredlogs", "15.0.1"), ("humanfriendly", "10.0"),
                          ("flatbuffers", "25.12.19"), ("packaging", "26.3"),
                          ("protobuf", "5.29.5"), ("sympy", "1.14.0"), ("mpmath", "1.3.0")]:
        normalized = name.replace("-", "_").lower()
        dist = next((p for p in sorted(files) if p.startswith(prefixes) and p.endswith(".dist-info/METADATA")
                     and p.split("/")[-2].split("-")[0].replace("-", "_").lower() == normalized), None)
        package_dir = "python/lib/python3.11/site-packages/" + name.replace("-", "_")
        package_dir_alt = "python/lib/python3.11/site-packages/" + name
        license_files = [p for p in sorted(files)
                         if ((dist and p.startswith(dist.rsplit("/", 1)[0] + "/"))
                             or p.startswith(package_dir + "/") or p.startswith(package_dir_alt + "/")
                             or p.startswith("notices/" + name + "/"))
                         and (Path(p).name.lower().startswith(("license", "copying", "copyright"))
                              or "/licenses/" in p.lower())]
        metadata = ""
        if dist:
            tar_name = "clean/" + dist
            try:
                obj = archive.extractfile(tar_name)
                metadata = obj.read(1024 * 1024).decode("utf-8", "replace") if obj else ""
            except (KeyError, OSError):
                pass
        headers = {}
        for line in metadata.splitlines():
            if ":" in line and not line[:1].isspace():
                key, value = line.split(":", 1)
                if key in {"Name", "Version", "License", "License-Expression"}:
                    headers[key] = value.strip()
        if not dist or headers.get("Name", "").lower().replace("_", "-") != name.lower().replace("_", "-"):
            raise BundleError(f"runtime package metadata missing or mismatched: {name}")
        if headers.get("Version") != version:
            raise BundleError(f"runtime package version mismatch: {name} expected {version}, found {headers.get('Version')}")
        license_field = headers.get("License-Expression") or headers.get("License", "")
        packages.append({"name": name, "version": version, "license_metadata": license_field,
                         "metadata_path": dist, "license_files": license_files,
                         "license_status": "bundled" if license_files else "release_license_source_missing"})
    return {"python_license_files": [p for p in sorted(files) if p == "python/lib/python3.11/LICENSE.txt"],
            "packages": packages}


def _build_descriptor(bundle: Path, lock: Path, report: Path, offline: Path, verified: Path) -> dict:
    report_doc, offline_doc, verified_doc = _read_json(report), _read_json(offline), _read_json(verified)
    bundle_digest = sha256_file(bundle)
    if report_doc.get("bundle", {}).get("sha256") != bundle_digest or report_doc.get("bundle", {}).get("bytes") != bundle.stat().st_size:
        raise BundleError("source report does not identify these exact bundle bytes")
    if not report_doc.get("clean_runtime_pass") or verified_doc.get("status") != "focused_guest_runtime_pass_not_image_acceptance":
        raise BundleError("source evidence does not record the verified clean guest runtime pass")
    if verified_doc.get("runtime_stderr_sha256") != hashlib.sha256(b"").hexdigest():
        raise BundleError("source guest runtime stderr was not clean")
    lock_hash = sha256_file(lock)
    if lock_hash != report_doc.get("lock_sha256"):
        raise BundleError("provided AI lock does not match source evidence")
    reused = offline_doc.get("reused_packages", [])
    if not {"onnxruntime", "numpy"}.issubset({p.get("package") for p in reused}):
        raise BundleError("offline evidence lacks the reused runtime and NumPy RECORD checks")
    for row in reused:
        if row.get("record_hashes_bad") or row.get("record_missing"):
            raise BundleError(f"installed RECORD verification failed for {row.get('package')}")
    entries = inspect_archive(bundle)
    with tarfile.open(bundle, mode="r:gz") as archive:
        licenses = _metadata_from_archive(archive, entries)
    return {
        "schema": SCHEMA,
        "name": "xtos-private-ai-cpu-runtime",
        "architecture": "x86_64",
        "runtime": REQUIRED_RUNTIME,
        "elf_system_sonames": ELF_SYSTEM_SONAMES,
        "bundle": {"file": bundle.name, "bytes": bundle.stat().st_size, "sha256": bundle_digest},
        "lock": {"file": lock.name, "sha256": lock_hash, "kind": "tools/xtos-onnx-baseline/requirements-ai.lock"},
        "members": entries,
        "package_licenses": licenses,
        "provenance": {
            "classification": "reused_verified_artifact",
            "fresh_wheel_install": False,
            "offline_lock_evidence_sha256": sha256_file(offline),
            "verified_guest_result_sha256": sha256_file(verified),
            "source_revision": report_doc.get("source"),
            "source_report_sha256": sha256_file(report),
            "scope": "reused CPython 3.11.15 + locked Python runtime closure; CPU guest inference evidence only",
            "limitations": ["original locked wheel archives were absent; installed RECORDs/cache provenance were reused, not a fresh wheel installation",
                            "release source archives are not included", "review and complete license-source release audit remains required",
                            "this is not a full PyTorch/TensorFlow stack", "no hard-real-time or model-accuracy claim"],
        },
    }


def _extract(bundle: Path, members: list[dict], destination: Path) -> None:
    expected = {e["path"]: e for e in members}
    destination.mkdir(mode=0o700)
    with tarfile.open(bundle, mode="r:gz") as archive:
        # Directories and regular files first, with no symlink parents ever created.
        archive_members = archive.getmembers()
        for member in sorted(archive_members, key=lambda item: ({"dir": 0, "file": 1, "symlink": 2}[_member_kind(item)],
                                                                   item.name.count("/"), item.name)):
            kind = _member_kind(member)
            path = _safe_member_path(member.name, directory=kind == "dir") or "."
            row = expected[path]
            target = destination / "clean" if path == "." else destination.joinpath("clean", *path.split("/"))
            if kind == "dir":
                target.mkdir(mode=row["mode"] or 0o755, parents=False, exist_ok=False)
                continue
            if kind != "file":
                continue
            if not target.parent.is_dir() or target.parent.is_symlink():
                raise BundleError(f"file parent is not a real extracted directory: {member.name!r}")
            source = archive.extractfile(member)
            if source is None:
                raise BundleError(f"cannot read archive member {member.name!r}")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(target, flags, row["mode"] or 0o644)
            h = hashlib.sha256()
            size = 0
            try:
                with os.fdopen(fd, "wb") as out, source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        h.update(block)
                        size += len(block)
                        out.write(block)
                    out.flush()
                if size != row["size"] or h.hexdigest() != row["sha256"]:
                    raise BundleError(f"extracted bytes changed for {member.name!r}")
                os.chmod(target, row["mode"])
            except BaseException:
                try: target.unlink()
                except OSError: pass
                raise
        # Create only descriptor-approved relative symlinks, after all write paths.
        for row in members:
            if row["type"] == "symlink":
                target = destination.joinpath("clean", *row["path"].split("/"))
                if not target.parent.is_dir():
                    raise BundleError(f"symlink parent is not a directory: {row['path']!r}")
                os.symlink(row["target"], target)
        for row in members:
            if row["type"] == "dir" and row["path"] != ".":
                target = destination.joinpath("clean", *row["path"].split("/"))
                os.chmod(target, row["mode"])


def _inspect_elf(root: Path, members: list[dict], descriptor: dict) -> dict:
    readelf = shutil.which("readelf")
    if not readelf:
        return {"status": "unverified", "reason": "readelf not installed"}
    paths = [e["path"] for e in members if e["type"] == "file"]
    elf_rows, bundled_sonames, all_needed = [], set(), set()
    for rel in paths:
        path = root / "clean" / rel
        try:
            with path.open("rb") as stream:
                if stream.read(4) != b"\x7fELF":
                    continue
        except OSError as exc:
            raise BundleError(f"cannot read extracted file {rel}: {exc}") from exc
        result = subprocess.run([readelf, "-W", "-h", "-d", str(path)], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise BundleError(f"readelf rejected ELF file {rel}: {result.stderr.strip()}")
        machine = next((line.split(":", 1)[1].strip() for line in result.stdout.splitlines() if "Machine:" in line), "unknown")
        if machine not in ("Advanced Micro Devices X86-64",):
            raise BundleError(f"non-x86_64 ELF in x86_64 runtime: {rel} ({machine})")
        soname = next((m.group(1) for line in result.stdout.splitlines() if (m := re.search(r"\(SONAME\).*\[([^]]+)\]", line))), None)
        needed = [m.group(1) for line in result.stdout.splitlines() if (m := re.search(r"\(NEEDED\).*\[([^]]+)\]", line))]
        bundled_sonames.add(Path(rel).name)
        if soname:
            bundled_sonames.add(soname)
        all_needed.update(needed)
        elf_rows.append({"path": rel, "machine": machine, "soname": soname, "needed": needed})
    # $ORIGIN-relative dependencies are private path references and must resolve.
    private_needed = set()
    for row in elf_rows:
        for needed in row["needed"]:
            if needed.startswith("$ORIGIN/"):
                parent = PurePosixPath(row["path"]).parent
                target = posixpath.normpath(posixpath.join(str(parent), needed.replace("$ORIGIN/", "", 1)))
                if not (root / "clean" / target).is_file():
                    raise BundleError(f"unresolved private ELF dependency: {row['path']} -> {needed}")
                private_needed.add(needed)
    external = sorted(n for n in all_needed if n not in bundled_sonames and not n.startswith("$ORIGIN/"))
    allow = set(descriptor.get("elf_system_sonames", []))
    if not set(external).issubset(allow):
        raise BundleError(f"ELF dependencies absent from descriptor allowlist: {sorted(set(external)-allow)}")
    return {"status": "pass", "inspection": "readelf static DT_NEEDED/SONAME and private $ORIGIN closure",
            "elf_count": len(elf_rows), "elf_files": elf_rows,
            "bundled_sonames": sorted(bundled_sonames), "private_origin_dependencies": sorted(private_needed),
            "external_system_dependencies": external,
            "target_resolution": "static allowlist only; installer probes run on selected host/QEMU CPU; re-check against final Live userspace root"}


def _file_manifest(root: Path) -> list[dict]:
    rows = []
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            rows.append({"path": rel, "type": "symlink", "target": os.readlink(path)})
        elif path.is_file():
            rows.append({"path": rel, "type": "file", "bytes": path.stat().st_size, "sha256": sha256_file(path)})
        elif path.is_dir():
            rows.append({"path": rel, "type": "dir"})
        else:
            raise BundleError(f"unexpected extracted filesystem object: {rel}")
    return rows


def _install(bundle: Path, descriptor_path: Path, lock: Path, output: Path, qemu: Path | None = None) -> dict:
    if output.exists() or output.is_symlink():
        raise BundleError(f"output must be fresh and nonexistent: {output}")
    raw_parent = output.absolute().parent
    if raw_parent != raw_parent.resolve(strict=True):
        raise BundleError("output parent must not contain symlink components")
    parent = raw_parent.resolve(strict=True)
    output = parent / output.name
    if output.name in ("", ".", ".."):
        raise BundleError("invalid output root")
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.stage-", dir=parent))
    payload = stage / "root"
    try:
        for source, label in ((bundle, "bundle"), (descriptor_path, "descriptor"), (lock, "dependency lock")):
            if source.is_symlink() or not source.is_file():
                raise BundleError(f"{label} must be a regular non-symlink file")
        if not 0 < bundle.stat().st_size <= MAX_ARCHIVE_BYTES:
            raise BundleError("bundle size outside configured limits")
        if descriptor_path.stat().st_size > 16_000_000 or lock.stat().st_size > 16_000_000:
            raise BundleError("descriptor or dependency lock exceeds configured size limit")
        if qemu is not None and (qemu.is_symlink() or not qemu.is_file() or not os.access(qemu, os.X_OK)):
            raise BundleError("QEMU launcher must be an executable regular non-symlink file")
        pinned_bundle = stage / "verified-bundle.tar.gz"
        pinned_descriptor = stage / "descriptor.json"
        pinned_lock = stage / "requirements-ai.lock"
        shutil.copyfile(bundle, pinned_bundle)
        shutil.copyfile(descriptor_path, pinned_descriptor)
        shutil.copyfile(lock, pinned_lock)
        desc, entries = _verify_descriptor(pinned_bundle, pinned_descriptor, pinned_lock)
        _extract(pinned_bundle, entries, payload)
        elf = _inspect_elf(payload, entries, desc)
        if elf["status"] != "pass":
            raise BundleError("ELF closure inspection unavailable")
        (payload / "bin").mkdir(mode=0o755)
        wrapper_path = payload / "bin/xtos-ai-python"
        wrapper_path.write_bytes(WRAPPER)
        os.chmod(wrapper_path, 0o755)
        private_python = payload / "clean/python/bin/python3.11"
        if not private_python.is_file() or not os.access(private_python, os.X_OK):
            raise BundleError("private CPython entrypoint missing or not executable")
        prefix = [str(qemu.resolve(strict=True)), "-cpu", "max"] if qemu else []
        version = subprocess.run(prefix + [str(private_python), "-I", "-B", "-c", "import sys; print('.'.join(map(str,sys.version_info[:3]))); print(sys.prefix)"],
                                 cwd=payload, env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"},
                                 capture_output=True, text=True, timeout=30)
        version_lines = version.stdout.splitlines()
        expected_prefix = str(payload / "clean/python")
        if (version.returncode or len(version_lines) != 2 or version_lines[0].strip() != REQUIRED_RUNTIME["CPython"]
                or version_lines[1].strip() != expected_prefix or version.stderr):
            raise BundleError(f"private CPython version/relocation check failed: rc={version.returncode}, stdout={version.stdout!r}, stderr={version.stderr!r}")
        runtime_probe = subprocess.run(prefix + [str(private_python), "-I", "-B", "-c", "import sys,numpy,onnxruntime; print('|'.join((sys.version.split()[0],numpy.__version__,onnxruntime.__version__,sys.executable)))"],
                                       cwd=payload, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "/usr/lib/python3/dist-packages", "PYTHONNOUSERSITE": "1", "PYTHONHOME": "/invalid/system/python"},
                                       capture_output=True, text=True, timeout=90)
        if runtime_probe.returncode or runtime_probe.stderr:
            raise BundleError(f"private Python/ORT import probe failed: rc={runtime_probe.returncode}, stderr={runtime_probe.stderr!r}")
        fields = runtime_probe.stdout.strip().split("|")
        if fields[:3] != ["3.11.15", "1.26.4", "1.23.2"] or not fields[3].startswith(str(private_python.parent)):
            raise BundleError(f"private Python identity mismatch: {runtime_probe.stdout!r}")
        provenance = dict(desc["provenance"])
        manifest = {
            "schema": "xtos-ai-private-runtime-manifest-v1",
            "status": "installed_verified_artifact",
            "architecture": desc["architecture"],
            "runtime": desc["runtime"],
            "bundle": desc["bundle"],
            "descriptor_sha256": sha256_file(pinned_descriptor),
            "lock": desc["lock"],
            "provenance": provenance,
            "license_sources": desc["package_licenses"],
            "elf_closure": elf,
            "execution_mode": "qemu-x86_64 -cpu max functional emulation" if qemu else "native host CPU",
            "private_python_probe": {"identity": "|".join(fields[:3] + ["clean/python/bin/python3.11"]), "stderr_bytes": len(runtime_probe.stderr.encode()),
                                     "isolated_mode": True, "system_python_fallback": False},
            "files": _file_manifest(payload),
            "files_scope": "all extracted and generated runtime files except this self-referential manifest",
            "livecopy_input": str(output),
        }
        (payload / "provision-manifest.json").write_bytes(canonical_json(manifest))
        # fsync the manifest and make the directory tree durable before publish.
        with (payload / "provision-manifest.json").open("rb") as stream:
            os.fsync(stream.fileno())
        if output.exists() or output.is_symlink():
            raise BundleError(f"output appeared during install: {output}")
        os.chmod(payload, 0o755)
        _publish_noreplace(payload, output)
        return {"status": manifest["status"], "output_root": str(output),
                "bundle_sha256": desc["bundle"]["sha256"], "descriptor_sha256": manifest["descriptor_sha256"],
                "lock_sha256": desc["lock"]["sha256"], "files": len(manifest["files"]),
                "runtime": desc["runtime"], "elf_count": elf["elf_count"],
                "livecopy_input": str(output)}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    describe = sub.add_parser("describe", help="create a full inventory descriptor from verified input evidence")
    describe.add_argument("--bundle", type=Path, required=True)
    describe.add_argument("--lock", type=Path, required=True)
    describe.add_argument("--report", type=Path, required=True)
    describe.add_argument("--offline-lock-evidence", type=Path, required=True)
    describe.add_argument("--verified-result", type=Path, required=True)
    describe.add_argument("--output", type=Path, required=True)
    install = sub.add_parser("install", help="verify and safely extract into a fresh output root")
    install.add_argument("--bundle", type=Path, required=True)
    install.add_argument("--descriptor", type=Path, required=True)
    install.add_argument("--lock", type=Path, required=True)
    install.add_argument("--output-root", type=Path, required=True)
    install.add_argument("--qemu-x86-64", type=Path, help="explicit qemu-x86_64 launcher for a host without AVX; functional evidence only")
    args = parser.parse_args(argv)
    try:
        if args.command == "describe":
            doc = _build_descriptor(args.bundle, args.lock, args.report, args.offline_lock_evidence, args.verified_result)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            if args.output.exists() or args.output.is_symlink():
                raise BundleError(f"descriptor output must be nonexistent: {args.output}")
            args.output.write_bytes(canonical_json(doc))
            print(json.dumps({"status": "described", "descriptor": str(args.output),
                              "descriptor_sha256": sha256_file(args.output), "members": len(doc["members"]),
                              "bundle_sha256": doc["bundle"]["sha256"], "lock_sha256": doc["lock"]["sha256"]}, sort_keys=True))
        else:
            result = _install(args.bundle, args.descriptor, args.lock, args.output_root, args.qemu_x86_64)
            print(json.dumps(result, sort_keys=True))
        return 0
    except (BundleError, OSError, subprocess.SubprocessError) as exc:
        print(f"provision_ai_runtime: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
