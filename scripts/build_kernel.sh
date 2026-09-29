#!/bin/bash
# XTOS Kernel Build Script
# Builds Linux 6.6 + PREEMPT_RT with XTOS configuration

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
KERNEL_DIR="$PROJECT_ROOT/kernel/linux"
BUILD_LOG="$PROJECT_ROOT/kernel/build.log"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1" | tee -a "$BUILD_LOG"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1" | tee -a "$BUILD_LOG"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1" | tee -a "$BUILD_LOG"
}

# Check if kernel source exists
if [ ! -d "$KERNEL_DIR" ]; then
    log_error "Kernel directory not found: $KERNEL_DIR"
    exit 1
fi

cd "$KERNEL_DIR"

# Check if kernel source is extracted
if [ ! -f "Makefile" ]; then
    log_error "Kernel source not properly extracted"
    exit 1
fi

log_info "Starting XTOS kernel build..."

# Apply PREEMPT_RT patch if not already applied
if [ ! -f ".rt_patch_applied" ]; then
    log_info "Applying PREEMPT_RT patch..."
    if [ -f "$PROJECT_ROOT/kernel/patch-6.6-rt15.patch.xz" ]; then
        xzcat "$PROJECT_ROOT/kernel/patch-6.6-rt15.patch.xz" | patch -p1 >> "$BUILD_LOG" 2>&1
        touch .rt_patch_applied
        log_info "PREEMPT_RT patch applied successfully"
    else
        log_warn "RT patch not found, skipping..."
    fi
fi

# Apply XTOS configuration
log_info "Applying XTOS kernel configuration..."
if [ -f "$PROJECT_ROOT/kernel/xtos_defconfig" ]; then
    make mrproper >> "$BUILD_LOG" 2>&1
    make defconfig >> "$BUILD_LOG" 2>&1
    
    # Merge XTOS config
    scripts/kconfig/merge_config.sh .config "$PROJECT_ROOT/kernel/xtos_defconfig" >> "$BUILD_LOG" 2>&1 || {
        log_warn "merge_config.sh failed, using manual merge"
        cat "$PROJECT_ROOT/kernel/xtos_defconfig" >> .config
        make olddefconfig >> "$BUILD_LOG" 2>&1
    }
    
    log_info "Configuration applied"
else
    log_error "XTOS configuration file not found"
    exit 1
fi

# Detect CPU count
NPROC=$(nproc)
log_info "Building kernel with $NPROC parallel jobs..."

# Build kernel
if make -j"$NPROC" bzImage modules >> "$BUILD_LOG" 2>&1; then
    log_info "Kernel compiled successfully"
    
    # Check bzImage
    if [ -f "arch/x86/boot/bzImage" ]; then
        BZIMAGE_SIZE=$(du -h arch/x86/boot/bzImage | cut -f1)
        log_info "bzImage created: $BZIMAGE_SIZE"
    else
        log_error "bzImage not found after build"
        exit 1
    fi
else
    log_error "Kernel build failed. Check $BUILD_LOG for details"
    exit 1
fi

log_info "Build completed successfully"
exit 0
