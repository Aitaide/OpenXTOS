#!/bin/bash
# In-guest RISC-V 64 XTOS system validation for QEMU.
#
# Builds a dedicated validation initramfs from the RISC-V 64 rootfs: /init is
# replaced by system/rootfs/xtos-guest-check, which loads the XTOS kernel
# modules and runs the static test binaries inside the guest. Boot output is
# parsed for machine-checkable markers.
#
# Requires: a completed ./scripts/build_system_riscv64.sh run.

set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_ROOT="${BUILD_ROOT:-$ROOT/.build}"
QEMU_TIMEOUT="${QEMU_TIMEOUT:-360}"
QEMU_MEM="${QEMU_MEM:-2G}"
QEMU_SMP="${QEMU_SMP:-2}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[RISCV64 GUEST]${NC} $*"; }
warn() { echo -e "${YELLOW}[RISCV64 GUEST WARN]${NC} $*"; }
error() { echo -e "${RED}[RISCV64 GUEST ERROR]${NC} $*" >&2; }

KERNEL="$BUILD_ROOT/kernel-install-riscv64/Image"
ROOTFS="$BUILD_ROOT/rootfs-riscv64"
CHECK="$ROOT/system/rootfs/xtos-guest-check"
OPENSBI="/usr/share/qemu/opensbi-riscv64-generic-fw_dynamic.bin"

if [ ! -f "$KERNEL" ]; then
    error "RISC-V 64 kernel not found: $KERNEL"
    error "Run ./scripts/build_system_riscv64.sh first"
    exit 1
fi
if [ ! -d "$ROOTFS" ]; then
    error "RISC-V 64 rootfs not found: $ROOTFS"
    error "Run ./scripts/build_system_riscv64.sh first"
    exit 1
fi
if [ ! -f "$CHECK" ]; then
    error "guest check script not found: $CHECK"
    exit 1
fi
if [ ! -f "$OPENSBI" ]; then
    error "OpenSBI firmware not found: $OPENSBI (install the opensbi package)"
    exit 1
fi

# Build the validation initramfs in a copy so the build output stays intact.
log "Packing validation initramfs..."
VALIDATION="$BUILD_ROOT/initramfs-riscv64-guestcheck.cpio.gz"
STAGE="$BUILD_ROOT/rootfs-riscv64-guestcheck"
rm -rf "$STAGE"
cp -a "$ROOTFS" "$STAGE"
rm -f "$STAGE/init"
install -m 0755 "$CHECK" "$STAGE/init"
(cd "$STAGE" && find . | cpio -o -H newc 2>/dev/null | gzip -6) > "$VALIDATION"
rm -rf "$STAGE"

OUTPUT="$BUILD_ROOT/qemu-riscv64-guestcheck-output.txt"
rm -f "$OUTPUT"

log "Booting RISC-V 64 QEMU for in-guest validation (OpenSBI -> Linux)..."
log "Output: $OUTPUT"

timeout "$QEMU_TIMEOUT" qemu-system-riscv64 \
    -machine virt \
    -cpu rv64 \
    -smp "$QEMU_SMP" \
    -m "$QEMU_MEM" \
    -bios "$OPENSBI" \
    -kernel "$KERNEL" \
    -initrd "$VALIDATION" \
    -append "console=ttyS0 panic=1" \
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
check "OpenSBI" "OpenSBI firmware booted"
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
    error "RISC-V 64 in-guest validation FAILED ($FAIL checks)"
    error "Check $OUTPUT for details"
    exit 1
else
    log "RISC-V 64 in-guest validation PASSED"
    exit 0
fi
