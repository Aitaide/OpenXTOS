#!/usr/bin/python3
"""Read-only smoke commands executed by the target interpreter inside chroot."""
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    authority = Path('/var/lib/xtos/action-gate')
    if authority.exists() or authority.is_symlink():
        raise RuntimeError('authority store must be absent in smoke root')
    before = sorted(str(p) for p in Path.home().iterdir())
    commands = (
        ('native_missing_loader', ['/usr/bin/xr-probe', '--loader', '/__xtos_live_xr_missing_loader__.so']),
        ('eye_missing_loader', ['/usr/bin/xr-eye-probe', '--loader', '/__xtos_live_xr_missing_loader__.so', '--accept-eye-data']),
        ('launcher_argv', ['/usr/bin/xr-feedback-demo', '--not-a-demo']),
        ('demo', ['/usr/bin/xr-feedback-demo', '--demo']),
    )
    rows = {}
    for name, argv in commands:
        result = subprocess.run(argv, input='', capture_output=True, text=True, timeout=15)
        if len(result.stdout) + len(result.stderr) > 16384:
            raise RuntimeError('unbounded XR smoke output')
        rows[name] = dict(argv=argv, exit=result.returncode,
                          stdout=result.stdout, stderr=result.stderr)
    if authority.exists() or authority.is_symlink() or before != sorted(str(p) for p in Path.home().iterdir()):
        raise RuntimeError('XR smoke left authority state')
    print(json.dumps(dict(python=dict(executable=sys.executable, implementation=sys.implementation.name,
                                     version=list(sys.version_info[:3])), commands=rows,
                          authority_store_created=False), sort_keys=True))


if __name__ == '__main__':
    main()
