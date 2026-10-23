"""Verified private AI runtime and service-source packaging for Live builds."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile

from provision_ai_runtime import WRAPPER, _publish_noreplace

VERSIONS = {'CPython': '3.11.15', 'onnxruntime': '1.23.2', 'numpy': '1.26.4'}
HEX = re.compile(r'[0-9a-f]{64}')


def _sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate AI manifest key')
        result[key] = value
    return result


def _json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16_000_000:
        raise ValueError('AI manifest must be a bounded regular file')
    value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_object)
    if not isinstance(value, dict):
        raise ValueError('AI manifest must contain an object')
    return value


def _relative(text):
    if (not isinstance(text, str) or not text or '\\' in text or '\x00' in text
            or text.startswith('/') or any(p in ('', '.', '..') for p in text.split('/'))):
        raise ValueError('noncanonical AI inventory path')
    return PurePosixPath(text)


def validate_runtime(root, descriptor, lock_sha256):
    root, descriptor = Path(root).absolute(), Path(descriptor).absolute()
    if root.is_symlink() or not root.is_dir() or root != root.resolve(strict=True):
        raise ValueError('AI runtime root must be a real canonical directory')
    if not isinstance(lock_sha256, str) or not HEX.fullmatch(lock_sha256):
        raise ValueError('invalid AI lock digest')
    manifest_path = root / 'provision-manifest.json'
    manifest, desc = _json(manifest_path), _json(descriptor)
    if (manifest.get('schema') != 'xtos-ai-private-runtime-manifest-v1'
            or manifest.get('status') != 'installed_verified_artifact'
            or desc.get('schema') != 'xtos-ai-runtime-bundle-v1'):
        raise ValueError('unverified AI runtime manifest')
    if manifest.get('descriptor_sha256') != _sha(descriptor):
        raise ValueError('AI descriptor identity mismatch')
    for document in (manifest, desc):
        if document.get('runtime') != VERSIONS or document.get('architecture') != 'x86_64':
            raise ValueError('AI runtime identity mismatch')
        if not isinstance(document.get('lock'), dict) or document['lock'].get('sha256') != lock_sha256:
            raise ValueError('AI dependency lock mismatch')
    if manifest.get('bundle') != desc.get('bundle'):
        raise ValueError('AI bundle identity mismatch')
    entries = manifest.get('files')
    if not isinstance(entries, list) or not 0 < len(entries) <= 25_000:
        raise ValueError('AI inventory missing or oversized')
    expected = {}
    for row in entries:
        if not isinstance(row, dict):
            raise ValueError('invalid AI inventory row')
        rel = str(_relative(row.get('path')))
        if rel in expected or rel == 'provision-manifest.json':
            raise ValueError('duplicate AI inventory member')
        expected[rel] = row
    actual = {p.relative_to(root).as_posix(): p for p in root.rglob('*')}
    actual.pop('provision-manifest.json', None)
    if set(actual) != set(expected):
        raise ValueError('AI runtime has missing or unlisted files')
    for rel, path in actual.items():
        row = expected[rel]
        mode = path.lstat().st_mode
        if path.is_symlink():
            target = row.get('target')
            if (row.get('type') != 'symlink' or not isinstance(target, str)
                    or os.readlink(path) != target or target.startswith('/')
                    or not path.resolve(strict=True).is_relative_to(root)):
                raise ValueError('AI symlink escapes or differs from inventory')
        elif stat.S_ISDIR(mode):
            if row.get('type') != 'dir' or mode & 0o7022:
                raise ValueError('unsafe AI directory')
        elif stat.S_ISREG(mode):
            if (row.get('type') != 'file' or type(row.get('bytes')) is not int
                    or row['bytes'] != path.stat().st_size
                    or not isinstance(row.get('sha256'), str) or not HEX.fullmatch(row['sha256'])
                    or row['sha256'] != _sha(path) or mode & 0o7022):
                raise ValueError('AI file content or mode mismatch')
        else:
            raise ValueError('special file in AI runtime')
    clean_actual = {'.': actual['clean']} if 'clean' in actual else {}
    clean_actual.update({rel[len('clean/'):] : path for rel, path in actual.items()
                         if rel.startswith('clean/')})
    if not clean_actual:
        raise ValueError('AI runtime clean root missing')
    members = desc.get('members')
    if not isinstance(members, list) or not 0 < len(members) <= 25_000:
        raise ValueError('AI descriptor member inventory missing or oversized')
    described = {}
    for row in members:
        if not isinstance(row, dict):
            raise ValueError('invalid AI descriptor member')
        rel = str(_relative(row.get('path'))) if row.get('path') != '.' else '.'
        if rel in described:
            raise ValueError('duplicate AI descriptor member')
        described[rel] = row
    if set(described) != set(clean_actual):
        raise ValueError('installed clean runtime differs from descriptor inventory')
    for rel, path in clean_actual.items():
        row = described[rel]
        mode = path.lstat().st_mode
        if type(row.get('mode')) is not int or row['mode'] != stat.S_IMODE(mode):
            raise ValueError('AI descriptor member mode mismatch')
        if path.is_symlink():
            if row.get('type') != 'symlink' or row.get('target') != os.readlink(path):
                raise ValueError('AI descriptor symlink mismatch')
        elif stat.S_ISDIR(mode):
            if row.get('type') != 'dir':
                raise ValueError('AI descriptor directory mismatch')
        elif stat.S_ISREG(mode):
            if (row.get('type') != 'file' or type(row.get('size')) is not int
                    or row['size'] != path.stat().st_size
                    or not isinstance(row.get('sha256'), str) or not HEX.fullmatch(row['sha256'])
                    or row['sha256'] != _sha(path)):
                raise ValueError('AI descriptor file content mismatch')
        else:
            raise ValueError('special file in AI descriptor tree')
    launcher = root / 'bin/xtos-ai-python'
    if launcher.is_symlink() or not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise ValueError('private AI Python launcher missing')
    if launcher.read_bytes() != WRAPPER:
        raise ValueError('private AI Python launcher is not the pinned wrapper')
    return {'schema': 'xtos-live-ai-input-v1', 'runtime': dict(VERSIONS),
            'provision_manifest_sha256': _sha(manifest_path),
            'descriptor_sha256': _sha(descriptor), 'lock_sha256': lock_sha256,
            'bundle': manifest['bundle'], 'file_count': len(expected),
            'trust_default': 'policy_unavailable_no_action'}


def install_runtime(stage, root, descriptor, repo, lock_sha256):
    stage, root, repo = Path(stage), Path(root), Path(repo)
    report = validate_runtime(root, descriptor, lock_sha256)
    if stage.is_symlink() or not stage.is_dir() or stage.absolute() != stage.resolve(strict=True):
        raise ValueError('Live stage must be a canonical directory')
    sources = repo / 'tools/xtos-ai-runtime/xtos_ai_runtime'
    files = sorted(sources.glob('*.py'))
    required = {'__init__.py', '__main__.py', 'client.py', 'server.py', 'worker.py', 'trust.py'}
    if not required.issubset({p.name for p in files}) or any(p.is_symlink() for p in files):
        raise ValueError('maintained AI service sources missing')
    targets = [stage / 'opt/xtos-ai', stage / 'usr/lib/xtos/xtos_ai_runtime',
               stage / 'usr/libexec/xtos/ai-service', stage / 'usr/libexec/xtos/ai-entry.py',
               stage / 'usr/share/xtos/ai-runtime.json']
    for target in targets:
        if os.path.lexists(target):
            raise ValueError('AI staging destination already exists')
        for parent in target.parents:
            if parent == stage:
                break
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise ValueError('AI staging parent is not a real directory')
    destination, package, launcher, bootstrap, record = targets
    with tempfile.TemporaryDirectory(prefix='.xtos-ai-', dir=stage) as temporary:
        candidate = Path(temporary) / 'runtime'
        shutil.copytree(root, candidate, symlinks=True)
        # Re-read the copied bytes; a copy is not evidence that the source stayed frozen.
        copied = validate_runtime(candidate, descriptor, lock_sha256)
        if copied != report:
            raise ValueError('AI input changed while copying')
        destination.parent.mkdir(parents=True, exist_ok=True)
        _publish_noreplace(candidate, destination)
    package.mkdir(parents=True)
    source_hashes = {}
    for source in files:
        target = package / source.name
        shutil.copyfile(source, target)
        source_hashes[source.name] = _sha(target)
        if source_hashes[source.name] != _sha(source):
            raise ValueError('AI service source changed while copying')
    report['service_sources_sha256'] = source_hashes
    launcher.parent.mkdir(parents=True, exist_ok=True)
    bootstrap.write_text('import runpy, sys\nsys.path.insert(0, "/usr/lib/xtos")\n'
                         'runpy.run_module("xtos_ai_runtime", run_name="__main__")\n', encoding='utf-8')
    launcher.write_text('#!/bin/sh\nset -eu\n'
                        'exec /opt/xtos-ai/bin/xtos-ai-python /usr/libexec/xtos/ai-entry.py "$@"\n', encoding='utf-8')
    launcher.chmod(0o755)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(report, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    return report
