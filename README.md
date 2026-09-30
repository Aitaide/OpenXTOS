# XTOS

XTOS is an open-source real-time software foundation for brain-computer interface (BCI), AR/VR interaction, and AI-assisted research workloads.

XTOS is currently built as a Linux 6.6 + PREEMPT_RT integration layer. It does not claim to replace the Linux scheduler or to provide medical-device certification.

## Repository scope

This repository is the complete XTOS development source tree. It includes the Linux 6.6.0 kernel source, PREEMPT_RT integration, XTOS kernel components, user-space SDK, tests, documentation, plans, and build scripts.

Kernel configurations and build outputs are generated under `.build/` or a caller-provided output directory; source files remain in the repository.

## Current capabilities

- Optional XTOS scheduler helper integrated through Kconfig.
- BCI core, buffer, and OpenBCI serdev driver sources.
- `libxtos-bci`: user-space BCI API.
- `libxtos-rt`: Linux real-time policy, affinity, locked-memory, and SPSC queue helpers.
- `libxtos-signal`: timestamp validation, correction, FFT power spectrum, configurable notch filtering, and a fourth-order band-pass baseline.
- SSVEP frequency-domain feature baseline with harmonic power and local-noise SNR.
- Reproducible signal-processing benchmark for FFT, band-pass, and SSVEP windows.
- Virtual EEG replay and user-space pipeline tests.
- Reproducible x86_64 Linux 6.6 + PREEMPT_RT preparation scripts.

## Build prerequisites

- Linux host
- Git, curl, xz-utils, patch
- GCC, GNU make, binutils
- Python 3.10 or newer
- Linux kernel build dependencies for a full kernel build
- QEMU is optional and is required only for VM boot validation

## Quick verification

```sh
./scripts/test.sh
```

This runs the user-space libraries, scheduler tests, virtual EEG tests, and signal-processing tests without requiring a kernel source tree or hardware.

To collect the fixed-window signal-processing benchmark:

```sh
./scripts/benchmark_signal.sh
```

The benchmark result is written under `.build/benchmarks/` and is not committed.

### Building the bundled system

Install host prerequisites on Debian/Ubuntu:

```sh
sudo apt-get install build-essential flex bison bc libssl-dev libelf-dev libc6-dev python3 cpio gzip kmod qemu-system-x86
```

Build and boot-verify the complete x86_64 development system:

```sh
JOBS=2 ./scripts/build_system.sh
./scripts/verify_system.sh
./scripts/run_system.sh
```

The build uses the committed Linux/PREEMPT_RT and BusyBox sources without source downloads. It produces `.build/images/xtos-bzImage` and `.build/images/xtos-rootfs.cpio.gz`, including kernel modules, a local shell, XTOS SDK libraries/headers, EEG tools, and guest self-tests. `BUILD_ROOT` can redirect all system build output to an external directory.

This is a bootable development system, not a desktop distribution, disk installer, or production appliance. See `docs/system_build.md` for the architecture, output layout, and validation boundaries.

## Verify the bundled Linux tree

```sh
./scripts/prepare_linux.sh
```

The repository contains Linux 6.6.0 with PREEMPT_RT and XTOS integrated under `kernel/linux/`. The script verifies that tree. Archive-based refresh is retired; the versioned source tree is authoritative.

To build the kernel:

```sh
JOBS=4 ./scripts/build_kernel.sh
```

Kernel builds and QEMU results are local evidence. They are not committed to this repository.

## Repository layout

```text
kernel/
  linux/                 Complete Linux 6.6.0-rt15 + XTOS kernel source
  patches/              XTOS integration patch and source history
  xtos_defconfig        XTOS configuration fragment
system/                 Root filesystem overlay and BusyBox configuration
userspace/busybox/       Complete BusyBox 1.37.0 source
sdk/                    User-space libraries and tests
scripts/                Reproducible preparation, build, and test scripts
tools/                  Virtual EEG tools and fixtures
examples/               Small user-space examples
tests/                  Scheduler tests
docs/                   Maintained technical documentation
plans/                  Development plans
```

## Status and limitations

The x86_64 PREEMPT_RT build and QEMU boot path have been verified locally. Real EEG hardware, real ARM64/RISC-V boards, AR/VR hardware, deterministic target-hardware latency, medical safety, and AI model accuracy remain unverified.

See `docs/XTOS_Phase2_Validation_Report.md`, `docs/eeg_user_pipeline.md`, and `docs/signal_processing_baseline.md` for the current evidence and boundaries.

## License

The bundled kernel source and PREEMPT_RT code follow their upstream licensing requirements. XTOS user-space components are released under Apache-2.0 unless a component file states otherwise. License review and SPDX headers are part of the release gate.
