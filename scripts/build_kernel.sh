#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_ROOT="${BUILD_ROOT:-$ROOT/.build}"
KERNEL_DIR="${KERNEL_DIR:-$ROOT/kernel/linux}"
OUTPUT_DIR="${OUTPUT_DIR:-$BUILD_ROOT/kernel-build}"
JOBS="${JOBS:-$(nproc)}"

"$ROOT/scripts/prepare_linux.sh"
mkdir -p "$OUTPUT_DIR"
# This fragment is merged by Kconfig and is never written into kernel/linux.
make -C "$KERNEL_DIR" O="$OUTPUT_DIR" ARCH="${ARCH:-x86}" defconfig
scripts="$KERNEL_DIR/scripts/kconfig/merge_config.sh"
KCONFIG_CONFIG="$OUTPUT_DIR/.config" "$scripts" -m "$OUTPUT_DIR/.config" "$ROOT/kernel/xtos_defconfig"
make -C "$KERNEL_DIR" O="$OUTPUT_DIR" ARCH="${ARCH:-x86}" olddefconfig
printf '%s\n' '--- XTOS kernel configuration ---'
required_config=(
    CONFIG_PREEMPT_RT=y
    CONFIG_XTOS_SCHED=y
    CONFIG_XTOS_BCI=m
    CONFIG_XTOS_BCI_OPENBCI=m
    CONFIG_SERIAL_DEV_BUS=m
)
for setting in "${required_config[@]}"; do
    if ! grep -qx "$setting" "$OUTPUT_DIR/.config"; then
        printf 'error: required kernel setting missing: %s\n' "$setting" >&2
        exit 1
    fi
done
grep -E '^(CONFIG_(PREEMPT_RT|HIGH_RES_TIMERS|HZ|XTOS_SCHED|XTOS_BCI|XTOS_BCI_OPENBCI|SERIAL_DEV_BUS))=' "$OUTPUT_DIR/.config"
make -C "$KERNEL_DIR" O="$OUTPUT_DIR" ARCH="${ARCH:-x86}" -j"$JOBS" bzImage modules
if [[ ! -s "$OUTPUT_DIR/arch/x86/boot/bzImage" ]]; then
    printf 'error: bzImage was not produced\n' >&2
    exit 1
fi
printf 'bzImage=%s\n' "$OUTPUT_DIR/arch/x86/boot/bzImage"