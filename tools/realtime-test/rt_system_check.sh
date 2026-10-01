#!/bin/bash
# XTOS Real-Time System Configuration Checker
# Validates and reports system configuration for RT workloads

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly OUTPUT_DIR="${RT_TEST_OUTPUT:-$SCRIPT_DIR/results}"
readonly TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
readonly REPORT="$OUTPUT_DIR/system_check_$TIMESTAMP.txt"

mkdir -p "$OUTPUT_DIR"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" | tee -a "$REPORT"
}

check_kernel() {
    log "=== Kernel Configuration ==="
    log "Kernel version: $(uname -r)"
    
    if uname -v | grep -q PREEMPT_RT; then
        log "✓ PREEMPT_RT enabled"
    elif grep -q "CONFIG_PREEMPT_RT=y" /boot/config-"$(uname -r)" 2>/dev/null; then
        log "✓ PREEMPT_RT compiled in"
    else
        log "✗ PREEMPT_RT not detected (required for hard real-time)"
    fi
    
    log "Command line: $(cat /proc/cmdline)"
    
    # Check for RT-hostile parameters
    if grep -q "idle=poll" /proc/cmdline; then
        log "⚠ idle=poll detected (increases latency)"
    fi
    
    if grep -q "nohz_full=" /proc/cmdline; then
        local cores
        cores=$(grep "nohz_full=" /proc/cmdline | sed 's/.*nohz_full=\([^ ]*\).*/\1/')
        log "✓ NO_HZ_FULL enabled for cores: $cores"
    else
        log "! NO_HZ_FULL not configured (consider isolcpus=domain,managed_irq,X-Y nohz_full=X-Y)"
    fi
    
    if grep -q "isolcpus=" /proc/cmdline; then
        local isolated
        isolated=$(grep "isolcpus=" /proc/cmdline | sed 's/.*isolcpus=\([^ ]*\).*/\1/')
        log "✓ CPU isolation: $isolated"
    else
        log "! No CPU isolation (consider isolcpus= for dedicated RT cores)"
    fi
    
    log ""
}

check_cpu() {
    log "=== CPU Configuration ==="
    log "Architecture: $(uname -m)"
    log "CPU model: $(lscpu | grep 'Model name' | cut -d: -f2 | xargs)"
    log "Total CPUs: $(nproc)"
    log "Online CPUs: $(cat /sys/devices/system/cpu/online)"
    
    # Check CPU frequency scaling
    if [ -d /sys/devices/system/cpu/cpu0/cpufreq ]; then
        local governor
        governor=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo "unknown")
        log "CPU governor: $governor"
        if [ "$governor" = "performance" ]; then
            log "✓ Performance governor active (recommended for RT)"
        else
            log "⚠ Governor is '$governor' (recommend 'performance' for RT workloads)"
        fi
    else
        log "! CPU frequency scaling not available or disabled"
    fi
    
    # Check for virtualization
    if lscpu | grep -q "Hypervisor vendor"; then
        log "⚠ Running under hypervisor: $(lscpu | grep 'Hypervisor vendor' | cut -d: -f2 | xargs)"
        log "  Real-time guarantees degraded in virtualized environments"
    else
        log "✓ Bare metal detected"
    fi
    
    log ""
}

check_memory() {
    log "=== Memory Configuration ==="
    local total_kb
    total_kb=$(grep MemTotal /proc/meminfo | awk '{print $2}')
    log "Total memory: $((total_kb / 1024)) MB"
    
    # Check transparent huge pages
    if [ -f /sys/kernel/mm/transparent_hugepage/enabled ]; then
        local thp
        thp=$(cat /sys/kernel/mm/transparent_hugepage/enabled)
        if echo "$thp" | grep -q '\[never\]'; then
            log "✓ Transparent Huge Pages disabled (recommended for RT)"
        else
            log "⚠ THP enabled: $thp (recommend 'never' for deterministic latency)"
        fi
    fi
    
    # Check swap
    local swap_kb
    swap_kb=$(grep SwapTotal /proc/meminfo | awk '{print $2}')
    if [ "$swap_kb" -eq 0 ]; then
        log "✓ Swap disabled"
    else
        log "⚠ Swap enabled: $((swap_kb / 1024)) MB (can cause unpredictable latency)"
    fi
    
    log ""
}

check_interrupts() {
    log "=== Interrupt Configuration ==="
    
    # Check IRQ balance
    if systemctl is-active --quiet irqbalance 2>/dev/null; then
        log "⚠ irqbalance service running (may interfere with CPU isolation)"
    else
        log "✓ irqbalance disabled"
    fi
    
    # Sample IRQ affinity for key devices
    log "IRQ affinity (first 5 entries):"
    head -n 5 /proc/interrupts | tee -a "$REPORT" >/dev/null
    
    log ""
}

check_clocksource() {
    log "=== Clock Source ==="
    local current
    current=$(cat /sys/devices/system/clocksource/clocksource0/current_clocksource)
    local available
    available=$(cat /sys/devices/system/clocksource/clocksource0/available_clocksource)
    
    log "Current: $current"
    log "Available: $available"
    
    if [ "$current" = "tsc" ]; then
        log "✓ TSC clocksource (best for x86_64 RT)"
    elif [ "$current" = "kvm-clock" ]; then
        log "⚠ kvm-clock (virtualized, not suitable for RT guarantees)"
    else
        log "! Clocksource: $current (verify suitability for RT workloads)"
    fi
    
    log ""
}

check_services() {
    log "=== System Services ==="
    
    # Check for RT-hostile services
    local hostile_services=(
        "cups"
        "bluetooth"
        "avahi-daemon"
        "ModemManager"
        "packagekit"
    )
    
    for service in "${hostile_services[@]}"; do
        if systemctl is-active --quiet "$service" 2>/dev/null; then
            log "⚠ $service running (may cause latency spikes)"
        fi
    done
    
    log ""
}

check_security() {
    log "=== Security Features ==="
    
    # Check SELinux
    if command -v getenforce &>/dev/null; then
        local selinux
        selinux=$(getenforce)
        log "SELinux: $selinux"
        if [ "$selinux" != "Disabled" ]; then
            log "  (may add latency; consider permissive mode for RT testing)"
        fi
    fi
    
    # Check AppArmor
    if [ -d /sys/kernel/security/apparmor ]; then
        log "AppArmor: enabled (may add latency)"
    fi
    
    log ""
}

generate_recommendations() {
    log "=== Recommendations for RT Optimization ==="
    log ""
    log "Kernel command line additions:"
    log "  isolcpus=domain,managed_irq,2-15 nohz_full=2-15 rcu_nocbs=2-15"
    log "  transparent_hugepage=never intel_pstate=disable processor.max_cstate=1"
    log "  intel_idle.max_cstate=0 idle=poll"
    log ""
    log "Runtime configuration:"
    log "  echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"
    log "  systemctl stop irqbalance"
    log "  echo 0 > /proc/sys/kernel/watchdog"
    log "  swapoff -a"
    log ""
    log "For XTOS testing:"
    log "  chrt -f 80 ./rt_cyclictest.sh --duration 86400 --priority 80 --threads 2"
    log ""
}

main() {
    log "XTOS Real-Time System Configuration Check"
    log "=========================================="
    log "Host: $(hostname)"
    log "Date: $(date)"
    log ""
    
    check_kernel
    check_cpu
    check_memory
    check_interrupts
    check_clocksource
    check_services
    check_security
    generate_recommendations
    
    log "=========================================="
    log "Report saved to: $REPORT"
}

main "$@"
