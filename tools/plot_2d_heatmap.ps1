param(
    [string]$NatsHost = "localhost",
    [int]$NatsPort = 4222,
    [string]$Protocol = "nats",
    [string]$PythonCommand = "python"
)
# Launch the Prism-native per-isotope 2D heatmap plotter on Windows.
# Produces the Y-Z projection with isotope-labelled source markers. Requires
# the Prism Python bindings, matplotlib, numpy, and scipy.

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonScript = Join-Path $scriptDir "plot_live_2d_heatmap.py"

& $PythonCommand $pythonScript `
    --protocol $Protocol `
    --server $NatsHost `
    --port $NatsPort
exit $LASTEXITCODE
