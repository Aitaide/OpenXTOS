#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Reproducible-build environment for all XTOS system builds.
#
# Source this file (dot scripts/reproducible_env.sh) before running
# build_system*.sh. It pins every known source of build non-determinism to
# values derived from the repository, so two builds from the same commit
# produce byte-identical artifacts (or, where that is impossible, differ
# only in documented ways).
#
#  * SOURCE_DATE_EPOCH       — reference time for every embedded timestamp
#                              (kernel banner, kconfig AUTOCONF_TIMESTAMP,
#                              BusyBox banner). Default: the timestamp of the
#                              HEAD commit; override for builds that must be
#                              pinned to a fixed epoch.
#  * KBUILD_BUILD_VERSION    — kernel "#N" build counter (default 1)
#  * KBUILD_BUILD_USER/HOST   — kernel banner "user@host" (default xbuild)
#  * KBUILD_BUILD_TIMESTAMP  — kernel banner date, from SOURCE_DATE_EPOCH
#  * LC_ALL / TZ             — stable locale and timezone for tools
#
# These are respected natively by Linux 6.6 kbuild and BusyBox 1.37 kconfig
# (SOURCE_DATE_EPOCH) — no source patching required.

xtos_repro_epoch() {
    if [ -n "${SOURCE_DATE_EPOCH:-}" ]; then
        printf '%s' "$SOURCE_DATE_EPOCH"
        return 0
    fi
    git -C "$(dirname "${BASH_SOURCE[0]}")/.." log -1 --pretty=%ct 2>/dev/null \
        || printf '%s' "$(date +%s)"
}

export SOURCE_DATE_EPOCH="$(xtos_repro_epoch)"
export KBUILD_BUILD_VERSION="${KBUILD_BUILD_VERSION:-1}"
export KBUILD_BUILD_USER="${KBUILD_BUILD_USER:-xbuild}"
export KBUILD_BUILD_HOST="${KBUILD_BUILD_HOST:-xbuild}"
export KBUILD_BUILD_TIMESTAMP="$(date -u -d "@$SOURCE_DATE_EPOCH" '+%a %b %d %H:%M:%S %Y %Z' 2>/dev/null || printf '@%s' "$SOURCE_DATE_EPOCH")"
export LC_ALL=C
export TZ=UTC

# Print a summary once per shell when XTOS_REPRO_VERBOSE is set.
if [ -n "${XTOS_REPRO_VERBOSE:-}" ]; then
    printf 'reproducible_env: SOURCE_DATE_EPOCH=%s (=%s)\n' \
        "$SOURCE_DATE_EPOCH" "$KBUILD_BUILD_TIMESTAMP" >&2
    printf 'reproducible_env: kernel banner will be: #%s %s@%s %s\n' \
        "$KBUILD_BUILD_VERSION" "$KBUILD_BUILD_USER" "$KBUILD_BUILD_HOST" \
        "$KBUILD_BUILD_TIMESTAMP" >&2
fi
