#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGES="${BUILD_ROOT:-$ROOT/.build}/images"
[[ -s "$IMAGES/xtos-bzImage" && -s "$IMAGES/xtos-rootfs.cpio.gz" ]] || {
    printf 'error: run scripts/build_system.sh first\n' >&2
    exit 1
}
exec qemu-system-x86_64 -machine pc -cpu max -m 512 -smp 2 \
    -kernel "$IMAGES/xtos-bzImage" -initrd "$IMAGES/xtos-rootfs.cpio.gz" \
    -append 'console=ttyS0 rdinit=/init loglevel=4' -nographic -no-reboot "$@"
