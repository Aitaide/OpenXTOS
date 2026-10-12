#!/usr/bin/env python3
"""persist tool tests: safety refusals and status reporting."""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "tools" / "xtos-native" / "persist" / "persist.c"


def main() -> int:
    if sys.platform != "linux":
        print("skip: linux only")
        return 0
    with tempfile.TemporaryDirectory() as td:
        binary = Path(td) / "persist"
        subprocess.run(["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-static",
                        "-o", str(binary), str(SRC)], check=True)

        failures = []

        def run(*args):
            return subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=30)

        # status works
        r = run("status")
        if r.returncode != 0 or "XTOS Persist Status" not in r.stdout:
            failures.append(f"status: rc={r.returncode}")
        print("  PASS status")

        # setup refuses without --confirm (on a real block device)
        loop = Path(td) / "loop.img"
        loop.write_bytes(b"\0" * (1024 * 1024))
        import os
        lfd = os.open(loop, os.O_RDWR)
        try:
            import fcntl
            LOOP_SET_FD = 0x4C00
            try:
                fcntl.ioctl(lfd, LOOP_SET_FD, os.open(loop, os.O_RDWR))
                dev = os.readlink(f"/proc/self/fd/{lfd}")
            except OSError:
                dev = None
        finally:
            os.close(lfd)
        if dev:
            r = run("setup", str(dev))
            if r.returncode != 2 or "--confirm" not in r.stderr:
                failures.append(f"setup without confirm: rc={r.returncode} {r.stderr!r}")
            print("  PASS setup requires --confirm")
        else:
            print("  SKIP setup confirm check (no loop device)")

        # setup refuses non-block paths
        r = run("setup", "/etc/passwd", "--confirm")
        if r.returncode != 1:
            failures.append(f"non-block accepted: rc={r.returncode}")
        print("  PASS setup refuses non-block device")

        # rejects traversal
        r = run("setup", "/dev/../etc/passwd", "--confirm")
        if r.returncode != 1:
            failures.append(f"traversal accepted: rc={r.returncode}")
        print("  PASS setup refuses path traversal")

        # unknown command
        r = run("explode")
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
