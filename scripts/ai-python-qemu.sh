#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.build/ai-acceptance/venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    echo "AI environment missing: $PYTHON" >&2
    exit 2
fi
BASE_PREFIX="$($PYTHON -c 'import sys; print(sys.base_prefix)')"
PY_VERSION="$($PYTHON -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
SITE="$ROOT/.build/ai-acceptance/venv/lib/python${PY_VERSION}/site-packages"
export PYTHONHOME="$BASE_PREFIX"
export PYTHONPATH="$SITE${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
export TF_CPP_MIN_LOG_LEVEL="${TF_CPP_MIN_LOG_LEVEL:-2}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
exec /usr/bin/qemu-x86_64 -cpu max -0 "$ROOT/scripts/ai-python-qemu.sh" "$PYTHON" "$@"
