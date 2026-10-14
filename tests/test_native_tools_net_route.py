#!/usr/bin/env python3
"""net route test: routing table dump works and includes loopback."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NET = ROOT / "tools" / "xtos-native" / "net" / "net.c"


def main() -> int:
    if sys.platform != "linux":
        print("skip: linux only")
        return 0
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "net"
        subprocess.run(
            ["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-static", "-I", str(ROOT / "tools" / "xtos-native" / "common"), "-o", str(out), str(NET)],
            check=True,
        )
        r = subprocess.run([str(out), "route"], capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            print(f"FAIL: rc={r.returncode} {r.stderr[:200]}")
            return 1
        if "DESTINATION" not in r.stdout:
            print("FAIL: no header")
            return 1
        if "127.0.0.0" not in r.stdout:
            print("FAIL: loopback route missing")
            return 1
        lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
        print(f"  PASS route table: {len(lines) - 1} routes")
        print("ALL PASS")
        return 0


if __name__ == "__main__":
    sys.exit(main())
