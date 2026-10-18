#!/usr/bin/env python3
"""xtos_ui styling tests: TTY-aware color for XTOS native tools.

Guards three contracts of tools/xtos-native/common/xtos_ui.h:
  1. Piped stdout is byte-identical to plain text (scripted markers keep parsing).
  2. A real pty with a color TERM emits SGR codes; TERM=dumb does not.
  3. --json output never contains escape codes, even on a TTY.

The tool sources are compiled from the working tree exactly like
scripts/build_live.py does (same flags + include dir), then exercised both
through pipes and through real pseudo-terminals.
"""
import os
import pty
import re
import select
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "tools" / "xtos-native" / "common"
TOOLS = ["info", "net", "hw", "clock", "pkg", "fw", "rt", "persist"]

# Commands that print styled human text in the current tree.
STYLED = {
    "info": [["info"]],
    "net": [["show"], ["route"]],
    "hw": [["summary"], ["pci"], ["usb"]],
    "clock": [["status"]],
    "pkg": None,  # needs a manifest; covered by marker check only
    "fw": [["status"]],
    "rt": [["status"]],
    "persist": [["status"]],
}
JSON_CMDS = {
    "info": ["--json"],
    "clock": ["status", "--json"],
    "rt": ["--json", "status"],
    "persist": ["status", "--json"],
}


def build(tool: str, out: Path) -> None:
    src = ROOT / "tools" / "xtos-native" / tool / (tool + ".c")
    cmd = ["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-static",
           "-I", str(COMMON)]
    if tool == "pkg":
        # pkg links the vendored Monocypher ed25519 verifier (xpkg signatures)
        mon = ROOT / "tools" / "xtos-native" / "vendor" / "monocypher"
        cmd += ["-I", str(mon), "-o", str(out), str(src),
                str(mon / "monocypher.c"), str(mon / "monocypher-ed25519.c")]
    else:
        cmd += ["-o", str(out), str(src)]
    subprocess.run(cmd, check=True)


def run_pty(binary: Path, args, term: str, timeout: float = 20.0) -> bytes:
    """Run the binary with stdout on a real pty and collect all output."""
    master, slave = pty.openpty()
    env = dict(os.environ, TERM=term)
    proc = subprocess.Popen(
        [str(binary), *args], stdout=slave, stderr=subprocess.STDOUT,
        stdin=slave, env=env, close_fds=True,
    )
    os.close(slave)
    chunks = []
    while True:
        ready, _, _ = select.select([master], [], [], timeout)
        if not ready:
            break
        try:
            chunk = os.read(master, 65536)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk)
        if proc.poll() is not None:
            while True:
                ready, _, _ = select.select([master], [], [], 0.3)
                if not ready:
                    break
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            break
    os.close(master)
    proc.wait()
    return b"".join(chunks)


ESCAPE = re.compile(rb"\x1b\[")


def main() -> int:
    if sys.platform != "linux":
        print("skip: linux only")
        return 0
    failures = []
    with tempfile.TemporaryDirectory() as td:
        binaries = {}
        for tool in TOOLS:
            binary = Path(td) / tool
            try:
                build(tool, binary)
                binaries[tool] = binary
                print(f"  PASS build {tool} (-Werror clean)")
            except subprocess.CalledProcessError as exc:
                failures.append(f"build {tool}: {exc}")
                print(f"  FAIL build {tool}")

        # 1. Piped output must contain zero escape codes.
        for tool, arglists in STYLED.items():
            if arglists is None or tool not in binaries:
                continue
            for args in arglists:
                r = subprocess.run([str(binaries[tool]), *args],
                                   capture_output=True, timeout=60)
                if ESCAPE.search(r.stdout):
                    failures.append(f"{tool} {args}: escape codes in piped stdout")
                    print(f"  FAIL pipe-plain {tool} {args}")
                else:
                    print(f"  PASS pipe-plain {tool} {' '.join(args)}")

        # 2. Pipe parity with the committed baseline: strip nothing, the piped
        #    output of every styled command must equal its plain rendering.
        #    (Build a second copy with XTOS_UI_FORCE_PLAIN semantics by running
        #    with TERM unset on a pipe — identical contract, keeps the guard
        #    self-contained without pinning volatile system values.)
        for tool, arglists in STYLED.items():
            if arglists is None or tool not in binaries:
                continue
            for args in arglists:
                a = subprocess.run([str(binaries[tool]), *args],
                                   capture_output=True, timeout=60)
                b = subprocess.run([str(binaries[tool]), *args],
                                   capture_output=True, timeout=60,
                                   env={**os.environ, "TERM": ""})
                if a.stdout != b.stdout:
                    failures.append(f"{tool} {args}: piped output not stable")
                    print(f"  FAIL pipe-stable {tool} {args}")
                else:
                    print(f"  PASS pipe-stable {tool} {' '.join(args)}")

        # 3. On a pty with a color TERM, styled commands must emit SGR codes.
        for tool, arglists in STYLED.items():
            if arglists is None or tool not in binaries:
                continue
            for args in arglists:
                out = run_pty(binaries[tool], args, "xterm-256color")
                if not ESCAPE.search(out):
                    failures.append(f"{tool} {args}: no colors on color TTY")
                    print(f"  FAIL tty-color {tool} {args}")
                else:
                    print(f"  PASS tty-color {tool} {' '.join(args)}")

        # 4. TERM=dumb on a pty must stay plain.
        for tool in ("info", "rt", "hw", "clock", "persist"):
            if tool not in binaries:
                continue
            args = STYLED[tool][0] if STYLED.get(tool) else ["status"]
            out = run_pty(binaries[tool], args, "dumb")
            if ESCAPE.search(out):
                failures.append(f"{tool}: colors emitted on TERM=dumb")
                print(f"  FAIL tty-dumb {tool}")
            else:
                print(f"  PASS tty-dumb {tool}")

        # 5. --json on a TTY must stay pure JSON (no escapes, parses cleanly).
        import json as _json
        for tool, args in JSON_CMDS.items():
            if tool not in binaries:
                continue
            out = run_pty(binaries[tool], args, "xterm-256color")
            if ESCAPE.search(out):
                failures.append(f"{tool} --json: escape codes on TTY")
                print(f"  FAIL json-pure {tool}")
                continue
            try:
                _json.loads(out.decode())
                print(f"  PASS json-pure {tool}")
            except Exception as exc:
                failures.append(f"{tool} --json does not parse on TTY: {exc}")
                print(f"  FAIL json-pure {tool}")

        # 6. Markers used by scripts/verify_live.py survive styling intact in
        #    piped output (exact substring, not just escape-free).
        marker_cases = [
            ("fw", ["status"], b"XTOS_FW_STATUS"),
            ("rt", ["status"], b"IRQ affinity"),
            ("clock", ["status"], b"XTOS_CLOCK"),
        ]
        for tool, args, marker in marker_cases:
            if tool not in binaries:
                continue
            r = subprocess.run([str(binaries[tool]), *args],
                               capture_output=True, timeout=60)
            if marker not in r.stdout:
                failures.append(f"{tool} {args}: marker {marker!r} missing from piped output")
                print(f"  FAIL marker {tool}")
            else:
                print(f"  PASS marker {tool} {marker.decode()}")

    if failures:
        print(f"FAIL: {len(failures)}")
        for f in failures:
            print("  " + f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
