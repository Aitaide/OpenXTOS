#!/bin/bash
# XTOS Environment Setup Script
# Checks and installs all necessary development tools

set -euo pipefail

echo "=== XTOS Development Environment Setup ==="
echo ""

# Required packages
PACKAGES=(
    "build-essential"
    "gcc"
    "make"
    "flex"
    "bison"
    "libssl-dev"
    "libelf-dev"
    "bc"
    "curl"
    "wget"
    "xz-utils"
    "git"
    "rt-tests"
    "linux-cpupower"
    "stress-ng"
)

echo "Checking required packages..."
MISSING=()

for pkg in "${PACKAGES[@]}"; do
    if ! dpkg -l | grep -q "^ii  $pkg"; then
        MISSING+=("$pkg")
    fi
done

if [ ${#MISSING[@]} -gt 0 ]; then
    echo "Installing missing packages: ${MISSING[*]}"
    apt-get update
    apt-get install -y "${MISSING[@]}"
else
    echo "All required packages are installed"
fi

echo ""
echo "=== Toolchain Versions ==="
gcc --version | head -1
make --version | head -1
flex --version | head -1
bison --version | head -1

echo ""
echo "=== System Information ==="
echo "CPU: $(nproc) cores"
echo "Memory: $(free -h | awk '/^Mem:/ {print $2}')"
echo "Kernel: $(uname -r)"

echo ""
echo "Environment setup complete!"
