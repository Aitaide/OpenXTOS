#!/usr/bin/env bash
# Cross-compile the bundled XTOS Linux kernel for ARM64 or RISC-V.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARCH_INPUT="${1:-}"
JOBS="${JOBS:-$(nproc)}"

case "$ARCH_INPUT" in
    arm64|aarch64)
        KARCH=arm64
        CROSS_COMPILE="${CROSS_COMPILE:-aarch64-linux-gnu-}"
        IMAGE=Image
        ;;
    riscv|riscv64)
        KARCH=riscv
        CROSS_COMPILE="${CROSS_COMPILE:-riscv64-linux-gnu-}"
        IMAGE=Image
        ;;
    *)
        printf 'usage: %s <arm64|riscv64>\n' "$0" >&2
        exit 2
        ;;
esac

KERNEL_SRC="$ROOT/kernel/linux"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT/.build/kernel-$KARCH}"
COMPILER="${CROSS_COMPILE}gcc"
if ! command -v "$COMPILER" >/dev/null 2>&1; then
    printf 'error: cross compiler not found: %s\n' "$COMPILER" >&2
    exit 1
fi

mkdir -p "$OUTPUT_DIR"
make -C "$KERNEL_SRC" O="$OUTPUT_DIR" ARCH="$KARCH" CROSS_COMPILE="$CROSS_COMPILE" defconfig
KCONFIG_CONFIG="$OUTPUT_DIR/.config" "$KERNEL_SRC/scripts/kconfig/merge_config.sh" -m "$OUTPUT_DIR/.config" \
    "$ROOT/kernel/xtos_defconfig"
make -C "$KERNEL_SRC" O="$OUTPUT_DIR" ARCH="$KARCH" CROSS_COMPILE="$CROSS_COMPILE" olddefconfig
make -C "$KERNEL_SRC" O="$OUTPUT_DIR" ARCH="$KARCH" CROSS_COMPILE="$CROSS_COMPILE" -j"$JOBS" "$IMAGE" modules
printf 'arch=%s\nimage=%s\noutput=%s\n' "$KARCH" "$OUTPUT_DIR/arch/$KARCH/boot/$IMAGE" "$OUTPUT_DIR"