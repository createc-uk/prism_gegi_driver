#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"

PRISM_PROTOCOL="${PRISM_PROTOCOL:-nats}"
PRISM_SERVER="${PRISM_SERVER:-localhost}"
PRISM_PORT="${PRISM_PORT:-4222}"
DETECTOR_IP="${DETECTOR_IP:-${GEGI_IP:-192.168.50.109}}"
DETECTOR_PORT="${DETECTOR_PORT:-${GEGI_PORT:-27015}}"
OUTPUT_DIR="${OUTPUT_DIR:-${PROJECT_ROOT}/data}"
CALIBRATION_FILE="${CALIBRATION_FILE:-${PROJECT_ROOT}/config/EnergyCal.csv}"
ISOTOPES_CONFIG="${ISOTOPES_CONFIG:-${PROJECT_ROOT}/config/isotopes.yaml}"
DRIVER_EXECUTABLE="${DRIVER_EXECUTABLE:-${PROJECT_ROOT}/build/bin/phds_gegi_driver_node}"
PYTHON_EXECUTABLE="${PYTHON_EXECUTABLE:-python3}"
DRIVER_CONFIG="${PROJECT_ROOT}/config/gegi_driver.yaml"
NODE_DIR="${PROJECT_ROOT}/src/phds_gegi_driver"

if [[ ! -e "${DRIVER_EXECUTABLE}" ]]; then
    printf 'Error: C++ driver executable not found: %s\nBuild the project first or set DRIVER_EXECUTABLE.\n' \
        "${DRIVER_EXECUTABLE}" >&2
    exit 1
fi
if [[ ! -x "${DRIVER_EXECUTABLE}" ]]; then
    printf 'Error: C++ driver executable is not executable: %s\n' "${DRIVER_EXECUTABLE}" >&2
    exit 1
fi
if ! command -v "${PYTHON_EXECUTABLE}" >/dev/null 2>&1; then
    printf 'Error: Python executable not found: %s\nSet PYTHON_EXECUTABLE to a valid interpreter.\n' \
        "${PYTHON_EXECUTABLE}" >&2
    exit 1
fi
for required_file in \
    "${DRIVER_CONFIG}" \
    "${CALIBRATION_FILE}" \
    "${ISOTOPES_CONFIG}" \
    "${NODE_DIR}/spectrum_node.py" \
    "${NODE_DIR}/activity_node.py" \
    "${NODE_DIR}/spherical_heatmap_node.py" \
    "${NODE_DIR}/data_recorder_node.py"; do
    if [[ ! -r "${required_file}" ]]; then
        printf 'Error: required file is not readable: %s\n' "${required_file}" >&2
        exit 1
    fi
done
mkdir -p -- "${OUTPUT_DIR}"

prism_args=(
    --protocol "${PRISM_PROTOCOL}"
    --server "${PRISM_SERVER}"
    --port "${PRISM_PORT}"
)
child_pids=()
shutting_down=0

shutdown() {
    local signal="${1:-TERM}"
    local status="${2:-0}"

    if (( shutting_down )); then
        return
    fi
    shutting_down=1
    trap - EXIT INT TERM

    if (( ${#child_pids[@]} > 0 )); then
        printf 'Stopping GeGi pipeline (%s)...\n' "${signal}" >&2
        kill -s "${signal}" "${child_pids[@]}" 2>/dev/null || true
        wait "${child_pids[@]}" 2>/dev/null || true
    fi

    exit "${status}"
}

trap 'shutdown INT 130' INT
trap 'shutdown TERM 143' TERM
trap 'shutdown TERM "$?"' EXIT

start_node() {
    local label="$1"
    shift
    printf 'Starting %s...\n' "${label}"
    "$@" &
    child_pids+=("$!")
}

start_node "C++ driver" \
    "${DRIVER_EXECUTABLE}" \
    "${prism_args[@]}" \
    --config-file "${DRIVER_CONFIG}" \
    --gegi-ip "${DETECTOR_IP}" \
    --gegi-port "${DETECTOR_PORT}"

start_node "main spectrum" \
    "${PYTHON_EXECUTABLE}" "${NODE_DIR}/spectrum_node.py" \
    "${prism_args[@]}" \
    --calibration-file "${CALIBRATION_FILE}"

start_node "singles spectrum" \
    "${PYTHON_EXECUTABLE}" "${NODE_DIR}/spectrum_node.py" \
    "${prism_args[@]}" \
    --calibration-file "${CALIBRATION_FILE}" \
    --energy-topic gegi.driver.energy_deposit_singles \
    --spectrum-topic gegi.spectrum_singles.histogram \
    --node-name spectrum_singles

start_node "activity" \
    "${PYTHON_EXECUTABLE}" "${NODE_DIR}/activity_node.py" \
    "${prism_args[@]}" \
    --isotopes-config "${ISOTOPES_CONFIG}" \
    --calibration-file "${CALIBRATION_FILE}"

start_node "spherical heatmap" \
    "${PYTHON_EXECUTABLE}" "${NODE_DIR}/spherical_heatmap_node.py" \
    "${prism_args[@]}" \
    --isotopes-config "${ISOTOPES_CONFIG}" \
    --csv-output-dir "${OUTPUT_DIR}"

start_node "data recorder" \
    "${PYTHON_EXECUTABLE}" "${NODE_DIR}/data_recorder_node.py" \
    "${prism_args[@]}" \
    --output-dir "${OUTPUT_DIR}" \
    --calibration-file "${CALIBRATION_FILE}" \
    --isotopes-config "${ISOTOPES_CONFIG}"

set +e
wait -n "${child_pids[@]}"
status=$?
set -e

if (( status == 0 )); then
    printf 'A pipeline process exited; stopping the remaining processes.\n' >&2
else
    printf 'A pipeline process failed with status %d; stopping the remaining processes.\n' "${status}" >&2
fi
shutdown TERM "${status}"
