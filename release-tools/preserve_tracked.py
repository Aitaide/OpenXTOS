#!/usr/bin/env python3
"""Preserve the exact tagged SDK test removed by inherited clean targets."""
import argparse
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import tempfile

PATHS = ('sdk/libxtos-bci/tests/test_samples',)


def sha(data):
    return hashlib.sha256(data).hexdigest()


@contextmanager
def preserved(root):
    root = Path(root).resolve()
    saved = []
    for name in PATHS:
        path = root / name
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError('expected regular tracked file: ' + name)
        current = path.read_bytes()
        committed = subprocess.check_output(['git', '-C', str(root), 'show', 'HEAD:' + name])
        if current != committed:
            raise RuntimeError('preexisting tracked-file change: ' + name)
        saved.append((path, current, stat.S_IMODE(info.st_mode)))
    try:
        yield
    finally:
        errors = []
        for path, content, mode in saved:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(prefix='.restore-', dir=path.parent, delete=False) as f:
                    temporary = Path(f.name)
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                temporary.chmod(mode)
                os.replace(temporary, path)
                if sha(path.read_bytes()) != sha(content) or stat.S_IMODE(path.stat().st_mode) != mode:
                    raise RuntimeError('restoration readback mismatch')
            except BaseException as error:
                errors.append(f'{path}: {error}')
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        if errors:
            raise RuntimeError('tracked-file restoration failed: ' + '; '.join(errors))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('command', nargs=argparse.REMAINDER)
    args = p.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        p.error('command is required')
    root = args.source.resolve()
    with preserved(root):
        subprocess.run(command, cwd=root, check=True)
    status = subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain', '--untracked-files=all'], text=True)
    if status:
        raise RuntimeError('source changed during maintenance: ' + status)


if __name__ == '__main__':
    main()
