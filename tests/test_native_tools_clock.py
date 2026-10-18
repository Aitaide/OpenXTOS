#!/usr/bin/env python3
"""clock tool tests: honest synchronization semantics with the resident
chronyd daemon. synchronized=true is only ever reported when the resident
daemon's `chronyc tracking` output proves continuous tracking; a one-shot
sync is history, never a claim."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "tools" / "xtos-native" / "clock" / "clock.c"


def main() -> int:
    if sys.platform != "linux":
        print("skip: linux only")
        return 0
    if os.geteuid() != 0:
        print("skip: clock sync path needs root")
        return 0
    if subprocess.run(["gcc", "--version"], capture_output=True).returncode != 0:
        print("skip: gcc unavailable")
        return 0
    failures = []
    with tempfile.TemporaryDirectory(prefix="xtos-clock-test-") as td:
        td = Path(td)
        binary = td / "clock"
        subprocess.run(["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-static",
                        "-I", str(ROOT / "tools" / "xtos-native" / "common"),
                        "-o", str(binary), str(SRC)], check=True)
        pidfile = td / "chronyd.pid"
        tracking_stub = td / "chronyc-tracking"
        unsync_stub = td / "chronyc-unsync"

        def status(pidfile_path, chronyc_path, extra=None):
            env = dict(os.environ)
            env["XTOS_CLOCK_PIDFILE"] = str(pidfile_path)
            env["XTOS_CHRONYC_PATH"] = str(chronyc_path)
            if extra:
                env.update(extra)
            r = subprocess.run([str(binary), "status", "--json"], capture_output=True,
                               text=True, env=env, timeout=60)
            return r, json.loads(r.stdout)

        # 1. no resident daemon: never synchronized, old one-shot history kept
        r, data = status(td / "nonexistent.pid", tracking_stub)
        if data["synchronized"] is not False or data["source"] != "unsynchronized":
            failures.append(f"1 no-daemon status wrong: {data}")
        else:
            print("  PASS no-daemon status")

        # 2. resident daemon actively tracking: synchronized true, honest source
        tracking_stub.write_text(
            "#!/bin/sh\n"
            "echo 'Reference ID    : C0A80101 (ntp.example.test)'\n"
            "echo 'Stratum         : 2'\n"
            "echo 'System time     : 0.000001 seconds fast of NTP time'\n")
        tracking_stub.chmod(0o755)
        pidfile.write_text(f"{os.getpid()}\n")
        r, data = status(pidfile, tracking_stub)
        if data["synchronized"] is not True or data["source"] != "chronyd-resident":
            failures.append(f"2 resident-tracking status wrong: {data}")
        elif "warning" in data:
            failures.append("2 warning must be absent when genuinely synchronized")
        else:
            print("  PASS resident tracking")

        # 3. resident daemon present but NOT tracking: honest false
        unsync_stub.write_text("#!/bin/sh\necho '501 Not synchronised'\n")
        unsync_stub.chmod(0o755)
        r, data = status(pidfile, unsync_stub)
        if data["synchronized"] is not False or data["source"] != "chronyd-resident" \
                or "warning" not in data:
            failures.append(f"3 resident-unsynced status wrong: {data}")
        else:
            print("  PASS resident not tracking")

        # 4. missing chronyc binary (or non-executable): resident, not tracking
        r, data = status(pidfile, td / "chronyc-missing")
        if data["synchronized"] is not False or data["resident_pid"] != os.getpid():
            failures.append(f"4 chronyc-missing status wrong: {data}")
        else:
            print("  PASS chronyc missing")

        # 5. stale pidfile (dead pid): treated as no daemon at all
        pidfile.write_text("999999999\n")
        r, data = status(pidfile, tracking_stub)
        if data["synchronized"] is not False or "resident_pid" in data:
            failures.append(f"5 stale pidfile treated as resident: {data}")
        else:
            print("  PASS stale pidfile")

        # 6. sync defers to the resident daemon instead of fighting it
        pidfile.write_text(f"{os.getpid()}\n")
        env = dict(os.environ)
        env["XTOS_CLOCK_PIDFILE"] = str(pidfile)
        r = subprocess.run([str(binary), "sync", "pool.ntp.org"], capture_output=True,
                           text=True, env=env, timeout=60)
        if r.returncode != 0 or "resident-chronyd-active" not in r.stdout:
            failures.append(f"6 sync did not defer: rc={r.returncode} out={r.stdout!r}")
        else:
            print("  PASS sync defers to daemon")

        # 7. text output stays plain when piped (xui contract) and says the same
        r = subprocess.run([str(binary), "status"], capture_output=True, text=True, env=env,
                           timeout=60)
        if "\x1b[" in r.stdout or "synchronized=false" not in r.stdout \
                or "source=chronyd-resident" not in r.stdout:
            failures.append(f"7 piped text status wrong: {r.stdout!r}")
        else:
            print("  PASS piped text status")

    if failures:
        print(f"FAIL: {len(failures)}")
        for f in failures:
            print("  " + f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
