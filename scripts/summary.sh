#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
printf 'XTOS development repository: %s\n' "$ROOT"
printf 'Current status: %s/STATUS.md\n' "$ROOT"
printf 'Development tasks: %s/plans/XTOS_Next_Development_Plan_Rev2.md\n' "$ROOT"
printf 'Architecture: %s/docs/XTOS_Technical_Specification.md\n' "$ROOT"
printf 'Build: ./scripts/build_system.sh\nVerify: ./scripts/verify_system.sh\n'
