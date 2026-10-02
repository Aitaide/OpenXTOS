#!/bin/bash
# Enable PREEMPT_RT configuration in a kernel .config file

set -e

CONFIG_FILE="$1"

if [ -z "$CONFIG_FILE" ] || [ ! -f "$CONFIG_FILE" ]; then
    echo "Usage: $0 <path-to-.config>" >&2
    exit 1
fi

echo "Enabling PREEMPT_RT in: $CONFIG_FILE"

# Use scripts/config from kernel source if available
KERNEL_DIR=$(dirname "$(dirname "$CONFIG_FILE")")
if [ -f "$KERNEL_DIR/scripts/config" ]; then
    KCONFIG="$KERNEL_DIR/scripts/config"
else
    # Fallback to sed-based approach
    KCONFIG=""
fi

if [ -n "$KCONFIG" ]; then
    # Use kernel's config script
    "$KCONFIG" --file "$CONFIG_FILE" \
        --enable EXPERT \
        --enable PREEMPT_RT \
        --enable HIGH_RES_TIMERS \
        --disable PREEMPT_NONE \
        --disable PREEMPT_VOLUNTARY \
        --disable PREEMPT_DYNAMIC
else
    # Manual sed approach
    sed -i 's/^CONFIG_PREEMPT_NONE=y/# CONFIG_PREEMPT_NONE is not set/' "$CONFIG_FILE"
    sed -i 's/^CONFIG_PREEMPT_VOLUNTARY=y/# CONFIG_PREEMPT_VOLUNTARY is not set/' "$CONFIG_FILE"
    sed -i 's/^CONFIG_PREEMPT_DYNAMIC=y/# CONFIG_PREEMPT_DYNAMIC is not set/' "$CONFIG_FILE"
    sed -i 's/^# CONFIG_EXPERT is not set/CONFIG_EXPERT=y/' "$CONFIG_FILE"
    sed -i 's/^# CONFIG_PREEMPT_RT is not set/CONFIG_PREEMPT_RT=y/' "$CONFIG_FILE"
    sed -i 's/^# CONFIG_HIGH_RES_TIMERS is not set/CONFIG_HIGH_RES_TIMERS=y/' "$CONFIG_FILE"
    
    # Append if not present
    grep -q "CONFIG_PREEMPT_RT=" "$CONFIG_FILE" || echo "CONFIG_PREEMPT_RT=y" >> "$CONFIG_FILE"
    grep -q "CONFIG_HIGH_RES_TIMERS=" "$CONFIG_FILE" || echo "CONFIG_HIGH_RES_TIMERS=y" >> "$CONFIG_FILE"
fi

echo "PREEMPT_RT configuration updated"
