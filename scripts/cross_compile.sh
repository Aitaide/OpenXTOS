#!/bin/bash
# Cross-compilation script for ARM64 and RISC-V architectures
# Usage: ./cross_compile.sh <architecture> <source_file> [output_name]

set -e

ARCH=$1
SOURCE=$2
OUTPUT=$3

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

usage() {
    echo "Usage: $0 <arch> <source_file> [output_name]"
    echo "  arch: arm64 or riscv64"
    echo "  source_file: C source file to compile"
    echo "  output_name: Optional output binary name"
    exit 1
}

if [ -z "$ARCH" ] || [ -z "$SOURCE" ]; then
    usage
fi

if [ ! -f "$SOURCE" ]; then
    echo -e "${RED}Error: Source file '$SOURCE' not found${NC}"
    exit 1
fi

# Set default output name if not provided
if [ -z "$OUTPUT" ]; then
    OUTPUT="${SOURCE%.*}_${ARCH}"
fi

case "$ARCH" in
    arm64|aarch64)
        echo -e "${YELLOW}Compiling for ARM64 architecture...${NC}"
        COMPILER="aarch64-linux-gnu-gcc"
        ARCH_NAME="ARM aarch64"
        ;;
    riscv64|riscv)
        echo -e "${YELLOW}Compiling for RISC-V 64-bit architecture...${NC}"
        COMPILER="riscv64-linux-gnu-gcc"
        ARCH_NAME="RISC-V"
        ;;
    *)
        echo -e "${RED}Error: Unsupported architecture '$ARCH'${NC}"
        echo "Supported: arm64, aarch64, riscv64, riscv"
        exit 1
        ;;
esac

# Check if compiler exists
if ! command -v $COMPILER &> /dev/null; then
    echo -e "${RED}Error: Compiler '$COMPILER' not found${NC}"
    echo "Please install the cross-compilation toolchain first"
    exit 1
fi

# Compile
echo "Compiler: $COMPILER"
echo "Source: $SOURCE"
echo "Output: $OUTPUT"
echo ""

$COMPILER -o "$OUTPUT" "$SOURCE"

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓ Compilation successful!${NC}"
    echo ""
    echo "Binary information:"
    file "$OUTPUT"
    echo ""
    echo "Binary size:"
    ls -lh "$OUTPUT" | awk '{print $5, $9}'
else
    echo -e "${RED}✗ Compilation failed${NC}"
    exit 1
fi
