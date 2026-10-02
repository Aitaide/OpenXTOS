#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_ROOT="${BUILD_ROOT:-$ROOT/.build}"
IMAGES="$BUILD_ROOT/images"
LOG="$BUILD_ROOT/system-qemu.log"
for file in xtos-bzImage xtos-rootfs.cpio.gz kernel.config; do
    [[ -s "$IMAGES/$file" ]] || { printf 'error: missing image %s; run scripts/build_system.sh\n' "$file" >&2; exit 1; }
done
command -v qemu-system-x86_64 >/dev/null
set +e
timeout --foreground "${QEMU_TIMEOUT:-120}" qemu-system-x86_64 \
    -machine pc -cpu max -m 512 -smp 2 \
    -kernel "$IMAGES/xtos-bzImage" -initrd "$IMAGES/xtos-rootfs.cpio.gz" \
    -append 'console=ttyS0 rdinit=/init loglevel=6 panic=1 xtos.selftest=1' \
    -nographic -no-reboot > "$LOG" 2>&1
status=$?
set -e
if [[ "$status" == 0 ]] && grep -qx 'XTOS_SYSTEM_TEST=PASS' <(tr -d '\r' < "$LOG") && \
   ! grep -q 'XTOS_SYSTEM_TEST=FAIL\|Kernel panic' "$LOG"; then
    printf 'system_qemu=pass log=%s\n' "$LOG"
    grep -E 'realtime=|xtos_modules=|guest_userspace=|XTOS_SYSTEM_TEST=' "$LOG"
else
    printf 'system_qemu=fail exit=%s log=%s\n' "$status" "$LOG" >&2
    tail -80 "$LOG" >&2
    exit 1
fi
