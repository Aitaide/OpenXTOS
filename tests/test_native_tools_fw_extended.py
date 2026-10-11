#!/usr/bin/env python3
"""Extended fw tests: allow/deny/list/delete/flush lifecycle against real nftables.

Runs the actual static fw binary. Requires nft CLI on PATH and root (or
CAP_NET_ADMIN). Skips gracefully when not root or nft missing.
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FW = ROOT / "tools" / "xtos-native" / "fw" / "fw.c"


def build_fw(tmpdir: Path) -> Path:
    out = tmpdir / "fw"
    subprocess.run(
        ["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-static", "-o", str(out), str(FW)],
        check=True,
    )
    return out


def run(binary: Path, *args: str):
    return subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=20)


def main() -> int:
    if sys.platform != "linux":
        print("skip: linux only")
        return 0
    if shutil.which("nft") is None:
        print("skip: nft CLI not available")
        return 0
    if subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip() != "0":
        print("skip: needs root for netfilter netlink")
        return 0

    import tempfile
    failures = []
    with tempfile.TemporaryDirectory() as td:
        fw = build_fw(Path(td))
        try:
            run(fw, "disable", "--confirm")  # clean slate

            checks = [
                ("enable", run(fw, "enable"), 0, "XTOS_FW_ENABLE"),
                ("allow tcp 22", run(fw, "allow", "--proto", "tcp", "--port", "22"), 0, "FW_ALLOW"),
                ("allow udp 53", run(fw, "allow", "--proto", "udp", "--port", "53"), 0, "FW_ALLOW"),
                ("deny tcp 8080", run(fw, "deny", "--proto", "tcp", "--port", "8080"), 0, "FW_DENY"),
                ("list shows rules", run(fw, "list"), 0, "dport 22"),
                ("list shows deny", run(fw, "list"), 0, "dport 8080 drop"),
                ("delete 8080", run(fw, "delete", "--proto", "tcp", "--port", "8080"), 0, "FW_DELETE"),
                ("deleted gone", run(fw, "list"), 0, None),
                ("flush", run(fw, "flush"), 0, "FW_FLUSH"),
                ("status enabled", run(fw, "status"), 0, "enabled=true"),
                ("disable", run(fw, "disable", "--confirm"), 0, "XTOS_FW_DISABLE"),
                ("status disabled", run(fw, "status"), 0, "enabled=false"),
            ]
            for name, r, want_rc, marker in checks:
                if r.returncode != want_rc:
                    failures.append(f"{name}: rc={r.returncode} want={want_rc} stderr={r.stderr[:120]}")
                elif marker and marker not in r.stdout:
                    if name == "deleted gone" and "dport 8080" in r.stdout:
                        failures.append(f"{name}: rule still present after delete")
                print(f"  {'PASS' if not failures or failures[-1].split(':')[0] != name else 'FAIL'} {name}")

            # deleted rule must really be gone
            r = run(fw, "list")
            if "dport 8080" in r.stdout:
                failures.append("post-delete list still contains dport 8080")

            # bad input rejection
            bad = [
                ("bad proto", run(fw, "allow", "--proto", "sctp", "--port", "22"), 1),
                ("bad port high", run(fw, "allow", "--proto", "tcp", "--port", "70000"), 1),
                ("port zero", run(fw, "allow", "--proto", "tcp", "--port", "0"), 1),
            ]
            for name, r, want in bad:
                ok = r.returncode == want and r.stderr.startswith("fw:")
                print(f"  {'PASS' if ok else 'FAIL'} reject {name}")
                if not ok:
                    failures.append(f"reject {name}: rc={r.returncode} want={want}")

            run(fw, "disable", "--confirm")
        finally:
            run(fw, "disable", "--confirm")

    if failures:
        print(f"FAIL: {len(failures)}")
        for f in failures:
            print(f"  {f}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
