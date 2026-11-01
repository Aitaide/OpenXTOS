#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KERNEL_DIR="${KERNEL_DIR:-$ROOT/kernel/linux}"
if [[ "${REFRESH_BUNDLED_KERNEL:-0}" == "1" ]]; then
    printf 'error: archive refresh is retired; build the versioned kernel/linux source directly\n' >&2
    exit 1
fi
exec python3 "$ROOT/scripts/kernel_source.py" --root "$ROOT" --kernel-dir "$KERNEL_DIR"
