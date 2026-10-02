# ARM64 Cross-Compilation for XTOS

**Status**: In progress  
**Date**: 2026-10-01  
**Target**: ARM64/AArch64 architecture support for XTOS real-time system

---

## Overview

This document describes ARM64 (AArch64) cross-compilation support for XTOS, enabling the system to run on ARM-based embedded platforms, single-board computers, and servers.

## Architecture

**Target Platform**: `aarch64` (ARM 64-bit)  
**Toolchain**: `aarch64-linux-gnu-gcc` (Debian 14.2.0-19)  
**QEMU Machine**: `virt` with Cortex-A57 CPU emulation  
**Kernel**: Linux 6.6.0 + PREEMPT_RT rt15  
**User-Space**: BusyBox 1.37.0 + XTOS libraries (static builds)

## Build Process

### Prerequisites

```bash
# Install ARM64 cross-compilation toolchain
sudo apt-get install crossbuild-essential-arm64 qemu-system-arm

# Verify toolchain
aarch64-linux-gnu-gcc --version
qemu-system-aarch64 --version
```

### Cross-Compile System

```bash
# Build kernel, modules, BusyBox, and XTOS libraries for ARM64
JOBS=4 ./scripts/build_system_arm64.sh
```

**Build stages**:

1. **Kernel**: ARM64 `defconfig` + PREEMPT_RT + Virtio drivers
2. **BusyBox**: Static build for ARM64
3. **XTOS Libraries**: Cross-compiled `libxtos-rt`, `libxtos-signal`, `libxtos-bci`
4. **Init & Tests**: Static ARM64 binaries
5. **Initramfs**: Packed CPIO archive

### QEMU Verification

```bash
# Boot ARM64 system in QEMU (180s timeout)
QEMU_TIMEOUT=180 ./scripts/verify_system_arm64.sh
```

**QEMU configuration**:
- Machine: `virt` (ARM Virtual Machine)
- CPU: `cortex-a57` (ARMv8-A)
- Memory: 2GB
- CPUs: 2
- Serial: `ttyAMA0` (PL011 UART)
- Peripherals: Virtio block, network, console, GPU

## Build Artifacts

```
.build/
├── kernel-build-arm64/          # Kernel build directory
│   ├── arch/arm64/boot/Image    # Uncompressed kernel image
│   └── .config                  # Kernel configuration
├── kernel-install-arm64/
│   ├── Image                    # Kernel image (primary)
│   └── dtbs/                    # Device tree blobs
├── busybox-build-arm64/         # BusyBox build
├── rootfs-arm64/                # Root filesystem
│   ├── bin/busybox              # Static BusyBox
│   ├── sbin/init                # Init binary
│   ├── lib/modules/             # Kernel modules
│   └── usr/lib/                 # XTOS static libraries
└── initramfs-arm64.cpio.gz      # Packed initramfs
```

## ARM64-Specific Configuration

### Kernel Changes

Compared to x86_64 build, ARM64 kernel enables:

- `SERIAL_AMBA_PL011` / `SERIAL_AMBA_PL011_CONSOLE`: ARM PL011 UART
- `VIRTIO_MMIO`: Memory-mapped Virtio devices
- `VIRTIO_BLK`, `VIRTIO_NET`, `VIRTIO_CONSOLE`: Virtio peripherals
- `HW_RANDOM_VIRTIO`: Virtual RNG
- `DRM_VIRTIO_GPU`: Virtio GPU driver

Disabled x86-specific options:
- `X86_INTEL_LPSS`
- `X86_PLATFORM_DEVICES`

### PREEMPT_RT Status

ARM64 kernel is built with full PREEMPT_RT support:
- `CONFIG_PREEMPT_RT=y`
- High-resolution timers
- Threaded IRQ handlers

Real-time performance on ARM64 depends on hardware interrupt latency and clock source quality (not validated in QEMU).

## Cross-Compilation Notes

### Static Linking

All user-space binaries are statically linked to avoid dynamic library dependencies during cross-compilation:

```bash
# BusyBox
CONFIG_STATIC=y

# XTOS test utilities
aarch64-linux-gnu-gcc -static ...
```

### Library Dependencies

XTOS libraries are compiled as static archives (`.a`) and linked into test binaries:

```bash
CC=aarch64-linux-gnu-gcc AR=aarch64-linux-gnu-ar \
    make -C userspace/libxtos-rt
```

### Toolchain Prefix

All build scripts use `CROSS_COMPILE=aarch64-linux-gnu-` and `ARCH=arm64` environment variables for consistent cross-compilation.

## Validation Criteria

### QEMU Verification (Automated)

**Required checks**:
- ✓ Kernel boot
- ✓ ARM64 architecture detection
- ✓ PREEMPT_RT enabled
- ✓ Init process started
- ✓ No kernel panic

**Optional checks** (may require adaptation):
- XTOS BCI modules loaded
- XTOS scheduler active
- BusyBox initialization
- System test markers

### Real Hardware Validation (Future)

**Target platforms**:
- Raspberry Pi 4/5 (BCM2711/BCM2712)
- NVIDIA Jetson Nano/Xavier (Tegra)
- RK3588-based SBCs (Orange Pi 5, Rock 5)
- ARM server platforms (Ampere Altra, AWS Graviton)

**Hardware-specific requirements**:
- Device tree blob (DTB) for target board
- Bootloader configuration (U-Boot, GRUB)
- Storage device drivers (SD/MMC, NVMe)
- Real-time validation (cyclictest on bare metal)

## Known Limitations

1. **QEMU is not real-time**: Virtual clock sources (`arm,armv8-timer`) do not provide deterministic latency
2. **No hardware validation yet**: ARM64 build verified in QEMU only
3. **Module compatibility**: XTOS BCI drivers may need adaptation for ARM serial hardware
4. **Device tree**: Generic `virt` DTB used; real boards need platform-specific DTBs

## Troubleshooting

### Build Failures

**Problem**: `aarch64-linux-gnu-gcc: command not found`  
**Solution**: Install cross-toolchain: `apt-get install crossbuild-essential-arm64`

**Problem**: Kernel modules fail to build  
**Solution**: Check `ARCH=arm64` and `CROSS_COMPILE=aarch64-linux-gnu-` are set

### QEMU Boot Failures

**Problem**: Kernel panic on boot  
**Solution**: Check kernel log in `.build/qemu-arm64-output.txt` for missing drivers

**Problem**: No serial output  
**Solution**: Ensure `console=ttyAMA0` is in kernel command line

### Static Linking Failures

**Problem**: Test binaries fail to link with `undefined reference`  
**Solution**: Ensure static libraries (`.a`) are built before test binaries

## Next Steps

1. **Verify QEMU boot**: Wait for build completion and run `verify_system_arm64.sh`
2. **Adapt XTOS modules**: Test BCI drivers with ARM serial hardware emulation
3. **Real hardware testing**: Deploy to physical ARM64 board with appropriate DTB
4. **RT benchmarking**: Run `cyclictest` on bare-metal ARM64 with isolated cores
5. **CI integration**: Add ARM64 cross-compilation to automated build pipeline

## References

- ARM64 Linux kernel: https://www.kernel.org/doc/html/latest/arch/arm64/
- QEMU ARM System Emulation: https://www.qemu.org/docs/master/system/target-arm.html
- Debian ARM64 Cross-Compilation: https://wiki.debian.org/CrossCompiling
- PREEMPT_RT on ARM: https://wiki.linuxfoundation.org/realtime/start
