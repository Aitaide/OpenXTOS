#!/bin/bash
# Build liblsl (Lab Streaming Layer) C library for the XTOS Live image.
# Upstream: https://github.com/sccn/liblsl, pinned release v1.17.7.
# Pinned source is expected at .build/userspace/live-developer-lsl/src
# (git clone https://github.com/sccn/liblsl && git checkout v1.17.7).
# Output: install/{lib/liblsl.so*, include/lsl*, include/lsl_c.h, include/lsl_cpp.h, bin/lslver}
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="${XTOS_LSL_DIR:-$ROOT/.build/userspace/live-developer-lsl}"
SRC="$WORK/src"
BUILD="$WORK/build"
INSTALL="$WORK/install"
TAG="v1.17.7"
COMMIT="64988c6a5e5dd8e1b0c25da40be07f8bd0e6d631"  # resolved by the subagent checkout; verified below
EXPECT_LIB_SHA256="75c0aeb9df2992e19cc7034f6f9a51f45dd61bba6dc3d29b5a38917d575090c1"

[ -d "$SRC/.git" ] || { echo "source missing: $SRC (git clone https://github.com/sccn/liblsl $SRC && git -C $SRC checkout $TAG)" >&2; exit 2; }
ACTUAL_TAG="$(git -C "$SRC" describe --tags 2>/dev/null || true)"
if [ "$ACTUAL_TAG" != "$TAG" ] && [ "$(git -C "$SRC" rev-parse HEAD)" != "$COMMIT" ]; then
    echo "source tree is not at $TAG/$COMMIT (got: ${ACTUAL_TAG:-none}) — refusing to build unpinned source" >&2
    exit 2
fi
if [ -n "$(git -C "$SRC" status --porcelain 2>/dev/null)" ]; then
    echo "source tree has local modifications — refusing" >&2
    exit 2
fi
command -v cmake >/dev/null || { echo "cmake missing on host" >&2; exit 2; }

rm -rf "$BUILD" "$INSTALL"
cmake -S "$SRC" -B "$BUILD" \
    -DCMAKE_BUILD_TYPE=Release \
    -DLSL_INSTALL=ON \
    -DCMAKE_INSTALL_PREFIX="$INSTALL" \
    -DLSL_BUILD_STATIC=OFF \
    -DLSL_BUILD_EXAMPLES=OFF \
    -DLSL_BUILD_TESTING=OFF \
    -DLSL_ALLOW_SUBPROJECT=ON \
    -DLSL_UNIXFOLDERS=ON
cmake --build "$BUILD" -j "$(nproc)"
cmake --install "$BUILD"

LIB="$INSTALL/lib/liblsl.so.1.17.7"
[ -f "$LIB" ] || { echo "expected library missing: $LIB" >&2; exit 1; }
GOT="$(sha256sum "$LIB" | cut -d' ' -f1)"
if [ "$GOT" != "$EXPECT_LIB_SHA256" ]; then
    echo "library hash mismatch:" >&2
    echo "  expected $EXPECT_LIB_SHA256" >&2
    echo "  actual   $GOT" >&2
    echo "  If upstream build is legitimately different on this host, update EXPECT_LIB_SHA256 with justification." >&2
    exit 1
fi

REPORT_LSLVER="$("$INSTALL/bin/lslver" 2>/dev/null | head -1)"
REPORT_COMMIT="$(git -C "$SRC" rev-parse HEAD)"
printf '{"component": "liblsl", "version": "%s", "commit": "%s", "license": "MIT", "upstream": "https://github.com/sccn/liblsl", "lib_sha256": "%s", "lslver": "%s"}\n' "$TAG" "$REPORT_COMMIT" "$GOT" "$REPORT_LSLVER"
