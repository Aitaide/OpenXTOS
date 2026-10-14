#!/usr/bin/env python3
"""LUKS persistence combined with xtos.toram=1: two-boot QEMU acceptance.

The candidate ISO carries `xtos.persist=XTOS-PERSIST xtos.luks=1 xtos.toram=1`
baked into its grub entry. boot1: unlock via the console passphrase prompt,
assert persistence=1 AND toram=1 (medium copied to RAM and released), write a
proof. boot2: unlock again, assert the proof survives byte-for-byte. The
LUKS-only three-failure fallback and the plain-persistence five-boot cycle are
covered by test_live_luks.py / test_live_persistence.py; this file only adds
the combination gate.
"""
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_live_luks import (  # noqa: E402
    EVIDENCE as _DEFAULT_EVIDENCE, FIXTURE as _DEFAULT_FIXTURE, LukasGuest,
    build_fixture, unlock_sequence,
)

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = Path(os.environ.get("XTOS_LUKSTORAM_CANDIDATE", ROOT / ".build/live/candidate-24lt"))
FIXTURE = Path(os.environ.get("XTOS_LUKSTORAM_FIXTURE", _DEFAULT_FIXTURE))
EVIDENCE = Path(os.environ.get("XTOS_LUKSTORAM_EVIDENCE", ROOT / ".build/live/evidence-24-lukstoram"))
KEY = Path(os.environ.get("XTOS_LUKSTORAM_KEY", ROOT / ".build/live/keys/verification"))


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    safe_roots = [(ROOT / ".build/live").resolve(), Path("/root/.hermes/cache/scratch").resolve()]

    def within_safe_root(path):
        resolved = path.resolve()
        return any(resolved != base and resolved.is_relative_to(base) for base in safe_roots)

    if not within_safe_root(CANDIDATE) or not within_safe_root(EVIDENCE) or not within_safe_root(FIXTURE):
        print("refusing: candidate, fixture, and evidence must be inside .build/live or private scratch")
        return 2
    if EVIDENCE.exists() or EVIDENCE.is_symlink():
        print("refusing: use a fresh evidence directory")
        return 2
    if not CANDIDATE.is_dir() or not (CANDIDATE / "images/xtos-developer-live-x86_64.iso").is_file():
        print("skip: LUKS+toram candidate ISO missing")
        return 0
    if not KEY.is_file() or shutil.which("qemu-system-x86_64") is None:
        print("skip: SSH key or QEMU unavailable")
        return 0
    if not build_fixture():
        return 1
    fixture_before = sha(FIXTURE)
    EVIDENCE.mkdir(parents=True)

    work = EVIDENCE / "lukstoram-working.raw"
    shutil.copyfile(FIXTURE, work)
    failures = []

    # ---- boot 1: unlock, toram active, write proof ----
    g = LukasGuest(EVIDENCE, "boot1", work)
    proof = ""
    try:
        ok = unlock_sequence(g)
        print(f"  boot1 unlock={ok}")
        if not ok:
            failures.append("boot1 unlock failed")
        g.wait_for(rb"XTOS_LIVE_TORAM copied=1 media_released=1", timeout=300)
        g.wait_for(rb"XTOS_READY[^\r\n]*[\r\n]", timeout=300)
        root_line = re.search(rb"XTOS_LIVE_ROOT.*toram=(\d)", bytes(g.data))
        toram = root_line.group(1).decode() if root_line else "?"
        print(f"  boot1 toram={toram}")
        if toram != "1":
            failures.append(f"boot1 toram != 1 (got {toram})")
        m = re.search(rb"persistence=(\d)", bytes(g.data))
        p1 = m.group(1).decode() if m else "?"
        print(f"  boot1 persistence={p1}")
        if p1 != "1":
            failures.append(f"boot1 persistence != 1 (got {p1})")
        g.trust_serial_hostkey()
        r = g.ssh("echo LUKSTORAM-PROOF > /root/LUKSTORAM-PROOF && cat /root/LUKSTORAM-PROOF && sync")
        proof = (r.stdout or "").strip()
        print(f"  boot1 wrote: {proof[:40]}")
        if "LUKSTORAM-PROOF" not in proof:
            failures.append(f"boot1 write failed: {r.stderr[:150]}")
        # The medium must have been released: /run/live-media unmounted.
        r = g.ssh("mount | grep -c live-media || true")
        media = (r.stdout or "").strip()
        print(f"  boot1 live-media mounts after release: {media!r}")
        if media not in ("0", ""):
            failures.append(f"live-media still mounted after toram: {media!r}")
        g.ssh("sync; /usr/libexec/xtos/busybox poweroff -f")
        try:
            g.process.wait(timeout=120)
        except Exception:
            pass
    finally:
        g.close()

    # ---- boot 2: unlock again, proof survives ----
    g = LukasGuest(EVIDENCE, "boot2", work)
    try:
        ok = unlock_sequence(g)
        print(f"  boot2 unlock={ok}")
        if not ok:
            failures.append("boot2 unlock failed")
        g.wait_for(rb"XTOS_READY[^\r\n]*[\r\n]", timeout=300)
        m = re.search(rb"persistence=(\d)", bytes(g.data))
        p2 = m.group(1).decode() if m else "?"
        print(f"  boot2 persistence={p2}")
        if p2 != "1":
            failures.append(f"boot2 persistence != 1 (got {p2})")
        g.trust_serial_hostkey()
        r = g.ssh("cat /root/LUKSTORAM-PROOF")
        content = (r.stdout or "").strip()
        print(f"  boot2 read: {content[:40]}")
        if "LUKSTORAM-PROOF" not in content:
            failures.append("boot2 did not recover the proof")
        if proof and content != proof:
            failures.append(f"boot2 proof mismatch: {content!r} != {proof!r}")
        g.ssh("sync; /usr/libexec/xtos/busybox poweroff -f")
        try:
            g.process.wait(timeout=120)
        except Exception:
            pass
    finally:
        g.close()

    if sha(FIXTURE) != fixture_before:
        failures.append("source fixture image was modified during the run")

    (EVIDENCE / "lukstoram-results.json").write_text(json.dumps({
        "boot1_toram": toram, "boot1_persistence": p1, "boot2_persistence": p2,
        "proof_recovered": "LUKSTORAM-PROOF" in content,
        "fixture_unchanged": sha(FIXTURE) == fixture_before,
    }, indent=2))

    if failures:
        print(f"FAIL: {len(failures)}")
        for f in failures:
            print("  " + f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
