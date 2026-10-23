#!/bin/bash
# XTOS Cyclictest Runner
# Tests real-time latency using cyclictest

set -euo pipefail

# Check if rt-tests is installed
if ! command -v cyclictest &> /dev/null; then
    echo "Installing rt-tests package..."
    apt-get update && apt-get install -y rt-tests
fi

# Test parameters
DURATION=60
INTERVAL=1000
PRIORITY=99
AFFINITY=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -d|--duration)
            DURATION="$2"
            shift 2
            ;;
        -i|--interval)
            INTERVAL="$2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done

echo "Running cyclictest for ${DURATION}s..."
echo "Interval: ${INTERVAL}us, Priority: ${PRIORITY}"
echo ""

# Run cyclictest
cyclictest -t$(nproc) -p${PRIORITY} -i${INTERVAL} -m -q -D${DURATION} | tee cyclictest_results.txt

# Parse results
echo ""
echo "=== Latency Summary ==="
grep -E "Min|Avg|Max" cyclictest_results.txt || echo "Results parsing failed"

exit 0
