#!/usr/bin/env bash
# Build the XTOS BCI user-space example and show the bundled kernel module path.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KERNEL_DIR="$ROOT/kernel/linux"
SDK_DIR="$ROOT/sdk/libxtos-bci"
EXAMPLES_DIR="$ROOT/examples"

make -C "$SDK_DIR" clean all
if [[ "${1:-}" == "install" ]]; then
    make -C "$SDK_DIR" install
    ldconfig
fi

gcc -Wall -Wextra -O2 -o "$EXAMPLES_DIR/bci_test" \
    "$EXAMPLES_DIR/bci_test.c" \
    -I"$SDK_DIR/include" \
    -L"$SDK_DIR" \
    -Wl,-rpath,"$SDK_DIR" \
    -lxtos-bci

printf 'user_library=%s\n' "$SDK_DIR/libxtos-bci.so"
printf 'example=%s\n' "$EXAMPLES_DIR/bci_test"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT/.build/kernel-build}"
KERNEL_DIR="${OUTPUT_DIR}"
printf 'kernel_modules=\n'
find "$KERNEL_DIR" -type f -name '*.ko' -path '*/drivers/xtos/*' 2>/dev/null | sort || true
printf 'build=pass\n'