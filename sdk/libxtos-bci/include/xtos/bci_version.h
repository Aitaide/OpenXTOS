/* SPDX-License-Identifier: Apache-2.0 */
#ifndef XTOS_BCI_VERSION_H
#define XTOS_BCI_VERSION_H

/*
 * libxtos-bci ABI version.
 *
 * Policy (see docs/abi_versioning.md):
 *   MAJOR increments on any ABI break (symbol removal/signature change,
 *   struct layout change). MINOR increments on added symbols. PATCH
 *   increments on behavior-only fixes.
 *
 * NOTE (2026-10-02): the BCI UAPI was integer-ized during the ARM64/RISC-V
 * port (impedance array float[32] -> uint32_t[32] kOhm, input_range_mv
 * float -> uint32_t). The ioctl command numbers are unchanged because
 * float and uint32_t occupy the same 4 bytes, but struct field semantics
 * changed; downstream consumers must recompile. Recorded here as the
 * baseline of ABI major 1.
 *
 * The shared-object file names are derived from these values:
 *   libxtos-bci.so.(MAJOR).(MINOR)  real file
 *   libxtos-bci.so.(MAJOR)          SONAME
 *   libxtos-bci.so                  development symlink
 */

#define XTOS_BCI_VERSION_MAJOR 1
#define XTOS_BCI_VERSION_MINOR 0
#define XTOS_BCI_VERSION_PATCH 0

#define XTOS_BCI_VERSION_STRING "1.0.0"

#endif /* XTOS_BCI_VERSION_H */
