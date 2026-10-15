#!/bin/bash
# ARM64 cross-compile build script for XTOS
# Requires: aarch64-linux-gnu-gcc, make, rsync, cpio, gzip, host python3

set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_ROOT="${BUILD_ROOT:-$ROOT/.build}"
JOBS="${JOBS:-$(nproc)}"

# ARM64 cross-compile toolchain
export ARCH=arm64
export CROSS_COMPILE=aarch64-linux-gnu-

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() { echo -e "${GREEN}[ARM64]${NC} $*"; }
warn() { echo -e "${YELLOW}[ARM64 WARN]${NC} $*"; }
error() { echo -e "${RED}[ARM64 ERROR]${NC} $*" >&2; }

# Validate toolchain
if ! command -v aarch64-linux-gnu-gcc &>/dev/null; then
    error "aarch64-linux-gnu-gcc not found. Install crossbuild-essential-arm64."
    exit 1
fi

log "ARM64 cross-compile toolchain: $(aarch64-linux-gnu-gcc --version | head -1)"
log "Build root: $BUILD_ROOT"
log "Parallel jobs: $JOBS"

# Stage 1: Kernel
log "=== Stage 1: Building ARM64 kernel ==="

KERNEL_SRC="$ROOT/kernel/linux"
KERNEL_BUILD="$BUILD_ROOT/kernel-build-arm64"
KERNEL_INSTALL="$BUILD_ROOT/kernel-install-arm64"

mkdir -p "$KERNEL_BUILD" "$KERNEL_INSTALL"

if [ ! -f "$KERNEL_BUILD/.config" ]; then
    log "Generating ARM64 defconfig..."
    make -C "$KERNEL_SRC" O="$KERNEL_BUILD" \
        ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- \
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
    
    # ARM64-specific: enable common peripherals
    make -C "$KERNEL_SRC" O="$KERNEL_BUILD" \
        ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- \
        scripts_config || true
    "$KERNEL_SRC/scripts/config" --file "$KERNEL_BUILD/.config" \
        --enable SERIAL_AMBA_PL011 \
        --enable SERIAL_AMBA_PL011_CONSOLE \
        --enable VIRTIO_MMIO \
        --enable VIRTIO_BLK \
        --enable VIRTIO_NET \
        --enable VIRTIO_CONSOLE \
        --enable HW_RANDOM_VIRTIO \
        --enable DRM_VIRTIO_GPU \
        --enable SERIAL_DEV_BUS \
        --enable SERIAL_DEV_CTRL_TTYPORT
    
    # Disable x86-specific options (no-ops on arm64, kept for parity)
    "$KERNEL_SRC/scripts/config" --file "$KERNEL_BUILD/.config" \
        --disable X86_INTEL_LPSS \
        --disable X86_PLATFORM_DEVICES
    
    # Regenerate after changes
    make -C "$KERNEL_SRC" O="$KERNEL_BUILD" \
        ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- \
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

log "Building ARM64 kernel image..."
make -C "$KERNEL_SRC" O="$KERNEL_BUILD" \
    ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- \
    -j"$JOBS" Image dtbs modules

log "Installing ARM64 kernel modules..."
ROOTFS="$BUILD_ROOT/rootfs-arm64"
mkdir -p "$ROOTFS"
make -C "$KERNEL_SRC" O="$KERNEL_BUILD" \
    ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- \
    INSTALL_MOD_PATH="$ROOTFS" \
    modules_install

# Copy kernel image and dtbs
mkdir -p "$KERNEL_INSTALL"
cp "$KERNEL_BUILD/arch/arm64/boot/Image" "$KERNEL_INSTALL/Image"
if [ -d "$KERNEL_BUILD/arch/arm64/boot/dts" ]; then
    log "Copying device tree blobs..."
    rsync -a "$KERNEL_BUILD/arch/arm64/boot/dts/" "$KERNEL_INSTALL/dtbs/"
fi

log "Kernel version: $(cat "$KERNEL_BUILD/include/config/kernel.release")"

# Stage 2: BusyBox
log "=== Stage 2: Building ARM64 BusyBox ==="

BUSYBOX_SRC="$ROOT/userspace/busybox"
BUSYBOX_BUILD="$BUILD_ROOT/busybox-build-arm64"

mkdir -p "$BUSYBOX_BUILD"

if [ ! -f "$BUSYBOX_BUILD/.config" ]; then
    log "Generating BusyBox config..."
    make -C "$BUSYBOX_SRC" O="$BUSYBOX_BUILD" defconfig
    
    # Enable static build
    sed -i 's/# CONFIG_STATIC is not set/CONFIG_STATIC=y/' "$BUSYBOX_BUILD/.config"
    
    # Disable hardware-accelerated hash functions (x86 SHA-NI not available on ARM64)
    sed -i 's/^CONFIG_SHA1_HWACCEL=y/# CONFIG_SHA1_HWACCEL is not set/' "$BUSYBOX_BUILD/.config"
    sed -i 's/^CONFIG_SHA256_HWACCEL=y/# CONFIG_SHA256_HWACCEL is not set/' "$BUSYBOX_BUILD/.config"
    
    # Disable tc (traffic control) - has kernel header compatibility issues on ARM64
    sed -i 's/^CONFIG_TC=y/# CONFIG_TC is not set/' "$BUSYBOX_BUILD/.config"
fi

log "Building static ARM64 BusyBox..."
make -C "$BUSYBOX_SRC" O="$BUSYBOX_BUILD" \
    ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- \
    -j"$JOBS"

log "Installing BusyBox to rootfs..."
make -C "$BUSYBOX_SRC" O="$BUSYBOX_BUILD" \
    ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- \
    CONFIG_PREFIX="$ROOTFS" \
    install

# Stage 3: XTOS user-space libraries
log "=== Stage 3: Building ARM64 XTOS libraries ==="

# libxtos-rt
log "Building libxtos-rt..."
make -C "$ROOT/sdk/libxtos-rt" clean
CC=aarch64-linux-gnu-gcc \
AR=aarch64-linux-gnu-ar \
    make -C "$ROOT/sdk/libxtos-rt" -j"$JOBS"

mkdir -p "$ROOTFS/usr/lib" "$ROOTFS/usr/include/xtos"
cp "$ROOT/sdk/libxtos-rt/libxtos-rt.a" "$ROOTFS/usr/lib/"
cp "$ROOT/sdk/libxtos-rt/include/xtos/rt.h" "$ROOTFS/usr/include/xtos/"
cp "$ROOT/sdk/libxtos-rt/include/xtos/rt_version.h" "$ROOTFS/usr/include/xtos/"

# libxtos-signal
log "Building libxtos-signal..."
make -C "$ROOT/sdk/libxtos-signal" clean
CC=aarch64-linux-gnu-gcc \
AR=aarch64-linux-gnu-ar \
    make -C "$ROOT/sdk/libxtos-signal" -j"$JOBS"

cp "$ROOT/sdk/libxtos-signal/libxtos-signal.a" "$ROOTFS/usr/lib/"
cp "$ROOT/sdk/libxtos-signal/include/xtos/signal.h" "$ROOTFS/usr/include/xtos/"
cp "$ROOT/sdk/libxtos-signal/include/xtos/signal_version.h" "$ROOTFS/usr/include/xtos/"

# libxtos-bci
log "Building libxtos-bci..."
make -C "$ROOT/sdk/libxtos-bci" clean
CC=aarch64-linux-gnu-gcc \
AR=aarch64-linux-gnu-ar \
    make -C "$ROOT/sdk/libxtos-bci" -j"$JOBS"

cp "$ROOT/sdk/libxtos-bci/libxtos-bci.a" "$ROOTFS/usr/lib/"
cp "$ROOT/sdk/libxtos-bci/include/xtos/bci.h" "$ROOTFS/usr/include/xtos/"
cp "$ROOT/sdk/libxtos-bci/include/xtos/bci_version.h" "$ROOTFS/usr/include/xtos/"

# Verify all installed libraries are aarch64 (guards against stale x86_64
# artifacts in the sdk tree breaking Stage 4 static links)
for lib in libxtos-rt libxtos-signal libxtos-bci; do
    if ! aarch64-linux-gnu-objdump -f "$ROOTFS/usr/lib/$lib.a" 2>/dev/null | grep -q "architecture: aarch64"; then
        error "$ROOTFS/usr/lib/$lib.a is not an aarch64 archive"
        error "Clean the sdk build tree and re-run (make clean in sdk/$lib)"
        exit 1
    fi
done

# Stage 4: Test utilities
log "=== Stage 4: Building ARM64 test utilities ==="

mkdir -p "$ROOTFS/sbin" "$ROOTFS/bin"

# Note: init is provided by BusyBox

# Cross-compile test utilities (if any exist in sdk/*/tests/)
TESTS_FAILED=0
if [ -d "$ROOT/sdk/libxtos-rt/tests" ]; then
    for test_src in "$ROOT/sdk/libxtos-rt/tests"/*.c; do
        if [ -f "$test_src" ]; then
            test_name=$(basename "$test_src" .c)
            log "Building test: $test_name"
            if ! aarch64-linux-gnu-gcc -static -O2 \
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
    if aarch64-linux-gnu-gcc -O2 -Wall -Wextra -static \
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
            if ! aarch64-linux-gnu-gcc -static -O2 -std=c11 -Wall -Wextra \
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
log "=== Stage 5: Creating ARM64 initramfs ==="

# Create essential directories
mkdir -p "$ROOTFS"/{dev,proc,sys,tmp,root,etc,mnt}

# Optional XTOS_* metadata is passed literally; regenerate even in reused build roots.
KERNEL_RELEASE="$(cat "$KERNEL_BUILD/include/config/kernel.release")"
python3 "$ROOT/scripts/gen_system_identity.py" --output-dir "$ROOTFS/etc" \
    --image-type "${XTOS_IMAGE_TYPE:-core}" --arch arm64 --kernel-release "$KERNEL_RELEASE" \
    --build-id "${XTOS_BUILD_ID:-unknown}" --source-state "${XTOS_SOURCE_STATE:-unknown}" \
    --hardware-profile "${XTOS_HARDWARE_PROFILE:-unknown}"

# Create /etc/inittab
cat > "$ROOTFS/etc/inittab" << 'EOF'
::sysinit:/bin/mount -t proc proc /proc
::sysinit:/bin/mount -t sysfs sysfs /sys
::sysinit:/bin/mount -t devtmpfs devtmpfs /dev
::sysinit:/bin/hostname xtos-arm64
::respawn:/bin/sh
::ctrlaltdel:/bin/reboot
::shutdown:/bin/umount -a -r
EOF

# Create /init symlink
ln -sf sbin/init "$ROOTFS/init"

# Stage 6: Include target static runtime provenance and component notices.
python3 "$ROOT/scripts/record_static_runtime.py" aarch64-linux-gnu-gcc "$BUILD_ROOT/host-files-arm64.txt"
python3 "$ROOT/scripts/collect_distribution_notices.py" "$ROOT" "$ROOTFS" "$BUILD_ROOT/host-files-arm64.txt" 'arm64 bundle'

# Build initramfs (reproducible: fixed mtimes, sorted entries, stable
# ownership and gzip header). Requires scripts/reproducible_env.sh for the
# SOURCE_DATE_EPOCH default; falls back to the HEAD commit time.
REPRO_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --pretty=%ct 2>/dev/null || date +%s)}"
log "Packing ARM64 initramfs (epoch=$REPRO_EPOCH)..."
INITRAMFS="$BUILD_ROOT/initramfs-arm64.cpio.gz"
(cd "$ROOTFS" \
    && find . -print0 | LC_ALL=C sort -z \
    | xargs -0 touch --no-dereference --date="@$REPRO_EPOCH" \
    && find . -print0 | LC_ALL=C sort -z \
    | cpio --null -o -H newc --owner=0:0 --reproducible 2>/dev/null \
    | gzip -n -6) > "$INITRAMFS"

# Summary
log "=== ARM64 Build Complete ==="
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
log "Architecture: arm64 (aarch64)"
log "Toolchain: $(aarch64-linux-gnu-gcc --version | head -1)"

# Check if kernel has RT
if grep -q "CONFIG_PREEMPT_RT=y" "$KERNEL_BUILD/.config" 2>/dev/null; then
    log "PREEMPT_RT: enabled"
else
    warn "PREEMPT_RT: disabled"
fi

log ""
log "To verify: ./scripts/verify_system_arm64.sh"
