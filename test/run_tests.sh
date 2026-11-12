#!/usr/bin/env bash
# Run local Python unit tests. No Docker, detector or NATS server is needed.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -n "${PYTHON:-}" ]]; then
    PYTHON_BIN="$PYTHON"
else
    PYTHON_BIN="$REPO_ROOT/.venv-tests/bin/python"
    if [[ ! -x "$PYTHON_BIN" ]] || ! "$PYTHON_BIN" -c 'import numpy, scipy, yaml' >/dev/null 2>&1; then
        python3 "$REPO_ROOT/test/setup_env.py"
    fi
fi
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "ERROR: Python interpreter not found: $PYTHON_BIN" >&2
    exit 1
fi
export PYTHONPATH="$REPO_ROOT/src/phds_gegi_driver${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON_BIN" "$REPO_ROOT/test/run_unit_tests.py" "$@"
