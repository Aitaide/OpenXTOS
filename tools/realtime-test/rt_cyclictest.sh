#!/bin/bash
# XTOS Long-Duration Real-Time Latency Test
# Uses cyclictest for extended validation of RT performance

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly OUTPUT_DIR="${RT_TEST_OUTPUT:-$SCRIPT_DIR/results}"
readonly TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

# Default parameters
DURATION_SEC=86400  # 24 hours
PRIORITY=80
THREADS=2
INTERVAL_US=1000
AFFINITY=""
HISTOGRAM=1000

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Long-duration cyclictest for XTOS real-time validation.

Options:
  --duration SECONDS    Test duration in seconds (default: 86400 = 24h)
  --priority PRIO       SCHED_FIFO priority (default: 80, range: 1-99)
  --threads N           Number of test threads (default: 2)
  --interval US         Timer interval in microseconds (default: 1000)
  --affinity CPUS       CPU affinity, e.g., "2,3" (default: auto)
  --histogram BINS      Histogram bin count (default: 1000)
  --help                Show this help

Examples:
  # 24-hour test on isolated cores 2-3
  $0 --duration 86400 --threads 2 --affinity 2,3 --priority 80

  # 1-hour smoke test
  $0 --duration 3600 --threads 1 --priority 80

Environment:
  RT_TEST_OUTPUT        Output directory (default: ./results)

Output:
  results/cyclictest_<timestamp>.log       Raw cyclictest output
  results/cyclictest_<timestamp>.json      Parsed statistics (JSON)
  results/cyclictest_<timestamp>.hist      Latency histogram data
EOF
    exit 0
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --duration)
                DURATION_SEC="$2"
                shift 2
                ;;
            --priority)
                PRIORITY="$2"
                shift 2
                ;;
            --threads)
                THREADS="$2"
                shift 2
                ;;
            --interval)
                INTERVAL_US="$2"
                shift 2
                ;;
            --affinity)
                AFFINITY="$2"
                shift 2
                ;;
            --histogram)
                HISTOGRAM="$2"
                shift 2
                ;;
            --help)
                usage
                ;;
            *)
                echo "Unknown option: $1" >&2
                usage
                ;;
        esac
    done
}

check_prerequisites() {
    if ! command -v cyclictest &>/dev/null; then
        echo "Error: cyclictest not found. Install rt-tests package:" >&2
        echo "  Debian/Ubuntu: apt-get install rt-tests" >&2
        echo "  Fedora/RHEL:   dnf install rt-tests" >&2
        exit 1
    fi

    if [ "$(id -u)" -ne 0 ]; then
        echo "Error: Must run as root for SCHED_FIFO priority" >&2
        exit 1
    fi

    if [ "$PRIORITY" -lt 1 ] || [ "$PRIORITY" -gt 99 ]; then
        echo "Error: Priority must be 1-99, got: $PRIORITY" >&2
        exit 1
    fi

    mkdir -p "$OUTPUT_DIR"
}

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*"
}

run_cyclictest() {
    local raw_log="$OUTPUT_DIR/cyclictest_$TIMESTAMP.log"
    local hist_file="$OUTPUT_DIR/cyclictest_$TIMESTAMP.hist"
    local json_file="$OUTPUT_DIR/cyclictest_$TIMESTAMP.json"
    
    local affinity_args=""
    if [ -n "$AFFINITY" ]; then
        affinity_args="--affinity=$AFFINITY"
    fi
    
    log "Starting cyclictest"
    log "Duration: ${DURATION_SEC}s ($((DURATION_SEC / 3600))h)"
    log "Priority: $PRIORITY (SCHED_FIFO)"
    log "Threads: $THREADS"
    log "Interval: ${INTERVAL_US}us"
    log "Affinity: ${AFFINITY:-auto}"
    log "Output: $raw_log"
    log ""
    
    # Record system state
    {
        echo "=== System Configuration ==="
        echo "Kernel: $(uname -r)"
        echo "Host: $(hostname)"
        echo "Start: $(date --iso-8601=seconds)"
        echo "CPUs online: $(cat /sys/devices/system/cpu/online)"
        echo "Command line: $(cat /proc/cmdline)"
        echo ""
    } | tee "$raw_log"
    
    # Run cyclictest
    local start_ts
    start_ts=$(date +%s)
    
    # shellcheck disable=SC2086
    cyclictest \
        --policy=fifo \
        --priority="$PRIORITY" \
        --threads="$THREADS" \
        --interval="$INTERVAL_US" \
        --duration="${DURATION_SEC}s" \
        --histogram="$HISTOGRAM" \
        --mlockall \
        --quiet \
        $affinity_args \
        2>&1 | tee -a "$raw_log"
    
    local end_ts
    end_ts=$(date +%s)
    local elapsed=$((end_ts - start_ts))
    
    log ""
    log "Test completed"
    log "Elapsed: ${elapsed}s ($((elapsed / 3600))h $((elapsed % 3600 / 60))m)"
    
    # Extract histogram
    if grep -q "^# Histogram" "$raw_log"; then
        sed -n '/^# Histogram/,/^[^#0-9]/p' "$raw_log" > "$hist_file"
        log "Histogram saved: $hist_file"
    fi
    
    # Parse statistics to JSON
    parse_statistics "$raw_log" "$json_file"
    
    log "Raw log: $raw_log"
    log "Statistics: $json_file"
}

parse_statistics() {
    local raw="$1"
    local json="$2"
    
    # Extract summary lines (example format):
    # T: 0 ( 1234) P:80 I:1000 C: 86400000 Min:    2 Act:    3 Avg:    4 Max:   15
    
    if ! grep -q "^T:" "$raw"; then
        log "Warning: No cyclictest summary found in output"
        echo '{"error":"no_summary"}' > "$json"
        return
    fi
    
    {
        echo '{'
        echo '  "schema": "xtos-rt-cyclictest-v1",'
        echo '  "timestamp": "'"$(date --iso-8601=seconds)"'",'
        echo '  "duration_sec": '"$DURATION_SEC"','
        echo '  "priority": '"$PRIORITY"','
        echo '  "threads": '"$THREADS"','
        echo '  "interval_us": '"$INTERVAL_US"','
        echo '  "system": {'
        echo '    "kernel": "'"$(uname -r)"'",'
        echo '    "hostname": "'"$(hostname)"'",'
        echo '    "cpu_model": "'"$(lscpu | grep 'Model name' | cut -d: -f2- | xargs)"'"'
        echo '  },'
        echo '  "results": ['
        
        local first=1
        while IFS= read -r line; do
            if [[ "$line" =~ ^T:\ +([0-9]+)\ +\(\ *([0-9]+)\)\ +P:([0-9]+)\ +I:([0-9]+)\ +C:\ +([0-9]+)\ +Min:\ +([0-9]+)\ +Act:\ +([0-9]+)\ +Avg:\ +([0-9]+)\ +Max:\ +([0-9]+) ]]; then
                local tid="${BASH_REMATCH[1]}"
                local pid="${BASH_REMATCH[2]}"
                local prio="${BASH_REMATCH[3]}"
                local interval="${BASH_REMATCH[4]}"
                local cycles="${BASH_REMATCH[5]}"
                local min_us="${BASH_REMATCH[6]}"
                local act_us="${BASH_REMATCH[7]}"
                local avg_us="${BASH_REMATCH[8]}"
                local max_us="${BASH_REMATCH[9]}"
                
                if [ "$first" -eq 1 ]; then
                    first=0
                else
                    echo ','
                fi
                
                cat <<THREAD
    {
      "thread_id": $tid,
      "pid": $pid,
      "priority": $prio,
      "interval_us": $interval,
      "cycles": $cycles,
      "latency_us": {
        "min": $min_us,
        "act": $act_us,
        "avg": $avg_us,
        "max": $max_us
      }
    }
THREAD
            fi
        done < <(grep "^T:" "$raw")
        
        echo ''
        echo '  ]'
        echo '}'
    } > "$json"
    
    log "Statistics parsed to JSON: $json"
}

main() {
    parse_args "$@"
    check_prerequisites
    
    log "=== XTOS Real-Time Cyclictest ==="
    run_cyclictest
    log "=== Test Complete ==="
}

main "$@"
