#!/usr/bin/env python3
"""liblsl build acceptance on the control host.

Verifies the pinned-source build script reproduces the locked artifact and a
C++ outlet->resolve->inlet round-trip returns the pushed sample on localhost
(KnownPeers={127.0.0.1}; no multicast dependency in the sandbox).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = Path(os.environ.get("XTOS_LSL_DIR", ROOT / ".build/userspace/live-developer-lsl"))
BUILD_SH = ROOT / "tools/lsl/build_lsl.sh"
LIB_SHA = "75c0aeb9df2992e19cc7034f6f9a51f45dd61bba6dc3d29b5a38917d575090c1"

PAIR_SRC = r'''
#include <stdio.h>
#include <chrono>
#include <thread>
#include <lsl_cpp.h>
int main(void) {
    try {
        lsl::stream_info info("XtosTest", "EEG", 1, 100.0, lsl::cf_float32, "xtos-test-1");
        lsl::stream_outlet outlet(info);
        std::this_thread::sleep_for(std::chrono::seconds(1));
        auto found = lsl::resolve_stream("name", "XtosTest", 1, 10.0);
        if (found.empty()) { fprintf(stderr, "resolve_failed\n"); return 1; }
        lsl::stream_inlet inlet(found[0]);
        inlet.open_stream(5.0);
        float sample[1] = {3.14f};
        for (int i = 0; i < 5; i++) { outlet.push_sample(sample); std::this_thread::sleep_for(std::chrono::milliseconds(100)); }
        float got[1] = {0};
        double t = inlet.pull_sample(got, 1, 10.0);
        printf("pair_ok=%f t=%f\n", got[0], t);
        return (t != 0 && got[0] > 3.0) ? 0 : 1;
    } catch (const std::exception &e) {
        fprintf(stderr, "pair_error=%s\n", e.what());
        return 1;
    }
}
'''


def main() -> int:
    if not (WORK / "src/.git").is_dir():
        print("skip: pinned liblsl source not cloned")
        return 0
    if shutil.which("cmake") is None or shutil.which("g++") is None:
        print("skip: cmake/g++ unavailable")
        return 0

    failures = []

    r = subprocess.run(["bash", str(BUILD_SH)], capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        print(f"FAIL: build_lsl.sh rc={r.returncode}")
        print(r.stderr[-800:])
        return 1
    json_line = next((line for line in reversed(r.stdout.splitlines()) if line.startswith('{"component"')), None)
    if json_line is None:
        print(f"FAIL: build script produced no report line\n{r.stdout[-500:]}")
        return 1
    report = json.loads(json_line)
    print(f"  built: {report['component']} {report['version']} ({report['commit'][:12]})")
    if report["lib_sha256"] != LIB_SHA:
        failures.append(f"lib hash drifted: {report['lib_sha256']}")
    ver = subprocess.run([str(WORK / "install/bin/lslver")], capture_output=True, text=True, timeout=30)
    if ver.returncode != 0 or "117" not in ver.stdout:
        failures.append(f"lslver: rc={ver.returncode} {ver.stdout!r}")

    with tempfile.TemporaryDirectory(prefix="lsl-pair-") as td:
        cfg = Path(td) / "lsl_api.cfg"
        cfg.write_text("[lab]\nKnownPeers={127.0.0.1}\n")
        src = Path(td) / "pair.cpp"
        src.write_text(PAIR_SRC)
        binary = Path(td) / "pair"
        cc = subprocess.run(["g++", "-O2", "-I", str(WORK / "install/include"), str(src),
                             "-o", str(binary), "-L", str(WORK / "install/lib"), "-llsl"],
                            capture_output=True, text=True, timeout=120)
        if cc.returncode != 0:
            failures.append(f"pair compile: {cc.stderr[:200]}")
        else:
            env = dict(os.environ, LSLAPICFG=str(cfg),
                       LD_LIBRARY_PATH=str(WORK / "install/lib"))
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60, env=env)
            print(f"  pair: {(run.stdout or run.stderr).strip()[:60]} rc={run.returncode}")
            if run.returncode != 0 or "pair_ok=3.14" not in run.stdout:
                failures.append(f"round-trip failed: rc={run.returncode} {run.stdout!r} {run.stderr[-200:]!r}")

    if failures:
        print(f"FAIL: {len(failures)}")
        for f in failures:
            print("  " + f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
