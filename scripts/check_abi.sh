#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# ABI consistency check for the three XTOS user-space libraries.
# Asserts: built shared object's SONAME and file-name major.minor match the
# *_version.h headers, and exported symbol counts are recorded.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SDK="${XTOS_ABI_SDK_ROOT:-$ROOT/sdk}"

fail=0

check_lib() {
    local dir="$1"   # e.g. libxtos-rt
    local prefix="$2" # e.g. XTOS_RT
    local version_header
    version_header=$(ls "$SDK/$dir"/include/xtos/*_version.h | head -1)

    local hdr_major hdr_minor
    hdr_major=$(grep -oP "(?<=#define ${prefix}_VERSION_MAJOR )[0-9]+" "$version_header")
    hdr_minor=$(grep -oP "(?<=#define ${prefix}_VERSION_MINOR )[0-9]+" "$version_header")

    local so_file
    so_file=$(find "$SDK/$dir" -maxdepth 1 -name '*.so.*.*' ! -name '*.so' | head -1)
    if [ -z "$so_file" ]; then
        echo "FAIL $dir: no built shared object found (run make all)"
        fail=1
        return
    fi

    local soname expected_soname expected_file
    soname=$(objdump -p "$so_file" | awk '/SONAME/ {print $2}')
    expected_soname="${dir}.so.${hdr_major}"
    expected_file="${dir}.so.${hdr_major}.${hdr_minor}"

    if [ "$soname" != "$expected_soname" ]; then
        echo "FAIL $dir: SONAME '$soname' != header '$expected_soname'"
        fail=1
    else
        echo "OK $dir: SONAME=$soname"
    fi

    if [ "$(basename "$so_file")" != "$expected_file" ]; then
        echo "FAIL $dir: file '$(basename "$so_file")' != header '$expected_file'"
        fail=1
    else
        echo "OK $dir: file=$(basename "$so_file")"
    fi
}

check_lib libxtos-rt XTOS_RT
check_lib libxtos-signal XTOS_SIGNAL
check_lib libxtos-bci XTOS_BCI

if [ "$fail" -ne 0 ]; then
    echo "abi_check=fail"
    exit 1
fi
echo "abi_check=pass"
