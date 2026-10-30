#!/usr/bin/env python3
"""Run a SCHED_OTHER busy competitor and announce verified readiness."""
import argparse
import json
import os
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cpu', type=int, required=True)
    args = parser.parse_args()
    if isinstance(args.cpu, bool) or args.cpu < 0:
        raise SystemExit('CPU must be a non-negative integer')
    try:
        os.sched_setaffinity(0, {args.cpu})
        os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
        affinity = sorted(os.sched_getaffinity(0))
        policy = os.sched_getscheduler(0)
    except OSError as exc:
        raise SystemExit(f'competitor setup failed: {exc}')
    if affinity != [args.cpu] or policy != os.SCHED_OTHER:
        raise SystemExit(f'competitor setup readback mismatch: affinity={affinity!r} policy={policy}')
    print(json.dumps({
        'ready': True,
        'pid': os.getpid(),
        'cpu': args.cpu,
        'affinity': affinity,
        'policy': 'SCHED_OTHER',
    }, sort_keys=True), flush=True)

    value = 1
    monotonic_ns = time.monotonic_ns
    while True:
        end = monotonic_ns() + 50_000_000
        while monotonic_ns() < end:
            value = (value * 2862933555777941757 + 3037000493) & ((1 << 64) - 1)


if __name__ == '__main__':
    main()
