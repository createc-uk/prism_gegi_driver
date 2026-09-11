param(
    [string]$NatsHost = "localhost",
    [int]$NatsPort = 4222,
    [string]$Protocol = "nats",
    [string]$PythonCommand = "python"
)
# Launch the Prism-native live spherical heatmap plotter on Windows.
# Requires the Prism Python bindings, matplotlib, and numpy in the selected
# Python environment. The NATS server must be reachable at NatsHost:NatsPort.

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonScript = Join-Path $scriptDir "plot_live_heatmap.py"

& $PythonCommand $pythonScript `
    --protocol $Protocol `
    --server $NatsHost `
    --port $NatsPort
exit $LASTEXITCODE
