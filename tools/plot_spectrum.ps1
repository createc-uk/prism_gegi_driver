param(
    [switch]$Cumulative,
    [string]$NatsHost = "localhost",
    [int]$NatsPort = 4222,
    [string]$Protocol = "nats",
    [string]$PythonCommand = "python"
)
# Launch the Prism-native live spectrum plotter on Windows.
# Requires the Prism Python bindings, matplotlib, and numpy in the selected
# Python environment. The NATS server must be reachable at NatsHost:NatsPort.
#
# Usage:
#   .\plot_spectrum.ps1
#   .\plot_spectrum.ps1 -Cumulative
#   .\plot_spectrum.ps1 -NatsHost <server-ip> -NatsPort 4222

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonScript = Join-Path $scriptDir "plot_live_spectrum.py"

$arguments = @(
    $pythonScript,
    "--protocol", $Protocol,
    "--server", $NatsHost,
    "--port", $NatsPort
)
if ($Cumulative) {
    $arguments += "--cumulative"
}

& $PythonCommand @arguments
exit $LASTEXITCODE
