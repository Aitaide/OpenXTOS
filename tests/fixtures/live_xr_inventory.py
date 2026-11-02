#!/usr/bin/python3
"""Read installed bytes as an ordinary guest; never execute device input."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

MANIFEST = 'usr/share/xtos/xr-sources.json'


def read_regular(root, relative):
    name = Path(relative)
    if (type(relative) is not str or not relative or name.is_absolute()
            or '..' in name.parts or str(name) != relative):
        raise ValueError('invalid installed XR path')
    path = root / name
    for ancestor in path.parents:
        if ancestor == root:
            break
        if ancestor.is_symlink() or not ancestor.is_dir():
            raise ValueError('unsafe installed XR ancestor')
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024:
            raise ValueError('invalid installed XR regular file')
        chunks = []
        size = 0
        while chunk := os.read(fd, 65536):
            size += len(chunk)
            if size > 4 * 1024 * 1024:
                raise ValueError('installed XR file exceeds bound')
            chunks.append(chunk)
        data = b''.join(chunks)
        return data, {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
                      'mode': stat.S_IMODE(info.st_mode)}
    finally:
        os.close(fd)


def inventory(root=Path('/')):
    root = Path(root).resolve(strict=True)
    data, manifest_row = read_regular(root, MANIFEST)
    try:
        record = json.loads(data)
    except (UnicodeError, ValueError) as exc:
        raise ValueError('invalid installed XR manifest') from exc
    if (not isinstance(record, dict) or record.get('schema') != 'xtos.xr.source-inventory.v1'
            or record.get('status') != 'installed_source_only'
            or record.get('physical_acceptance') is not False
            or record.get('vendor_runtime_bundled') is not False):
        raise ValueError('installed XR manifest boundary mismatch')
    for key in ('payload_hashes', 'source_hashes'):
        values = record.get(key)
        if not isinstance(values, dict) or not 1 <= len(values) <= 64:
            raise ValueError('invalid installed XR inventory map')
        for name, digest in values.items():
            if type(name) is not str or type(digest) is not str or re.fullmatch('[a-f0-9]{64}', digest) is None:
                raise ValueError('invalid installed XR binding')
    payload = {MANIFEST: manifest_row}
    for name, digest in record['payload_hashes'].items():
        if name == MANIFEST:
            raise ValueError('installed XR manifest cannot hash itself')
        _, row = read_regular(root, name)
        if row['sha256'] != digest:
            raise ValueError('installed XR payload hash mismatch: ' + name)
        payload[name] = row
    return {'payload_inventory': payload, 'source_hashes': record['source_hashes'],
            'physical_acceptance': False}


if __name__ == '__main__':
    print(json.dumps(inventory(), sort_keys=True))
