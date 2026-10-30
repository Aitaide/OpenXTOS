#!/usr/bin/env python3
"""Install hash-bound source-only EEG loop and its fixed CLI entrypoint."""
import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAME_NOREPLACE = 1
_AT_FDCWD = -100
_renameat2 = _LIBC.renameat2
_renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                       ctypes.c_char_p, ctypes.c_uint)
_renameat2.restype = ctypes.c_int


def _rename_noreplace(source, destination):
    """Atomically publish an absent path without replacing a caller path."""
    source, destination = os.fsencode(source), os.fsencode(destination)
    result = _renameat2(_AT_FDCWD, source, _AT_FDCWD, destination, _RENAME_NOREPLACE)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), os.fsdecode(destination))


PACKAGES = {
    'xtos_action_gate': ('__init__.py', '__main__.py', 'schema.py', 'store.py'),
    'xtos_ai_runtime': ('__init__.py', '__main__.py', 'client.py', 'protocol.py', 'server.py', 'trust.py', 'worker.py'),
    'xtos_eeg_loop': ('__init__.py', 'controller.py', 'feature_binding.py', 'inference.py', 'lifecycle.py', 'simulation.py', 'stream.py'),
}
PACKAGE_DIRS = {'xtos_action_gate': 'xtos_action_gate',
                'xtos_ai_runtime': 'xtos-ai-runtime/xtos_ai_runtime',
                'xtos_eeg_loop': 'xtos_eeg_loop'}


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file_snapshot(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('EEG loop destination member is not a regular file: ' + str(path))
        chunks = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        return info.st_mode & 0o7777, b''.join(chunks)
    finally:
        os.close(fd)


def _regular_file_sha(path):
    return hashlib.sha256(_regular_file_snapshot(path)[1]).hexdigest()


def regular_source(root, package, name):
    source = root / 'tools' / PACKAGE_DIRS[package] / name
    if source.is_symlink() or not source.is_file():
        raise ValueError('maintained source missing or not regular: ' + str(source))
    return source


def inventory(root):
    rows = {}
    for package, names in PACKAGES.items():
        for name in names:
            source = regular_source(root, package, name)
            rows[f'usr/lib/xtos/{package}/{name}'] = sha(source)
    return rows


def _write(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(data)
    path.chmod(mode)


def install(stage, repo):
    stage, repo = Path(stage).absolute(), Path(repo).resolve(strict=True)
    if stage.is_symlink() or not stage.is_dir() or stage.resolve(strict=True) != stage:
        raise ValueError('staging root must be a real directory')
    expected = inventory(repo)
    existing_packages = set()
    for package, names in PACKAGES.items():
        target = stage / f'usr/lib/xtos/{package}'
        if not os.path.lexists(target):
            continue
        if target.is_symlink() or not target.is_dir():
            raise ValueError('EEG loop package destination is not a directory')
        actual = {p.name for p in target.iterdir()}
        if actual != set(names):
            raise ValueError('EEG loop package source mismatch: ' + package)
        for name in names:
            member = target / name
            try:
                member_sha = _regular_file_sha(member)
            except (OSError, ValueError) as exc:
                raise ValueError('EEG loop package member is not regular: ' + str(member)) from exc
            if member_sha != expected[f'usr/lib/xtos/{package}/{name}']:
                raise ValueError('EEG loop package source mismatch: ' + package)
        existing_packages.add(package)
    manifest = {'schema': 'xtos-eeg-loop-source-inventory-v1',
                'status': 'installed_source_only', 'source_hashes': expected,
                'generated_hashes': {},
                'authority': 'fixed TrustedStore path; never initialized by install or entrypoint',
                'fixture_or_test_sources_installed': False}
    with tempfile.TemporaryDirectory(prefix='.xtos-eeg-loop-', dir=stage) as tmp:
        temporary = Path(tmp)
        for relative, digest in expected.items():
            package, name = relative.split('/')[3:5]
            source = regular_source(repo, package, name)
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if sha(source) != digest or sha(target) != digest:
                raise ValueError('EEG loop source changed while staging: ' + relative)
        entry = temporary / 'usr/libexec/xtos/eeg-loop-entry.py'
        _write(entry, b'#!/usr/bin/python3\nfrom pathlib import Path\nimport runpy, sys\nsys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib/xtos"))\nrunpy.run_module("xtos_eeg_loop.stream", run_name="__main__")\n', 0o755)
        launcher = temporary / 'usr/bin/eeg-loop'
        _write(launcher, b'#!/bin/sh\nset -eu\nexec /opt/xtos-ai/bin/xtos-ai-python /usr/libexec/xtos/eeg-loop-entry.py "$@"\n', 0o755)
        manifest['generated_hashes'] = {
            'usr/libexec/xtos/eeg-loop-entry.py': sha(entry),
            'usr/bin/eeg-loop': sha(launcher),
        }
        record = temporary / 'usr/share/xtos/eeg-loop-sources.json'
        _write(record, (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode())
        if inventory(repo) != expected:
            raise ValueError('EEG loop source changed before publication')
        generated = ('usr/libexec/xtos/eeg-loop-entry.py', 'usr/bin/eeg-loop',
                     'usr/share/xtos/eeg-loop-sources.json')
        existing_generated = set()
        for relative in generated:
            source = temporary / relative
            target = stage / relative
            if not os.path.lexists(target):
                continue
            try:
                target_mode, target_bytes = _regular_file_snapshot(target)
            except (OSError, ValueError) as exc:
                raise ValueError('EEG loop generated destination mismatch: ' + relative) from exc
            if (target_mode != source.stat().st_mode & 0o7777 or
                    target_bytes != source.read_bytes()):
                raise ValueError('EEG loop generated destination mismatch: ' + relative)
            existing_generated.add(relative)
        publish = tuple(f'usr/lib/xtos/{package}' for package in PACKAGES
                        if package not in existing_packages)
        publish += tuple(relative for relative in generated if relative not in existing_generated)
        # Published paths are intentionally retained if a later publication fails.
        # A retry can reuse exact bytes, and no rollback can delete caller data.
        for relative in publish:
            source = temporary / relative
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(target):
                raise FileExistsError(errno.EEXIST, 'destination appeared during install', str(target))
            _rename_noreplace(source, target)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rootfs', type=Path, required=True)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        report = install(args.rootfs, args.source_root)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
