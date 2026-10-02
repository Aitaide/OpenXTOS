/* SPDX-License-Identifier: Apache-2.0 */
#ifndef XTOS_RT_VERSION_H
#define XTOS_RT_VERSION_H

/*
 * libxtos-rt ABI version.
 *
 * Policy (see docs/abi_versioning.md):
 *   MAJOR increments on any ABI break (symbol removal/signature change,
 *   struct layout change). MINOR increments on added symbols. PATCH
 *   increments on behavior-only fixes.
 *
 * The shared-object file names are derived from these values:
 *   libxtos-rt.so.(MAJOR).(MINOR)  real file
 *   libxtos-rt.so.(MAJOR)          SONAME
 *   libxtos-rt.so                  development symlink
 */

#define XTOS_RT_VERSION_MAJOR 1
#define XTOS_RT_VERSION_MINOR 0
#define XTOS_RT_VERSION_PATCH 0

#define XTOS_RT_VERSION_STRING "1.0.0"

#endif /* XTOS_RT_VERSION_H */
