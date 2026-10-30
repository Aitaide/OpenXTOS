#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-$ROOT/.build}/xtos-cyton-parser.XXXXXXXX")"
trap 'rm -rf -- "$WORK"' EXIT
"${CC:-gcc}" -std=c11 -Wall -Wextra -Werror -fsanitize=address,undefined \
    -fno-sanitize-recover=all -fno-omit-frame-pointer \
    -I"$ROOT/kernel/linux/drivers/xtos/bci" -o "$WORK/test" \
    "$ROOT/kernel/linux/drivers/xtos/bci/test_cyton_packet.c"
UBSAN_OPTIONS=halt_on_error=1 "$WORK/test"
