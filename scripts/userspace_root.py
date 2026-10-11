#!/usr/bin/env python3
"""Open an owned userspace root and its manifest without following symlinks.

Manifest validation establishes the assembler's structural ownership contract;
it is not a sandbox for malicious binaries or proof of package authenticity.
"""
import json
import os
from pathlib import Path
import re
import stat

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
MANIFEST_PARTS = ('usr', 'share', 'xtos', 'userspace-manifest.json')
MAX_MANIFEST_BYTES = 32 * 1024 * 1024


def open_directory(path):
    absolute = Path(os.path.abspath(path))
    if absolute == Path('/'):
        raise ValueError('refusing host root')
    current = os.open('/', DIR_FLAGS)
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, DIR_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def validate_manifest(data):
    if not isinstance(data, dict) or type(data.get('schema_version')) is not int or data['schema_version'] != 1:
        raise ValueError('owned manifest schema_version must be 1')
    arch = data.get('architecture')
    if arch not in ('x86_64', 'arm64', 'riscv64'):
        raise ValueError('owned manifest architecture is invalid')
    if not re.fullmatch(r'[0-9a-f]{64}', str(data.get('lock_sha256', ''))):
        raise ValueError('owned manifest lock digest is invalid')
    lock, packages = data.get('lock'), data.get('packages')
    if not isinstance(lock, dict) or lock.get('schema_version') != 1 or lock.get('architecture') != arch or lock.get('package_format') != 'deb':
        raise ValueError('owned manifest lock metadata is invalid')
    locked = lock.get('packages')
    if not isinstance(locked, list) or not locked or not isinstance(packages, list) or len(packages) != len(locked):
        raise ValueError('owned manifest package inventory is invalid')
    for row, entry in zip(packages, locked):
        if not isinstance(row, dict) or not isinstance(entry, dict) or row.get('package') != entry or not isinstance(row.get('files'), list):
            raise ValueError('owned manifest package records differ from lock')
        if not entry.get('name') or not re.fullmatch(r'[0-9a-f]{64}', str(entry.get('sha256', ''))):
            raise ValueError('owned manifest package identity is invalid')
    return data


def open_owned_root(path):
    """Return a caller-owned root fd and parsed manifest; close fd on failure."""
    root_fd = open_directory(path)
    current = os.dup(root_fd)
    try:
        for component in MANIFEST_PARTS[:-1]:
            child = os.open(component, DIR_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        fd = os.open(MANIFEST_PARTS[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MANIFEST_BYTES:
                raise ValueError('owned manifest must be a bounded regular file')
            data = json.loads(stream.read(MAX_MANIFEST_BYTES + 1))
        return root_fd, validate_manifest(data)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        os.close(root_fd)
        raise ValueError('owned userspace manifest is invalid or inaccessible: ' + str(exc)) from exc
    finally:
        os.close(current)
