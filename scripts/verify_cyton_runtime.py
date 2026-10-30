#!/usr/bin/env python3
"""Verify the staged XTOS Cyton userspace runtime contract.

Static checks do not execute target code. The explicit --smoke option executes
trusted packaged code in chroot using the host kernel and synthetic packets.
Neither mode establishes guest boot, serial-device or hardware validation.
The staged tree must remain owned and unchanged during verification; chroot is
not a sandbox for untrusted binaries.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import signal
import subprocess

from userspace import resolve_root
from userspace_root import open_directory

SOURCE_DIR = Path("usr/share/xtos/eeg-pipeline")
FILTER_BINARY = Path("usr/bin/xtos-eeg-pipeline")
INTERPRETER = Path("usr/bin/python3")
SOURCES = ("openbci_cyton.py", "cyton_capture.py", "cyton_replay.py")
SMOKE_TIMEOUT_S = 30
# Sent on stdin, not installed as a guest system tool. All temporary output is
# confined to the selected build root and removed by the target interpreter.
SMOKE_SCRIPT = r'''
import ctypes, hashlib, json, os, pathlib, subprocess, sys, tempfile
if sys.version_info < (3, 10):
    raise RuntimeError("Python >= 3.10 is required")
getattr(ctypes.CDLL(None, use_errno=True), "renameat2")
source = pathlib.Path("/usr/share/xtos/eeg-pipeline")
sys.path.insert(0, str(source))
import openbci_cyton, cyton_capture, cyton_replay
with tempfile.TemporaryDirectory(prefix="cyton-smoke-", dir="/run") as temporary:
    base = pathlib.Path(temporary)
    read_fd, write_fd = os.pipe()
    try:
        packets = b"".join(bytes([0xA0, counter]) + b"\0" * 30 + b"\xC0"
                           for counter in range(4))
        os.write(write_fd, packets)
        capture = base / "capture"
        manifest = cyton_capture.capture_fd(read_fd, capture,
            device_label="synthetic-runtime-pipe", duration_s=0.05)
        if manifest["status"] != "completed" or manifest["stats"]["accepted_packets"] != 4:
            raise RuntimeError("synthetic capture failed")
    finally:
        os.close(write_fd)
        os.close(read_fd)
    result = subprocess.run([sys.executable, "-B", str(source / "cyton_replay.py"),
        "--capture", str(capture), "--output", str(base / "processed")],
        capture_output=True, text=True, timeout=10)
    if result.returncode or result.stderr:
        raise RuntimeError("packaged replay CLI failed: " + result.stderr)
    replay = json.loads(result.stdout)
    if replay["status"] != "processed" or replay["channel_pairs_processed"] != 4:
        raise RuntimeError("synthetic replay failed")
    for pair in range(4):
        if not (base / "processed" / ("pair-%d-filtered.csv" % pair)).is_file():
            raise RuntimeError("missing filtered output")
    checks = {"imports": True, "renameat2": True, "synthetic_capture": True,
              "default_replay_cli": True, "eight_channel_filter": True}
    print(json.dumps({"schema": "xtos-cyton-runtime-smoke-v1", "status": "pass",
        "checks": checks, "python_version": list(sys.version_info[:3]),
        "physical_device_evidence": False,
        "source_sha256": {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
            for name in ("openbci_cyton.py", "cyton_capture.py", "cyton_replay.py")},
        "filter_sha256": hashlib.sha256(pathlib.Path("/usr/bin/xtos-eeg-pipeline").read_bytes()).hexdigest(),
        "python_executable_sha256": hashlib.sha256(pathlib.Path(sys.executable).read_bytes()).hexdigest()}))
'''


def _smoke(root: Path) -> dict:
    process = subprocess.Popen(
        ["chroot", str(root), "/usr/bin/python3", "-I", "-B", "-"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
    timed_out = False
    try:
        try:
            stdout, stderr = process.communicate(SMOKE_SCRIPT, timeout=SMOKE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=3)
    finally:
        # Include any nested replay/filter children in cleanup, even if the
        # supervisor exited early. This does not claim protection from SIGKILL.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    detail = {"exit_code": process.returncode, "timed_out": timed_out,
              "stderr": stderr, "stdout": stdout, "scope": "chroot_host_kernel_synthetic"}
    if process.returncode or timed_out or stderr:
        return detail
    try:
        value = json.loads(stdout)
        required = ("imports", "renameat2", "synthetic_capture", "default_replay_cli", "eight_channel_filter")
        valid = (isinstance(value, dict)
                 and value.get("schema") == "xtos-cyton-runtime-smoke-v1"
                 and value.get("status") == "pass"
                 and value.get("physical_device_evidence") is False
                 and isinstance(value.get("checks"), dict)
                 and all(value["checks"].get(key) is True for key in required))
        if valid:
            version = value.get("python_version")
            valid = (isinstance(version, list) and len(version) == 3
                     and all(type(part) is int and part >= 0 for part in version)
                     and tuple(version[:2]) >= (3, 10)
                     and value.get("source_sha256") == {
                         name: hashlib.sha256((root / SOURCE_DIR / name).read_bytes()).hexdigest()
                         for name in SOURCES}
                     and value.get("filter_sha256") == hashlib.sha256((root / FILTER_BINARY).read_bytes()).hexdigest()
                     and value.get("python_executable_sha256") == hashlib.sha256(
                         resolve_root(root, "/" + INTERPRETER.as_posix()).read_bytes()).hexdigest())
        if valid:
            detail["result"] = value
    except (ValueError, RecursionError, OSError):
        pass
    return detail


def _regular(root: Path, relative: Path, errors: list[dict], *, executable=False) -> Path | None:
    path = root / relative
    try:
        for parent in reversed(relative.parents):
            if not stat.S_ISDIR((root / parent).lstat().st_mode):
                raise ValueError("ancestor must be a real non-symlink directory")
        info = path.lstat()
    except (OSError, ValueError) as exc:
        errors.append({"path": relative.as_posix(), "reason": str(exc)})
        return None
    if not stat.S_ISREG(info.st_mode):
        errors.append({"path": relative.as_posix(), "reason": "must be a regular non-symlink file"})
        return None
    if executable and not (info.st_mode & 0o111):
        errors.append({"path": relative.as_posix(), "reason": "must be executable"})
        return None
    return path


def verify(rootfs: Path, *, mode="runtime", smoke=False) -> dict:
    if mode not in ("runtime", "source-staged"):
        raise ValueError("invalid verification mode")
    if smoke and mode != "runtime":
        raise ValueError("--smoke requires runtime mode")
    root = Path(os.path.abspath(rootfs))
    errors: list[dict] = []
    report = {
        "schema": "xtos-cyton-runtime-v1",
        "status": "fail",
        "rootfs": str(root),
        "source_dir": "/" + SOURCE_DIR.as_posix(),
        "filter_binary": "/" + FILTER_BINARY.as_posix(),
        "interpreter": "/" + INTERPRETER.as_posix(),
        "mode": mode,
        "interpreter_checked": False,
        "interpreter_runtime_smoke": False,
        "physical_device_evidence": False,
        "errors": errors,
    }
    try:
        fd = open_directory(root)
        os.close(fd)
    except (OSError, ValueError) as exc:
        errors.append({"path": ".", "reason": str(exc)})
        return report
    for name in SOURCES:
        _regular(root, SOURCE_DIR / name, errors)
    _regular(root, FILTER_BINARY, errors, executable=True)
    if mode == "runtime":
        report["interpreter_checked"] = True
        try:
            target = resolve_root(root, "/" + INTERPRETER.as_posix())
            _regular(root, target.relative_to(root), errors, executable=True)
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append({"path": INTERPRETER.as_posix(), "reason": str(exc)})
    if smoke and not errors:
        if os.geteuid() != 0:
            errors.append({"path": ".", "reason": "chroot smoke requires root"})
        else:
            try:
                report["smoke"] = _smoke(root)
                report["interpreter_runtime_smoke"] = "result" in report["smoke"]
                if not report["interpreter_runtime_smoke"]:
                    errors.append({"path": "runtime", "reason": "chroot smoke failed"})
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append({"path": "runtime", "reason": str(exc)})
    report["status"] = "fail" if errors else "pass"
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rootfs", required=True, type=Path)
    parser.add_argument("--mode", choices=("runtime", "source-staged"), default="runtime")
    parser.add_argument("--smoke", action="store_true", help="Execute trusted staged code in chroot (root required)")
    args = parser.parse_args(argv)
    if args.smoke and args.mode != "runtime":
        parser.error("--smoke requires runtime mode")
    report = verify(args.rootfs, mode=args.mode, smoke=args.smoke)
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
