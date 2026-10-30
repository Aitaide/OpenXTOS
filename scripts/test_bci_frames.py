#!/usr/bin/env python3
"""Run the maintained frame suites with fresh per-attempt evidence paths."""
from pathlib import Path
import importlib.util
import json
import os
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
SUITES = (
    'test_bci_frame_uapi.py', 'test_bci_frame_ring.py',
    'test_bci_frame_ring_runner.py', 'test_bci_frame_core.py',
    'test_bci_frame_snapshot_kernel.py', 'test_bci_frame_sdk.py',
    'test_bci_frame_install.py', 'test_bci_frame_guest_sources.py',
    'test_bci_frame_integration.py',
)


def main():
    out = ROOT/'.build/xtos-roadmap-v1/p2-maintained'/uuid.uuid4().hex
    out.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(ROOT/'tests'))
    suite = unittest.TestSuite()
    inventory = []
    for name in SUITES:
        path = ROOT/'tests'/name
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[path.stem] = module
        spec.loader.exec_module(module)
        # Only these two older suites have fixed output names. Do not overwrite
        # the original component reports or binaries during routine maintenance.
        if name in ('test_bci_frame_ring.py', 'test_bci_frame_sdk.py'):
            module.EVIDENCE = out/path.stem
        selected = unittest.defaultTestLoader.loadTestsFromModule(module)
        pending = [selected]
        while pending:
            item = pending.pop()
            if isinstance(item, unittest.TestSuite):
                pending.extend(reversed(list(item)))
            else:
                inventory.append(item.id())
        suite.addTests(selected)
    if not inventory or len(set(inventory)) != len(inventory):
        raise RuntimeError('frame suite inventory is empty or duplicated')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = result.wasSuccessful() and not result.skipped and result.testsRun == len(inventory)
    report = {'status': 'PASS' if passed else 'FAIL', 'method_ids': inventory,
              'methods_run': result.testsRun, 'errors': len(result.errors),
              'failures': len(result.failures), 'skips': len(result.skipped),
              'scope': 'maintained CPU/source/install fixtures only; no kernel or guest execution',
              'physical_evidence': False, 'stage_acceptance': False}
    with (out/'report.json').open('x') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
    print('BCI_FRAME_MAINTENANCE_REPORT='+str(out/'report.json'), flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
