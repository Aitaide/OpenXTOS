#!/usr/bin/env python3
"""Emit bounded failure context without credentials or complete build logs."""
import sys
from pathlib import Path

for arg in sys.argv[1:]:
    root = Path(arg)
    if not root.exists():
        continue
    paths = sorted(root.glob('*.log')) + sorted(root.glob('baremetal-release/evidence/*.log'))
    for path in paths[-9:]:
        lines = path.read_text(errors='replace').splitlines()
        needles = ('error:', 'Error ', 'FAILED', 'Traceback', 'FAIL', 'RuntimeError', 'No such file', 'missing ')
        hits = [i for i, line in enumerate(lines) if any(n in line for n in needles)]
        selected = set()
        for i in hits[-3:]:
            selected.update(range(max(0, i - 2), min(len(lines), i + 6)))
        if not selected:
            selected.update(range(max(0, len(lines) - 12), len(lines)))
        text = '\n'.join(lines[i] for i in sorted(selected))[-3500:]
        text = text.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
        print(f'::warning title=Release diagnostics {path.name}::{text}')
