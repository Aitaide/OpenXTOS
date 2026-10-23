#!/usr/bin/env bash
# Build the vendored mbedTLS (3.6 LTS) static library with the XTOS minimal
# client configuration. Output: .build/mbedtls/libmbedtls_xtos.a
#
# Every library/*.c is compiled with the pinned config; modules disabled by
# the config compile to empty translation units, so one archive covers
# crypto + x509 + tls. The archive is cached and rebuilt only when the
# vendor sources or the config change.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR="$ROOT/tools/xtos-native/vendor/mbedtls"
OUT="$ROOT/.build/mbedtls"
STAMP="$OUT/build-stamp.txt"

mkdir -p "$OUT/obj"

CONFIG_HASH="$(sha256sum "$VENDOR/xtos_mbedtls_config.h" | cut -d' ' -f1)"
SOURCES_HASH="$(find "$VENDOR/library" "$VENDOR/include" -type f -name '*.c' -o -type f -name '*.h' | sort | xargs sha256sum | sha256sum | cut -d' ' -f1)"

if [ -f "$OUT/libmbedtls_xtos.a" ] && [ -f "$STAMP" ]; then
    if [ "$(cat "$STAMP")" = "$CONFIG_HASH $SOURCES_HASH" ]; then
        exit 0
    fi
fi

CC="${CC:-gcc}"
# Vendored upstream code is compiled as-is: no -Wall/-Wextra/-Werror gate.
CFLAGS="-O2 -std=c99 \
  -DMBEDTLS_CONFIG_FILE='\"xtos_mbedtls_config.h\"' \
  -I$VENDOR/include -I$VENDOR/library -I$VENDOR"

rm -rf "$OUT/obj"
mkdir -p "$OUT/obj"

# Compile with a bounded job pool.
find "$VENDOR/library" -maxdepth 1 -name '*.c' -printf '%f\n' | sort | \
  xargs -P "$(nproc)" -I{} sh -c "
    '$CC' $CFLAGS -c '$VENDOR/library/{}' -o '$OUT/obj/{}.o'
"

rm -f "$OUT/libmbedtls_xtos.a"
ar rcs "$OUT/libmbedtls_xtos.a" "$OUT"/obj/*.o
echo "$CONFIG_HASH $SOURCES_HASH" > "$STAMP"
echo "built: $OUT/libmbedtls_xtos.a ($(stat -c%s "$OUT/libmbedtls_xtos.a") bytes)"
