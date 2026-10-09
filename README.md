# OpenXTOS

Real-time operating system research platform for brain-computer-interface
(BCI) and deterministic signal-processing workloads, built on Linux 6.6.0
with the PREEMPT_RT patch (rt15).

OpenXTOS ships a complete, buildable system: a PREEMPT_RT Linux kernel with
in-tree XTOS components (a scheduler helper and a BCI device framework),
a static BusyBox userland, and an Apache-2.0 user-space SDK for real-time
and signal-processing work. It boots and self-tests under QEMU on x86_64,
ARM64 (AArch64), and RISC-V 64.

## What's inside

| Directory | Contents |
|-----------|----------|
| `kernel/` | Vendored Linux 6.6.0 source with PREEMPT_RT rt15 applied, plus the XTOS integration patch (`kernel/patches/`) and the XTOS kernel config fragment (`kernel/xtos_defconfig`) |
| `userspace/` | Vendored BusyBox 1.37.0 source (unmodified) |
| `sdk/` | `libxtos-rt` (scheduling helpers + SPSC ring queue), `libxtos-signal` (FFT, notch, bandpass, SSVEP, P300 feature extraction), `libxtos-bci` (device session API) — all Apache-2.0 |
| `system/` | Rootfs skeleton, BusyBox defconfig, boot init, in-guest self-test |
| `tools/` | EEG CSV pipeline, signal benchmark, EEG replay validator |
| `examples/` | BCI session example (`bci_test.c`) |
| `scripts/` | Build, verify, and cross-compile automation for all three architectures |

XTOS kernel components (in the integration patch):
- **Scheduler helper** (`kernel/sched/xtos_sched.c`): opt-in O(1) priority
  queue usable by integration layers without replacing Linux's main
  scheduler class.
- **BCI framework** (`drivers/xtos/bci/`): char-device BCI core, ring buffer,
  and an OpenBCI Cyton serdev driver (24-bit ADC, packet reassembly).
- **UAPI** (`include/uapi/linux/xtos_bci.h`): integer-only ioctl ABI.

## Requirements

- Linux host with GCC, make, flex, bison, bc, libssl-dev, libelf-dev,
  cpio, gzip, rsync, python3
- QEMU for verification (`qemu-system-x86`, `qemu-system-arm`,
  `qemu-system-riscv64`; OpenSBI for RISC-V)
- Cross toolchains for non-x86 builds:
  `crossbuild-essential-arm64`, `gcc-riscv64-linux-gnu`
- ~10 GB free disk; the build does not download any source — Linux and
  BusyBox are vendored in this repository.

## Build and run (x86_64)

```bash
JOBS=$(nproc) ./scripts/build_system.sh          # kernel + BusyBox + SDK + initramfs
QEMU_TIMEOUT=180 ./scripts/verify_system.sh     # boots under QEMU, runs in-guest self-test
./scripts/run_system.sh                          # interactive BusyBox shell instead
```

## Cross-architecture (QEMU-verified)

```bash
JOBS=$(nproc) ./scripts/build_system_arm64.sh      && ./scripts/verify_system_arm64.sh
JOBS=$(nproc) ./scripts/build_system_riscv64.sh   && ./scripts/verify_system_riscv64.sh
```

Both builders merge `kernel/xtos_defconfig` (PREEMPT_RT, XTOS_SCHED,
XTOS_BCI, HZ_1000) and produce a kernel image plus initramfs; the
`verify_guest_*` scripts boot the image and run the in-guest module-load
and SDK test suite.

## Reproducible builds

```bash
. ./scripts/reproducible_env.sh
JOBS=$(nproc) ./scripts/build_system.sh
```

Pins `SOURCE_DATE_EPOCH` (default: HEAD commit time), kernel build
user/host/version, archive mtimes, and gzip headers. Clean rebuilds from
the same commit produce byte-identical kernel and initramfs artifacts.

## SDK overview

```c
#include <xtos/rt.h>        /* scheduler helpers */
#include <xtos/spsc.h>      /* lock-free SPSC sample queue */
#include <xtos/signal.h>    /* FFT, notch, bandpass, SSVEP, P300 */
#include <xtos/bci.h>       /* BCI device sessions (ioctl wrappers) */
```

Each library carries an ABI version header
(`include/xtos/<lib>_version.h`); shared objects follow
`libxtos-*.so.MAJOR.MINOR` with SONAME `libxtos-*.so.MAJOR`.
`scripts/check_abi.sh` verifies the two stay in sync.

## Scope and limitations

- QEMU validation confirms boot, module load, and user-space test
  execution. It is **not** evidence of real-hardware latency.
- Real OpenBCI hardware bring-up, AR/VR input, and medical-grade safety
  claims are out of scope for this repository.
- The XTOS scheduler helper is an auxiliary layer; it does not replace
  Linux's scheduler.

## Licensing

- XTOS user-space SDK, tools, examples, scripts, and system files:
  **Apache-2.0** (see `LICENSE`).
- Vendored Linux 6.6.0 (`kernel/linux/`): GPL-2.0 (see
  `kernel/linux/COPYING`). The PREEMPT_RT patch and the XTOS kernel
  integration patch (`kernel/patches/`) are GPL-2.0.
- Vendored BusyBox 1.37.0 (`userspace/busybox/`): GPL-2.0.

The Apache-2.0 SDK communicates with GPL-2.0 kernel components through
the UAPI boundary (ioctl/syscall), not by linking; see `LICENSE-NOTICE`.

## Attribution and brand use

Commercial use remains permitted under the applicable component licenses.
Preserve their required copyright, license, attribution and modification
notices, and meet GPL corresponding-source obligations when distributing
covered binaries. See [NOTICE](NOTICE) for project attribution information
and [TRADEMARKS.md](TRADEMARKS.md) for the bilingual brand guide.

Clear attribution of the actual upstream version, integrated components,
modifications and https://xtos.cn is recommended in an About, system-information
or open-source notices screen and accompanying documentation. This is not
an additional software-license condition. Logo/co-branding permission is
separate; attribution does not imply official certification or endorsement.

Source distributions include these files at the repository root. Include
the applicable notices and license texts with downstream artifacts; a logo
or website link is not a substitute for GPL or Apache compliance.

## Upstream

This repository is the open-source distribution of XTOS. It contains the
complete buildable system source; internal development history, plans,
and test infrastructure live in the private development tree.
