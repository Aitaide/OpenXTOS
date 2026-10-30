#!/usr/bin/env python3
"""Install only the source modules for the explicitly selected serial API."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

SOURCES = {
    'cyton_protocol.py': 'tools/xtos-devices/cyton_protocol.py',
    'cyton_serial_plugin.py': 'tools/xtos-devices/cyton_serial_plugin.py',
    'ganglion_protocol.py': 'tools/xtos-devices/ganglion_protocol.py',
    'ganglion_plugin.py': 'tools/xtos-devices/ganglion_plugin.py',
    'serial_transport.py': 'tools/xtos-devices/serial_transport.py',
    'xtos_devices.py': 'tools/xtos-devices/xtos_devices.py',
    'openbci_cyton.py': 'tools/xtos-eeg-pipeline/openbci_cyton.py',
}
DESTINATION = Path('usr/lib/xtos/devices')
RECORD = Path('usr/share/xtos/device-plugin-sources.json')
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAME = _LIBC.renameat2
_RENAME.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
_RENAME.restype = ctypes.c_int


def _publish_noreplace(source, destination):
    if _RENAME(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))


def _hash_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _read_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('device plugin destination member is not a regular file')
        chunks = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        return info.st_mode & 0o7777, b''.join(chunks)
    finally:
        os.close(fd)


def _inventory(source):
    data = {}
    for name, relative in SOURCES.items():
        path = source / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError('maintained serial plugin source missing or not regular: ' + relative)
        blob = path.read_bytes()
        if not blob:
            raise ValueError('maintained serial plugin source is empty: ' + relative)
        data[name] = blob
    return data


def _record(files):
    return {
        'schema': 'xtos-device-plugin-source-inventory-v1',
        'status': 'installed_source_only',
        'destination': str(DESTINATION),
        'source_sha256': {name: _hash_bytes(blob) for name, blob in sorted(files.items())},
        'physical_device_evidence': False,
        'fixture_or_test_sources_installed': False,
        'authority_initialized': False,
    }


def install(stage, source_root):
    stage = Path(stage).absolute()
    source_root = Path(source_root).resolve(strict=True)
    if stage.is_symlink() or not stage.is_dir() or stage.resolve(strict=True) != stage:
        raise ValueError('staging root must be an existing real directory')
    for relative in (DESTINATION, RECORD):
        current = stage
        for part in relative.parts[:-1]:
            current = current/part
            if os.path.lexists(current) and (current.is_symlink() or not current.is_dir()):
                raise ValueError('device plugin destination ancestor must be a real directory')
    files = _inventory(source_root)
    expected = _record(files)
    package = stage / DESTINATION
    record_path = stage / RECORD
    package_exists = os.path.lexists(package)
    record_exists = os.path.lexists(record_path)
    if package_exists:
        if package.is_symlink() or not package.is_dir():
            raise ValueError('device plugin package path is not a real directory')
        if {path.name for path in package.iterdir()} != set(files):
            raise ValueError('device plugin package member set differs')
        for name, blob in files.items():
            try:
                mode, actual = _read_regular(package/name)
            except (OSError, ValueError) as exc:
                raise ValueError('device plugin member mismatch: '+name) from exc
            if actual != blob or mode != 0o644:
                raise ValueError('device plugin member bytes or mode differ: '+name)
    if record_exists:
        try:
            mode, raw = _read_regular(record_path)
            actual_record = json.loads(raw)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError('device plugin source record is invalid') from exc
        if mode != 0o644 or actual_record != expected:
            raise ValueError('device plugin source record differs')
    if package_exists and record_exists:
        return expected

    # Stage and revalidate all bytes before any public path appears. Retain a
    # published package on later failure; exact-byte retry is safe and preserves
    # any caller replacement instead of attempting a racy rollback.
    with tempfile.TemporaryDirectory(prefix='.xtos-devices-', dir=stage) as temporary:
        temporary = Path(temporary)
        staged_package = temporary / DESTINATION
        staged_package.mkdir(parents=True)
        for name, blob in files.items():
            target = staged_package/name
            target.write_bytes(blob)
            target.chmod(0o644)
            if _hash_bytes(target.read_bytes()) != expected['source_sha256'][name]:
                raise ValueError('staged device plugin bytes differ: '+name)
        staged_record = temporary / RECORD
        staged_record.parent.mkdir(parents=True)
        staged_record.write_text(json.dumps(expected, sort_keys=True, indent=2)+'\n')
        staged_record.chmod(0o644)
        if _inventory(source_root) != files:
            raise ValueError('maintained device plugin sources changed while staging')
        # Validate both destinations above, then publish absent complete paths
        # atomically. Existing exact paths are reused without rewriting them.
        if not package_exists:
            package.parent.mkdir(parents=True, exist_ok=True)
            _publish_noreplace(staged_package, package)
        if not record_exists:
            record_path.parent.mkdir(parents=True, exist_ok=True)
            _publish_noreplace(staged_record, record_path)
    return expected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rootfs', required=True, type=Path)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    report = install(args.rootfs, args.source_root)
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
