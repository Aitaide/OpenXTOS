#!/usr/bin/env python3
"""Install source-only XR applications and exercise their real target closure.

The caller must provide an exclusively owned, trusted staging root. This helper
never provisions dependencies, normalizes a rootfs, or selects a host XR runtime.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import shutil
import subprocess
import sys

MANIFEST = 'usr/share/xtos/xr-sources.json'
# Exact installer source contract; native build provenance cannot be recompiled here.
PYTHON_SOURCE = {'usr/lib/xtos/xtos_xr/' + name: 'tools/xtos-xr/xtos_xr/' + name
                 for name in ('__init__.py', '__main__.py', 'runtime_events.py')}
PYTHON_SOURCE.update({'usr/lib/xtos/xtos_action_gate/' + name: 'tools/xtos_action_gate/' + name
                      for name in ('__init__.py', '__main__.py', 'schema.py', 'store.py')})
NATIVE_SOURCE = (
    'tools/xtos-xr/Makefile', 'tools/xtos-xr/src/runtime_adapter.c',
    'tools/xtos-xr/src/xr_probe.c', 'tools/xtos-xr/include/xtos_xr/runtime_adapter.h',
    'tools/xtos-xr/src/eye_session.c', 'tools/xtos-xr/src/xr_eye_probe.c',
    'tools/xtos-xr/include/xtos_xr/eye_session.h',
    'tools/xtos-xr/vendor/openxr/include/openxr/openxr.h',
    'tools/xtos-xr/vendor/openxr/include/openxr/openxr_platform.h',
    'tools/xtos-xr/vendor/openxr/include/openxr/openxr_platform_defines.h',
)
SOURCE_NAMES = frozenset(PYTHON_SOURCE.values()) | frozenset(NATIVE_SOURCE)
LAUNCHER = b'#!/bin/sh\nset -eu\nexec /usr/bin/python3 /usr/libexec/xtos/xr-feedback-entry.py "$@"\n'
ENTRY = b'#!/usr/bin/python3\nfrom pathlib import Path\nimport runpy,sys\nsys.path.insert(0,str(Path(__file__).resolve().parents[2]/"lib/xtos"))\nrunpy.run_module("xtos_xr",run_name="__main__")\n'
ENV = {'PATH': '/usr/bin:/bin', 'HOME': '/root', 'LC_ALL': 'C',
       'PYTHONDONTWRITEBYTECODE': '1'}


def _file(path):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('XR payload is not a regular file: ' + str(path))
        data = b''
        while chunk := os.read(fd, 1 << 20):
            data += chunk
        return data, stat.S_IMODE(info.st_mode)
    finally:
        os.close(fd)


def _binding(data):
    return dict(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))


def _relative(root, name):
    relative = Path(name)
    if relative.is_absolute() or '..' in relative.parts or str(relative) != name:
        raise ValueError('invalid XR inventory path')
    path = root / relative
    for parent in path.parents:
        if parent == root:
            break
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError('XR inventory ancestor conflict: ' + name)
    return path


def _target_run(root, argv):
    command = [shutil.which('chroot') or '/usr/sbin/chroot', str(root), *argv]
    try:
        result = subprocess.run(command, input='', capture_output=True, text=True,
                                env={**ENV, **({'PYTHONOPTIMIZE': str(sys.flags.optimize)} if sys.flags.optimize else {})}, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('XR target runtime closure failed: ' + str(exc)) from exc
    if len(result.stdout) + len(result.stderr) > 16384:
        raise ValueError('XR target output exceeds bound')
    return dict(argv=argv, exit=result.returncode, stdout=result.stdout, stderr=result.stderr)


def _json_result(row, expected_exit):
    if row['exit'] != expected_exit or row['stderr']:
        raise ValueError('XR target runtime closure failed: ' + json.dumps(row))
    try:
        result = json.loads(row['stdout'])
    except (ValueError, TypeError) as exc:
        raise ValueError('XR target did not return JSON: ' + json.dumps(row)) from exc
    row['result'] = result
    return result


def _root(rootfs):
    root = Path(rootfs).absolute()
    if root.is_symlink() or not root.is_dir() or root.resolve(strict=True) != root or root == Path('/'):
        raise ValueError('XR staging root must be a real owned directory, not host root')
    info = root.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError('unsafe XR staging root permissions')
    return root


def verify(rootfs, repo, *, expected_record=None):
    """Read back installer payload and run target Python, ELF and launcher."""
    root, repo = _root(rootfs), Path(repo).resolve(strict=True)
    record_bytes, mode = _file(_relative(root, MANIFEST))
    record = json.loads(record_bytes)
    if not isinstance(record, dict):
        raise ValueError('XR source-only manifest must be an object')
    if expected_record is not None and record != expected_record:
        raise ValueError('XR installed manifest differs from installer result')
    if (mode != 0o644 or record.get('schema') != 'xtos.xr.source-inventory.v1' or
            record.get('status') != 'installed_source_only' or
            record.get('physical_acceptance') is not False or
            record.get('vendor_runtime_bundled') is not False or
            record.get('python_interpreter') != '/usr/bin/python3'):
        raise ValueError('XR source-only manifest mismatch')
    sources = {}
    source_hashes = record.get('source_hashes')
    if (not isinstance(source_hashes, dict) or set(source_hashes) != SOURCE_NAMES or
            any(not isinstance(name, str) or not isinstance(digest, str) or
                len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)
                for name, digest in source_hashes.items())):
        raise ValueError('XR source inventory mismatch')
    for name, digest in source_hashes.items():
        data, _ = _file(_relative(repo, name))
        row = _binding(data)
        if row['sha256'] != digest:
            raise ValueError('XR source hash mismatch: ' + name)
        sources[name] = row
    expected_payload = {'usr/lib/xtos/xtos_xr/' + name for name in
                        ('__init__.py', '__main__.py', 'runtime_events.py')}
    expected_payload |= {'usr/lib/xtos/xtos_action_gate/' + name for name in
                         ('__init__.py', '__main__.py', 'schema.py', 'store.py')}
    expected_payload |= {'usr/bin/xr-probe', 'usr/bin/xr-eye-probe', 'usr/bin/xr-feedback-demo',
                         'usr/libexec/xtos/xr-feedback-entry.py'}
    payload_hashes = record.get('payload_hashes')
    if (not isinstance(payload_hashes, dict) or set(payload_hashes) != expected_payload or
            any(not isinstance(name, str) or not isinstance(digest, str) or
                len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)
                for name, digest in payload_hashes.items())):
        raise ValueError('XR payload inventory mismatch')
    for package in ('xtos_xr', 'xtos_action_gate'):
        directory = _relative(root, 'usr/lib/xtos/' + package)
        expected_names = {Path(name).name for name in expected_payload
                          if name.startswith('usr/lib/xtos/' + package + '/')}
        if directory.is_symlink() or not directory.is_dir() or {p.name for p in directory.iterdir()} != expected_names:
            raise ValueError('XR package inventory mismatch: ' + package)
    payload = {}
    for name, digest in record['payload_hashes'].items():
        data, mode = _file(_relative(root, name))
        expected_mode = 0o755 if name in ('usr/bin/xr-probe', 'usr/bin/xr-eye-probe', 'usr/bin/xr-feedback-demo',
                                        'usr/libexec/xtos/xr-feedback-entry.py') else 0o644
        row = _binding(data)
        if row['sha256'] != digest or mode != expected_mode:
            raise ValueError('XR payload bytes/mode mismatch: ' + name)
        if name in PYTHON_SOURCE and row != sources[PYTHON_SOURCE[name]]:
            raise ValueError('XR Python source/payload mismatch: ' + name)
        payload[name] = dict(row, mode=mode)
    payload[MANIFEST] = dict(_binding(record_bytes), mode=0o644)
    if _file(root/'usr/bin/xr-feedback-demo')[0] != LAUNCHER or _file(root/'usr/libexec/xtos/xr-feedback-entry.py')[0] != ENTRY:
        raise ValueError('XR launcher interpreter/argv mismatch')
    if not _file(root/'usr/bin/xr-probe')[0].startswith(b'\x7fELF'):
        raise ValueError('XR native probe is not ELF')
    if (root/'var/lib/xtos/action-gate').exists():
        raise ValueError('XR smoke requires absent authority store')
    python = _target_run(root, ['/usr/bin/python3', *(['-O'] if sys.flags.optimize else []), '-I', '-B', '-c',
        'import json,sys; print(json.dumps(dict(executable=sys.executable,implementation=sys.implementation.name,version=list(sys.version_info[:3]),optimization=sys.flags.optimize)))'])
    identity = _json_result(python, 0)
    if identity.get('executable') != '/usr/bin/python3' or identity.get('implementation') != 'cpython':
        raise ValueError('XR target Python identity mismatch')
    native = _target_run(root, ['/usr/bin/xr-probe', '--loader', '/__xtos_live_xr_missing_loader__.so'])
    result = _json_result(native, 3)
    if (not isinstance(result, dict) or result.get('physical_acceptance') is not False
            or result != dict(status='unavailable', safe_action='no_action', physical_acceptance=False)):
        raise ValueError('XR missing-loader boundary mismatch')
    eye_native = _target_run(root, ['/usr/bin/xr-eye-probe', '--loader',
        '/__xtos_live_xr_missing_loader__.so', '--accept-eye-data'])
    eye_result = _json_result(eye_native, 1)
    if (not isinstance(eye_result, dict) or eye_result.get('status') != 'runtime_unavailable'
            or eye_result.get('safe_action') != 'no_action'
            or eye_result.get('physical_acceptance') is not False
            or type(eye_result.get('sample_count')) is not int or eye_result['sample_count'] != 0
            or eye_result.get('samples') != []
            or eye_result.get('source_kind') != 'runtime_unverified'):
        raise ValueError('XR eye missing-loader boundary mismatch')
    argv_check = _target_run(root, ['/usr/bin/xr-feedback-demo', '--not-a-demo'])
    if argv_check['exit'] != 2 or 'usage:' not in argv_check['stderr']:
        raise ValueError('XR target runtime closure failed: launcher did not forward actual argv: ' + json.dumps(argv_check))
    demo = _target_run(root, ['/usr/bin/xr-feedback-demo', '--demo'])
    result = _json_result(demo, 0)
    if result.get('safe_action') != 'no_action' or result.get('physical_acceptance') is not False:
        raise ValueError('XR demo crossed no-action boundary')
    if (root/'var/lib/xtos/action-gate').exists():
        raise ValueError('XR smoke created authority store')
    return dict(schema='xtos.xr.live-install.v1', status='installed_source_only',
                physical_acceptance=False, vendor_runtime_bundled=False,
                source_inventory=sources, payload_inventory=payload,
                runtime_smoke=dict(python=identity, python_check=python,
                    native_missing_loader=native, eye_missing_loader=eye_native,
                    launcher_argv=argv_check, demo=demo,
                    authority_store_created=False))


def install_and_verify(rootfs, repo):
    """Invoke the selected source export's installer, then verify its readback."""
    root = _root(rootfs)
    repo = Path(repo).resolve(strict=True)
    command = [sys.executable, '-I', '-B', '-c',
        'import json,sys;sys.path.insert(0,sys.argv[1]);import install_xr;'
        'print(json.dumps(install_xr.install(sys.argv[2],sys.argv[3]),sort_keys=True))',
        str(repo/'scripts'), str(Path(rootfs).absolute()), str(repo)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=100)
    if result.returncode:
        raise ValueError('XR installer failed: ' + result.stderr[-16384:])
    return verify(rootfs, repo, expected_record=json.loads(result.stdout))
