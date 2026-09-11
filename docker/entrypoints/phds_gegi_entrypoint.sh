#!/usr/bin/env bash
set -euo pipefail

runtime_python_path="/opt/prism-python:/opt/phds_gegi_driver/src/phds_gegi_driver"
if [[ -n "${PYTHONPATH:-}" ]]; then
    export PYTHONPATH="${runtime_python_path}:${PYTHONPATH}"
else
    export PYTHONPATH="${runtime_python_path}"
fi

exec "$@"
