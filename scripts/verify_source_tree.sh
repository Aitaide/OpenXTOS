#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KERNEL_DIR="${KERNEL_DIR:-$ROOT/kernel/linux}"
# Enumerate both tracked/untracked repository inputs and every selected-kernel file.
exec python3 "$ROOT/scripts/kernel_source.py" --root "$ROOT" --kernel-dir "$KERNEL_DIR" --scan
