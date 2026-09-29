#!/bin/bash
# Kernel cross-compilation wrapper for ARM64 and RISC-V
# Usage: ./cross_compile_kernel.sh <architecture> <kernel_source_dir> [config]

set -e

ARCH=$1
KERNEL_SRC=$2
CONFIG=${3:-defconfig}

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

usage() {
    echo "Usage: $0 <arch> <kernel_source_dir> [config]"
    echo "  arch: arm64 or riscv64"
    echo "  kernel_source_dir: Path to Linux kernel source"
    echo "  config: Kernel config (default: defconfig)"
    echo ""
    echo "Examples:"
    echo "  $0 arm64 /path/to/linux-6.6"
    echo "  $0 riscv64 /path/to/linux-6.6 defconfig"
    exit 1
}

if [ -z "$ARCH" ] || [ -z "$KERNEL_SRC" ]; then
    usage
fi

if [ ! -d "$KERNEL_SRC" ]; then
    echo -e "${RED}Error: Kernel source directory '$KERNEL_SRC' not found${NC}"
    exit 1
fi

case "$ARCH" in
    arm64|aarch64)
        echo -e "${BLUE}=== ARM64 Kernel Cross-Compilation ===${NC}"
        KARCH="arm64"
        CROSS_COMPILE="aarch64-linux-gnu-"
        ;;
    riscv64|riscv)
        echo -e "${BLUE}=== RISC-V Kernel Cross-Compilation ===${NC}"
        KARCH="riscv"
        CROSS_COMPILE="riscv64-linux-gnu-"
        ;;
    *)
        echo -e "${RED}Error: Unsupported architecture '$ARCH'${NC}"
        exit 1
        ;;
esac

# Check compiler
COMPILER="${CROSS_COMPILE}gcc"
if ! command -v $COMPILER &> /dev/null; then
    echo -e "${RED}Error: Compiler '$COMPILER' not found${NC}"
    exit 1
fi

cd "$KERNEL_SRC"

echo -e "${YELLOW}Kernel source: $KERNEL_SRC${NC}"
echo -e "${YELLOW}Architecture: $KARCH${NC}"
echo -e "${YELLOW}Cross-compiler: $CROSS_COMPILE${NC}"
echo -e "${YELLOW}Configuration: $CONFIG${NC}"
echo ""

# Step 1: Generate configuration
echo -e "${GREEN}[1/3] Generating kernel configuration...${NC}"
make ARCH=$KARCH CROSS_COMPILE=$CROSS_COMPILE $CONFIG

if [ $? -ne 0 ]; then
    echo -e "${RED}Configuration failed${NC}"
    exit 1
fi

# Step 2: Build kernel
echo ""
echo -e "${GREEN}[2/3] Building kernel image...${NC}"
make ARCH=$KARCH CROSS_COMPILE=$CROSS_COMPILE -j$(nproc)

if [ $? -ne 0 ]; then
    echo -e "${RED}Kernel build failed${NC}"
    exit 1
fi

# Step 3: Build device trees
echo ""
echo -e "${GREEN}[3/3] Building device trees...${NC}"
make ARCH=$KARCH CROSS_COMPILE=$CROSS_COMPILE dtbs

if [ $? -ne 0 ]; then
    echo -e "${YELLOW}Warning: Device tree build failed (non-fatal)${NC}"
fi

echo ""
echo -e "${GREEN}✓ Kernel cross-compilation completed successfully!${NC}"
echo ""
echo "Output files:"
if [ "$KARCH" = "arm64" ]; then
    echo "  - Kernel: arch/arm64/boot/Image"
    echo "  - Compressed: arch/arm64/boot/Image.gz"
    ls -lh arch/arm64/boot/Image* 2>/dev/null | awk '{print "    ", $9, "("$5")"}'
elif [ "$KARCH" = "riscv" ]; then
    echo "  - Kernel: arch/riscv/boot/Image"
    echo "  - Compressed: arch/riscv/boot/Image.gz"
    ls -lh arch/riscv/boot/Image* 2>/dev/null | awk '{print "    ", $9, "("$5")"}'
fi

echo ""
echo "To test with QEMU:"
if [ "$KARCH" = "arm64" ]; then
    echo "  qemu-system-aarch64 -M virt -cpu cortex-a57 -m 2048 \\"
    echo "    -kernel arch/arm64/boot/Image \\"
    echo "    -append \"console=ttyAMA0\" -nographic"
elif [ "$KARCH" = "riscv" ]; then
    echo "  qemu-system-riscv64 -M virt -m 2048 \\"
    echo "    -kernel arch/riscv/boot/Image \\"
    echo "    -append \"console=ttyS0\" -nographic"
fi
