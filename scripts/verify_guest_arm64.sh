#!/bin/bash
# In-guest ARM64 XTOS system validation for QEMU.
#
# Builds a dedicated validation initramfs from the ARM64 rootfs: /init is
# replaced by system/rootfs/xtos-guest-check, which loads the XTOS kernel
# modules and runs the static test binaries inside the guest. Boot output is
# parsed for machine-checkable markers.
#
# Requires: a completed ./scripts/build_system_arm64.sh run.

set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_ROOT="${BUILD_ROOT:-$ROOT/.build}"
QEMU_TIMEOUT="${QEMU_TIMEOUT:-300}"
QEMU_MEM="${QEMU_MEM:-2G}"
QEMU_SMP="${QEMU_SMP:-2}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[ARM64 GUEST]${NC} $*"; }
warn() { echo -e "${YELLOW}[ARM64 GUEST WARN]${NC} $*"; }
error() { echo -e "${RED}[ARM64 GUEST ERROR]${NC} $*" >&2; }

KERNEL="$BUILD_ROOT/kernel-install-arm64/Image"
ROOTFS="$BUILD_ROOT/rootfs-arm64"
CHECK="$ROOT/system/rootfs/xtos-guest-check"

if [ ! -f "$KERNEL" ]; then
    error "ARM64 kernel not found: $KERNEL"
    error "Run ./scripts/build_system_arm64.sh first"
    exit 1
fi
if [ ! -d "$ROOTFS" ]; then
    error "ARM64 rootfs not found: $ROOTFS"
    error "Run ./scripts/build_system_arm64.sh first"
    exit 1
fi
if [ ! -f "$CHECK" ]; then
    error "guest check script not found: $CHECK"
    exit 1
fi

# Build the validation initramfs in a copy so the build output stays intact.
log "Packing validation initramfs..."
VALIDATION="$BUILD_ROOT/initramfs-arm64-guestcheck.cpio.gz"
STAGE="$BUILD_ROOT/rootfs-arm64-guestcheck"
rm -rf "$STAGE"
cp -a "$ROOTFS" "$STAGE"
rm -f "$STAGE/init"
install -m 0755 "$CHECK" "$STAGE/init"
(cd "$STAGE" && find . | cpio -o -H newc 2>/dev/null | gzip -6) > "$VALIDATION"
rm -rf "$STAGE"

OUTPUT="$BUILD_ROOT/qemu-arm64-guestcheck-output.txt"
rm -f "$OUTPUT"

log "Booting ARM64 QEMU for in-guest validation..."
log "Output: $OUTPUT"

timeout "$QEMU_TIMEOUT" qemu-system-aarch64 \
    -machine virt \
    -cpu cortex-a57 \
    -m "$QEMU_MEM" \
    -smp "$QEMU_SMP" \
    -kernel "$KERNEL" \
    -initrd "$VALIDATION" \
    -append "console=ttyAMA0 panic=1" \
    -nographic \
    -no-reboot \
    2>&1 | tee "$OUTPUT" || true

sleep 1

log ""
log "=== In-guest Validation Results ==="

PASS=0
FAIL=0

check() {
    local pattern="$1"
    local description="$2"
    if grep -q "$pattern" "$OUTPUT" 2>/dev/null; then
        log "[✓] $description"
        PASS=$((PASS+1))
    else
        error "[✗] $description"
        FAIL=$((FAIL+1))
    fi
}

check "XTOS_GUEST_CHECK=START" "Guest check ran"
check "realtime=1" "PREEMPT_RT active (/sys/kernel/realtime)"
check "preempt_rt=y" "CONFIG_PREEMPT_RT in ikconfig"
check "xtos_sched=y" "XTOS scheduler built in"
check "xtos_modules=pass" "XTOS BCI modules loaded"
check "module_xtos_bci_core=ok" "xtos_bci_core module"
check "module_xtos_bci_buffer=ok" "xtos_bci_buffer module"
check "module_openbci_cyton=ok" "openbci_cyton module"
check "guest_test_rt=pass" "test_rt in guest"
check "guest_test_spsc=pass" "test_spsc in guest"
check "guest_test_signal=pass" "test_signal in guest"
check "guest_userspace=pass" "All guest user-space tests"
check "XTOS_GUEST_CHECK=DONE" "Guest check completed (clean poweroff)"

if grep -q "Kernel panic" "$OUTPUT" 2>/dev/null; then
    error "[✗] Kernel panic detected"
    FAIL=$((FAIL+1))
else
    log "[✓] No kernel panic"
    PASS=$((PASS+1))
fi

log ""
log "=== Summary ==="
log "Passed: $PASS"
log "Failed: $FAIL"

if [ "$FAIL" -gt 0 ]; then
    error "ARM64 in-guest validation FAILED ($FAIL checks)"
    error "Check $OUTPUT for details"
    exit 1
else
    log "ARM64 in-guest validation PASSED"
    exit 0
fi
