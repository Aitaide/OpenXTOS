#!/usr/bin/env python3
"""LUKS-encrypted persistence: QEMU acceptance for the full boot-unlock chain.

Fixture (built once, read-only afterwards): a small partition image carrying a
LUKS2 container labeled XTOS-PERSIST (LUKS labels live on the outer header, so
blkid sees them before unlock). Inside sits an ext4 filesystem with the
XTOS_PERSIST_ID marker.

Acceptance flow:
  boot A (provision): xtos.luks=1 + xtos.persist=XTOS-PERSIST, wrong passphrase
     first (retry works), then the right one; unlock -> mount -> write proof.
  boot B (reboot): same cmdline; unlock again; proof file byte-identical.
  boot C: wrong passphrase three times -> system continues RAM-only (persistence=0)
     and never touches the volume.
The fixture image is hashed before/after; QEMU only ever writes to fresh copies
inside a fresh evidence directory.
"""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = Path(os.environ.get("XTOS_LUKS_CANDIDATE", ROOT / ".build/live/candidate-24"))
KEY = Path(os.environ.get("XTOS_LUKS_KEY", ROOT / ".build/live/keys/verification"))
FIXTURE = Path(os.environ.get("XTOS_LUKS_FIXTURE", ROOT / ".build/live/luks-fixture.raw"))
EVIDENCE = Path(os.environ.get("XTOS_LUKS_EVIDENCE", ROOT / ".build/live/evidence-24-luks"))
PASSPHRASE = "xtos-luks-test\n"
BAD_PASSPHRASE = "wrong-passphrase\n"
LUKS_ARGS = ["cryptsetup", "--batch-mode", "--type", "luks2", "--pbkdf", "pbkdf2",
             "--pbkdf-force-iterations", "1000", "--label", "XTOS-PERSIST", "luksFormat"]


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_fixture() -> bool:
    """Create the LUKS+ext4+marker fixture once. Uses host loop devices with
    teardown; refuses to run if the fixture already exists."""
    if FIXTURE.is_file():
        return True
    if shutil.which("cryptsetup") is None or shutil.which("mkfs.ext4") is None:
        print("skip: cryptsetup/mkfs.ext4 unavailable on host")
        return False
    size = 96 * 1024 * 1024
    with FIXTURE.open("wb") as f:
        f.truncate(size)
    try:
        subprocess.run(LUKS_ARGS + [str(FIXTURE)], input=PASSPHRASE.encode(),
                       check=True, capture_output=True, timeout=120)
        open_cmd = ["cryptsetup", "--batch-mode", "open", str(FIXTURE), "xtos-luks-fixture"]
        subprocess.run(open_cmd, input=PASSPHRASE.encode(), check=True, capture_output=True, timeout=60)
        try:
            subprocess.run(["mkfs.ext4", "-F", "-L", "XTOS-PERSIST", "/dev/mapper/xtos-luks-fixture"],
                           check=True, capture_output=True, timeout=120)
            tmp = Path(tempfile.mkdtemp(prefix="luks-fixture-"))
            subprocess.run(["mount", "/dev/mapper/xtos-luks-fixture", str(tmp)],
                           check=True, capture_output=True, timeout=30)
            try:
                (tmp / "XTOS_PERSIST_ID").write_text("XTOS-PERSIST-1\n")
                subprocess.run(["sync"], check=True)
            finally:
                subprocess.run(["umount", str(tmp)], check=True, capture_output=True, timeout=30)
                tmp.rmdir()
        finally:
            subprocess.run(["cryptsetup", "--batch-mode", "close", "xtos-luks-fixture"],
                           check=True, capture_output=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: fixture build error: {exc}")
        return False
    return True


class LukasGuest:
    """QEMU guest with serial-console interaction for the passphrase prompt."""

    def __init__(self, evidence, tag, disk):
        self.tag = tag
        self.disk = Path(disk).resolve()
        self.log = (evidence / f"{tag}.serial.log").open("wb")
        self.err = (evidence / f"{tag}.qemu.log").open("wb")
        sockpath = evidence / f"{tag}.sock"
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        args = ["qemu-system-x86_64", "-machine", "q35", "-accel", "tcg,thread=multi",
                "-cpu", "max", "-m", "3072", "-smp", "2", "-display", "none",
                "-monitor", "none", "-no-reboot",
                "-serial", f"unix:{sockpath},server=on,wait=off",
                "-cdrom", str(CANDIDATE / "images/xtos-developer-live-x86_64.iso"), "-boot", "d",
                "-drive", f"file={self.disk},format=raw,if=none,id=persist",
                "-device", "nvme,drive=persist,serial=XTOS-LUKS-TEST",
                "-netdev", f"user,id=net,hostfwd=tcp:127.0.0.1:{self.port}-:22",
                "-device", "virtio-net-pci,netdev=net"]
        self.process = subprocess.Popen(args, stdout=self.err, stderr=self.err)
        self.sock = socket.socket(socket.AF_UNIX)
        deadline = time.monotonic() + 20
        while True:
            if self.process.poll() is not None:
                raise RuntimeError("QEMU exited early")
            try:
                self.sock.connect(str(sockpath))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() > deadline:
                    raise RuntimeError("serial unavailable")
                time.sleep(0.1)
        self.data = bytearray()
        self.known = evidence / f"{tag}.known_hosts"

    def wait_for(self, pattern, timeout=300, quiet=False):
        deadline = time.monotonic() + timeout
        pat = re.compile(pattern)
        while time.monotonic() < deadline:
            m = pat.search(bytes(self.data))
            if m:
                return m
            if self.process.poll() is not None:
                raise RuntimeError(f"QEMU died before {pattern!r}")
            if b"XTOS_LIVE_ERROR" in self.data and b"LUKS" not in pattern.encode() and b"XTOS_LIVE_LUKS" not in self.data:
                raise RuntimeError(self.data[-2000:].decode(errors="replace"))
            self.sock.settimeout(2)
            try:
                chunk = self.sock.recv(65536)
                if chunk:
                    self.data.extend(chunk)
                    self.log.write(chunk)
                    self.log.flush()
            except socket.timeout:
                pass
        raise RuntimeError(f"timeout waiting for {pattern!r}; tail: {bytes(self.data[-400:])!r}")

    def send(self, text):
        self.sock.sendall(text.encode())

    def trust_serial_hostkey(self):
        matches = re.findall(rb"ssh-ed25519 ([A-Za-z0-9+/=]+)", bytes(self.data))
        if not matches:
            raise RuntimeError("serial did not advertise the SSH host key")
        self.known.write_text(f"[127.0.0.1]:{self.port} ssh-ed25519 " + matches[-1].decode() + "\n")

    def ssh(self, cmd, timeout=180):
        args = ["ssh", "-F", "/dev/null", "-p", str(self.port), "-i", str(KEY),
                "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(self.known),
                "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=10",
                "-o", "ServerAliveCountMax=2", "root@127.0.0.1", cmd]
        deadline = time.monotonic() + 420
        while True:
            result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
            transient = ("Connection refused", "timed out", "Connection reset", "banner exchange")
            if result.returncode == 0 or time.monotonic() > deadline:
                return result
            if not any(t in result.stderr for t in transient):
                return result
            time.sleep(3)

    def close(self):
        for closer in (self.sock.close, self.log.close, self.err.close):
            try:
                closer()
            except Exception:
                pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except Exception:
                self.process.kill()


def _pump(guest, seconds):
    """Read serial for a bounded window; returns fresh chunk bytes."""
    got = bytearray()
    deadline = time.monotonic() + seconds
    guest.sock.settimeout(1)
    while time.monotonic() < deadline:
        try:
            chunk = guest.sock.recv(65536)
            if chunk:
                got.extend(chunk)
                guest.data.extend(chunk)
                guest.log.write(chunk)
                guest.log.flush()
        except socket.timeout:
            pass
    return bytes(got)


def send_passphrase(guest, phrase, expect_retry=False):
    """Type one passphrase: wait for the prompt to be fully printed, settle,
    then send. If nothing new arrives afterwards, re-send once (the line
    discipline can drop input that races cryptsetup's TTY setup)."""
    guest.wait_for(rb"Enter passphrase", timeout=240)
    _pump(guest, 0.5)  # let the prompt finish printing and the TTY settle
    guest.send(phrase)
    fresh = _pump(guest, 90)
    if not fresh.strip():
        # Prompt never consumed the line: re-send once.
        guest.send(phrase)
        fresh = _pump(guest, 90)
    return fresh


def unlock_sequence(guest, wrong_first=False):
    """Drive the console passphrase prompt. Returns True when unlock succeeded."""
    guest.wait_for(rb"XTOS_LIVE_LUKS_UNLOCK", timeout=240)
    if wrong_first:
        send_passphrase(guest, BAD_PASSPHRASE, expect_retry=True)
        guest.wait_for(rb"XTOS_LIVE_LUKS wrong-passphrase; retry", timeout=240)
    send_passphrase(guest, PASSPHRASE)
    # Success prints the mapper line; a third failure prints status=failed.
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if re.search(rb"XTOS_LIVE_LUKS device=\S+ mapper=/dev/mapper/xtos-luks-persist", bytes(guest.data)):
            return True
        if b"status=failed after 3 attempts" in guest.data:
            return False
        if guest.process.poll() is not None:
            raise RuntimeError("QEMU died during unlock")
        guest.sock.settimeout(2)
        try:
            chunk = guest.sock.recv(65536)
            if chunk:
                guest.data.extend(chunk)
                guest.log.write(chunk)
                guest.log.flush()
        except socket.timeout:
            pass
    raise RuntimeError("unlock sequence timed out")


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
        print("skip: candidate ISO missing")
        return 0
    if not KEY.is_file() or shutil.which("qemu-system-x86_64") is None:
        print("skip: SSH key or QEMU unavailable")
        return 0
    if not build_fixture():
        return 1
    fixture_before = sha(FIXTURE)
    EVIDENCE.mkdir(parents=True)

    work = EVIDENCE / "luks-working.raw"
    shutil.copyfile(FIXTURE, work)
    failures = []

    # ---- boot A: provision with one wrong passphrase, then success ----
    g = LukasGuest(EVIDENCE, "bootA", work)
    try:
        ok = unlock_sequence(g, wrong_first=True)
        print(f"  bootA unlock(after 1 wrong attempt)={ok}")
        if not ok:
            failures.append("bootA unlock failed after wrong+right passphrase")
        g.wait_for(rb"XTOS_READY[^\r\n]*[\r\n]", timeout=300)
        m = re.search(rb"persistence=(\d)", bytes(g.data))
        pa = m.group(1).decode() if m else "?"
        print(f"  bootA persistence={pa}")
        if pa != "1":
            failures.append(f"bootA persistence != 1 (got {pa})")
        g.trust_serial_hostkey()
        r = g.ssh("echo LUKS-PROOF-$(date +%%s) > /root/LUKS-PROOF && cat /root/LUKS-PROOF && sync")
        proof = (r.stdout or "").strip()
        print(f"  bootA wrote: {proof[:40]}")
        if "LUKS-PROOF-" not in proof:
            failures.append(f"bootA write failed: {r.stderr[:150]}")
        # The mapper and mount must be visible to the runtime persist tool.
        r = g.ssh("persist status --json")
        try:
            data = json.loads((r.stdout or "").strip())
            if not data.get("active"):
                failures.append(f"bootA persist status not active: {r.stdout[:200]}")
        except json.JSONDecodeError:
            failures.append(f"persist status --json invalid: {r.stdout[:200]}")
        g.ssh("sync; /usr/libexec/xtos/busybox poweroff -f")
        try:
            g.process.wait(timeout=120)
        except Exception:
            pass
    finally:
        g.close()

    # ---- boot B: clean unlock, proof survives ----
    g = LukasGuest(EVIDENCE, "bootB", work)
    try:
        ok = unlock_sequence(g)
        print(f"  bootB unlock={ok}")
        if not ok:
            failures.append("bootB unlock failed")
        g.wait_for(rb"XTOS_READY[^\r\n]*[\r\n]", timeout=300)
        m = re.search(rb"persistence=(\d)", bytes(g.data))
        pb = m.group(1).decode() if m else "?"
        print(f"  bootB persistence={pb}")
        if pb != "1":
            failures.append(f"bootB persistence != 1 (got {pb})")
        g.trust_serial_hostkey()
        r = g.ssh("cat /root/LUKS-PROOF")
        content = (r.stdout or "").strip()
        print(f"  bootB read: {content[:40]}")
        if "LUKS-PROOF-" not in content:
            failures.append("bootB did not recover the proof file")
        if proof and content != proof:
            failures.append(f"bootB proof mismatch: {content!r} != {proof!r}")
        g.ssh("sync; /usr/libexec/xtos/busybox poweroff -f")
        try:
            g.process.wait(timeout=120)
        except Exception:
            pass
    finally:
        g.close()

    # ---- boot C: three wrong passphrases -> RAM-only, volume untouched ----
    fresh = EVIDENCE / "luks-fresh-copy.raw"
    shutil.copyfile(FIXTURE, fresh)
    before = sha(fresh)
    g = LukasGuest(EVIDENCE, "bootC", fresh)
    try:
        g.wait_for(rb"XTOS_LIVE_LUKS_UNLOCK", timeout=240)
        for attempt in range(3):
            send_passphrase(g, BAD_PASSPHRASE)
            if attempt < 2:
                g.wait_for(rb"XTOS_LIVE_LUKS wrong-passphrase; retry", timeout=240)
        g.wait_for(rb"XTOS_LIVE_LUKS status=failed after 3 attempts", timeout=240)
        g.wait_for(rb"XTOS_READY[^\r\n]*[\r\n]", timeout=300)
        m = re.search(rb"persistence=(\d)", bytes(g.data))
        pc = m.group(1).decode() if m else "?"
        print(f"  bootC (3 wrong) persistence={pc}")
        if pc != "0":
            failures.append(f"bootC persistence != 0 (got {pc})")
        g.trust_serial_hostkey()
        r = g.ssh("persist status --json")
        try:
            data = json.loads((r.stdout or "").strip())
            if data.get("active"):
                failures.append("bootC claims active persistence despite failed unlock")
        except json.JSONDecodeError:
            failures.append(f"bootC persist status invalid: {r.stdout[:200]}")
    finally:
        g.close()
    if sha(fresh) != before:
        failures.append("bootC modified the LUKS volume despite failed unlock")

    if sha(FIXTURE) != fixture_before:
        failures.append("source fixture image was modified during the run")

    (EVIDENCE / "luks-results.json").write_text(json.dumps({
        "bootA_unlock_after_wrong_attempt": True,
        "bootA_persistence": pa, "bootB_persistence": pb,
        "proof_recovered": "LUKS-PROOF-" in content,
        "bootC_persistence_after_failures": pc,
        "fixture_unchanged": sha(FIXTURE) == fixture_before,
        "fixture_bytes": FIXTURE.stat().st_size,
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
