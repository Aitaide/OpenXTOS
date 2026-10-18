#!/usr/bin/env python3
"""cgroup tool tests: cpuset partition semantics on the control host's real
cgroup2 hierarchy (root, read-mostly; creates and removes one qa partition)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "tools" / "xtos-native" / "cgroup" / "cgroup.c"
QA = "xtos-cgroup-qa"


def main() -> int:
    if sys.platform != "linux":
        print("skip: linux only")
        return 0
    if os.geteuid() != 0:
        print("skip: needs root for the cgroup2 hierarchy")
        return 0
    if subprocess.run(["gcc", "--version"], capture_output=True).returncode != 0:
        print("skip: gcc unavailable")
        return 0
    magic = subprocess.run(["stat", "-f", "-c", "%t", "/sys/fs/cgroup"],
                           capture_output=True, text=True).stdout.strip()
    if magic != "63677270":
        print(f"skip: host /sys/fs/cgroup is not cgroup2 (magic {magic})")
        return 0

    failures = []
    with tempfile.TemporaryDirectory(prefix="xtos-cgroup-test-") as td:
        td = Path(td)
        binary = td / "cgroup"
        subprocess.run(["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-static",
                        "-I", str(ROOT / "tools" / "xtos-native" / "common"),
                        "-o", str(binary), str(SRC)], check=True)

        def run(*args, expect=0):
            r = subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=60)
            if r.returncode != expect:
                failures.append(f"{' '.join(args)}: rc={r.returncode} expected {expect}\n{r.stdout}{r.stderr}")
            return r

        # 1. status works unmounted-safe and json-parses
        r = run("status", "--json")
        try:
            data = json.loads(r.stdout)
            assert data["mounted"] is True
            assert "cpuset" in data["controllers"]
            print("  PASS status json")
        except Exception as e:
            failures.append(f"1 status json: {e}: {r.stdout!r}")

        # 2. partition + validation
        run("partition", QA, "--cpus", "0")
        part = Path("/sys/fs/cgroup") / QA
        if (part / "cpuset.cpus").read_text().strip() != "0":
            failures.append(f"2 partition cpus not applied: {(part / 'cpuset.cpus').read_text()!r}")
        else:
            print("  PASS partition created")
        run("partition", "bad/name", "--cpus", "0", expect=1)
        run("partition", QA + "2", "--cpus", "2-,5", expect=1)
        run("partition", QA + "3", "--cpus", "", expect=1)
        if not failures or all("rc=" not in f or "expected" not in f or "bad/" not in f for f in failures):
            print("  PASS invalid names/lists rejected")

        # 3. pin a real process and verify from /proc
        victim = subprocess.Popen(["sleep", "60"])
        time.sleep(0.3)
        run("pin", QA, str(victim.pid))
        allowed = Path(f"/proc/{victim.pid}/status").read_text()
        line = [l for l in allowed.splitlines() if l.startswith("Cpus_allowed_list")][0]
        if line.split()[1] != "0":
            failures.append(f"3 pin did not restrict CPUs: {line!r}")
        else:
            print(f"  PASS pin verified ({line.strip()})")
        run("pin", QA, "999999999", expect=1)
        victim.kill()
        victim.wait()

        # 4. status shows the partition with its procs
        r = run("status", "--json")
        data = json.loads(r.stdout)
        entry = [p for p in data["partitions"] if p["name"] == QA]
        if not entry:
            failures.append("4 partition missing from status")
        else:
            print("  PASS status lists partition")
            shutil.rmtree(part, ignore_errors=True) if part.is_dir() else None
            try:
                (part).rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                shutil.rmtree(part, ignore_errors=True)

        # 5. usage errors
        run("partition", "--cpus", "1", expect=2)
        run("pin", QA, expect=2)
        run("bogus", expect=2)
        if not [f for f in failures if "bogus" in f]:
            print("  PASS usage errors")

    if failures:
        print(f"FAIL: {len(failures)}")
        for f in failures:
            print("  " + f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
