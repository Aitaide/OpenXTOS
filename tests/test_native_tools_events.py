#!/usr/bin/env python3
"""events tool acceptance: determinism, schema, and stream integrity."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "tools/xtos-native/events/events.c"
FLAGS = ["-O2", "-Wall", "-Wextra", "-Werror", "-static"]


def run(binary, *args, timeout=30):
    return subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=timeout)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="xtos-events-") as td:
        binary = Path(td) / "events"
        build = subprocess.run(["gcc", *FLAGS, "-o", str(binary), str(SRC)], capture_output=True, text=True)
        if build.returncode != 0:
            print(f"FAIL: build\n{build.stderr}")
            return 1

        failures = []

        def check(name, ok, detail=""):
            print(f"  {'PASS' if ok else 'FAIL'} {name}")
            if not ok:
                failures.append(f"{name}: {detail}")

        # usage / bad args
        r = run(binary)
        check("no-args shows usage", r.returncode == 2 and "usage" in r.stderr, r.stderr[:100])
        r = run(binary, "--seed")
        check("dangling flag rejected", r.returncode == 2)
        r = run(binary, "--controllers", "9", "--count", "1")
        check("controller bound enforced", r.returncode == 2)

        # schema of every line
        r = run(binary, "--seed", "7", "--count", "200", "--interval-us", "0")
        lines = [line for line in r.stdout.splitlines() if line]
        check("exit ok", r.returncode == 0)
        check("emitted all events", len(lines) == 200, f"got {len(lines)}")
        valid = 0
        types = set()
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (isinstance(event.get("t_ns"), int) and event.get("src") == "sim" and
                    event.get("type") in ("pose", "button", "axis") and
                    isinstance(event.get("controller"), int) and 0 <= event["controller"] < 2):
                if event["type"] == "pose" and all(isinstance(event.get(k), float) for k in ("x", "y", "z", "w")):
                    valid += 1
                    types.add("pose")
                elif event["type"] == "button" and isinstance(event.get("button"), int) and event.get("down") in (0, 1):
                    valid += 1
                    types.add("button")
                elif event["type"] == "axis" and isinstance(event.get("axis"), int) and isinstance(event.get("value"), float):
                    valid += 1
                    types.add("axis")
        check("all events schema-valid", valid == len(lines), f"{valid}/{len(lines)}")
        check("all three event types seen", types == {"pose", "button", "axis"}, str(types))

        # determinism of the payload: same seed => identical lines ignoring t_ns
        def payload(binary_path, seed):
            out = run(binary_path, "--seed", str(seed), "--count", "100", "--interval-us", "0").stdout
            # Keep only the deterministic payload: everything from "src" onward.
            return [line[line.index('"src"'):] if '"src"' in line else line
                    for line in out.splitlines()]
        check("same seed deterministic", payload(binary, 7) == payload(binary, 7))
        check("different seed differs", payload(binary, 7) != payload(binary, 8))

        # monotonic timestamps are non-decreasing
        stamps = [int(json.loads(line)["t_ns"]) for line in lines]
        check("timestamps monotonic", all(a <= b for a, b in zip(stamps, stamps[1:])))

        # interval pacing: 20 events at 5ms should take >= 90ms wall
        import time
        start = time.monotonic()
        run(binary, "--seed", "3", "--count", "20", "--interval-us", "5000", timeout=30)
        elapsed = time.monotonic() - start
        check("interval pacing honored", elapsed >= 0.09, f"{elapsed:.3f}s")

    if failures:
        print(f"FAIL: {len(failures)}")
        for f in failures:
            print("  " + f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
