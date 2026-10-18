#!/usr/bin/env python3
"""Read-only rt smoke tests; tune is exercised only by the fixture suite."""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "tools" / "xtos-native" / "rt" / "rt.c"


def main() -> int:
    if sys.platform != "linux":
        print("skip: linux only")
        return 0
    with tempfile.TemporaryDirectory() as td:
        binary = Path(td) / "rt"
        subprocess.run(["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-static", "-I", str(ROOT / "tools" / "xtos-native" / "common"),
                        "-o", str(binary), str(SRC)], check=True)

        failures = []

        def run(*args):
            return subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=30)

        # status works and reports kernel state honestly
        r = run("status")
        if r.returncode != 0 or "CPUs:" not in r.stdout or "RT budget:" not in r.stdout:
            failures.append(f"status: rc={r.returncode} {r.stdout!r}")
        print(f"  PASS status (realtime={'active' if 'PREEMPT_RT active' in r.stdout else 'not (honest)'})")

        # status --json is valid JSON
        r = run("--json", "status")
        import json
        try:
            d = json.loads(r.stdout)
            for k in ("realtime_kernel", "online_cpus", "rt_runtime_us", "rt_period_us"):
                if k not in d:
                    failures.append(f"json missing {k}")
            print("  PASS status --json")
        except Exception as e:
            failures.append(f"json parse: {e}")
            print("  FAIL status --json")


        # unknown command → usage
        r = run("bogus")
        if r.returncode != 2:
            failures.append(f"unknown cmd rc={r.returncode}")
        print("  PASS unknown command usage")

    if failures:
        print(f"FAIL: {len(failures)}")
        for f in failures:
            print("  " + f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
