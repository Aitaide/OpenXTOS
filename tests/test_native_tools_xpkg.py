#!/usr/bin/env python3
"""pkg app (xpkg) tests: install/list/verify/info/remove round-trip plus
rejection of unsafe or corrupt packages (traversal, shadows, symlinks,
hardlinks, setuid, hash mismatch, set mismatch, duplicate install).

Runs as the current user with XTOS_APP_ROOT pointing at a temp dir, so no
root and no installed XTOS image are required.
"""
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "tools" / "xtos-native" / "pkg" / "pkg.c"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def build_xpkg(path, *, name="demo-app", version="1.0.0", arch="x86_64",
               payload=None, omit_sums=False, corrupt_sums=False,
               skip_in_sums=(), extra_raw=(), manifest_override=None,
               tar_format=None, compresslevel=None, signing_key=None,
               signature_bytes=None):
    payload = payload if payload is not None else {
        "bin/tool": b"#!/bin/sh\necho hello\n",
        "share/data.txt": b"0123456789abcdef",
    }
    files_meta = [{"path": f"payload/{rel}", "sha256": sha(data), "size": len(data)}
                  for rel, data in sorted(payload.items())]
    manifest = {
        "schema_version": 1, "name": name, "version": version, "architecture": arch,
        "summary": "Fixture application", "license": "MIT", "vendor": "XTOS test",
        "xtos_min_version": "1.0.0-beta.4", "files": files_meta,
    }
    if manifest_override:
        manifest.update(manifest_override)
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    signature = signature_bytes
    if signing_key:
        # ed25519 detached signature over the exact manifest bytes — the same
        # construction scripts/make_xpkg.py produces and pkg verifies in C.
        with tempfile.TemporaryDirectory() as sdir:
            msg = Path(sdir) / "manifest.json"
            msg.write_bytes(manifest_bytes)
            sigp = Path(sdir) / "sig.bin"
            subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(signing_key),
                            "-rawin", "-in", str(msg), "-out", str(sigp)], check=True)
            signature = sigp.read_bytes()
    sums_lines = [f"{sha(data)}  payload/{rel}" for rel, data in sorted(payload.items())
                  if rel not in skip_in_sums]
    if corrupt_sums:
        sums_lines = [f"{'0' * 64}  payload/{rel}" if rel == "share/data.txt" else f"{sha(data)}  payload/{rel}"
                      for rel, data in sorted(payload.items())]
    sums_bytes = ("\n".join(sums_lines) + "\n").encode()
    open_kwargs = {}
    if tar_format is not None:
        open_kwargs["format"] = tar_format
    if compresslevel is not None:
        open_kwargs["compresslevel"] = compresslevel
    with tarfile.open(path, "w:gz", **open_kwargs) as tar:
        def add_dir(arcname):
            info = tarfile.TarInfo(arcname)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)

        def add_file(arcname, data, mode=0o644):
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            info.mode = mode
            tar.addfile(info, io.BytesIO(data))

        add_dir("payload/")
        add_dir("xpkg/")
        seen_dirs = set()
        for rel in sorted(payload):
            parts = rel.split("/")
            for i in range(1, len(parts)):
                d = "payload/" + "/".join(parts[:i]) + "/"
                if d not in seen_dirs:
                    seen_dirs.add(d)
                    add_dir(d)
        for rel, data in sorted(payload.items()):
            add_file(f"payload/{rel}", data, 0o755 if rel.startswith("bin/") else 0o644)
        if not omit_sums:
            add_file("xpkg/SHA256SUMS", sums_bytes)
        add_file("xpkg/manifest.json", manifest_bytes)
        if signature is not None:
            add_file("xpkg/signature.bin", signature)
        for info, data in extra_raw:
            tar.addfile(info, io.BytesIO(data) if info.size > 0 else None)


def raw_member(arcname, *, type_=tarfile.REGTYPE, mode=0o644, linkname="", size=0, data=b""):
    info = tarfile.TarInfo(arcname)
    info.type = type_
    info.mode = mode
    info.linkname = linkname
    info.size = size
    return info, data


def main() -> int:
    if sys.platform != "linux":
        print("skip: linux only")
        return 0
    if subprocess.run(["gcc", "--version"], capture_output=True).returncode != 0:
        print("skip: gcc unavailable")
        return 0
    failures = []
    with tempfile.TemporaryDirectory(prefix="xtos-xpkg-test-") as td:
        td = Path(td)
        binary = td / "pkg"
        subprocess.run(["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-static",
                        "-I", str(ROOT / "tools" / "xtos-native" / "common"),
                        "-I", str(ROOT / "tools" / "xtos-native" / "vendor" / "monocypher"),
                        "-o", str(binary), str(SRC),
                        str(ROOT / "tools" / "xtos-native" / "vendor" / "monocypher" / "monocypher.c"),
                        str(ROOT / "tools" / "xtos-native" / "vendor" / "monocypher" / "monocypher-ed25519.c")],
                       check=True)
        approot = td / "apps"
        approot.mkdir()

        def run(*args, stdin_bytes=None):
            env = dict(os.environ)
            env["XTOS_APP_ROOT"] = str(approot)
            return subprocess.run([str(binary), *args], capture_output=True,
                                   text=True, env=env, timeout=300)

        def expect_fail(r, label):
            if r.returncode == 0:
                failures.append(f"{label}: expected failure, got rc=0 out={r.stdout!r}")
            elif any(approot.glob(".stage-*")):
                failures.append(f"{label}: staging dir left behind")

        good = td / "demo-app-1.0.0.xpkg"
        build_xpkg(good)

        # 1. clean install
        r = run("app", "install", str(good))
        if r.returncode != 0 or "XTOS_PKG_APP_INSTALL name=demo-app version=1.0.0" not in r.stdout:
            failures.append(f"install failed: rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")
        else:
            print("  PASS install")
            tool = approot / "demo-app/1.0.0/payload/bin/tool"
            if not tool.is_file() or tool.read_bytes() != b"#!/bin/sh\necho hello\n":
                failures.append("installed payload bytes differ")
            reg = json.loads((approot / "registry.json").read_text())
            if reg["apps"]["demo-app"]["version"] != "1.0.0" or reg["apps"]["demo-app"]["files"] != 2:
                failures.append(f"registry entry wrong: {reg}")
            if len(reg["apps"]["demo-app"]["xpkg_sha256"]) != 64:
                failures.append("registry xpkg sha missing")

        # 2. list
        r = run("app", "list")
        if "demo-app" not in r.stdout or "installed" not in r.stdout:
            failures.append(f"list missing app: {r.stdout!r}")
        else:
            print("  PASS list")
        if "\x1b" in r.stdout:
            failures.append("piped list output contains ANSI escapes")
        r = run("--json", "app", "list")
        rows = json.loads(r.stdout)
        if not rows or rows[0]["name"] != "demo-app" or rows[0]["status"] != "installed":
            failures.append(f"json list wrong: {r.stdout!r}")

        # 3. info
        r = run("app", "info", "demo-app")
        if "1.0.0" not in r.stdout or "MIT" not in r.stdout or "Fixture application" not in r.stdout:
            failures.append(f"info incomplete: {r.stdout!r}")
        else:
            print("  PASS info")

        # 4. verify clean
        r = run("app", "verify", "demo-app")
        if r.returncode != 0 or "corrupt=0" not in r.stdout or "missing=0" not in r.stdout or "extra=0" not in r.stdout:
            failures.append(f"verify clean failed: {r.stdout!r}")
        else:
            print("  PASS verify clean")

        # 5. tamper detection + restore
        victim = approot / "demo-app/1.0.0/payload/share/data.txt"
        backup = victim.read_bytes()
        try:
            victim.write_bytes(backup + b"X")
            r = run("app", "verify", "demo-app")
            if r.returncode == 0 or "corrupt=1" not in r.stdout:
                failures.append(f"tamper not detected: {r.stdout!r}")
            else:
                print("  PASS tamper detection")
        finally:
            victim.write_bytes(backup)
        r = run("app", "verify", "demo-app")
        if r.returncode != 0:
            failures.append(f"restore verify failed: {r.stdout!r}")
        else:
            print("  PASS restore clean")

        # 6. duplicate install refused
        r = run("app", "install", str(good))
        expect_fail(r, "duplicate install")
        if "already installed" not in r.stderr:
            failures.append(f"duplicate install message: {r.stderr!r}")
        else:
            print("  PASS duplicate install refused")

        # 7. remove + post-remove state
        r = run("app", "remove", "demo-app")
        if r.returncode != 0 or "XTOS_PKG_APP_REMOVE name=demo-app" not in r.stdout:
            failures.append(f"remove failed: {r.stdout!r} err={r.stderr!r}")
        else:
            print("  PASS remove")
        if (approot / "demo-app").exists():
            failures.append("app dir still present after remove")
        reg = json.loads((approot / "registry.json").read_text())
        if reg["apps"]:
            failures.append(f"registry not emptied: {reg}")
        r = run("app", "verify", "demo-app")
        if r.returncode == 0:
            failures.append("verify of removed app unexpectedly succeeded")

        # 8. corrupt SHA256SUMS
        bad = td / "bad-hash.xpkg"
        build_xpkg(bad, corrupt_sums=True)
        expect_fail(run("app", "install", str(bad)), "corrupt sums")
        print("  PASS corrupt sums rejected")

        # 9. payload file missing from sums
        skip = td / "skip-sums.xpkg"
        build_xpkg(skip, skip_in_sums=("share/data.txt",))
        expect_fail(run("app", "install", str(skip)), "sums set mismatch")
        print("  PASS sums set mismatch rejected")

        # 10. missing SHA256SUMS
        nosums = td / "no-sums.xpkg"
        build_xpkg(nosums, omit_sums=True)
        expect_fail(run("app", "install", str(nosums)), "missing sums")
        print("  PASS missing sums rejected")

        # 11. symlink member
        sym = td / "symlink.xpkg"
        build_xpkg(sym, extra_raw=[raw_member("payload/evil", type_=tarfile.SYMTYPE, linkname="/etc/passwd")])
        expect_fail(run("app", "install", str(sym)), "symlink member")
        print("  PASS symlink member rejected")

        # 12. hardlink member
        hard = td / "hardlink.xpkg"
        build_xpkg(hard, extra_raw=[raw_member("payload/hl", type_=tarfile.LNKTYPE, linkname="payload/bin/tool")])
        expect_fail(run("app", "install", str(hard)), "hardlink member")
        print("  PASS hardlink member rejected")

        # 13. traversal member
        trav = td / "traversal.xpkg"
        build_xpkg(trav, extra_raw=[raw_member("../evil.txt", data=b"x", size=1)])
        expect_fail(run("app", "install", str(trav)), "traversal member")
        print("  PASS traversal member rejected")

        # 14. shadow member (regular file used as directory prefix)
        shadow = td / "shadow.xpkg"
        with tarfile.open(shadow, "w:gz") as tar:
            info = tarfile.TarInfo("payload/dirx")
            info.size = 3
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(b"abc"))
            info2 = tarfile.TarInfo("payload/dirx/deeper.txt")
            info2.size = 4
            info2.mode = 0o644
            tar.addfile(info2, io.BytesIO(b"ABCD"))
        expect_fail(run("app", "install", str(shadow)), "shadow member")
        print("  PASS shadow member rejected")

        # 15. setuid payload
        suid = td / "suid.xpkg"
        payload = {"bin/privileged": b"#!/bin/sh\n"}
        files_meta = [{"path": "payload/bin/privileged", "sha256": sha(payload["bin/privileged"]),
                       "size": len(payload["bin/privileged"])}]
        with tarfile.open(suid, "w:gz") as tar:
            d = tarfile.TarInfo("payload/")
            d.type = tarfile.DIRTYPE
            d.mode = 0o755
            tar.addfile(d)
            d2 = tarfile.TarInfo("xpkg/")
            d2.type = tarfile.DIRTYPE
            d2.mode = 0o755
            tar.addfile(d2)
            f = tarfile.TarInfo("payload/bin/privileged")
            f.size = len(payload["bin/privileged"])
            f.mode = 0o4755
            tar.addfile(f, io.BytesIO(payload["bin/privileged"]))
            manifest = {"schema_version": 1, "name": "suid-app", "version": "1.0.0",
                        "architecture": "x86_64", "files": files_meta}
            m = tarfile.TarInfo("xpkg/manifest.json")
            mb = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
            m.size = len(mb)
            m.mode = 0o644
            tar.addfile(m, io.BytesIO(mb))
            s = tarfile.TarInfo("xpkg/SHA256SUMS")
            sb = f"{sha(payload['bin/privileged'])}  payload/bin/privileged\n".encode()
            s.size = len(sb)
            s.mode = 0o644
            tar.addfile(s, io.BytesIO(sb))
        expect_fail(run("app", "install", str(suid)), "setuid payload")
        print("  PASS setuid payload rejected")

        # 16. invalid name token in manifest
        badname = td / "badname.xpkg"
        build_xpkg(badname, name="Bad_Name")
        expect_fail(run("app", "install", str(badname)), "invalid name")
        print("  PASS invalid app name rejected")

        # 17. junk archive is not an xpkg
        junk = td / "junk.xpkg"
        junk.write_bytes(b"not a tar at all")
        expect_fail(run("app", "install", str(junk)), "junk archive")
        print("  PASS junk archive rejected")

        # 18. final state clean: no stages, no stray installs
        if any(approot.glob(".stage-*")):
            failures.append("staging dirs left behind at end")
        r = run("app", "list")
        if "demo-app" in r.stdout and "unregistered" not in r.stdout:
            # registry is authoritative; after removal nothing should remain
            failures.append(f"list shows removed app: {r.stdout!r}")

        # 19. upgrade happy path: install v1 then upgrade to v2 atomically
        r = run("app", "install", str(good))
        if r.returncode != 0:
            failures.append(f"upgrade-prep install failed: {r.stderr!r}")
        v2 = td / "demo-app-2.0.0.xpkg"
        v2_payload = {"bin/tool": b"#!/bin/sh\necho v2\n", "share/data.txt": b"v2-data-0123456789"}
        build_xpkg(v2, version="2.0.0", payload=v2_payload)
        r = run("app", "upgrade", str(v2))
        if r.returncode != 0 or "XTOS_PKG_APP_UPGRADE name=demo-app version=2.0.0" not in r.stdout \
                or "from_version=1.0.0" not in r.stdout:
            failures.append(f"upgrade failed: rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")
        else:
            print("  PASS upgrade")
            tool = approot / "demo-app/2.0.0/payload/bin/tool"
            if not tool.is_file() or tool.read_bytes() != v2_payload["bin/tool"]:
                failures.append("upgraded payload bytes differ")
            if (approot / "demo-app/1.0.0").exists():
                failures.append("old version dir still present after upgrade")
            reg = json.loads((approot / "registry.json").read_text())
            if reg["apps"]["demo-app"]["version"] != "2.0.0":
                failures.append(f"registry version not upgraded: {reg}")
            r = run("app", "verify", "demo-app")
            if r.returncode != 0 or "corrupt=0" not in r.stdout:
                failures.append(f"verify after upgrade failed: {r.stdout!r}")
            else:
                print("  PASS verify after upgrade")

        # 20. upgrade to the already-installed version refused
        r = run("app", "upgrade", str(v2))
        expect_fail(r, "same-version upgrade")
        if "already at version 2.0.0" not in r.stderr:
            failures.append(f"same-version upgrade message: {r.stderr!r}")
        else:
            print("  PASS same-version upgrade refused")

        # 21. upgrade of a not-installed app refused
        other = td / "other-app-1.0.0.xpkg"
        build_xpkg(other, name="other-app", version="1.0.0")
        r = run("app", "upgrade", str(other))
        expect_fail(r, "upgrade not installed")
        if "is not installed" not in r.stderr:
            failures.append(f"upgrade-not-installed message: {r.stderr!r}")
        else:
            print("  PASS upgrade of uninstalled app refused")

        # 22. corrupt upgrade package leaves the current version intact
        bad3 = td / "demo-app-3.0.0-bad.xpkg"
        build_xpkg(bad3, version="3.0.0", corrupt_sums=True)
        r = run("app", "upgrade", str(bad3))
        expect_fail(r, "corrupt upgrade")
        reg = json.loads((approot / "registry.json").read_text())
        if reg["apps"]["demo-app"]["version"] != "2.0.0":
            failures.append(f"corrupt upgrade changed registry: {reg}")
        if (approot / "demo-app/3.0.0").exists():
            failures.append("corrupt upgrade left a new version dir")
        r = run("app", "verify", "demo-app")
        if r.returncode != 0:
            failures.append("current version damaged by failed upgrade")
        else:
            print("  PASS corrupt upgrade rejected, current version intact")

        # 23. stale target left by an interrupted upgrade is cleaned up
        stale = approot / "demo-app/3.0.0"
        stale.mkdir(parents=True)
        (stale / "junk").write_bytes(b"stale")
        v3 = td / "demo-app-3.0.0.xpkg"
        build_xpkg(v3, version="3.0.0", payload={"bin/tool": b"#!/bin/sh\necho v3\n"})
        r = run("app", "upgrade", str(v3))
        if r.returncode != 0:
            failures.append(f"stale-target upgrade failed: {r.stderr!r}")
        elif (stale / "junk").exists():
            failures.append("stale target junk survived upgrade")
        else:
            print("  PASS stale target cleaned on upgrade")

        # 24. final state after upgrade section
        if any(approot.glob(".stage-*")):
            failures.append("staging dirs left behind after upgrade tests")
        r = run("app", "list")
        if "demo-app" not in r.stdout or "3.0.0" not in r.stdout:
            failures.append(f"final list missing upgraded app: {r.stdout!r}")

        # 25. PAX long-name member (>100 chars) via the native reader
        long_key = ("d" * 90) + "/" + ("e" * 40) + ".txt"
        long_bytes = b"deep pax payload\n"
        paxpkg = td / "long-pax.xpkg"
        build_xpkg(paxpkg, version="1.0.0", payload={long_key: long_bytes},
                   tar_format=tarfile.PAX_FORMAT)
        r = run("app", "upgrade", str(paxpkg))
        if r.returncode != 0:
            failures.append(f"pax long-name install failed: rc={r.returncode} err={r.stderr!r}")
        else:
            f = approot / "demo-app/1.0.0/payload" / long_key
            if not f.is_file() or f.read_bytes() != long_bytes:
                failures.append("pax long-name payload bytes differ")
            else:
                print("  PASS pax long-name member")

        # 26. GNU long-name member ('L' meta) via the native reader
        gnupkg = td / "long-gnu.xpkg"
        build_xpkg(gnupkg, version="1.0.1", payload={long_key: long_bytes},
                   tar_format=tarfile.GNU_FORMAT)
        r = run("app", "upgrade", str(gnupkg))
        if r.returncode != 0:
            failures.append(f"gnu long-name install failed: rc={r.returncode} err={r.stderr!r}")
        else:
            f = approot / "demo-app/1.0.1/payload" / long_key
            if not f.is_file() or f.read_bytes() != long_bytes:
                failures.append("gnu long-name payload bytes differ")
            else:
                print("  PASS gnu long-name member")

        # 27. stored deflate blocks (compresslevel=0)
        storedpkg = td / "stored.xpkg"
        build_xpkg(storedpkg, version="1.0.2",
                   payload={"bin/tool": b"#!/bin/sh\necho stored\n"}, compresslevel=0)
        r = run("app", "upgrade", str(storedpkg))
        if r.returncode != 0:
            failures.append(f"stored-block install failed: {r.stderr!r}")
        else:
            print("  PASS stored-block member")

        # 28. corrupted compressed stream is rejected (native CRC check)
        raw = bytearray(good.read_bytes())
        raw[len(raw) // 2] ^= 0xff
        corruptgz = td / "corrupt-gz.xpkg"
        corruptgz.write_bytes(bytes(raw))
        expect_fail(run("app", "upgrade", str(corruptgz)), "corrupt gzip stream")
        print("  PASS corrupt gzip stream rejected")

        # 29. large compressible member spans the output-buffer compaction
        big_data = ("\n".join(f"row {i:06d} of compressible text" for i in range(20000))).encode()
        bigpkg = td / "big.xpkg"
        build_xpkg(bigpkg, version="1.0.3", payload={"share/big.txt": big_data})
        r = run("app", "upgrade", str(bigpkg))
        if r.returncode != 0:
            failures.append(f"large member install failed: {r.stderr!r}")
        else:
            f = approot / "demo-app/1.0.3/payload/share/big.txt"
            if not f.is_file() or f.read_bytes() != big_data:
                failures.append("large member bytes differ")
            else:
                print("  PASS large member")

        # 30. final state after native-reader section
        if any(approot.glob(".stage-*")) or any(approot.glob(".spool-*")):
            failures.append("staging/spool leftovers after native-reader tests")
        r = run("app", "verify", "demo-app")
        if r.returncode != 0 or "corrupt=0" not in r.stdout:
            failures.append(f"final verify failed: {r.stdout!r}")

        # ---- third-party signatures (v1.1): key -> manifest -> payload ----
        devkey = td / "dev-ed25519.pem"
        devpub = td / "dev-pub.pem"
        otherkey = td / "other-ed25519.pem"
        otherpub = td / "other-pub.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(devkey)],
                       check=True, capture_output=True)
        subprocess.run(["openssl", "pkey", "-in", str(devkey), "-pubout", "-out", str(devpub)],
                       check=True, capture_output=True)
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(otherkey)],
                       check=True, capture_output=True)
        subprocess.run(["openssl", "pkey", "-in", str(otherkey), "-pubout", "-out", str(otherpub)],
                       check=True, capture_output=True)

        # 31. signed package installs without a key too (signature is inert metadata)
        signed1 = td / "signed-1.0.0.xpkg"
        build_xpkg(signed1, name="vendor-app", version="1.0.0", signing_key=devkey)
        r = run("app", "install", str(signed1))
        if r.returncode != 0:
            failures.append(f"31 signed-install-nokey failed: {r.stderr!r}")
        elif "status=installed" not in r.stdout:
            failures.append(f"31 signed install without key bad output: {r.stdout!r}")
        else:
            r = run("--json", "app", "info", "vendor-app")
            if '"version": "1.0.0"' not in r.stdout:
                failures.append(f"31 app info failed after keyless signed install: {r.stdout!r}")
            else:
                print("  PASS signed install without key")

        # 32. install with --pubkey verifies and reports it
        signed2 = td / "signed-2.0.0.xpkg"
        build_xpkg(signed2, name="vendor-app2", version="2.0.0", signing_key=devkey)
        r = run("--json", "app", "install", str(signed2), "--pubkey", str(devpub))
        if r.returncode != 0 or '"third_party_signed": true' not in r.stdout:
            failures.append(f"32 signed install with key failed: {r.stdout!r} {r.stderr!r}")
        else:
            print("  PASS signed install with key")

        # 33. wrong key is refused, nothing left behind (fresh app so existence is meaningful)
        signed3 = td / "signed-3.0.0.xpkg"
        build_xpkg(signed3, name="vendor-app7", version="3.0.0", signing_key=devkey)
        r = run("app", "install", str(signed3), "--pubkey", str(otherpub))
        expect_fail(r, "33 wrong-key install")
        if "signature verification failed" not in r.stderr:
            failures.append(f"33 unexpected refusal text: {r.stderr!r}")
        if (approot / "vendor-app7").exists():
            failures.append("33 wrong-key install left an app directory")
        if any(approot.glob(".stage-*")):
            failures.append("33 wrong-key install left staging dir")

        # 34. signature over a different manifest must not verify
        with tempfile.TemporaryDirectory() as sdir:
            msg = Path(sdir) / "manifest.json"
            msg.write_bytes(b'{"decoy": true}\n')
            sigp = Path(sdir) / "sig.bin"
            subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(devkey),
                            "-rawin", "-in", str(msg), "-out", str(sigp)], check=True)
            decoy_sig = sigp.read_bytes()
        decoy = td / "decoy-signed.xpkg"
        build_xpkg(decoy, name="vendor-app3", version="1.0.0", signature_bytes=decoy_sig)
        r = run("app", "install", str(decoy), "--pubkey", str(devpub))
        expect_fail(r, "34 decoy signature")
        if "signature verification failed" not in r.stderr:
            failures.append(f"34 unexpected refusal text: {r.stderr!r}")

        # 35. --pubkey on an unsigned package is refused
        unsigned = td / "unsigned-1.0.0.xpkg"
        build_xpkg(unsigned, name="vendor-app4", version="1.0.0")
        r = run("app", "install", str(unsigned), "--pubkey", str(devpub))
        expect_fail(r, "35 pubkey on unsigned package")
        if "no xpkg/signature.bin" not in r.stderr:
            failures.append(f"35 unexpected refusal text: {r.stderr!r}")

        # 36. malformed signature (63 bytes) is refused
        short = td / "short-sig.xpkg"
        build_xpkg(short, name="vendor-app5", version="1.0.0", signature_bytes=b"S" * 63)
        r = run("app", "install", str(short), "--pubkey", str(devpub))
        expect_fail(r, "36 short signature")
        if "exactly 64 bytes" not in r.stderr:
            failures.append(f"36 unexpected refusal text: {r.stderr!r}")

        # 37. signed upgrade path: install signed 1.0.0 with key, upgrade signed 1.1.0 with key
        up1 = td / "up-1.0.0.xpkg"
        build_xpkg(up1, name="vendor-app6", version="1.0.0", signing_key=devkey)
        r = run("app", "install", str(up1), "--pubkey", str(devpub))
        if r.returncode != 0:
            failures.append(f"37 signed base install failed: {r.stderr!r}")
        up2 = td / "up-1.1.0.xpkg"
        build_xpkg(up2, name="vendor-app6", version="1.1.0", signing_key=devkey)
        r = run("app", "upgrade", str(up2), "--pubkey", str(devpub))
        if r.returncode != 0 or "third-party-signature=verified" not in r.stdout:
            failures.append(f"37 signed upgrade failed: {r.stdout!r} {r.stderr!r}")
        else:
            print("  PASS signed upgrade with key")

        # 38. wrong-key upgrade refused: current version stays authoritative
        up3 = td / "up-2.0.0.xpkg"
        build_xpkg(up3, name="vendor-app6", version="2.0.0", signing_key=otherkey)
        r = run("app", "upgrade", str(up3), "--pubkey", str(devpub))
        expect_fail(r, "38 wrong-key upgrade")
        r = run("app", "info", "vendor-app6")
        if "1.1.0" not in r.stdout:
            failures.append(f"38 registry did not stay at 1.1.0: {r.stdout!r}")

        # 39. the app root's PARENT may not exist in the running system
        # (the live image carries no /opt unless the tool creates it):
        # install must mkdir -p the whole chain, device parity with the
        # in-guest proof that found this.
        nested = td / "nested-root" / "deep" / "xtos-apps"  # intentionally NOT created
        env2 = dict(os.environ)
        env2["XTOS_APP_ROOT"] = str(nested)
        r2 = subprocess.run([str(binary), "app", "install", str(signed1)],
                            capture_output=True, text=True, env=env2, timeout=300)
        if r2.returncode != 0:
            failures.append(f"39 install with missing parent dirs failed: {r2.stderr!r}")
        elif not (nested / "vendor-app" / "1.0.0").is_dir():
            failures.append("39 nested app tree not created")
        else:
            print("  PASS install creates missing app-root parents")

    if failures:
        print(f"FAIL: {len(failures)}")
        for f in failures:
            print("  " + f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
