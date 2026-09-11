#!/usr/bin/env bash
# Run the GeGi Python unit tests. No detector or NATS server is required.
# Node-importing tests require the separately installed Prism Python bindings.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON_BIN="${PYTHON:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "ERROR: Python 3 interpreter '$PYTHON_BIN' was not found." >&2
    exit 1
fi

export PYTHONPATH="$REPO_ROOT/src/phds_gegi_driver${PYTHONPATH:+:$PYTHONPATH}"

if ! "$PYTHON_BIN" -c "import prism" >/dev/null 2>&1; then
    echo "ERROR: Prism Python bindings are not importable by '$PYTHON_BIN'." >&2
    echo "Install them with: $PYTHON_BIN -m pip install ./deps/prism/bindings/python" >&2
    exit 1
fi

echo "Running GeGi unit tests with $PYTHON_BIN ..."
exec "$PYTHON_BIN" -m unittest discover -s test -p "test_*.py" "$@"
