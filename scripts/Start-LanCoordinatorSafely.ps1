[CmdletBinding()]
param(
    [string]$ConfigPath = "",

    [string]$ExecutablePath = "",

    [ValidateRange(0, 60)]
    [int]$MaxSourceBusyRetries = 10,

    [ValidateRange(0, 3600)]
    [int]$SourceBusyRetrySeconds = 60,

    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Resolve-LocalPath {
    param(
        [string]$BasePath,
        [string]$Value
    )

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }
    if ([IO.Path]::IsPathRooted($Value)) {
        return [IO.Path]::GetFullPath($Value)
    }
    return [IO.Path]::GetFullPath((Join-Path $BasePath $Value))
}

function Get-CoordinatorConfiguration {
    param([string]$Path)

    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    $file = Get-Item -LiteralPath $resolved -ErrorAction Stop
    if ($file.Length -gt 1MB) {
        throw "LAN Assist configuration is unexpectedly large."
    }
    $config = Get-Content -LiteralPath $resolved -Raw |
        ConvertFrom-Json -ErrorAction Stop
    if (
        $config.schema_version -ne 1 -or
        $config.mode -ne "coordinator"
    ) {
        throw "LAN Assist coordinator configuration is invalid."
    }

    $root = Split-Path -Parent $resolved
    [pscustomobject]@{
        ConfigPath = $resolved
        ConfigRoot = $root
        StartupStatusPath = Join-Path $root "coordinator-startup-status.json"
    }
}

function Write-CoordinatorStartupStatus {
    param(
        [string]$Path,
        [string]$Status,
        [string]$FailureCategory,
        [int]$Attempt,
        [int]$MaximumAttempts,
        [int]$RetryDelaySeconds
    )

    $payload = [ordered]@{
        SchemaVersion = 1
        Event = "CoordinatorStartupStatus"
        Status = $Status
        FailureCategory = $FailureCategory
        Attempt = $Attempt
        MaximumAttempts = $MaximumAttempts
        RetryDelaySeconds = $RetryDelaySeconds
        UpdatedUtc = [DateTime]::UtcNow.ToString("o")
    }
    $parent = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $temporary = Join-Path $parent (
        ".{0}.{1}.tmp" -f (Split-Path -Leaf $Path),
        [Guid]::NewGuid().ToString("N")
    )
    $backup = Join-Path $parent (
        ".{0}.{1}.bak" -f (Split-Path -Leaf $Path),
        [Guid]::NewGuid().ToString("N")
    )
    try {
        [IO.File]::WriteAllText(
            $temporary,
            ($payload | ConvertTo-Json -Compress),
            [Text.UTF8Encoding]::new($false)
        )
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            [IO.File]::Replace($temporary, $Path, $backup)
        } else {
            [IO.File]::Move($temporary, $Path)
        }
    } finally {
        Remove-Item `
            -LiteralPath $temporary, $backup `
            -Force `
            -ErrorAction SilentlyContinue
    }
}

function ConvertFrom-CoordinatorEvent {
    param([AllowNull()][object]$Line)

    $text = [string]$Line
    if ([string]::IsNullOrWhiteSpace($text) -or $text.Length -gt 64KB) {
        return $null
    }
    if (-not $text.TrimStart().StartsWith(
        "{",
        [StringComparison]::Ordinal
    )) {
        return $null
    }
    try {
        $value = $text | ConvertFrom-Json -ErrorAction Stop
    } catch {
        return $null
    }
    if ($null -eq $value) {
        return $null
    }
    if ($value.PSObject.Properties.Name -notcontains "Event") {
        return $null
    }
    return $value
}

function Invoke-CoordinatorProcess {
    param(
        [string]$Executable,
        [string]$Configuration
    )

    if ($Executable.Contains('"') -or $Configuration.Contains('"')) {
        throw "Coordinator process paths contain an unsupported quote."
    }

    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $extension = [IO.Path]::GetExtension($Executable)
    if ($extension -eq ".ps1") {
        $windowsPowerShell = Join-Path `
            $env:SystemRoot `
            "System32\WindowsPowerShell\v1.0\powershell.exe"
        $startInfo.FileName = $windowsPowerShell
        $startInfo.Arguments = (
            '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
            '-File "{0}" --config "{1}"' -f
                $Executable,
                $Configuration
        )
    } else {
        $startInfo.FileName = $Executable
        $startInfo.Arguments = '--config "{0}"' -f $Configuration
    }
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    # Leave stderr inherited so a long-running coordinator cannot deadlock on
    # an unread redirected error buffer.
    $startInfo.RedirectStandardError = $false

    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    $sawCoordinatorStatus = $false
    $terminalEvent = $null
    try {
        if (-not $process.Start()) {
            throw "LAN Assist coordinator process did not start."
        }
        while ($true) {
            $line = $process.StandardOutput.ReadLine()
            if ($null -eq $line) {
                break
            }
            $eventRecord = ConvertFrom-CoordinatorEvent $line
            if ($null -eq $eventRecord) {
                continue
            }
            if ($eventRecord.Event -eq "CoordinatorStatus") {
                $sawCoordinatorStatus = $true
                if (
                    $eventRecord.PSObject.Properties.Name -contains
                        "FailureCategory" -and
                    -not [string]::IsNullOrWhiteSpace(
                        [string]$eventRecord.FailureCategory
                    )
                ) {
                    $terminalEvent = $eventRecord
                }
            }
            if ($eventRecord.Event -eq "ServiceStopped") {
                $terminalEvent = $eventRecord
            }
        }
        $process.WaitForExit()
        [pscustomobject]@{
            ExitCode = $process.ExitCode
            SawCoordinatorStatus = $sawCoordinatorStatus
            TerminalEvent = $terminalEvent
        }
    } finally {
        $process.Dispose()
    }
}

if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $PSScriptRoot "VideoTranscoderLanAssist.json"
}
$coordinator = Get-CoordinatorConfiguration $ConfigPath
if ([string]::IsNullOrWhiteSpace($ExecutablePath)) {
    $ExecutablePath = Join-Path `
        $coordinator.ConfigRoot `
        "VideoTranscoderLanAssist.exe"
}
$resolvedExecutable = (
    Resolve-Path -LiteralPath $ExecutablePath -ErrorAction Stop
).Path

$validationOutput = @(
    & $resolvedExecutable `
        "--config" $coordinator.ConfigPath `
        "--validate-config" 2>$null
)
$validationExit = $LASTEXITCODE
if ($validationExit -ne 0) {
    throw "LAN Assist executable rejected the coordinator configuration."
}
$validation = $validationOutput | Select-Object -Last 1 | ConvertFrom-Json
if (
    $validation.Event -ne "ConfigValidated" -or
    $validation.Status -ne "Ready" -or
    $validation.Mode -ne "coordinator"
) {
    throw "LAN Assist coordinator validation evidence is invalid."
}

if ($ValidateOnly) {
    [ordered]@{
        Event = "CoordinatorLauncherValidated"
        Status = "Ready"
        Mode = "coordinator"
    } | ConvertTo-Json -Compress
    exit 0
}

$maximumAttempts = $MaxSourceBusyRetries + 1
for ($attempt = 1; $attempt -le $maximumAttempts; $attempt++) {
    Write-CoordinatorStartupStatus `
        $coordinator.StartupStatusPath `
        "Starting" `
        "" `
        $attempt `
        $maximumAttempts `
        0

    $result = Invoke-CoordinatorProcess `
        $resolvedExecutable `
        $coordinator.ConfigPath
    $exitCode = $result.ExitCode
    $sawCoordinatorStatus = $result.SawCoordinatorStatus
    $terminalEvent = $result.TerminalEvent

    if ($exitCode -eq 0) {
        Write-CoordinatorStartupStatus `
            $coordinator.StartupStatusPath `
            "Stopped" `
            "" `
            $attempt `
            $maximumAttempts `
            0
        exit 0
    }

    $retryableBusy = (
        -not $sawCoordinatorStatus -and
        $exitCode -eq 1 -and
        $null -ne $terminalEvent -and
        [string]$terminalEvent.Event -eq "ServiceStopped" -and
        [string]$terminalEvent.Status -eq "Failed" -and
        [string]$terminalEvent.FailureCategory -eq "ResumeSourceBusy"
    )
    if (-not $retryableBusy) {
        $category = if (
            $null -ne $terminalEvent -and
            $terminalEvent.PSObject.Properties.Name -contains
                "FailureCategory" -and
            -not [string]::IsNullOrWhiteSpace(
                [string]$terminalEvent.FailureCategory
            )
        ) {
            [string]$terminalEvent.FailureCategory
        } else {
            "CoordinatorFailed"
        }
        Write-CoordinatorStartupStatus `
            $coordinator.StartupStatusPath `
            "Failed" `
            $category `
            $attempt `
            $maximumAttempts `
            0
        exit $exitCode
    }

    if ($attempt -ge $maximumAttempts) {
        Write-CoordinatorStartupStatus `
            $coordinator.StartupStatusPath `
            "Blocked" `
            "ResumeSourceBusy" `
            $attempt `
            $maximumAttempts `
            0
        exit $exitCode
    }

    Write-CoordinatorStartupStatus `
        $coordinator.StartupStatusPath `
        "Waiting" `
        "ResumeSourceBusy" `
        $attempt `
        $maximumAttempts `
        $SourceBusyRetrySeconds
    if ($SourceBusyRetrySeconds -gt 0) {
        Start-Sleep -Seconds $SourceBusyRetrySeconds
    }
}

throw "LAN Assist coordinator retry state is invalid."
