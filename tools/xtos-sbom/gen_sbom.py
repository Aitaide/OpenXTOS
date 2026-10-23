#!/usr/bin/env python3
"""Generate the XTOS SBOM in CycloneDX 1.5 JSON format.

Covers everything shipped in the repository and produced by the system
build: the vendored Linux kernel (with PREEMPT_RT patch and XTOS integration
patch), BusyBox, the three XTOS SDK libraries, the user-space tools, and the
XTOS kernel components. Machine-readable output is written to
docs/sbom.json; a human summary is printed to stdout.

Python dependencies (onnxruntime, torch, tensorflow, numpy) are OPTIONAL
baseline components executed in isolated environments and are not part of
the shipped system image; they are listed with scope 'excluded' so auditors
see them but tools do not count them in the shipped inventory.

Run:  python3 tools/xtos-sbom/gen_sbom.py [--out docs/sbom.json]
"""

import argparse
import datetime
import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def project_version() -> str:
    """Read the XTOS project version from the VERSION file."""
    line = ""
    for raw in (ROOT / "VERSION").read_text().splitlines():
        raw = raw.strip()
        if raw and not raw.startswith("#") and not line:
            line = raw
    return line.split("=", 1)[1].strip() if "=" in line else "unknown"

LICENSE_KERNEL = "GPL-2.0 WITH Linux-syscall-note"
LICENSE_GPL2 = "GPL-2.0-only"
LICENSE_APACHE = "Apache-2.0"

# (name, version, purl-ish download, license, description, scope)
COMPONENTS = [
    {
        "name": "linux",
        "version": "6.6.0",
        "extra_version": "-rt15",
        "license": LICENSE_KERNEL,
        "description": "Linux kernel source vendored in kernel/linux (upstream 6.6.0)",
        "scope": "required",
        "upstream": "https://kernel.org/pub/linux/kernel/v6.x/linux-6.6.0.tar.xz",
        "path": "kernel/linux",
        "hash_globs": ["Makefile", "COPYING"],
    },
    {
        "name": "preempt-rt",
        "version": "6.6-rt15",
        "license": LICENSE_GPL2,
        "description": "PREEMPT_RT realtime patch set applied to the vendored kernel",
        "scope": "required",
        "upstream": "https://cdn.kernel.org/pub/linux/kernel/projects/rt/6.6/older/patch-6.6-rt15.patch.xz",
        "path": "kernel/patches/preempt-rt-6.6-rt15.patch",
        "hash_globs": ["kernel/patches/preempt-rt-6.6-rt15.patch"],
    },
    {
        "name": "xtos-linux-integration",
        "version": "1.0.1",
        "license": LICENSE_GPL2,
        "description": ("XTOS kernel integration patch: scheduler helper, BCI core, "
                        "BCI buffer, OpenBCI Cyton serdev driver, UAPI headers"),
        "scope": "required",
        "path": "kernel/patches/xtos-linux-6.6.patch",
        "hash_globs": ["kernel/patches/xtos-linux-6.6.patch"],
    },
    {
        "name": "busybox",
        "version": "1.37.0",
        "license": LICENSE_GPL2,
        "description": "BusyBox userland vendored in userspace/busybox",
        "scope": "required",
        "upstream": "https://busybox.net/downloads/busybox-1.37.0.tar.bz2",
        "upstream_sha256": "3311dff32e746499f4df0d5df04d7eb396382d7e108bb9250e7b519b837043a4",
        "path": "userspace/busybox",
        "hash_globs": ["LICENSE"],
    },
    {
        "name": "libxtos-rt",
        "version": "1.0.1",
        "license": LICENSE_APACHE,
        "description": "XTOS real-time runtime library (scheduling helpers, SPSC ring queue)",
        "scope": "required",
        "path": "sdk/libxtos-rt",
        "hash_globs": ["src/rt.c", "src/spsc.c"],
    },
    {
        "name": "libxtos-signal",
        "version": "1.0.1",
        "license": LICENSE_APACHE,
        "description": "XTOS deterministic signal processing library (FFT, notch, bandpass, SSVEP, P300)",
        "scope": "required",
        "path": "sdk/libxtos-signal",
        "hash_globs": ["src/signal.c"],
    },
    {
        "name": "libxtos-bci",
        "version": "1.0.1",
        "license": LICENSE_APACHE,
        "description": "XTOS BCI session library (device open, ioctl wrappers, impedance/stat APIs)",
        "scope": "required",
        "path": "sdk/libxtos-bci",
        "hash_globs": ["src/session.c"],
    },
    {
        "name": "xtos-eeg-pipeline",
        "version": "1.0.1",
        "license": LICENSE_APACHE,
        "description": "Virtual EEG CSV pipeline tool (parse, SPSC, bandpass, latency stats)",
        "scope": "required",
        "path": "tools/xtos-eeg-pipeline",
        "hash_globs": ["pipeline.c"],
    },
    {
        "name": "xtos-signal-benchmark",
        "version": "1.0.1",
        "license": LICENSE_APACHE,
        "description": "Fixed-input signal processing benchmark tool",
        "scope": "required",
        "path": "tools/xtos-signal-benchmark",
        "hash_globs": ["benchmark.c"],
    },
    {
        "name": "xtos-eeg-replay",
        "version": "1.0.1",
        "license": LICENSE_APACHE,
        "description": "Virtual EEG replay CSV validator (Python)",
        "scope": "required",
        "path": "tools/xtos-eeg-replay",
        "hash_globs": ["replay.py"],
    },
    {
        "name": "xtos-fuzz",
        "version": "1.0.1",
        "license": LICENSE_APACHE,
        "description": "Fuzz harnesses and corpus generator (development tooling)",
        "scope": "excluded",
        "path": "tools/xtos-fuzz",
        "hash_globs": ["fuzz_csv_parse.c", "fuzz_openbci_packet.c"],
    },
    {
        "name": "xtos-onnx-baseline",
        "version": "1.0.1",
        "license": LICENSE_APACHE,
        "description": "Optional ONNX/PyTorch/TensorFlow inference parity baselines (isolated envs, not shipped)",
        "scope": "excluded",
        "path": "tools/xtos-onnx-baseline",
        "hash_globs": ["baseline.py"],
    },
]

# Optional host-side Python baselines (excluded scope, recorded for audit)
OPTIONAL_PYTHON_DEPS = [
    ("onnxruntime", "1.23.2", "MIT", "https://pypi.org/project/onnxruntime/1.23.2/"),
    ("numpy", "2.2.4", "BSD-3-Clause", "https://pypi.org/project/numpy/2.2.4/"),
    ("torch", "2.6.0", "BSD-3-Clause", "https://pypi.org/project/torch/2.6.0/"),
    ("onnx", "1.20.1", "Apache-2.0", "https://pypi.org/project/onnx/1.20.1/"),
    ("tensorflow", "2.18.0", "Apache-2.0", "https://pypi.org/project/tensorflow/2.18.0/"),
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True)
        return result.stdout.strip()
    except Exception:
        return "unknown"


def component_ref(name: str) -> str:
    return "xtos-component-" + name.lower().replace("/", "-").replace(" ", "-")


def build_component(entry: dict) -> dict:
    comp = {
        "type": "library",
        "bom-ref": component_ref(entry["name"]),
        "name": entry["name"],
        "version": entry["version"],
        "licenses": [{"license": {"id": entry["license"]}}],
        "description": entry["description"],
        "scope": entry["scope"],
        "purl": f"pkg:generic/{entry['name']}@{entry['version']}",
    }
    if "upstream" in entry:
        comp["externalReferences"] = [
            {"type": "distribution", "url": entry["upstream"]}]
    hashes = []
    base = ROOT / entry["path"]
    if base.is_dir():
        for rel in entry.get("hash_globs", []):
            candidate = base / rel
            if candidate.is_file():
                hashes.append({"alg": "SHA-256", "content": sha256_file(candidate)})
        # property marker for source directory
        comp.setdefault("properties", []).append(
            {"name": "xtos:source-path", "value": entry["path"]})
    elif base.is_file():
        hashes.append({"alg": "SHA-256", "content": sha256_file(base)})
        comp.setdefault("properties", []).append(
            {"name": "xtos:source-path", "value": entry["path"]})
    if hashes:
        comp["hashes"] = hashes
    if "upstream_sha256" in entry:
        comp.setdefault("properties", []).append(
            {"name": "xtos:upstream-archive-sha256", "value": entry["upstream_sha256"]})
    return comp


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "sbom.json")
    parser.add_argument("--check", action="store_true",
                        help="regenerate in memory and compare the committed "
                             "SBOM's components, hashes, licenses, and "
                             "dependency graph (time-varying fields excluded); "
                             "exit 1 on drift")
    args = parser.parse_args()

    components = [build_component(entry) for entry in COMPONENTS]

    for name, version, license_id, url in OPTIONAL_PYTHON_DEPS:
        components.append({
            "type": "library",
            "bom-ref": component_ref("optional-" + name),
            "name": name,
            "version": version,
            "licenses": [{"license": {"id": license_id}}],
            "description": "Optional host-side ML baseline dependency (isolated environment)",
            "scope": "excluded",
            "purl": f"pkg:pypi/{name}@{version}",
            "externalReferences": [{"type": "distribution", "url": url}],
        })

    system_ref = component_ref("xtos-system")
    system_component = {
        "type": "application",
        "bom-ref": system_ref,
        "name": "xtos",
        "version": project_version(),
        "licenses": [{"license": {"id": LICENSE_APACHE}}],
        "description": ("XTOS real-time operating system research build: "
                        "Linux 6.6.0-rt15 kernel + XTOS BCI components + "
                        "BusyBox userland + XTOS SDK"),
        "scope": "required",
    }

    # system depends on every required component; excluded components have
    # their own dependency node so tools can still walk them.
    dependencies = [{"ref": system_ref,
                     "dependsOn": [c["bom-ref"] for c in components
                                   if c["scope"] == "required"]}]
    for comp in components:
        dependencies.append({"ref": comp["bom-ref"], "dependsOn": []})

    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": "urn:uuid:" + hashlib.sha256(
            (git_commit() + datetime.datetime.now().date().isoformat()).encode()
        ).hexdigest()[:36],
        "version": 1,
        "metadata": {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "component": system_component,
            "tools": [{"vendor": "XTOS", "name": "gen_sbom.py", "version": "1"}],
            "properties": [
                {"name": "xtos:git-commit", "value": git_commit()},
                {"name": "xtos:kernel-release", "value": "6.6.0-rt15"},
                {"name": "xtos:generated-by-host", "value": platform.platform()},
            ],
        },
        "components": [system_component] + components,
        "dependencies": dependencies,
    }

    if args.check:
        return check_against(bom, args.out)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(bom, indent=2) + "\n")

    required = [c for c in components if c["scope"] == "required"]
    excluded = [c for c in components if c["scope"] == "excluded"]
    print(f"sbom written: {args.out}")
    print(f"components: {len(components)} total, {len(required)} required, "
          f"{len(excluded)} excluded (dev/optional)")
    print(f"licenses present: " + ", ".join(sorted(
        {c["licenses"][0]["license"]["id"] for c in components})))
    return 0


def normalize(bom: dict) -> str:
    """Strip time-varying fields for comparison."""
    trimmed = json.loads(json.dumps(bom))
    trimmed.pop("serialNumber", None)
    trimmed.get("metadata", {}).pop("timestamp", None)
    for prop in trimmed.get("metadata", {}).get("properties", []):
        if prop["name"] == "xtos:generated-by-host":
            prop["value"] = "<stripped>"
        if prop["name"] == "xtos:git-commit":
            # Provenance, not content: the generating commit moves with every
            # commit that carries this file, so comparing it makes the gate
            # permanently red after any commit. Content freshness is what
            # the check is for.
            prop["value"] = "<stripped>"
    return json.dumps(trimmed, indent=2, sort_keys=True)


def check_against(fresh: dict, committed: Path) -> int:
    if not committed.is_file():
        print(f"sbom check FAIL: {committed} not found (run gen_sbom.py)")
        return 1
    old = json.loads(committed.read_text())
    if normalize(old) != normalize(fresh):
        print(f"sbom check FAIL: {committed} is stale; regenerate with "
              f"python3 tools/xtos-sbom/gen_sbom.py")
        return 1
    print("sbom_check=pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
