# XTOS Development Environment Setup Report

## Overview
This document describes the setup process for the XTOS (eXtended real-Time Operating System) development environment.

**Date**: 2026-09-30  
**Target**: Linux 6.6 LTS + PREEMPT_RT patch  
**Architecture**: x86_64

---

## Directory Structure

```
xtos-project/
├── kernel/
│   ├── linux/              # Linux 6.6 source code
│   ├── xtos_defconfig      # XTOS kernel configuration
│   └── patch-6.6-rt15.patch.xz  # PREEMPT_RT patch
├── drivers/                # Custom device drivers
├── sdk/                    # User-space SDK
├── tools/                  # Development utilities
├── examples/               # Example applications
├── tests/                  # Test suites
└── scripts/
    ├── setup_environment.sh    # Environment setup
    ├── build_kernel.sh         # Kernel build automation
    └── run_cyclictest.sh       # Latency testing
```

---

## Development Tools

### Required Packages
- **gcc** (14.2.0): C compiler
- **make** (4.4.1): Build automation
- **flex**: Lexical analyzer generator
- **bison**: Parser generator
- **libssl-dev**: SSL development libraries
- **libelf-dev**: ELF library for kernel modules
- **bc**: Basic calculator for kernel build
- **rt-tests**: Real-time testing tools (cyclictest)

### Installation Command
```bash
apt-get update
apt-get install -y build-essential gcc make flex bison \
    libssl-dev libelf-dev bc curl wget xz-utils git \
    rt-tests linux-cpupower stress-ng
```

---

## Kernel Source

### Linux 6.6 LTS
- **Source**: https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.6.tar.xz
- **Size**: ~117 MB compressed
- **Location**: `/root/xtos-project/kernel/linux-6.6.tar.xz`

### PREEMPT_RT Patch
- **Patch**: patch-6.6-rt15
- **Source**: https://mirrors.edge.kernel.org/pub/linux/kernel/projects/rt/6.6/older/
- **Size**: 88 KB compressed
- **Location**: `/root/xtos-project/kernel/patch-6.6-rt15.patch.xz`

---

## XTOS Kernel Configuration

The `xtos_defconfig` enables the following real-time features:

### Core Real-Time Features
```
CONFIG_PREEMPT_RT=y              # Full preemption
CONFIG_NO_HZ_FULL=y              # Tickless operation
CONFIG_HIGH_RES_TIMERS=y         # High-resolution timers
CONFIG_IRQ_FORCED_THREADING=y    # Threaded IRQ handlers
CONFIG_RCU_NOCB_CPU=y            # No-callback RCU
```

### Performance Tuning
- **Timer Frequency**: 1000 Hz (CONFIG_HZ_1000)
- **Preemption Model**: Full RT preemption
- **RCU Boost**: Enabled (500μs delay)
- **CPU Isolation**: Enabled for dedicated cores

### Target Performance Metrics
- **Scheduling Latency**: < 10 μs
- **Maximum Interrupt Latency**: < 50 μs (target)
- **EEG Processing Latency**: < 15 ms (end-to-end)

---

## Build Process

### 1. Extract Kernel Source
```bash
cd /root/xtos-project/kernel/linux
tar -xf ../linux-6.6.tar.xz --strip-components=1
```

### 2. Apply PREEMPT_RT Patch
```bash
xzcat ../patch-6.6-rt15.patch.xz | patch -p1
```

### 3. Apply XTOS Configuration
```bash
make mrproper
make defconfig
scripts/kconfig/merge_config.sh .config ../xtos_defconfig
make olddefconfig
```

### 4. Build Kernel
```bash
make -j$(nproc) bzImage modules
```

**Expected Output**: 
- `arch/x86/boot/bzImage` (bootable kernel image)
- Compiled kernel modules

### 5. Automated Build Script
```bash
/root/xtos-project/scripts/build_kernel.sh
```

This script automates the entire build process with error handling and logging to `kernel/build.log`.

---

## Real-Time Performance Testing

### Cyclictest
Cyclictest measures system latency by running high-priority threads and measuring scheduling delays.

**Command**:
```bash
cyclictest -t$(nproc) -p99 -i1000 -n -m -q -D60
```

**Parameters**:
- `-t$(nproc)`: One thread per CPU core
- `-p99`: Real-time priority 99 (highest)
- `-i1000`: 1000μs interval
- `-n`: Use clock_nanosleep
- `-m`: Lock memory (prevent swapping)
- `-q`: Quiet mode (summary only)
- `-D60`: Run for 60 seconds

**Target Results**:
- **Min Latency**: ~1-2 μs
- **Average Latency**: ~5-8 μs
- **Max Latency**: < 50 μs (acceptable for EEG)

### Automated Test Script
```bash
/root/xtos-project/scripts/run_cyclictest.sh -d 60 -i 1000
```

---

## Git Repository Management

### Initialization
```bash
git init
git branch -m main
git config user.name "XTOS Build System"
git config user.email "xtos@example.com"
```

### Branch Strategy
- **main**: Stable releases
- **develop**: Integration branch
- **feature/***: Feature development
- **hotfix/***: Emergency fixes

### Initial Commit
```bash
git add .gitignore README.md docs/ scripts/ kernel/xtos_defconfig
git commit -m "Initial XTOS environment setup"
```

---

## Next Steps

### Phase 1 (Month 1) - Remaining Tasks
1. ✅ Environment setup and tool installation
2. ✅ Download Linux 6.6 source
3. ✅ Download PREEMPT_RT patch
4. ✅ Create XTOS configuration
5. ⏳ Extract kernel source (in progress)
6. ⏳ Apply RT patch
7. ⏳ Build kernel
8. ⏳ Run cyclictest baseline

### Phase 2 (Month 2) - Scheduler Implementation
- Custom real-time scheduler with EEG-aware priority
- Latency reduction optimizations
- CPU affinity management

### Phase 3 (Month 3) - Driver Framework
- EEG device driver interface
- USB/Serial device support
- Real-time data streaming

---

## Troubleshooting

### Common Build Issues

**Issue**: Missing dependencies
```bash
# Solution: Run setup script
./scripts/setup_environment.sh
```

**Issue**: Patch application fails
```bash
# Solution: Ensure clean source tree
cd kernel/linux
make mrproper
xzcat ../patch-6.6-rt15.patch.xz | patch -p1
```

**Issue**: Build fails with memory error
```bash
# Solution: Reduce parallel jobs
make -j2 bzImage modules
```

---

## Performance Monitoring Commands

### Check CPU Frequency Scaling
```bash
cpupower frequency-info
```

### Disable CPU Idle States (for testing)
```bash
cpupower idle-set -D 0
```

### Monitor Real-Time Threads
```bash
ps -eo pid,class,rtprio,ni,comm | grep FF
```

### Check Kernel Configuration
```bash
zcat /proc/config.gz | grep -E "PREEMPT|HZ|RCU"
```

---

## References

- Linux Kernel Documentation: https://kernel.org/doc/html/latest/
- PREEMPT_RT Wiki: https://wiki.linuxfoundation.org/realtime/
- Cyclictest Guide: https://wiki.linuxfoundation.org/realtime/documentation/howto/tools/cyclictest/
- XTOS Technical Specification: `docs/XTOS_Technical_Specification.md`

---

**Status**: Environment setup in progress  
**Last Updated**: 2026-09-30
