#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KERNEL_DIR="${KERNEL_DIR:-$ROOT/kernel/linux}"
PATCH_FILE="$ROOT/kernel/patches/xtos-linux-6.6.patch"

if [[ ! -f "$KERNEL_DIR/Makefile" ]]; then
    printf 'error: bundled Linux source is missing: %s\n' "$KERNEL_DIR" >&2
    exit 1
fi

version="$(sed -n -n 's/^VERSION = //p' "$KERNEL_DIR/Makefile")"
patchlevel="$(sed -n -n 's/^PATCHLEVEL = //p' "$KERNEL_DIR/Makefile")"
sublevel="$(sed -n -n 's/^SUBLEVEL = //p' "$KERNEL_DIR/Makefile")"
if [[ "$version.$patchlevel.$sublevel" != "6.6.0" ]]; then
    printf 'error: expected Linux 6.6.0, found %s\n' "$version.$patchlevel.$sublevel" >&2
    exit 1
fi
if [[ ! -s "$KERNEL_DIR/localversion-rt" ]] || ! grep -qx -- '-rt15' "$KERNEL_DIR/localversion-rt"; then
    printf 'error: bundled PREEMPT_RT marker is missing\n' >&2
    exit 1
fi
if [[ ! -s "$PATCH_FILE" ]]; then
    printf 'error: missing XTOS integration patch: %s\n' "$PATCH_FILE" >&2
    exit 1
fi
for required in \
    "$KERNEL_DIR/kernel/sched/xtos_sched.c" \
    "$KERNEL_DIR/kernel/sched/xtos/Kconfig" \
    "$KERNEL_DIR/drivers/xtos/bci/xtos_bci_core.c" \
    "$KERNEL_DIR/include/linux/xtos.h"; do
    if [[ ! -f "$required" ]]; then
        printf 'error: bundled XTOS source is missing: %s\n' "$required" >&2
        exit 1
    fi
done

if [[ "${REFRESH_BUNDLED_KERNEL:-0}" == "1" ]]; then
    printf 'error: archive refresh is retired; build the versioned kernel/linux source directly\n' >&2
    exit 1
fi
printf 'bundled_linux=%s\n' "$KERNEL_DIR"
printf 'bundled_release=6.6.0-rt15\n'
printf 'xtos_integration=present\n'