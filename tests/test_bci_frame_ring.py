#!/usr/bin/env python3
"""Compile unchanged production frame-ring C with a service-only pthread shim."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest
ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / ".build/xtos-roadmap-v1/p2-ring"
CASES = ["alloc", "publish", "validation", "identity", "exhaustion", "peek", "commit", "discard_wake", "concurrent", "boundaries", "readonly", "profiles"]
def run(mode="normal", case=None, label="verification"):
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    binary = EVIDENCE / (label + "-" + mode)
    flags = {"normal": [], "asan": ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"], "tsan": ["-fsanitize=thread", "-fno-omit-frame-pointer"]}[mode]
    log = EVIDENCE / (label + "-" + mode + ".json")
    reservation = binary.with_name(binary.name + ".run")
    if any(path.exists() or path.is_symlink() for path in (binary, log)):
        raise RuntimeError("refusing to overwrite evidence: " + str(binary))
    binary.parent.mkdir(parents=True, exist_ok=True)
    try:
        reservation.mkdir()
    except FileExistsError as error:
        raise RuntimeError("refusing to overwrite evidence: " + str(reservation)) from error
    # Retain the claim after success, compilation failure or interruption.
    # A contender must never enter the compiler against this run's artifacts.
    if any(path.exists() or path.is_symlink() for path in (binary, log)):
        raise RuntimeError("refusing to overwrite evidence: " + str(binary))
    command = ["gcc", "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-DXTOS_BCI_FRAME_HOST_TEST", "-g", "-O1", "-Wall", "-Wextra", "-Werror", "-Wconversion", "-Wshadow", "-pthread", *flags, str(ROOT / "tests/bci_frame_host/frame_test.c"), "-o", str(binary)]
    results = []
    def invoke(args):
        p = subprocess.run(args, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, env={**os.environ, "ASAN_OPTIONS": "detect_leaks=1:abort_on_error=1", "UBSAN_OPTIONS": "halt_on_error=1"})
        results.append({"command": args, "exit": p.returncode, "output": p.stdout})
        return p.returncode
    rc = invoke(command)
    if not rc:
        for name in ([case] if case else CASES):
            if invoke([str(binary), name]):
                rc = 1
    sources = ["kernel/linux/drivers/xtos/bci/xtos_bci_frame.c", "kernel/linux/drivers/xtos/bci/xtos_bci_frame.h", "tests/bci_frame_host/frame_shim.h", "tests/bci_frame_host/frame_test.c", "tests/test_bci_frame_ring.py", "kernel/linux/include/uapi/linux/xtos_bci_frame.h", "kernel/linux/include/uapi/linux/xtos_bci_config.h"]
    identities = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sources}
    text = json.dumps({"mode": mode, "label": label, "source_sha256": identities, "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest() if binary.exists() else None, "results": results}, indent=2) + "\n"
    with log.open("x") as stream:
        stream.write(text)
    print(text)
    print("LOG_SHA256", hashlib.sha256(text.encode()).hexdigest())
    return rc
class FrameRingTests(unittest.TestCase):
    def test_production_ring(self):
        import time
        self.assertEqual(run(label="unittest-" + str(time.time_ns())), 0)
if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["normal", "asan", "tsan"], default="normal")
    p.add_argument("--case")
    p.add_argument("--label", required=True)
    a = p.parse_args()
    raise SystemExit(run(a.mode, a.case, a.label))
