[CmdletBinding()]
param(
    [string]$ConfigPath = "",

    [string]$ExecutablePath = "",

    [ValidateRange(1, 30)]
    [int]$SshTimeoutSeconds = 8,

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

function Get-HelperConfiguration {
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
        $config.mode -ne "helper" -or
        [string]::IsNullOrWhiteSpace([string]$config.ssh_destination) -or
        [string]::IsNullOrWhiteSpace([string]$config.staging_root)
    ) {
        throw "LAN Assist helper configuration is invalid."
    }
    if (
        [string]$config.ssh_destination -match '^-' -or
        [string]$config.ssh_destination -match '[\s\x00-\x1f]'
    ) {
        throw "LAN Assist SSH destination is invalid."
    }
    $staging = [string]$config.staging_root
    if ($staging -notmatch '^(\\\\[^\\]+\\[^\\]+)(?:\\|$)') {
        throw "LAN Assist staging root must be a UNC path."
    }
    $shareRoot = $Matches[1]

    $root = Split-Path -Parent $resolved
    $statusValue = if ($config.PSObject.Properties.Name -contains "status_file") {
        [string]$config.status_file
    } else {
        ""
    }
    $eventLogValue = if (
        $config.PSObject.Properties.Name -contains "event_log_file"
    ) {
        [string]$config.event_log_file
    } else {
        ""
    }
    $eventLogMaxBytes = if (
        $config.PSObject.Properties.Name -contains "event_log_max_bytes"
    ) {
        [int64]$config.event_log_max_bytes
    } else {
        8MB
    }
    $eventLogBackupCount = if (
        $config.PSObject.Properties.Name -contains "event_log_backup_count"
    ) {
        [int]$config.event_log_backup_count
    } else {
        5
    }
    $sshExecutable = if (
        $config.PSObject.Properties.Name -notcontains "ssh_executable" -or
        [string]::IsNullOrWhiteSpace([string]$config.ssh_executable)
    ) {
        "ssh"
    } else {
        [string]$config.ssh_executable
    }
    $statusPath = Resolve-LocalPath $root $statusValue
    $eventLogPath = Resolve-LocalPath $root $eventLogValue
    [pscustomobject]@{
        ConfigPath = $resolved
        ConfigRoot = $root
        StatusPath = $statusPath
        EventLogPath = $eventLogPath
        EventLogMaxBytes = $eventLogMaxBytes
        EventLogBackupCount = $eventLogBackupCount
        StagingRoot = $staging
        ShareRoot = $shareRoot
        SshDestination = [string]$config.ssh_destination
        SshExecutable = $sshExecutable
    }
}

function Write-AtomicHelperStatus {
    param(
        [string]$Path,
        [Collections.IDictionary]$Payload
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return
    }
    $parent = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $temporary = Join-Path $parent (
        ".{0}.{1}.tmp" -f (
            (Split-Path -Leaf $Path),
            [Guid]::NewGuid().ToString("N")
        )
    )
    try {
        [IO.File]::WriteAllText(
            $temporary,
            ($Payload | ConvertTo-Json -Compress),
            [Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        Remove-Item `
            -LiteralPath $temporary `
            -Force `
            -ErrorAction SilentlyContinue
    }
}

function Write-HelperEventLog {
    param(
        [string]$Path,
        [string]$Kind,
        [string]$Category,
        [string]$Phase,
        [int]$WinError,
        [int]$RetryCount,
        [DateTime]$TimestampUtc,
        [int64]$MaxBytes,
        [int]$BackupCount
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return
    }
    $parent = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $record = [ordered]@{
        Event = "HelperStatus"
        Kind = $Kind
        Category = $Category
        SourceSizeBytes = 0
        EncodeSeconds = 0.0
        Phase = $Phase
        WinError = $WinError
        RetryCount = $RetryCount
        LoggedUtc = $TimestampUtc.ToString("o")
    }
    $json = ($record | ConvertTo-Json -Compress) + [Environment]::NewLine
    $encoding = [Text.UTF8Encoding]::new($false)
    $bytes = $encoding.GetBytes($json)
    if (
        (Test-Path -LiteralPath $Path -PathType Leaf) -and
        ((Get-Item -LiteralPath $Path).Length + $bytes.Length -gt $MaxBytes)
    ) {
        for ($index = $BackupCount; $index -ge 1; $index--) {
            $source = if ($index -eq 1) {
                $Path
            } else {
                "{0}.{1}" -f $Path, ($index - 1)
            }
            $destination = "{0}.{1}" -f $Path, $index
            if (Test-Path -LiteralPath $source -PathType Leaf) {
                Move-Item `
                    -LiteralPath $source `
                    -Destination $destination `
                    -Force
            }
        }
    }
    $stream = [IO.FileStream]::new(
        $Path,
        [IO.FileMode]::Append,
        [IO.FileAccess]::Write,
        [IO.FileShare]::Read
    )
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally {
        $stream.Dispose()
    }
}

function Write-HelperStatus {
    param(
        [string]$Path,
        [string]$Kind,
        [string]$Category,
        [string]$Phase = "",
        [int]$WinError = 0,
        [int]$RetryCount = 0,
        [string]$EventLogPath = "",
        [int64]$EventLogMaxBytes = 8MB,
        [int]$EventLogBackupCount = 5
    )

    $timestamp = [DateTime]::UtcNow
    $payload = [ordered]@{
        Event = "HelperStatus"
        Kind = $Kind
        Category = $Category
        SourceSizeBytes = 0
        EncodeSeconds = 0.0
        Phase = $Phase
        WinError = $WinError
        RetryCount = $RetryCount
        UpdatedUtc = $timestamp.ToString("o")
    }
    $eventLogFailure = $null
    try {
        Write-HelperEventLog `
            -Path $EventLogPath `
            -Kind $Kind `
            -Category $Category `
            -Phase $Phase `
            -WinError $WinError `
            -RetryCount $RetryCount `
            -TimestampUtc $timestamp `
            -MaxBytes $EventLogMaxBytes `
            -BackupCount $EventLogBackupCount
    } catch {
        $eventLogFailure = $_
    }
    Write-AtomicHelperStatus -Path $Path -Payload $payload
    if ($null -ne $eventLogFailure) {
        $failure = [ordered]@{
            Event = "HelperStatus"
            Kind = "Blocked"
            Category = "HelperEventLogWriteFailed"
            SourceSizeBytes = 0
            EncodeSeconds = 0.0
            Phase = "EventLogWrite"
            WinError = 0
            RetryCount = 0
            UpdatedUtc = [DateTime]::UtcNow.ToString("o")
        }
        Write-AtomicHelperStatus -Path $Path -Payload $failure
        throw $eventLogFailure
    }
}

function Get-ShareConnectionRecord {
    param(
        [string]$ShareRoot,
        [AllowNull()]
        [string[]]$Lines = $null
    )

    if ($null -eq $Lines) {
        $Lines = @(& net.exe use 2>$null)
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to inspect existing SMB sessions safely."
        }
    }
    $sharePattern = [regex]::Escape($ShareRoot.TrimEnd("\"))
    $pattern = (
        '^\s*(?<status>\S+)\s+' +
        '(?:(?<local>[A-Za-z]:)\s+)?' +
        '(?<remote>' + $sharePattern + ')(?=\s|$)'
    )
    foreach ($line in $Lines) {
        $match = [regex]::Match(
            [string]$line,
            $pattern,
            [Text.RegularExpressions.RegexOptions]::IgnoreCase
        )
        if ($match.Success) {
            return [pscustomobject]@{
                Status = $match.Groups["status"].Value
                RemotePath = $match.Groups["remote"].Value
            }
        }
    }
    return $null
}

function Test-ExistingShareConnection {
    param([string]$ShareRoot)

    $record = Get-ShareConnectionRecord $ShareRoot
    return (
        $null -ne $record -and
        [string]$record.Status -eq "OK" -and
        [string]$record.RemotePath -ieq $ShareRoot.TrimEnd("\")
    )
}

function Get-StagingProbeFailure {
    param([Management.Automation.ErrorRecord]$ErrorRecord)

    $exception = $ErrorRecord.Exception
    $winError = if ($exception -is [ComponentModel.Win32Exception]) {
        [int]$exception.NativeErrorCode
    } else {
        [int]($exception.HResult -band 0xFFFF)
    }
    $authErrors = @(5, 65, 86, 1219, 1326, 1327, 1328, 1329, 1330, 1331, 1907, 1909)
    $networkErrors = @(
        53, 54, 59, 64, 67, 121, 1222, 1231, 1232, 1236,
        2250, 10050, 10051, 10053, 10054, 10060, 10064, 10065
    )
    if ($winError -in $authErrors) {
        return [pscustomobject]@{
            Category = "AuthBlocked"
            ExitCode = 21
            WinError = $winError
        }
    }
    if ($winError -in $networkErrors) {
        return [pscustomobject]@{
            Category = "CoordinatorDisconnected"
            ExitCode = 22
            WinError = $winError
        }
    }
    return [pscustomobject]@{
        Category = "StagingAccessBlocked"
        ExitCode = 23
        WinError = $winError
    }
}

function Test-SshIdentity {
    param(
        [string]$Executable,
        [string]$Destination,
        [int]$TimeoutSeconds
    )

    $command = Get-Command $Executable -ErrorAction SilentlyContinue
    if (-not $command) {
        return $false
    }
    $arguments = @(
        "-n", "-T",
        "-o", "BatchMode=yes",
        "-o", "PreferredAuthentications=publickey",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "NumberOfPasswordPrompts=0",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectionAttempts=1",
        "-o", "ConnectTimeout=$TimeoutSeconds",
        $Destination,
        "cmd.exe", "/d", "/c", "exit", "0"
    )
    & $command.Source @arguments 1>$null 2>$null
    return $LASTEXITCODE -eq 0
}

if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $PSScriptRoot "VideoTranscoderLanAssist.json"
}
$helper = Get-HelperConfiguration $ConfigPath
if ([string]::IsNullOrWhiteSpace($ExecutablePath)) {
    $ExecutablePath = Join-Path $helper.ConfigRoot "VideoTranscoderLanAssist.exe"
}
$resolvedExecutable = (Resolve-Path -LiteralPath $ExecutablePath -ErrorAction Stop).Path

$validationOutput = @(
    & $resolvedExecutable `
        "--config" $helper.ConfigPath `
        "--validate-config" 2>$null
)
$validationExit = $LASTEXITCODE
if ($validationExit -ne 0) {
    throw "LAN Assist executable rejected the helper configuration."
}
$validation = $validationOutput | Select-Object -Last 1 | ConvertFrom-Json
if (
    $validation.Event -ne "ConfigValidated" -or
    $validation.Status -ne "Ready" -or
    $validation.Mode -ne "helper"
) {
    throw "LAN Assist configuration validation evidence is invalid."
}

if ($ValidateOnly) {
    [ordered]@{
        Event = "TravelSafeLauncherValidated"
        Status = "Ready"
        Mode = "helper"
    } | ConvertTo-Json -Compress
    exit 0
}

if (-not (Test-SshIdentity `
    $helper.SshExecutable `
    $helper.SshDestination `
    $SshTimeoutSeconds
)) {
    Write-HelperStatus `
        -Path $helper.StatusPath `
        -Kind "NotStarted" `
        -Category "HomeNetworkUnavailable" `
        -EventLogPath $helper.EventLogPath `
        -EventLogMaxBytes $helper.EventLogMaxBytes `
        -EventLogBackupCount $helper.EventLogBackupCount
    exit 0
}

if (-not (Test-ExistingShareConnection $helper.ShareRoot)) {
    Write-HelperStatus `
        -Path $helper.StatusPath `
        -Kind "Blocked" `
        -Category "SmbSessionRequired" `
        -EventLogPath $helper.EventLogPath `
        -EventLogMaxBytes $helper.EventLogMaxBytes `
        -EventLogBackupCount $helper.EventLogBackupCount
    exit 20
}

try {
    [void][IO.File]::GetAttributes($helper.StagingRoot)
} catch {
    $probeFailure = Get-StagingProbeFailure $_
    Write-HelperStatus `
        -Path $helper.StatusPath `
        -Kind "Blocked" `
        -Category $probeFailure.Category `
        -Phase "StagingProbe" `
        -WinError $probeFailure.WinError `
        -EventLogPath $helper.EventLogPath `
        -EventLogMaxBytes $helper.EventLogMaxBytes `
        -EventLogBackupCount $helper.EventLogBackupCount
    exit $probeFailure.ExitCode
}

# Do not mutate the event log between validation and process startup. A local
# sync client can temporarily hardlink a newly written log, and the executable
# correctly rejects that unsafe identity. The helper records its own durable
# HelperProcessStarted event only after configuration validation succeeds.
& $resolvedExecutable "--config" $helper.ConfigPath
if ($null -eq $LASTEXITCODE) {
    throw "LAN Assist helper did not return an exit code."
}
exit $LASTEXITCODE
