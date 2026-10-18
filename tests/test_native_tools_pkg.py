#!/usr/bin/env python3
"""pkg tool tests: manifest parsing, sha256 verify, overrides, unicode paths.

Builds the static binary and runs it chrooted against the assembled live
userspace tree (default .build/userspace/live-developer-v3). Skips when the
tree is absent.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "tools" / "xtos-native" / "pkg" / "pkg.c"
DEFAULT_ROOTFS = ROOT / ".build" / "userspace" / "live-developer-v3"


def main() -> int:
    if sys.platform != "linux":
        print("skip: linux only")
        return 0
    rootfs = DEFAULT_ROOTFS
    if not (rootfs / "usr/share/xtos/userspace-manifest.json").is_file():
        print("skip: assembled userspace tree missing")
        return 0

    with tempfile.TemporaryDirectory() as td:
        binary = Path(td) / "pkg"
        mon = ROOT / "tools" / "xtos-native" / "vendor" / "monocypher"
        subprocess.run(["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-static",
                        "-I", str(ROOT / "tools" / "xtos-native" / "common"),
                        "-I", str(mon),
                        "-o", str(binary), str(SRC),
                        str(mon / "monocypher.c"), str(mon / "monocypher-ed25519.c")], check=True)
        target = rootfs / "usr/bin/.pkg-selftest"
        shutil.copy2(binary, target)

        def run(*args):
            return subprocess.run(["chroot", str(rootfs), "/usr/bin/.pkg-selftest", *args],
                                  capture_output=True, text=True, timeout=300)

        failures = []

        # summary counts only top-level packages (not lock.packages duplicates)
        r = run("summary")
        manifest = json.loads((rootfs / "usr/share/xtos/userspace-manifest.json").read_text())
        want = len(manifest["packages"])
        if f"Packages:      {want}" not in r.stdout:
            failures.append(f"summary package count: {r.stdout!r} want {want}")
        print(f"  PASS summary count={want}")

        # list works with filter
        r = run("list", "python3")
        if "python3 " not in r.stdout and "python3\n" not in r.stdout:
            failures.append("list filter python3 empty")
        print("  PASS list filter")

        # show exposes version/source
        r = run("show", "bash")
        if "Version:" not in r.stdout or "Source:" not in r.stdout:
            failures.append(f"show bash incomplete: {r.stdout!r}")
        print("  PASS show bash")

        # verify single package clean
        r = run("verify", "bash")
        if "corrupt=0" not in r.stdout or "missing=0" not in r.stdout:
            failures.append(f"verify bash: {r.stdout!r}")
        print("  PASS verify bash clean")

        # tamper detection round-trip
        doc = rootfs / "usr/share/doc/bash/copyright"
        backup = doc.read_bytes()
        try:
            with doc.open("ab") as fh:
                fh.write(b"\nTAMPER-TEST\n")
            r = run("verify", "bash")
            if "corrupt=1" not in r.stdout:
                failures.append(f"tamper not detected: {r.stdout!r}")
            print("  PASS tamper detection")
        finally:
            doc.write_bytes(backup)
        r = run("verify", "bash")
        if "corrupt=0" not in r.stdout:
            failures.append(f"restore failed: {r.stdout!r}")
        print("  PASS restore clean")

        # full verify on the build tree: zero corrupt/missing
        r = run("verify", "--all")
        if "corrupt=0" not in r.stdout or "missing=0" not in r.stdout:
            failures.append(f"verify --all: {r.stdout.strip().splitlines()[-1]!r}")
        print("  PASS verify --all clean")

        # unicode path decode (go testdata with \u00de etc.)
        r = run("verify", "golang-1.24-src")
        if "missing=0" not in r.stdout or "corrupt=0" not in r.stdout:
            failures.append(f"unicode paths: {r.stdout!r}")
        print("  PASS unicode path verify")

        target.unlink(missing_ok=True)

    if failures:
        print(f"FAIL: {len(failures)}")
        for f in failures:
            print("  " + f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
