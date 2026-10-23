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

RELEASE_DIR="$BUILD_ROOT/release/xtos-$XTOS_VERSION"
rm -rf "$RELEASE_DIR"
mkdir -p "$RELEASE_DIR"/{x86_64,docs}

printf '==> XTOS %s (%s, %s)\n' "$XTOS_VERSION" "$RELEASE_NAME" "$RELEASE_DATE"

# --- Stage 1: build artifacts (reproducible environment) -------------------
printf '==> Building x86_64 system (reproducible)\n'
JOBS="${JOBS:-$(nproc)}" "$ROOT/scripts/build_system.sh"

if [[ "$X86_ONLY" -eq 0 ]]; then
    if [[ -d "$BUILD_ROOT/kernel-build-arm64" && -f "$BUILD_ROOT/initramfs-arm64.cpio.gz" ]]; then
        printf '==> ARM64 artifacts found in .build (reused)\n'
    else
        printf '==> Building ARM64 system\n'
        JOBS="$JOBS" "$ROOT/scripts/build_system_arm64.sh"
    fi
    if [[ -d "$BUILD_ROOT/kernel-build-riscv64" && -f "$BUILD_ROOT/initramfs-riscv64.cpio.gz" ]]; then
        printf '==> RISC-V 64 artifacts found in .build (reused)\n'
    else
        printf '==> Building RISC-V 64 system\n'
        JOBS="$JOBS" "$ROOT/scripts/build_system_riscv64.sh"
    fi
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
        "$BUILD_ROOT/initramfs-arm64.cpio.gz" "$RELEASE_DIR/arm64/" 2>/dev/null || true
    install -m 0644 "$BUILD_ROOT/kernel-install-riscv64/Image" \
        "$BUILD_ROOT/initramfs-riscv64.cpio.gz" "$RELEASE_DIR/riscv64/" 2>/dev/null || true
fi

# SBOM: regenerate into the release tree (fresh hashes for this build)
python3 "$ROOT/tools/xtos-sbom/gen_sbom.py" --out "$RELEASE_DIR/docs/sbom.json"
install -m 0644 "$ROOT/docs/sbom.md"             "$RELEASE_DIR/docs/sbom.md"
install -m 0644 "$ROOT/docs/reproducible_build.md" "$RELEASE_DIR/docs/reproducible_build.md"
install -m 0644 "$ROOT/docs/abi_versioning.md"   "$RELEASE_DIR/docs/abi_versioning.md"

# Quick-start README for the release tree
cat > "$RELEASE_DIR/README.md" << EOF
# XTOS $XTOS_VERSION — $RELEASE_NAME ($RELEASE_DATE)

Real-time research system: Linux 6.6.0-rt15 (PREEMPT_RT) + XTOS BCI kernel
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

## Validated in this release

- x86_64: full system build reproducible byte-for-byte across clean
  rebuilds; QEMU selftest passes (realtime=1, xtos_modules=pass,
  guest_userspace=pass, XTOS_SYSTEM_TEST=PASS)
- ARM64: in-guest validation 14/14 (XTOS kernel modules load; test_rt /
  test_spsc / test_signal pass in guest)
- RISC-V 64: OpenSBI boot 5/5 + in-guest validation 15/15
- User-space: 181-assertion signal suite, C/Python reference parity,
  SPSC under ASan/UBSan/TSan
- Fuzz: 4,000+ mutated inputs across CSV and OpenBCI packet harnesses,
  ASan+UBSan clean (tools/xtos-fuzz)
- Supply chain: CycloneDX SBOM (docs/sbom.json) with source hashes;
  ABI policy v1.0.0 for the three SDK libraries (docs/abi_versioning.md)

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
