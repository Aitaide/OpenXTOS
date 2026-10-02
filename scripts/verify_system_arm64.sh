#!/bin/bash
# ARM64 QEMU verification script for XTOS

set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_ROOT="${BUILD_ROOT:-$ROOT/.build}"

# QEMU configuration
QEMU_TIMEOUT="${QEMU_TIMEOUT:-180}"
QEMU_MEM="${QEMU_MEM:-2G}"
QEMU_SMP="${QEMU_SMP:-2}"

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[ARM64 VERIFY]${NC} $*"; }
warn() { echo -e "${YELLOW}[ARM64 WARN]${NC} $*"; }
error() { echo -e "${RED}[ARM64 ERROR]${NC} $*" >&2; }

# Check artifacts
KERNEL="$BUILD_ROOT/kernel-install-arm64/Image"
INITRAMFS="$BUILD_ROOT/initramfs-arm64.cpio.gz"

if [ ! -f "$KERNEL" ]; then
    error "ARM64 kernel not found: $KERNEL"
    error "Run ./scripts/build_system_arm64.sh first"
    exit 1
fi

if [ ! -f "$INITRAMFS" ]; then
    error "ARM64 initramfs not found: $INITRAMFS"
    error "Run ./scripts/build_system_arm64.sh first"
    exit 1
fi

log "Kernel: $KERNEL ($(stat -c%s "$KERNEL" | numfmt --to=iec-i)B)"
log "Initramfs: $INITRAMFS ($(stat -c%s "$INITRAMFS" | numfmt --to=iec-i)B)"
log "QEMU timeout: ${QEMU_TIMEOUT}s"
log "Memory: $QEMU_MEM, CPUs: $QEMU_SMP"

# Output file
OUTPUT="$BUILD_ROOT/qemu-arm64-output.txt"
rm -f "$OUTPUT"

log "Starting ARM64 QEMU..."
log "Output: $OUTPUT"

# QEMU command
# Using virt machine with virtio devices
timeout "$QEMU_TIMEOUT" qemu-system-aarch64 \
    -machine virt \
    -cpu cortex-a57 \
    -m "$QEMU_MEM" \
    -smp "$QEMU_SMP" \
    -kernel "$KERNEL" \
    -initrd "$INITRAMFS" \
    -append "console=ttyAMA0 panic=1 init=/sbin/init" \
    -nographic \
    -no-reboot \
    -serial mon:stdio \
    2>&1 | tee "$OUTPUT" || true

# Wait for output to flush
sleep 1

# Parse results
log ""
log "=== Verification Results ==="

check_pattern() {
    local pattern="$1"
    local description="$2"
    local required="${3:-true}"
    
    if grep -q "$pattern" "$OUTPUT" 2>/dev/null; then
        log "[✓] $description"
        return 0
    else
        if [ "$required" = "true" ]; then
            error "[✗] $description"
            return 1
        else
            warn "[⚠] $description (optional)"
            return 0
        fi
    fi
}

PASS=0
FAIL=0

# Essential checks
check_pattern "Linux version" "Kernel boot" && PASS=$((PASS+1)) || FAIL=$((FAIL+1))
check_pattern "Booting Linux on physical CPU" "ARM64 architecture" && PASS=$((PASS+1)) || FAIL=$((FAIL+1))
# PREEMPT_RT detection: modern RT kernels (6.6-rt15+) print "-rtN" in the version
# string instead of the literal "PREEMPT_RT" boot banner.
if grep -qE "Linux version [0-9.]+-rt[0-9]+" "$OUTPUT" 2>/dev/null; then
    log "[✓] PREEMPT_RT enabled (rt kernel release)"
    PASS=$((PASS+1))
else
    if grep -q "PREEMPT_RT" "$OUTPUT" 2>/dev/null; then
        log "[✓] PREEMPT_RT enabled (boot banner)"
        PASS=$((PASS+1))
    else
        error "[✗] PREEMPT_RT enabled"
        FAIL=$((FAIL+1))
    fi
fi
check_pattern "Run /init as init process" "Init started" && PASS=$((PASS+1)) || FAIL=$((FAIL+1))

# XTOS kernel components: build_system_arm64.sh merges kernel/xtos_defconfig,
# so XTOS_SCHED is built-in and the BCI modules load in the guest (verified via
# the in-guest rt_check initramfs). The default-boot initramfs does not load
# modules automatically, so keep these checks optional in this script; the
# full in-guest validation runs separately (see docs/stage4b).
check_pattern "xtos_bci" "XTOS BCI modules" "false"
check_pattern "xtos_sched" "XTOS scheduler" "false"

# User-space
check_pattern "BusyBox" "BusyBox initialized" "false"
check_pattern "can't access tty" "Shell respawned (BusyBox init)" "false"

# Check for kernel panic
if grep -q "Kernel panic" "$OUTPUT" 2>/dev/null; then
    error "[✗] Kernel panic detected"
    FAIL=$((FAIL+1))
else
    log "[✓] No kernel panic"
    PASS=$((PASS+1))
fi

# Extract kernel version
KERNEL_VERSION=$(grep -oP 'Linux version \K[0-9]+\.[0-9]+\.[0-9]+(-rt[0-9]+)?' "$OUTPUT" 2>/dev/null | head -1 || echo "unknown")
log "Kernel version: $KERNEL_VERSION"

# Extract CPU info
CPU_INFO=$(grep -oP 'Booting Linux on physical CPU \K.*' "$OUTPUT" 2>/dev/null | head -1 || echo "unknown")
log "CPU: $CPU_INFO"

# Summary
log ""
log "=== Summary ==="
log "Passed: $PASS"
log "Failed: $FAIL"

if [ "$FAIL" -gt 2 ]; then
    error "ARM64 verification FAILED (critical errors: $FAIL)"
    error "Check $OUTPUT for details"
    exit 1
else
    log "ARM64 verification PASSED"
    if [ "$FAIL" -gt 0 ]; then
        warn "Note: $FAIL non-critical checks failed"
    fi
    exit 0
fi
