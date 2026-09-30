# PREEMPT_RT Validation

## Scope

This validation targets the Linux source tree at `/root/xtos-project/kernel/linux`.
The tree reports Linux `6.6.0-rt15` through `localversion-rt`. The existing XTOS
scheduler and BCI changes were left in place and were not modified by this task.

The RT build uses a separate Kbuild output directory:

- Source: `/root/xtos-project/kernel/linux`
- Independent output: `/root/xtos-project/kernel/preempt-rt-build`
- Script: [`scripts/build_preempt_rt_validation.sh`](../scripts/build_preempt_rt_validation.sh)
- Final config: `kernel/preempt-rt-build/.config`
- Build and validation log: `kernel/preempt-rt-build/build.log`
- QEMU log: `kernel/preempt-rt-build/qemu.log`
- bzImage: `kernel/preempt-rt-build/arch/x86/boot/bzImage`

The source tree `.config` was not overwritten. Its SHA-256 was
`6beb1f41784751afbe1802659e3f7b23449dfcbfc008469aa21685a32708eab9` both before
and after the validation build.

## 1. Configuration

The actual definition is in `kernel/Kconfig.preempt`:

```text
config PREEMPT_RT
    bool "Fully Preemptible Kernel (Real-Time)"
    depends on EXPERT && ARCH_SUPPORTS_RT
    select PREEMPT_BUILD_AUTO if HAVE_PREEMPT_AUTO
    select PREEMPTION
```

On this x86_64 tree, `CONFIG_ARCH_SUPPORTS_RT=y`, so the RT option is available
when `CONFIG_EXPERT=y` is selected.

The relevant mutual exclusion is also in `kernel/Kconfig.preempt`:

```text
config PREEMPT_DYNAMIC
    bool "Preemption behaviour defined on boot"
    depends on HAVE_PREEMPT_DYNAMIC && !PREEMPT_RT && !PREEMPT_AUTO
```

Therefore this is not a valid RT configuration:

```text
CONFIG_PREEMPT_NONE=y
CONFIG_PREEMPT_DYNAMIC=y
```

The independent resolved configuration is:

```text
CONFIG_EXPERT=y
CONFIG_ARCH_SUPPORTS_RT=y
CONFIG_PREEMPT_RT=y
CONFIG_PREEMPTION=y
CONFIG_PREEMPT_BUILD=y
CONFIG_PREEMPT_BUILD_AUTO=y
# CONFIG_PREEMPT_DYNAMIC is not set
# CONFIG_PREEMPT_NONE is not set
# CONFIG_PREEMPT_VOLUNTARY is not set
# CONFIG_PREEMPT is not set
CONFIG_HIGH_RES_TIMERS=y
CONFIG_HZ=1000
CONFIG_IKCONFIG=y
CONFIG_IKCONFIG_PROC=y
```

`IKCONFIG` and `IKCONFIG_PROC` were enabled only so that the boot validation can
read `/proc/config.gz`. RT runtime visibility is also checked through the kernel
sysfs attribute `/sys/kernel/realtime`, which is provided by the RT build.

The script uses `scripts/config`, then runs `make O=... ARCH=x86 olddefconfig`.
It does not use or replace the source tree's existing `.config`.

### Out-of-tree build note

This source tree has existing generated configuration state and unrelated local
XTOS changes. This Linux 6.6 Makefile refuses a normal `O=` invocation when the
source tree contains `.config`, `include/config`, or generated headers. The
script temporarily moves only those generated paths out of the source tree,
restores them with an `EXIT` trap, and performs the build in the independent
output directory. The source files and XTOS code are not changed.

## 2. Compilation

The x86_64 build completed successfully:

```text
Kernel: arch/x86/boot/bzImage is ready  (#1)
bzImage_bytes=12494208
```

`file` identified the result as an x86 boot executable, version
`6.6.0-rt15-xtos-rt-validation`, with `#1 SMP PREEMPT_RT` in the build version.
The build log is preserved at `kernel/preempt-rt-build/build.log`.

## 3. QEMU Boot

The generated bzImage booted with QEMU and a generated minimal initramfs
containing dynamically linked BusyBox, its required loader/libraries, and
`cyclictest`. QEMU command parameters are encoded in the script:

```text
-machine pc -cpu max -m 1024 -smp 2
-kernel kernel/preempt-rt-build/arch/x86/boot/bzImage
-initrd kernel/preempt-rt-build/initramfs.cpio.gz
-append "console=ttyS0 rdinit=/init loglevel=7"
-nographic -no-reboot
```

Observed in `kernel/preempt-rt-build/qemu.log`:

```text
Linux version 6.6.0-rt15-xtos-rt-validation ... #1 SMP PREEMPT_RT
uname: Linux (none) 6.6.0-rt15-xtos-rt-validation #1 SMP PREEMPT_RT ... x86_64 GNU/Linux
sysfs realtime: 1
config PREEMPT_RT: CONFIG_PREEMPT_RT=y
config PREEMPT_DYNAMIC: unset
qemu_exit=0
qemu_validation=pass
```

This verifies both the kernel build identity and runtime RT configuration. The
QEMU log also shows RT-specific initialization, including RCU priority boosting
and `rcuc` kthreads.

## 4. Latency Tests

### Host test

`cyclictest` ran for 10 seconds on the current host kernel:

```text
Linux debian 6.12.57+deb13-amd64 ... SMP PREEMPT_DYNAMIC
T: 0 ... P:80 I:1000 C:10000 Min: 6 Act: 13 Avg: 14 Max: 50
```

Saved at `kernel/preempt-rt-build/host-cyclictest.log`.

This is a measurement of the host's Debian `PREEMPT_DYNAMIC` kernel, not the
new XTOS RT kernel. `stress-ng` is not installed on the host, so no loaded-host
measurement was reported.

### QEMU test

The initramfs ran one 10-second `cyclictest` thread inside the RT kernel:

```text
--- QEMU cyclictest, virtualized, 10 seconds ---
T: 0 ... P:80 I:1000 C:9964 Min:85 Act:304 Avg:256 Max:9202
--- end validation ---
```

The QEMU run also emitted the expected inability to set host power-management
latency (`cpu_dma_latency`) and an initramfs `shm_open` warning; cyclictest
still completed and reported the full sample statistics. The complete output is
saved in `kernel/preempt-rt-build/qemu.log`. These environment warnings do not
turn the result into a host-hardware measurement.

QEMU timer and scheduling latency is virtualized and includes host contention,
VM exits, emulated devices, and TCG behavior. It must not be used as a real
hardware real-time guarantee or as a substitute for target-board testing.

## 5. Real Hardware Validation

Not performed in this environment. The available validation proves:

- the source's real `CONFIG_PREEMPT_RT` Kconfig option resolves to `y`;
- the independent x86_64 RT kernel compiles;
- the resulting bzImage boots in QEMU;
- runtime config and `/sys/kernel/realtime` identify an RT kernel; and
- timer latency tools execute on the host and in QEMU.

A real claim about deterministic latency still requires the target x86_64
hardware, its BIOS/firmware, CPU power and C-state policy, IRQ/device topology,
SMIs, workload, isolation/affinity policy, and a long-duration cyclictest or
`rtla timerlat` campaign under representative load. QEMU results are explicitly
excluded from that claim.
