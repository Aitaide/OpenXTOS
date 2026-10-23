#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_ROOT="${BUILD_ROOT:-$ROOT/.build}"
printf 'repository=%s\n' "$ROOT"
"$ROOT/scripts/prepare_linux.sh"
for file in "$BUILD_ROOT/images/xtos-bzImage" "$BUILD_ROOT/images/xtos-rootfs.cpio.gz" "$BUILD_ROOT/system-qemu.log"; do
    if [[ -s "$file" ]]; then
        printf 'present=%s\n' "$file"
    else
        printf 'not_built=%s\n' "$file"
    fi
done
printf 'status=%s/STATUS.md\nplans=%s/plans/\narchitecture=%s/docs/XTOS_Technical_Specification.md\n' "$ROOT" "$ROOT" "$ROOT"
