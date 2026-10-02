#!/bin/bash
# RISC-V 64 cross-compile build script for XTOS
# Requires: riscv64-linux-gnu-gcc, make, rsync, cpio, gzip, file
#
# Mirrors build_system_arm64.sh: RT kernel + BusyBox + XTOS user-space
# libraries + static test binaries + initramfs, for qemu-system-riscv64.

set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_ROOT="${BUILD_ROOT:-$ROOT/.build}"
JOBS="${JOBS:-$(nproc)}"

# RISC-V 64 cross-compile toolchain
export ARCH=riscv
export CROSS_COMPILE=riscv64-linux-gnu-

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() { echo -e "${GREEN}[RISCV64]${NC} $*"; }
warn() { echo -e "${YELLOW}[RISCV64 WARN]${NC} $*"; }
error() { echo -e "${RED}[RISCV64 ERROR]${NC} $*" >&2; }

# Validate toolchain
if ! command -v riscv64-linux-gnu-gcc &>/dev/null; then
    error "riscv64-linux-gnu-gcc not found. Install gcc-riscv64-linux-gnu."
    exit 1
fi
if ! command -v file &>/dev/null; then
    error "file not found. Install file."
    exit 1
fi

log "RISC-V 64 cross-compile toolchain: $(riscv64-linux-gnu-gcc --version | head -1)"
log "Build root: $BUILD_ROOT"
log "Parallel jobs: $JOBS"

# Stage 1: Kernel
log "=== Stage 1: Building RISC-V 64 kernel ==="

KERNEL_SRC="$ROOT/kernel/linux"
KERNEL_BUILD="$BUILD_ROOT/kernel-build-riscv64"
KERNEL_INSTALL="$BUILD_ROOT/kernel-install-riscv64"

mkdir -p "$KERNEL_BUILD" "$KERNEL_INSTALL"

if [ ! -f "$KERNEL_BUILD/.config" ]; then
    log "Generating RISC-V 64 defconfig..."
    make -C "$KERNEL_SRC" O="$KERNEL_BUILD" \
        ARCH=riscv CROSS_COMPILE=riscv64-linux-gnu- \
        defconfig

    # Enable PREEMPT_RT
    "$ROOT/scripts/enable_rt_config.sh" "$KERNEL_BUILD/.config"

    # Merge XTOS configuration fragment (scheduler helper + BCI modules),
    # same as the x86_64 build in scripts/build_kernel.sh
    KCONFIG_MERGE="$KERNEL_SRC/scripts/kconfig/merge_config.sh"
    if [ -f "$KCONFIG_MERGE" ] && [ -f "$ROOT/kernel/xtos_defconfig" ]; then
        log "Merging XTOS kernel configuration fragment..."
        KCONFIG_CONFIG="$KERNEL_BUILD/.config" "$KCONFIG_MERGE" -m \
            "$KERNEL_BUILD/.config" "$ROOT/kernel/xtos_defconfig"
    else
        warn "merge_config.sh or kernel/xtos_defconfig not found; XTOS kernel components disabled"
    fi

    # QEMU virt platform requirements:
    #   - rv64 gc (G = IMAFD + C compressed)
    #   - serial console on 16550 UART at 0x10000000
    #   - virtio-mmio devices
    "$KERNEL_SRC/scripts/config" --file "$KERNEL_BUILD/.config" \
        --enable SERIAL_8250 \
        --enable SERIAL_8250_CONSOLE \
        --enable SERIAL_EARLYCON \
        --enable SERIAL_OF_PLATFORM \
        --enable VIRTIO_MMIO \
        --enable VIRTIO_BLK \
        --enable VIRTIO_NET \
        --enable VIRTIO_CONSOLE \
        --enable HW_RANDOM_VIRTIO \
        --enable DRM_VIRTIO_GPU \
        --enable SERIAL_DEV_BUS \
        --enable SERIAL_DEV_CTRL_TTYPORT

    # Regenerate after changes
    make -C "$KERNEL_SRC" O="$KERNEL_BUILD" \
        ARCH=riscv CROSS_COMPILE=riscv64-linux-gnu- \
        olddefconfig

    # Verify required settings actually took effect
    for setting in CONFIG_PREEMPT_RT=y CONFIG_XTOS_SCHED=y CONFIG_XTOS_BCI=m CONFIG_XTOS_BCI_OPENBCI=m; do
        if ! grep -qx "$setting" "$KERNEL_BUILD/.config"; then
            error "Required kernel setting missing after olddefconfig: $setting"
            exit 1
        fi
    done
    log "XTOS kernel settings verified: XTOS_SCHED=y XTOS_BCI=m XTOS_BCI_OPENBCI=m"
fi

log "Building RISC-V 64 kernel image..."
make -C "$KERNEL_SRC" O="$KERNEL_BUILD" \
    ARCH=riscv CROSS_COMPILE=riscv64-linux-gnu- \
    -j"$JOBS" Image dtbs modules

log "Installing RISC-V 64 kernel modules..."
ROOTFS="$BUILD_ROOT/rootfs-riscv64"
mkdir -p "$ROOTFS"
make -C "$KERNEL_SRC" O="$KERNEL_BUILD" \
    ARCH=riscv CROSS_COMPILE=riscv64-linux-gnu- \
    INSTALL_MOD_PATH="$ROOTFS" \
    modules_install

# Copy kernel image and dtbs
mkdir -p "$KERNEL_INSTALL"
cp "$KERNEL_BUILD/arch/riscv/boot/Image" "$KERNEL_INSTALL/Image"
if [ -d "$KERNEL_BUILD/arch/riscv/boot/dts" ]; then
    log "Copying device tree blobs..."
    rsync -a "$KERNEL_BUILD/arch/riscv/boot/dts/" "$KERNEL_INSTALL/dtbs/"
fi

log "Kernel version: $(cat "$KERNEL_BUILD/include/config/kernel.release")"

# Stage 2: BusyBox
log "=== Stage 2: Building RISC-V 64 BusyBox ==="

BUSYBOX_SRC="$ROOT/userspace/busybox"
BUSYBOX_BUILD="$BUILD_ROOT/busybox-build-riscv64"

mkdir -p "$BUSYBOX_BUILD"

if [ ! -f "$BUSYBOX_BUILD/.config" ]; then
    log "Generating BusyBox config..."
    make -C "$BUSYBOX_SRC" O="$BUSYBOX_BUILD" defconfig

    # Enable static build
    sed -i 's/# CONFIG_STATIC is not set/CONFIG_STATIC=y/' "$BUSYBOX_BUILD/.config"

    # Disable hardware-accelerated hash functions (x86 SHA-NI not available)
    sed -i 's/^CONFIG_SHA1_HWACCEL=y/# CONFIG_SHA1_HWACCEL is not set/' "$BUSYBOX_BUILD/.config"
    sed -i 's/^CONFIG_SHA256_HWACCEL=y/# CONFIG_SHA256_HWACCEL is not set/' "$BUSYBOX_BUILD/.config"

    # Disable tc (traffic control) - kernel header compatibility issues
    sed -i 's/^CONFIG_TC=y/# CONFIG_TC is not set/' "$BUSYBOX_BUILD/.config"
fi

log "Building static RISC-V 64 BusyBox..."
make -C "$BUSYBOX_SRC" O="$BUSYBOX_BUILD" \
    ARCH=riscv CROSS_COMPILE=riscv64-linux-gnu- \
    -j"$JOBS"

log "Installing BusyBox to rootfs..."
make -C "$BUSYBOX_SRC" O="$BUSYBOX_BUILD" \
    ARCH=riscv CROSS_COMPILE=riscv64-linux-gnu- \
    CONFIG_PREFIX="$ROOTFS" \
    install

# Stage 3: XTOS user-space libraries
log "=== Stage 3: Building RISC-V 64 XTOS libraries ==="

build_lib() {
    local lib="$1"
    log "Building $lib..."
    make -C "$ROOT/sdk/$lib" clean
    CC=riscv64-linux-gnu-gcc \
    AR=riscv64-linux-gnu-ar \
        make -C "$ROOT/sdk/$lib" -j"$JOBS"

    cp "$ROOT/sdk/$lib/$lib.a" "$ROOTFS/usr/lib/"
}

mkdir -p "$ROOTFS/usr/lib" "$ROOTFS/usr/include/xtos"

build_lib libxtos-rt
cp "$ROOT/sdk/libxtos-rt/include/xtos/rt.h" "$ROOTFS/usr/include/xtos/"
cp "$ROOT/sdk/libxtos-rt/include/xtos/rt_version.h" "$ROOTFS/usr/include/xtos/"
cp "$ROOT/sdk/libxtos-rt/include/xtos/spsc.h" "$ROOTFS/usr/include/xtos/"

build_lib libxtos-signal
cp "$ROOT/sdk/libxtos-signal/include/xtos/signal.h" "$ROOTFS/usr/include/xtos/"
cp "$ROOT/sdk/libxtos-signal/include/xtos/signal_version.h" "$ROOTFS/usr/include/xtos/"

build_lib libxtos-bci
cp "$ROOT/sdk/libxtos-bci/include/xtos/bci.h" "$ROOTFS/usr/include/xtos/"
cp "$ROOT/sdk/libxtos-bci/include/xtos/bci_version.h" "$ROOTFS/usr/include/xtos/"

# Verify all installed libraries are riscv64 (guards against stale host
# artifacts in the sdk tree breaking Stage 4 static links)
for lib in libxtos-rt libxtos-signal libxtos-bci; do
    if ! riscv64-linux-gnu-objdump -f "$ROOTFS/usr/lib/$lib.a" 2>/dev/null | grep -q "architecture: riscv:rv64"; then
        error "$ROOTFS/usr/lib/$lib.a is not a RISC-V archive"
        error "Clean the sdk build tree and re-run (make clean in sdk/$lib)"
        exit 1
    fi
done

# Stage 4: Test utilities
log "=== Stage 4: Building RISC-V 64 test utilities ==="

mkdir -p "$ROOTFS/sbin" "$ROOTFS/bin"

# Note: init is provided by BusyBox

# Cross-compile test utilities (if any exist in sdk/*/tests/)
TESTS_FAILED=0
if [ -d "$ROOT/sdk/libxtos-rt/tests" ]; then
    for test_src in "$ROOT/sdk/libxtos-rt/tests"/*.c; do
        if [ -f "$test_src" ]; then
            test_name=$(basename "$test_src" .c)
            log "Building test: $test_name"
            if ! riscv64-linux-gnu-gcc -static -O2 \
                "$test_src" \
                -I"$ROOT/sdk/libxtos-rt/include" \
                -I"$ROOT/sdk/libxtos-signal/include" \
                -I"$ROOT/sdk/libxtos-bci/include" \
                -L"$ROOTFS/usr/lib" \
                -lxtos-rt -lxtos-signal -lxtos-bci -lm -lpthread \
                -o "$ROOTFS/bin/$test_name"; then
                warn "Failed to build $test_name"
                TESTS_FAILED=$((TESTS_FAILED+1))
            fi
        fi
    done
fi
if [ "$TESTS_FAILED" -gt 0 ]; then
    warn "$TESTS_FAILED test binary(ies) failed to build; continuing"
fi

# Cross-compile the BCI example (mirrors x86_64 build_system.sh)
if [ -f "$ROOT/examples/bci_test.c" ]; then
    log "Building bci_test example..."
    if riscv64-linux-gnu-gcc -O2 -Wall -Wextra -static \
        -I"$ROOT/sdk/libxtos-bci/include" \
        "$ROOT/examples/bci_test.c" \
        "$ROOTFS/usr/lib/libxtos-bci.a" \
        -o "$ROOTFS/bin/bci_test"; then
        log "bci_test built"
    else
        warn "Failed to build bci_test"
    fi
fi

# Cross-compile signal test (same suite the x86_64 guest runs)
if [ -d "$ROOT/sdk/libxtos-signal/tests" ]; then
    for test_src in "$ROOT/sdk/libxtos-signal/tests"/*.c; do
        if [ -f "$test_src" ]; then
            test_name=$(basename "$test_src" .c)
            log "Building signal test: $test_name"
            if ! riscv64-linux-gnu-gcc -static -O2 -std=c11 -Wall -Wextra \
                "$test_src" \
                -I"$ROOT/sdk/libxtos-signal/include" \
                -L"$ROOTFS/usr/lib" \
                -lxtos-signal -lm \
                -o "$ROOTFS/bin/$test_name"; then
                warn "Failed to build $test_name"
            fi
        fi
    done
fi

# Stage 5: Create initramfs
log "=== Stage 5: Creating RISC-V 64 initramfs ==="

# Create essential directories
mkdir -p "$ROOTFS"/{dev,proc,sys,tmp,root,etc,mnt}

# Create /etc/inittab
cat > "$ROOTFS/etc/inittab" << 'EOF'
::sysinit:/bin/mount -t proc proc /proc
::sysinit:/bin/mount -t sysfs sysfs /sys
::sysinit:/bin/mount -t devtmpfs devtmpfs /dev
::sysinit:/bin/hostname xtos-riscv64
::respawn:/bin/sh
::ctrlaltdel:/bin/reboot
::shutdown:/bin/umount -a -r
EOF

# Create /init symlink
ln -sf sbin/init "$ROOTFS/init"

# Build initramfs (reproducible: fixed mtimes, sorted entries, stable
# ownership and gzip header). Requires scripts/reproducible_env.sh for the
# SOURCE_DATE_EPOCH default; falls back to the HEAD commit time.
REPRO_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --pretty=%ct 2>/dev/null || date +%s)}"
log "Packing RISC-V 64 initramfs (epoch=$REPRO_EPOCH)..."
INITRAMFS="$BUILD_ROOT/initramfs-riscv64.cpio.gz"
(cd "$ROOTFS" \
    && find . -print0 | LC_ALL=C sort -z \
    | xargs -0 touch --no-dereference --date="@$REPRO_EPOCH" \
    && find . -print0 | LC_ALL=C sort -z \
    | cpio --null -o -H newc --owner=0:0 --reproducible 2>/dev/null \
    | gzip -n -6) > "$INITRAMFS"

# Summary
log "=== RISC-V 64 Build Complete ==="
log "Kernel: $KERNEL_INSTALL/Image"
log "Initramfs: $INITRAMFS"
log "DTBs: $KERNEL_INSTALL/dtbs/"
log "Rootfs: $ROOTFS"

# File sizes
log ""
log "Build artifacts:"
ls -lh "$KERNEL_INSTALL/Image" "$INITRAMFS" 2>/dev/null || true

# Verification info
KERNEL_RELEASE=$(cat "$KERNEL_BUILD/include/config/kernel.release" 2>/dev/null || echo "unknown")
log ""
log "Kernel version: $KERNEL_RELEASE"
log "Architecture: riscv64 (RV64GC)"
log "Toolchain: $(riscv64-linux-gnu-gcc --version | head -1)"

# Check if kernel has RT
if grep -q "CONFIG_PREEMPT_RT=y" "$KERNEL_BUILD/.config" 2>/dev/null; then
    log "PREEMPT_RT: enabled"
else
    warn "PREEMPT_RT: disabled"
fi

log ""
log "To verify: ./scripts/verify_system_riscv64.sh"
