# Contributing to OpenXTOS

Thanks for your interest in improving OpenXTOS.

## Getting started

1. Fork the repository and clone your fork.
2. Install the build prerequisites (see README.md "Requirements").
3. Verify you can build and boot the system before making changes:

```bash
JOBS=$(nproc) ./scripts/build_system.sh
QEMU_TIMEOUT=180 ./scripts/verify_system.sh
```

## What to keep in mind

- **The kernel is vendored.** `kernel/linux/` is the unmodified Linux 6.6.0
  tree with PREEMPT_RT and the XTOS integration patch already applied. Do
  not reorganize it. XTOS-specific kernel code lives in
  `kernel/patches/xtos-linux-6.6.patch` — if you change XTOS kernel
  components, update the patch so a fresh tree can be reconstructed.
- **License boundaries matter.** Files under `kernel/linux/`,
  `kernel/patches/`, and `userspace/busybox/` are GPL-2.0. New files in
  `sdk/`, `tools/`, `system/`, `scripts/`, and `examples/` should carry an
  `SPDX-License-Identifier: Apache-2.0` line. See LICENSE-NOTICE.
- **The SDK UAPI is integer-only.** Kernel-space code compiles with
  `-mgeneral-regs-only` on ARM64/RISC-V; do not introduce floating-point
  types in kernel headers or drivers.
- **ABI versions.** If you change a public header in `sdk/<lib>/include/`,
  bump the version in the matching `<lib>_version.h` and its Makefile, then
  run `./scripts/check_abi.sh`.
- **Reproducibility.** Builds run under `scripts/reproducible_env.sh`.
  Avoid embedding timestamps, hostnames, or absolute paths in artifacts.

## Submitting changes

1. Create a feature branch.
2. Keep commits focused; conventional commit subjects
   (`feat:`, `fix:`, `docs:` …) are appreciated.
3. Make sure these pass locally:

```bash
JOBS=$(nproc) ./scripts/build_system.sh   # builds clean
QEMU_TIMEOUT=300 ./scripts/verify_system.sh
./scripts/check_abi.sh
```

4. Open a pull request describing what changed and how you verified it
   (CI runs the x86_64 build + QEMU self-test and both cross-arch builds).

## Reporting issues

Include: host OS, toolchain versions, the exact script you ran, and the
full error output. For boot problems, attach the QEMU serial log.
