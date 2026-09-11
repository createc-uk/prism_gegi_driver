[CmdletBinding()]
param(
    [ValidateSet("Auto", "Host", "Container")]
    [string]$ExecutionTarget = "Auto",

    [string]$ContainerName = "",
    [string]$ImageHint = "phds_gegi_driver",
    [string]$PrismCommand = "prism",
    [string]$NatsHost = "localhost",

    [ValidateRange(1, 65535)]
    [int]$NatsPort = 4222,

    [ValidateSet("echo", "hz")]
    [string]$Mode = "echo",

    [ValidateRange(0, [int]::MaxValue)]
    [int]$SampleCount = 0,

    [Nullable[int]]$WindowSeconds = $null,

    [ValidateRange(1, 86400)]
    [int]$DurationSeconds = 30,

    [string[]]$Topics = @(
        "gegi.driver.compton_event",
        "gegi.driver.energy_deposit",
        "gegi.detector.run_info",
        "gegi.detector.detector_info",
        "gegi.spectrum.histogram",
        "gegi.heatmap.cloud_meta",
        "gegi.heatmap.source_direction",
        "gegi.heatmap.source_directions",
        "gegi.heatmap.source_isotopes",
        "gegi.activity.total_activity",
        "gegi.activity.results"
    )
)

$ErrorActionPreference = "Stop"

function Test-ContainerRunning {
    param([Parameter(Mandatory = $true)][string]$Name)

    $runningId = & docker ps -q --filter "name=^${Name}$"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to query Docker for container '$Name'."
    }

    return -not [string]::IsNullOrWhiteSpace(($runningId | Out-String).Trim())
}

function Find-ContainerByImageHint {
    param([Parameter(Mandatory = $true)][string]$Hint)

    if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
        return ""
    }

    $rows = & docker ps --format "{{.Names}}|{{.Image}}"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to query running Docker containers."
    }

    foreach ($row in $rows) {
        if ([string]::IsNullOrWhiteSpace($row)) {
            continue
        }

        $parts = $row -split "\|", 2
        if ($parts.Count -ne 2) {
            continue
        }

        $name = $parts[0].Trim()
        $image = $parts[1].Trim()
        if ($image -like "${Hint}*" -or $name -like "gegi*") {
            return $name
        }
    }

    return ""
}

function ConvertTo-SingleQuotedLiteral {
    param([Parameter(Mandatory = $true)][string]$Value)

    return "'" + $Value.Replace("'", "''") + "'"
}

function New-PrismWatcherCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Topic,
        [Parameter(Mandatory = $true)][string]$Target
    )

    $arguments = @(
        $Mode,
        "--protocol", "nats",
        "--ip", $NatsHost,
        "--port", $NatsPort.ToString(),
        "--topic", $Topic,
        "--duration", $DurationSeconds.ToString()
    )

    if ($Mode -eq "echo" -and $SampleCount -gt 0) {
        $arguments += @("--max", $SampleCount.ToString())
    }
    elseif ($Mode -eq "hz") {
        $window = if ($null -eq $WindowSeconds) { 5 } else { $WindowSeconds.Value }
        $arguments += @("--window", $window.ToString())
    }

    $quotedArguments = $arguments | ForEach-Object {
        ConvertTo-SingleQuotedLiteral -Value ([string]$_)
    }

    if ($Target -eq "Container") {
        $prefix = @(
            "& docker exec -it",
            (ConvertTo-SingleQuotedLiteral -Value $ContainerName),
            (ConvertTo-SingleQuotedLiteral -Value $PrismCommand)
        ) -join " "
        return "$prefix $($quotedArguments -join ' ')"
    }

    return "& $(ConvertTo-SingleQuotedLiteral -Value $PrismCommand) $($quotedArguments -join ' ')"
}

if ($Topics.Count -eq 0 -or ($Topics | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count -ne $Topics.Count) {
    throw "Topics must contain at least one non-empty dot topic."
}

foreach ($topic in $Topics) {
    if ($topic.StartsWith("/") -or $topic -notmatch "^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)+$") {
        throw "Invalid Prism topic '$topic'. Expected a dot-separated topic such as 'gegi.spectrum.histogram'."
    }
}

if ($Mode -eq "hz" -and $SampleCount -ne 0) {
    throw "-SampleCount is valid only with -Mode echo (Prism echo --max)."
}

if ($Mode -eq "echo" -and $null -ne $WindowSeconds) {
    throw "-WindowSeconds is valid only with -Mode hz (Prism hz --window)."
}

if ($null -ne $WindowSeconds -and $WindowSeconds.Value -lt 1) {
    throw "-WindowSeconds must be at least 1."
}

$resolvedTarget = $ExecutionTarget
if ($ExecutionTarget -eq "Auto") {
    if (-not [string]::IsNullOrWhiteSpace($ContainerName)) {
        $resolvedTarget = "Container"
    }
    elseif ($null -ne (Get-Command $PrismCommand -ErrorAction SilentlyContinue)) {
        $resolvedTarget = "Host"
    }
    else {
        $ContainerName = Find-ContainerByImageHint -Hint $ImageHint
        if (-not [string]::IsNullOrWhiteSpace($ContainerName)) {
            $resolvedTarget = "Container"
        }
        else {
            $resolvedTarget = "Host"
        }
    }
}

if ($resolvedTarget -eq "Container") {
    if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker is not available. Use -ExecutionTarget Host or install Docker."
    }
    if ([string]::IsNullOrWhiteSpace($ContainerName)) {
        $ContainerName = Find-ContainerByImageHint -Hint $ImageHint
    }
    if ([string]::IsNullOrWhiteSpace($ContainerName) -or -not (Test-ContainerRunning -Name $ContainerName)) {
        throw "No matching running GeGi container was found. Pass -ContainerName <name> or use -ExecutionTarget Host."
    }

    & docker exec $ContainerName $PrismCommand --version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Prism CLI '$PrismCommand' is not executable in container '$ContainerName'. Pass its container path with -PrismCommand or use -ExecutionTarget Host."
    }
}
else {
    if ($null -eq (Get-Command $PrismCommand -ErrorAction SilentlyContinue)) {
        throw "Prism CLI '$PrismCommand' was not found on the host. Add it to PATH or pass -PrismCommand <path>."
    }

    & $PrismCommand --version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Prism CLI '$PrismCommand' failed its version check."
    }
}

$currentPowerShell = (Get-Process -Id $PID).Path
Write-Host "Opening $Mode watchers via Prism CLI on: $resolvedTarget"
if ($resolvedTarget -eq "Container") {
    Write-Host "Container: $ContainerName"
}
Write-Host "NATS: ${NatsHost}:$NatsPort"
Write-Host "Duration: $DurationSeconds seconds"
if ($Mode -eq "echo" -and $SampleCount -gt 0) {
    Write-Host "Maximum messages per topic: $SampleCount"
}
if ($Mode -eq "hz") {
    $displayWindow = if ($null -eq $WindowSeconds) { 5 } else { $WindowSeconds.Value }
    Write-Host "Rate window: $displayWindow seconds"
}

foreach ($topic in $Topics) {
    $command = New-PrismWatcherCommand -Topic $topic -Target $resolvedTarget
    $startupScript = "`$Host.UI.RawUI.WindowTitle = 'GeGi $Mode $topic'; $command"
    $encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($startupScript))

    Start-Process $currentPowerShell -ArgumentList @(
        "-NoExit",
        "-EncodedCommand",
        $encodedCommand
    ) | Out-Null
}

Write-Host "Launched $($Topics.Count) watcher windows."
Write-Host "Use Ctrl+C in a watcher window to stop it early."
