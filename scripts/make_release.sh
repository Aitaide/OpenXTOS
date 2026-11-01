#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Release packaging for XTOS Developer Preview.
#
# Produces a self-contained release directory under .build/release/:
#   xtos-<version>/            # release tree
#     VERSION, RELEASE_NOTES.md, SBOM
#     x86_64/{xtos-bzImage, xtos-rootfs.cpio.gz, kernel.config}
#     sha256.txt                # artifact digests for third-party verification
#   xtos-<version>.tar.gz      # the release archive
#
# Everything is built with the reproducible environment, so artifact hashes
# can be compared against a second clean build (see docs/reproducible_build.md).
#
# Usage: ./scripts/make_release.sh [--x86-only]
#   (ARM64/RISC-V64 images are packaged too unless --x86-only; they take
#    ~25 minutes to cross-build from scratch each.)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_ROOT="${BUILD_ROOT:-$ROOT/.build}"
X86_ONLY=0
[[ "${1:-}" == "--x86-only" ]] && X86_ONLY=1

. "$ROOT/scripts/reproducible_env.sh"

# Read version from the VERSION file (first KEY=VALUE line)
XTOS_VERSION="$(sed -n 's/^XTOS_VERSION=//p' "$ROOT/VERSION" | head -1)"
if [[ -z "$XTOS_VERSION" ]]; then
    printf 'error: cannot read XTOS_VERSION from VERSION\n' >&2
    exit 1
fi
RELEASE_NAME="$(sed -n 's/^XTOS_RELEASE_NAME=//p' "$ROOT/VERSION" | head -1)"
RELEASE_DATE="$(sed -n 's/^XTOS_RELEASE_DATE=//p' "$ROOT/VERSION" | head -1)"
KERNEL_PIN="$ROOT/kernel/source-pin.json"

validate_kernel_artifacts() {
    local arch="$1" build_dir="$2" image="$3" initramfs="$4"
    local config="$build_dir/.config" release_file="$build_dir/include/config/kernel.release"
    local artifact
    for artifact in "$image" "$initramfs" "$config" "$release_file"; do
        if [[ ! -s "$artifact" ]]; then
            printf 'error: %s kernel artifact missing or empty: %s\n' "$arch" "$artifact" >&2
            return 1
        fi
    done
    if [[ "$arch" == x86_64 ]] && ! cmp -s "$config" "$BUILD_ROOT/images/kernel.config"; then
        printf 'error: packaged config differs from resolved kernel config\n' >&2
        return 1
    fi
    python3 - "$KERNEL_PIN" "$config" "$release_file" "$arch" "$image" "$ROOT/kernel/linux/scripts/extract-ikconfig" <<'PY'
import json
import re
import subprocess
import sys
from pathlib import Path

pin_path, config_path, release_path, arch, image_path, extractor = sys.argv[1:]
pin = json.loads(Path(pin_path).read_text())
version = pin.get("version")
config = Path(config_path).read_text()
match = re.search(r'^CONFIG_LOCALVERSION="((?:[^"\\]|\\.)*)"$', config, re.M)
localversion = match.group(1) if match else ""
release = Path(release_path).read_text().strip()
expected = f"{version}{localversion}"
if not isinstance(version, str) or not version or (localversion and not localversion.startswith("-")):
    print(f"error: invalid kernel pin/localversion for {arch}", file=sys.stderr)
    raise SystemExit(1)
if release != expected:
    print(f"error: {arch} kernel.release mismatch: expected {expected!r} from source pin and CONFIG_LOCALVERSION, got {release!r}", file=sys.stderr)
    raise SystemExit(1)
if config.splitlines().count('CONFIG_PREEMPT_RT=y') != 1:
    raise SystemExit('error: resolved kernel config must enable PREEMPT_RT')
image_file = Path(image_path)
if image_file.stat().st_size > 256 * 1024 * 1024:
    raise SystemExit('error: actual kernel image exceeds bounded release validation')
image = image_file.read_bytes()
if arch == 'x86_64':
    if len(image) < 0x210 or image[0x1fe:0x200] != b'\x55\xaa' or image[0x202:0x206] != b'HdrS':
        raise SystemExit('error: actual kernel image has invalid x86 boot header')
    offset = 0x200 + int.from_bytes(image[0x20e:0x210], 'little')
    version = image[offset:offset + 512].split(b'\0', 1)[0].split(b' ', 1)[0]
    if offset < 0x210 or version != release.encode('ascii'):
        raise SystemExit('error: actual kernel image version differs from release sidecar')
else:
    magic = b'ARM\x64' if arch == 'arm64' else b'RSC\x05'
    if image[56:60] != magic or b'Linux version ' + release.encode('ascii') + b' ' not in image:
        raise SystemExit('error: actual kernel image architecture/version differs from sidecar')
embedded = subprocess.run(['sh', extractor, image_path], capture_output=True, timeout=30)
if embedded.returncode or embedded.stdout != Path(config_path).read_bytes():
    raise SystemExit('error: actual kernel image embedded config differs from resolved config')
print(release)
PY
}

RELEASE_DIR="$BUILD_ROOT/release/xtos-$XTOS_VERSION"
rm -rf "$RELEASE_DIR"
mkdir -p "$RELEASE_DIR"/{x86_64,docs}

printf '==> XTOS %s (%s, %s)\n' "$XTOS_VERSION" "$RELEASE_NAME" "$RELEASE_DATE"

# --- Stage 1: build artifacts (reproducible environment) -------------------
printf '==> Building x86_64 system (reproducible)\n'
JOBS="${JOBS:-$(nproc)}" "$ROOT/scripts/build_system.sh"
X86_KERNEL_RELEASE="$(validate_kernel_artifacts x86_64 "$BUILD_ROOT/kernel-build" \
    "$BUILD_ROOT/images/xtos-bzImage" "$BUILD_ROOT/images/xtos-rootfs.cpio.gz")"

if [[ "$X86_ONLY" -eq 0 ]]; then
    printf '==> Building ARM64 system\n'
    JOBS="$JOBS" "$ROOT/scripts/build_system_arm64.sh"
    ARM64_KERNEL_RELEASE="$(validate_kernel_artifacts arm64 "$BUILD_ROOT/kernel-build-arm64" \
        "$BUILD_ROOT/kernel-install-arm64/Image" "$BUILD_ROOT/initramfs-arm64.cpio.gz")"
    printf '==> Building RISC-V 64 system\n'
    JOBS="$JOBS" "$ROOT/scripts/build_system_riscv64.sh"
    RISCV64_KERNEL_RELEASE="$(validate_kernel_artifacts riscv64 "$BUILD_ROOT/kernel-build-riscv64" \
        "$BUILD_ROOT/kernel-install-riscv64/Image" "$BUILD_ROOT/initramfs-riscv64.cpio.gz")"
fi

# --- Stage 2: assemble the release tree -------------------------------------
printf '==> Assembling release tree\n'
install -m 0644 "$ROOT/VERSION" "$RELEASE_DIR/VERSION"
install -m 0644 "$BUILD_ROOT/images/xtos-bzImage"            "$RELEASE_DIR/x86_64/xtos-bzImage"
install -m 0644 "$BUILD_ROOT/images/xtos-rootfs.cpio.gz"     "$RELEASE_DIR/x86_64/xtos-rootfs.cpio.gz"
install -m 0644 "$BUILD_ROOT/images/kernel.config"          "$RELEASE_DIR/x86_64/kernel.config"

ARCH_DIRS=()
[[ "$X86_ONLY" -eq 0 ]] && ARCH_DIRS+=(arm64 riscv64)
if [[ "$X86_ONLY" -eq 0 ]]; then
    mkdir -p "$RELEASE_DIR/arm64" "$RELEASE_DIR/riscv64"
    install -m 0644 "$BUILD_ROOT/kernel-install-arm64/Image" \
        "$BUILD_ROOT/initramfs-arm64.cpio.gz" "$RELEASE_DIR/arm64/"
    install -m 0644 "$BUILD_ROOT/kernel-build-arm64/.config" "$RELEASE_DIR/arm64/kernel.config"
    install -m 0644 "$BUILD_ROOT/kernel-install-riscv64/Image" \
        "$BUILD_ROOT/initramfs-riscv64.cpio.gz" "$RELEASE_DIR/riscv64/"
    install -m 0644 "$BUILD_ROOT/kernel-build-riscv64/.config" "$RELEASE_DIR/riscv64/kernel.config"
fi

# SBOM: regenerate into the release tree (fresh hashes for this build)
python3 "$ROOT/tools/xtos-sbom/gen_sbom.py" --out "$RELEASE_DIR/docs/sbom.json"
install -m 0644 "$ROOT/docs/sbom.md"             "$RELEASE_DIR/docs/sbom.md"
install -m 0644 "$ROOT/docs/reproducible_build.md" "$RELEASE_DIR/docs/reproducible_build.md"
install -m 0644 "$ROOT/docs/abi_versioning.md"   "$RELEASE_DIR/docs/abi_versioning.md"

# Quick-start README uses releases read back from each built kernel.
README_KERNEL_RELEASES="Linux $X86_KERNEL_RELEASE (PREEMPT_RT)"
if [[ "$X86_ONLY" -eq 0 ]]; then
    README_KERNEL_RELEASES="x86_64 $X86_KERNEL_RELEASE, ARM64 $ARM64_KERNEL_RELEASE, RISC-V 64 $RISCV64_KERNEL_RELEASE (PREEMPT_RT)"
fi
cat > "$RELEASE_DIR/README.md" << EOF
# XTOS $XTOS_VERSION — $RELEASE_NAME ($RELEASE_DATE)

Real-time research system: $README_KERNEL_RELEASES + XTOS BCI kernel
components + BusyBox 1.37.0 + XTOS SDK (Apache-2.0).

## Run (QEMU)

x86_64:
    qemu-system-x86_64 -machine pc -cpu max -m 512 -smp 2 \\
        -kernel x86_64/xtos-bzImage -initrd x86_64/xtos-rootfs.cpio.gz \\
        -append 'console=ttyS0 rdinit=/init xtos.selftest=1' -nographic

arm64:
    qemu-system-aarch64 -machine virt -cpu cortex-a57 -m 2G -smp 2 \\
        -kernel arm64/Image -initrd arm64/initramfs-arm64.cpio.gz \\
        -append 'console=ttyAMA0 panic=1' -nographic

riscv64:
    qemu-system-riscv64 -machine virt -cpu rv64 -m 2G -smp 2 \\
        -bios /usr/share/qemu/opensbi-riscv64-generic-fw_dynamic.bin \\
        -kernel riscv64/Image -initrd riscv64/initramfs-riscv64.cpio.gz \\
        -append 'console=ttyS0 panic=1' -nographic

Drop 'xtos.selftest=1' from the cmdline for an interactive BusyBox shell.

## Verify integrity

    sha256sum -c sha256.txt

## Contents

    VERSION             release version (single source of truth)
    RELEASE_NOTES.md    what this release contains and how it was validated
    sha256.txt          artifact digests (reproducible-build anchored)
    docs/               SBOM (CycloneDX 1.5), reproducible-build report, ABI policy
    x86_64/ ...         kernel image + initramfs per architecture

License: Apache-2.0 (project) + GPL-2.0 (kernel components) — see docs/sbom.md.
EOF

# Release notes: generated skeleton + validation evidence
GIT_COMMIT="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
cat > "$RELEASE_DIR/RELEASE_NOTES.md" << EOF
# XTOS $XTOS_VERSION Release Notes — $RELEASE_NAME

Release date: $RELEASE_DATE
Source commit: $GIT_COMMIT
Build environment: SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH (reproducible; see docs/reproducible_build.md)

## What this is

The first packaged XTOS Developer Preview: a bootable, self-testing
real-time research system for brain-computer-interface and deterministic
signal-processing workloads.

## Validation status

Validation status: NOT_VERIFIED_BY_PACKAGING

The package contains fresh producer outputs whose release/config identities
were checked against kernel/source-pin.json. This script does not run boot,
functional, sanitizer, reproducibility or physical-device acceptance.
Independent reports must identify this exact source commit and artifact
hashes before any of those gates can be claimed as passed. Historical
6.6/rt15 reports remain historical and do not validate this package.

## Not in scope (explicitly)

- Real-hardware latency guarantees (QEMU numbers are not latency evidence)
- Real OpenBCI device bring-up (needs serial hardware)
- Medical-grade safety claims
- Linux ABI compatibility beyond the shipped userland

## Known issues

- In-guest bci_test reports 'Failed to create session' without a real
  serial device — expected in QEMU on every architecture.
EOF

# --- Stage 3: checksums + archive --------------------------------------------
printf '==> Writing checksums\n'
(
    cd "$RELEASE_DIR"
    find . -type f ! -name 'sha256.txt*' -print0 | LC_ALL=C sort -z \
        | xargs -0 sha256sum > sha256.txt.new
    mv sha256.txt.new sha256.txt
)

printf '==> Packing archive\n'
ARCHIVE="$BUILD_ROOT/release/xtos-$XTOS_VERSION.tar.gz"
(
    cd "$BUILD_ROOT/release"
    tar --sort=name --mtime="@$SOURCE_DATE_EPOCH" --owner=0 --group=0 \
        -czf "$ARCHIVE" "xtos-$XTOS_VERSION"
)

printf '==> Verifying archive\n'
tar -tzf "$ARCHIVE" > /dev/null
sha256sum "$ARCHIVE"

printf '\nRelease ready:\n  tree:  %s\n  archive: %s\n' "$RELEASE_DIR" "$ARCHIVE"
printf 'Release contents:\n'
find "$RELEASE_DIR" -type f | LC_ALL=C sort | sed "s|$RELEASE_DIR/|  |"
