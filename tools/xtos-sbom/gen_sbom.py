#!/usr/bin/env python3
"""Generate the XTOS SBOM in CycloneDX 1.5 JSON format.

Covers everything shipped in the repository and produced by the system
build: the vendored Linux kernel (mainline PREEMPT_RT and XTOS integration
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
        "name": "xtos-rt-diagnostics",
        "version": "1.0.1",
        "license": LICENSE_APACHE,
        "description": "Read-only mainline/legacy PREEMPT_RT identity and explicit native tuning tools; no physical latency guarantee",
        "scope": "required",
        "path": ".",
        "hash_globs": ["tools/xtos-native/info/info.c", "tools/xtos-native/rt/rt.c",
                       "tools/xtos-native/common/xtos_rt_state.h", "tools/xtos-native/common/xtos_ui.h",
                       "tools/xtos-native/rt/rt_irq.h", "system/rootfs/usr/bin/xtos-rt-status"],
    },
    {
        "name": "xtos-xr",
        "version": "0.1.0",
        "license": LICENSE_APACHE,
        "description": "Application OpenXR loader/event/time and opt-in headless eye session adapter, bounded multimodal software-only confirmation; no rendering or headset evidence",
        "scope": "required",
        "path": "tools/xtos-xr",
        "requires": ["openxr-headers", "xtos-action-gate"],
        "hash_globs": ["Makefile", "include/xtos_xr/runtime_adapter.h",
                       "src/runtime_adapter.c", "src/xr_probe.c",
                       "include/xtos_xr/eye_session.h", "src/eye_session.c", "src/xr_eye_probe.c",
                       "xtos_xr/__init__.py", "xtos_xr/__main__.py", "xtos_xr/runtime_events.py"],
    },
    {
        "name": "openxr-headers",
        "version": "1.1.57",
        "license": LICENSE_APACHE,
        "description": "Pinned Khronos OpenXR-SDK headers under the Apache-2.0 option; no loader, compositor or vendor runtime included",
        "scope": "required",
        "upstream": "https://github.com/KhronosGroup/OpenXR-SDK/tree/release-1.1.57",
        "path": "tools/xtos-xr/vendor/openxr",
        "hash_globs": ["include/openxr/openxr.h", "include/openxr/openxr_platform.h",
                       "include/openxr/openxr_platform_defines.h", "COPYING.adoc",
                       "LICENSES/Apache-2.0.txt", "LICENSES/MIT.txt", "provenance.json"],
    },
    {
        "name": "linux",
        "version": "7.3.1",
        "license": LICENSE_KERNEL,
        "description": "Linux kernel with mainline PREEMPT_RT support; configuration/source inventory, not runtime acceptance",
        "scope": "required",
        "upstream": "https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.3.1.tar.xz",
        "path": "kernel/linux",
        "hash_globs": ["Makefile", "COPYING"],
    },
    {
        "name": "preempt-rt",
        "version": "6.6-rt15",
        "license": LICENSE_GPL2,
        "description": "Historical 6.6-rt15 external PREEMPT_RT patch; not applied to Linux 7.3.1",
        "scope": "excluded",
        "upstream": "https://cdn.kernel.org/pub/linux/kernel/projects/rt/6.6/older/patch-6.6-rt15.patch.xz",
        "path": "kernel/patches/preempt-rt-6.6-rt15.patch",
        "hash_globs": ["kernel/patches/preempt-rt-6.6-rt15.patch"],
    },
    {
        "name": "xtos-linux-integration",
        "version": "1.0.1",
        "license": LICENSE_GPL2,
        "description": ("XTOS kernel integration patch: scheduler helper, BCI core, "
                        "BCI buffer, OpenBCI Cyton serdev driver and shared packet parser, UAPI headers"),
        "scope": "required",
        "path": "kernel/patches/xtos-linux-7.3.1.patch",
        "hash_globs": ["kernel/patches/xtos-linux-7.3.1.patch"],
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
        "version": "1.1.0",
        "license": LICENSE_APACHE,
        "description": "XTOS deterministic signal library (FFT, notch, bandpass, SSVEP, P300, bounded versioned EEG mean/RMS windows)",
        "scope": "required",
        "path": "sdk/libxtos-signal",
        "hash_globs": ["src/signal.c", "src/eeg_window.c", "include/xtos/signal.h",
                       "include/xtos/signal_version.h", "include/xtos/eeg_window.h"],
    },
    {
        "name": "libxtos-bci",
        "version": "1.0.1",
        "license": LICENSE_APACHE,
        "description": "XTOS BCI session library (versioned frame read and immutable snapshots, configuration, legacy sample APIs)",
        "scope": "required",
        "path": "sdk/libxtos-bci",
        "hash_globs": ["src/session.c", "include/xtos/bci.h", "include/xtos/bci_uapi.h",
                       "include/xtos/bci_config_uapi.h", "include/xtos/bci_snapshot_uapi.h",
                       "include/xtos/bci_frame_uapi.h", "include/xtos/bci_version.h"],
    },
    {
        "name": "xtos-eeg-pipeline",
        "version": "1.0.1",
        "license": LICENSE_APACHE,
        "description": "Virtual EEG CSV pipeline, Cyton Python capture/replay and bounded device-report validation",
        "scope": "required",
        "path": "tools/xtos-eeg-pipeline",
        "hash_globs": ["Makefile", "pipeline.c", "openbci_cyton.py", "cyton_capture.py",
                       "cyton_replay.py", "device_contract.py"],
    },
    {
        "name": "bci",
        "version": "1.0.1",
        "license": LICENSE_GPL2,
        "description": "Native Cyton configuration, diagnostics, explicit reference calibration, and eight-channel streaming command",
        "scope": "required",
        "path": "tools/xtos-cyton",
        "hash_globs": ["Makefile", "main.c", "bci_stream.c", "bci_stream.h",
                       "cyton_serial.c", "cyton_serial.h", "cli_options.h", "bci_decode.h",
                       "bci_config_cli.c", "bci_config_cli.h",
                       "bci_diagnostic_cli.c", "bci_diagnostic_cli.h",
                       "bci_diagnostic.c", "bci_diagnostic.h", "bci_diagnostic_options.h",
                       "cyton_diagnostic_serial.c", "cyton_diagnostic_serial.h",
                       "bci_calibration.c", "bci_calibration.h", "bci_calibration_cli.c",
                       "bci_calibration_cli.h", "bci_sha256.h"],
    },
    {
        "name": "xtos-eeg-stream",
        "version": "1.0.1",
        "license": LICENSE_APACHE,
        "description": "Native bounded BCI frame/window JSONL producer for the EEG loop consumer",
        "scope": "required",
        "path": "tools/xtos-eeg-stream",
        "hash_globs": ["Makefile", "main.c", "eeg_export.c", "eeg_export.h",
                       "eeg_stream.c", "eeg_stream.h"],
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
        "name": "xtos-device-plugins",
        "version": "0.1.0",
        "license": LICENSE_APACHE,
        "description": "Source-only fixture, Cyton serial, pinned Ganglion v2 codec, optional Linux BLE and explicit TCP byte transports; no hardware identity proof",
        "scope": "required",
        "path": "tools/xtos-devices",
        "hash_globs": ["xtos_devices.py", "serial_transport.py", "cyton_protocol.py",
                       "cyton_serial_plugin.py", "ganglion_protocol.py", "ganglion_plugin.py",
                       "ganglion_ble.py", "network_transport.py", "tls_transport.py"],
    },
    {
        "name": "xtos-ble-runtime-provisioner",
        "version": "0.1.0",
        "license": LICENSE_APACHE,
        "description": "Explicit offline CPython 3.13 Linux x86_64 BLE wheel provisioner and lock; not part of the default image, no BlueZ or radio acceptance",
        "scope": "excluded",
        "path": ".",
        "hash_globs": ["scripts/install_ble_runtime.py", "system/packages/ble-runtime-cp313-x86_64.json"],
    },
    {
        "name": "xtos-eeg-loop",
        "version": "1.0.1",
        "license": LICENSE_APACHE,
        "description": "Bounded EEG feature binding, signed CPU transport, lifecycle, harmless simulation, controller, and JSONL process source",
        "scope": "required",
        "path": "tools/xtos_eeg_loop",
        "hash_globs": ["__init__.py", "controller.py", "feature_binding.py", "inference.py",
                       "lifecycle.py", "simulation.py", "stream.py"],
    },
    {
        "name": "xtos-ai-runtime",
        "version": "1.1.0",
        "license": LICENSE_APACHE,
        "description": "Bounded local AF_UNIX model runtime and signed model trust source",
        "scope": "required",
        "path": "tools/xtos-ai-runtime/xtos_ai_runtime",
        "hash_globs": ["__init__.py", "__main__.py", "client.py", "protocol.py", "server.py", "trust.py", "worker.py"],
    },
    {
        "name": "xtos-ai-provider-experiment",
        "version": "0.1.0",
        "license": LICENSE_APACHE,
        "description": "Explicit ONNX Runtime provider-selection experiment; no signed admission, action authority, or hardware claim",
        "scope": "excluded",
        "path": "tools/xtos-ai-provider-experiment",
        "hash_globs": ["provider_experiment.py"],
    },
    {
        "name": "xtos-action-gate",
        "version": "1.0.1",
        "license": LICENSE_APACHE,
        "description": "Fixed root-owned approval store and exact harmless action request schema",
        "scope": "required",
        "path": "tools/xtos_action_gate",
        "hash_globs": ["__init__.py", "__main__.py", "schema.py", "store.py"],
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
    pin_path = ROOT / "kernel/source-pin.json"
    pin = {}
    if entry["name"] in ("linux", "xtos-linux-integration"):
        pin = json.loads(pin_path.read_text())
        entry = dict(entry)
        if entry["name"] == "linux":
            entry.update(version=pin["version"], upstream=pin["upstream"],
                         upstream_sha256=pin["archive_sha256"])
        else:
            entry["path"] = pin["xtos_patch"]
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
    if entry["name"] == "xtos-linux-integration":
        parser_path = ROOT / "kernel/linux/drivers/xtos/bci/xtos_cyton_packet.h"
        comp.setdefault("properties", []).append({
            "name": "openbci-cyton-packet.h", "value": sha256_file(parser_path)})
        for name in ("drivers/Kconfig", "drivers/Makefile", "init/Kconfig", "kernel/sched/Makefile",
                     "drivers/xtos/Kconfig", "drivers/xtos/Makefile",
                     "drivers/xtos/bci/Kconfig", "drivers/xtos/bci/Makefile",
                     "drivers/xtos/bci/xtos_bci_core.c", "drivers/xtos/bci/xtos_bci_buffer.c",
                     "drivers/xtos/bci/xtos_bci_snapshot.c", "drivers/xtos/bci/xtos_bci_snapshot.h",
                     "drivers/xtos/bci/xtos_cyton_config.h", "drivers/xtos/bci/xtos_cyton_packet.h",
                     "drivers/xtos/bci/devices/openbci_cyton.c",
                     "kernel/sched/xtos/Kconfig", "kernel/sched/xtos_sched.c", "kernel/sched/xtos_prio.h",
                     "include/linux/xtos.h", "include/linux/xtos_bci.h", "include/linux/xtos_queue.h",
                     "include/uapi/linux/xtos_bci.h", "include/uapi/linux/xtos_bci_config.h",
                     "include/uapi/linux/xtos_bci_snapshot.h",
                     "drivers/xtos/bci/xtos_bci_frame.c",
                     "drivers/xtos/bci/xtos_bci_frame.h",
                     "drivers/xtos/bci/xtos_bci_frame_snapshot.c",
                     "drivers/xtos/bci/xtos_bci_frame_snapshot.h",
                     "include/uapi/linux/xtos_bci_frame.h"):
            comp["properties"].append({
                "name": "xtos:source:" + name,
                "value": sha256_file(ROOT / "kernel/linux" / name)})
    if hashes:
        comp["hashes"] = hashes
    if entry["name"] == "linux":
        comp.setdefault("properties", []).extend([
            {"name": "xtos:source-pin-sha256", "value": sha256_file(pin_path)},
            {"name": "xtos:upstream-archive-bytes", "value": str(pin["archive_bytes"])},
            {"name": "xtos:rt-mode", "value": pin["rt_mode"]},
            {"name": "xtos:rt-config", "value": pin["rt_config"]},
            {"name": "xtos:rt-runtime-evidence", "value": "not-validated"},
        ])
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
                        "Linux mainline-RT kernel + XTOS BCI components + "
                        "BusyBox userland + XTOS SDK"),
        "scope": "required",
    }

    # system depends on every required component; excluded components have
    # their own dependency node so tools can still walk them.
    dependencies = [{"ref": system_ref,
                     "dependsOn": [c["bom-ref"] for c in components
                                   if c["scope"] == "required"]}]
    for comp in components:
        requires = [component_ref(name) for name in ('libxtos-bci', 'libxtos-signal')] if comp['name'] == 'xtos-eeg-stream' else []
        if comp['name'] == 'xtos-device-plugins':
            requires = [component_ref('xtos-eeg-pipeline')]
        if comp['name'] == 'xtos-linux-integration':
            requires = [component_ref('linux')]
        if comp['name'] == 'xtos-xr':
            requires = [component_ref(name) for name in ('openxr-headers', 'xtos-action-gate')]
        dependencies.append({"ref": comp["bom-ref"], "dependsOn": requires})

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
                {"name": "xtos:kernel-release", "value": json.loads((ROOT / "kernel/source-pin.json").read_text())["version"]},
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
