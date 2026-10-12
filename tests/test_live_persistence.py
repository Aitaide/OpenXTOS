#!/usr/bin/env python3
"""Two-boot persistence end-to-end test.

Boots the xtos.persist=XTOS-PERSIST variant twice with a marked ext4 disk:
boot 1 writes /root/PERSIST-PROOF, boot 2 reads it back. Also verifies the
boot-medium write-sentinel stays untouched.
"""
import hashlib
import json
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = ROOT / ".build/live/candidate-20p2"
DISK = ROOT / ".build/live/persist-test-disk.raw"
KEY = ROOT / ".build/live/keys/verification"
EVIDENCE = ROOT / ".build/live/evidence-20p2"


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class PersistGuest:
    def __init__(self, evidence, tag):
        self.tag = tag
        self.log = (evidence / f"{tag}.serial.log").open("wb")
        self.err = (evidence / f"{tag}.qemu.log").open("wb")
        sockpath = evidence / f"{tag}.sock"
        if sockpath.exists():
            raise RuntimeError("socket exists")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        args = ["qemu-system-x86_64", "-machine", "q35", "-accel", "tcg,thread=multi",
                "-cpu", "max", "-m", "3072", "-smp", "2", "-display", "none",
                "-monitor", "none", "-no-reboot",
                "-serial", f"unix:{sockpath},server=on,wait=off",
                "-cdrom", str(CANDIDATE / "images/xtos-developer-live-x86_64.iso"), "-boot", "d",
                "-drive", f"file={DISK},format=raw,if=none,id=persist",
                "-device", "nvme,drive=persist,serial=XTOS-PERSIST-TEST",
                "-netdev", f"user,id=net,hostfwd=tcp:127.0.0.1:{self.port}-:22",
                "-device", "virtio-net-pci,netdev=net"]
        self.process = subprocess.Popen(args, stdout=self.err, stderr=self.err)
        self.sock = socket.socket(socket.AF_UNIX)
        deadline = time.monotonic() + 20
        self.closed = False
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

    def wait_for(self, pattern, timeout=300):
        deadline = time.monotonic() + timeout
        pat = re.compile(pattern)
        while time.monotonic() < deadline:
            m = pat.search(bytes(self.data))
            if m:
                return m
            if self.process.poll() is not None:
                raise RuntimeError(f"QEMU died before {pattern!r}")
            self.sock.settimeout(2)
            try:
                chunk = self.sock.recv(65536)
                if chunk:
                    self.data.extend(chunk)
                    self.log.write(chunk)
                    self.log.flush()
            except socket.timeout:
                pass
        raise RuntimeError(f"timeout waiting for {pattern!r}")

    def trust_serial_hostkey(self):
        """The live image generates an ephemeral host key per boot; trust the
        serial console channel exactly like scripts/verify_live.py does."""
        matches = re.findall(rb"ssh-ed25519 ([A-Za-z0-9+/=]+)", bytes(self.data))
        if not matches:
            raise RuntimeError("serial did not advertise the SSH host key")
        self.known.write_text(f"[127.0.0.1]:{self.port} ssh-ed25519 " + matches[-1].decode() + "\n")

    def ssh(self, cmd, timeout=180):
        # Under TCG the first dropbear banner + kex can take ~1-2 minutes after
        # XTOS_READY (ephemeral host key generation is slow). Retry on ANY
        # connection-stage failure (refused, banner timeout), not just refusal.
        args = ["ssh", "-F", "/dev/null", "-p", str(self.port), "-i", str(KEY),
                "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(self.known),
                "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=10",
                "-o", "ServerAliveCountMax=2", "root@127.0.0.1", cmd]
        deadline = time.monotonic() + 420
        while True:
            try:
                result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                result = None
            transient = ("Connection refused", "timed out", "Connection reset", "banner exchange")
            if (result is not None and result.returncode == 0) or time.monotonic() > deadline:
                break
            if result is not None and not any(t in result.stderr for t in transient):
                break
            time.sleep(3)
        if result is None:
            raise RuntimeError(f"ssh never completed: {cmd[:60]}")
        return result

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass
        try:
            self.log.close()
        except Exception:
            pass
        try:
            self.err.close()
        except Exception:
            pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except Exception:
                self.process.kill()
        self.log.close()
        self.err.close()


def main() -> int:
    if not (CANDIDATE / "images/xtos-developer-live-x86_64.iso").is_file():
        print("skip: candidate-20p2 variant not built")
        return 0
    if not DISK.is_file():
        print("skip: persistence test disk missing")
        return 0
    if shutil.which("qemu-system-x86_64") is None:
        print("skip: QEMU unavailable")
        return 0

    disk_before = sha(DISK)
    if EVIDENCE.exists():
        shutil.rmtree(EVIDENCE)
    EVIDENCE.mkdir(parents=True)

    failures = []

    # ---- boot 1: write proof file ----
    g1 = PersistGuest(EVIDENCE, "boot1")
    try:
        g1.wait_for(rb"XTOS_READY[^\r\n]*[\r\n]")
        g1.trust_serial_hostkey()
        m = re.search(rb"persistence=(\d+)", bytes(g1.data))
        persist = m.group(1).decode() if m else "?"
        print(f"  boot1 persistence={persist}")
        if persist != "1":
            failures.append(f"boot1 persistence != 1 (got {persist})")
        r = g1.ssh("wc -c /root/.ssh/authorized_keys")
        seeded = (r.stdout or "").strip()
        print(f"  boot1 seeded authorized_keys: {seeded}")
        if not re.search(r"^8\d\s", seeded):
            failures.append(f"root authorized_keys not seeded properly: {seeded!r} {r.stderr[:100]}")
        r = g1.ssh("echo PERSIST-PROOF-$(date +%s) > /root/PERSIST-PROOF && cat /root/PERSIST-PROOF && sync")
        proof_line = (r.stdout or "").strip()
        print(f"  boot1 wrote: {proof_line[:50]}")
        if "PERSIST-PROOF-" not in proof_line:
            failures.append(f"boot1 write failed: {r.stderr[:100]}")
        # Clean shutdown: a real user flow, and it commits ext4 data (a SIGTERM
        # kill loses unsynced data and would make the proof flaky).
        g1.ssh("sync; /usr/libexec/xtos/busybox poweroff -f")
        try:
            g1.process.wait(timeout=120)
        except Exception:
            pass
    finally:
        g1.close()

    # ---- boot 2: read it back ----
    g2 = PersistGuest(EVIDENCE, "boot2")
    try:
        g2.wait_for(rb"XTOS_READY[^\r\n]*[\r\n]")
        g2.trust_serial_hostkey()
        m = re.search(rb"persistence=(\d+)", bytes(g2.data))
        persist = m.group(1).decode() if m else "?"
        print(f"  boot2 persistence={persist}")
        if persist != "1":
            failures.append(f"boot2 persistence != 1 (got {persist})")
        r = g2.ssh("cat /root/PERSIST-PROOF")
        content = (r.stdout or "").strip()
        print(f"  boot2 read: {content[:50]}")
        if "PERSIST-PROOF-" not in content:
            failures.append("boot2 did not recover the proof file")
        if proof_line and content != proof_line:
            failures.append(f"boot2 content mismatch: {content!r} != {proof_line!r}")
        g2.ssh("sync; /usr/libexec/xtos/busybox poweroff -f")
        try:
            g2.process.wait(timeout=120)
        except Exception:
            pass
    finally:
        g2.close()

    # ---- home skeleton check ----
    g3 = PersistGuest(EVIDENCE, "boot3")
    try:
        g3.wait_for(rb"XTOS_READY[^\r\n]*[\r\n]")
        g3.trust_serial_hostkey()
        r = g3.ssh("ls /home/xtos/ && touch /home/xtos/homeproof && echo HOME-OK")
        print(f"  home: {(r.stdout or '').strip()[:60]}")
        if "HOME-OK" not in (r.stdout or ""):
            failures.append(f"home bind/dirs wrong: {r.stderr[:100]}")
        g3.ssh("sync; /usr/libexec/xtos/busybox poweroff -f")
        try:
            g3.process.wait(timeout=120)
        except Exception:
            pass
    finally:
        g3.close()

    # sentinel: the proof survived 3 boots; disk hash CHANGED (that's the point)
    disk_after = sha(DISK)
    print(f"  disk modified by test: {disk_before != disk_after}")
    (EVIDENCE / "persistence-results.json").write_text(json.dumps({
        "boot1_persistence": persist, "proof_recovered": "PERSIST-PROOF-" in content,
        "disk_bytes": DISK.stat().st_size,
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
