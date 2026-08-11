[CmdletBinding()]
param(
    [string]$TaskName = "VideoTranscoder LAN Helper",

    [string]$TrayTaskName = "VideoTranscoder LAN Tray",

    [string]$DeploymentRoot = (
        "M:\Development\Video Transcoder Batch\LAN Assist"
    ),

    [string]$ReleaseRoot = "",

    [ValidateRange(4096, 67108864)]
    [int64]$EventLogMaxBytes = 8MB,

    [ValidateRange(1, 20)]
    [int]$EventLogBackupCount = 5,

    [string]$ControlId = "",

    [string]$VbsLauncherPath = (
        "C:\Users\playa\AppData\Local\StartupLaunchers\" +
        "VideoTranscoder-LAN-Helper.vbs"
    )
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash
}

function Install-VerifiedFile {
    param(
        [string]$Source,
        [string]$Destination
    )

    $sourcePath = (Resolve-Path -LiteralPath $Source -ErrorAction Stop).Path
    $destinationParent = (
        Resolve-Path -LiteralPath (Split-Path -Parent $Destination) `
            -ErrorAction Stop
    ).Path
    $destinationPath = Join-Path `
        $destinationParent `
        (Split-Path -Leaf $Destination)
    $stagePath = "{0}.{1}.codex-new" -f (
        $destinationPath,
        [Guid]::NewGuid().ToString("N")
    )
    $replacementBackupPath = "{0}.{1}.codex-replaced" -f (
        $destinationPath,
        [Guid]::NewGuid().ToString("N")
    )
    $expectedHash = Get-Sha256 $sourcePath
    $replacementCompleted = $false
    try {
        Copy-Item -LiteralPath $sourcePath -Destination $stagePath
        if ((Get-Sha256 $stagePath) -ne $expectedHash) {
            throw "Staged release file failed SHA-256 verification."
        }
        if (Test-Path -LiteralPath $destinationPath -PathType Leaf) {
            $replaceSucceeded = $false
            for ($attempt = 1; $attempt -le 10; $attempt++) {
                try {
                    [IO.File]::Replace(
                        $stagePath,
                        $destinationPath,
                        $replacementBackupPath,
                        $true
                    )
                    $replaceSucceeded = $true
                    break
                } catch [IO.IOException] {
                    if ($attempt -eq 10) {
                        throw
                    }
                    Start-Sleep -Seconds 2
                }
            }
            if (-not $replaceSucceeded) {
                throw "Atomic release replacement did not complete."
            }
            $replacementCompleted = $true
        } else {
            [IO.File]::Move($stagePath, $destinationPath)
        }
        if ((Get-Sha256 $destinationPath) -ne $expectedHash) {
            throw "Installed release file failed SHA-256 verification."
        }
        if ($replacementCompleted) {
            Remove-Item -LiteralPath $replacementBackupPath -Force
        }
    } finally {
        Remove-Item `
            -LiteralPath $stagePath `
            -Force `
            -ErrorAction SilentlyContinue
    }
}

function Get-ValidatedHelperControl {
    param(
        [string]$Path,
        [string]$ExpectedControlId
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Existing helper control file is redirected."
    }
    $value = Get-Content -LiteralPath $Path -Raw |
        ConvertFrom-Json -ErrorAction Stop
    $expectedKeys = @(
        "schema_version",
        "control_id",
        "revision",
        "pc_in_use"
    )
    $actualKeys = @($value.PSObject.Properties.Name)
    if (
        $actualKeys.Count -ne $expectedKeys.Count -or
        @(Compare-Object $actualKeys $expectedKeys).Count -ne 0 -or
        (
            $value.schema_version -isnot [int] -and
            $value.schema_version -isnot [long]
        ) -or
        [int]$value.schema_version -ne 1 -or
        [string]$value.control_id -cne $ExpectedControlId -or
        (
            $value.revision -isnot [int] -and
            $value.revision -isnot [long]
        ) -or
        [int64]$value.revision -lt 1 -or
        $value.pc_in_use -isnot [bool]
    ) {
        throw "Existing helper control file is invalid."
    }
    return $value
}

function Initialize-HelperControl {
    param(
        [string]$Path,
        [string]$ExpectedControlId
    )

    $existing = Get-ValidatedHelperControl $Path $ExpectedControlId
    if ($null -ne $existing) {
        return $existing
    }
    $parent = (Resolve-Path -LiteralPath (Split-Path -Parent $Path)).Path
    $temporary = Join-Path $parent (
        ".helper-control.{0}.tmp" -f [Guid]::NewGuid().ToString("N")
    )
    $payload = [ordered]@{
        schema_version = 1
        control_id = $ExpectedControlId
        revision = 1
        pc_in_use = $false
    } | ConvertTo-Json -Compress
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($payload)
    $stream = $null
    try {
        $stream = [IO.FileStream]::new(
            $temporary,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None,
            4096,
            [IO.FileOptions]::WriteThrough
        )
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
        $stream.Dispose()
        $stream = $null
        [IO.File]::Move($temporary, $Path)
    } finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
        Remove-Item `
            -LiteralPath $temporary `
            -Force `
            -ErrorAction SilentlyContinue
    }
    return Get-ValidatedHelperControl $Path $ExpectedControlId
}

function Restore-HelperControlState {
    param(
        [string]$ControlPath,
        [bool]$ControlWasPresent,
        [bool]$ControlWasValidated,
        [string]$ControlHashBefore,
        [string]$StatusPath,
        [bool]$StatusWasPresent,
        [bool]$StatusWasValidated,
        [string]$StatusHashBefore
    )

    if ($ControlWasPresent -and $ControlWasValidated) {
        if ((Get-Sha256 $ControlPath) -ne $ControlHashBefore) {
            throw "Existing control command changed during rollback."
        }
    } elseif (-not $ControlWasPresent -and (Test-Path -LiteralPath $ControlPath)) {
        Remove-Item -LiteralPath $ControlPath -Force -ErrorAction Stop
    }
    if ($StatusWasPresent -and $StatusWasValidated) {
        if ((Get-Sha256 $StatusPath) -ne $StatusHashBefore) {
            throw "Existing control status changed during rollback."
        }
    } elseif (-not $StatusWasPresent -and (Test-Path -LiteralPath $StatusPath)) {
        Remove-Item -LiteralPath $StatusPath -Force -ErrorAction Stop
    }
}

function Test-ZeroTaskDuration {
    param([object]$Value)

    try {
        $duration = if ($Value -is [TimeSpan]) {
            [TimeSpan]$Value
        } else {
            [Xml.XmlConvert]::ToTimeSpan([string]$Value)
        }
    } catch {
        return $false
    }
    return $duration -eq [TimeSpan]::Zero
}

function Get-TaskAccountSid {
    param([string]$UserId)

    if ([string]::IsNullOrWhiteSpace($UserId)) {
        throw "Scheduled task principal has no user identity."
    }
    try {
        if ($UserId -match '^S-1-') {
            return ([Security.Principal.SecurityIdentifier]::new($UserId)).Value
        }
        $account = [Security.Principal.NTAccount]::new($UserId)
        return $account.Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    } catch {
        throw "Scheduled task principal identity could not be resolved."
    }
}

function Assert-LegacyHelperVbs {
    param(
        [string]$Path,
        [string]$ExpectedLauncherPath
    )

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (
        $item.PSIsContainer -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw "Legacy helper wrapper identity is unsafe."
    }
    $powerShellPath = Join-Path `
        $env:SystemRoot `
        "System32\WindowsPowerShell\v1.0\powershell.exe"
    $expected = @"
Option Explicit

Dim shell, command, exitCode
Set shell = CreateObject("WScript.Shell")

command = Quote("$powerShellPath") & _
    " -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File " & _
    Quote("$ExpectedLauncherPath")

exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode

Function Quote(value)
    Quote = Chr(34) & value & Chr(34)
End Function
"@
    $actualNormalized = ([IO.File]::ReadAllText($Path) -replace "`r`n", "`n").
        TrimEnd([char[]]"`r`n")
    $expectedNormalized = ($expected -replace "`r`n", "`n").
        TrimEnd([char[]]"`r`n")
    $validationGuard = (
        'If WScript.Arguments.Named.Exists("validate") Then WScript.Quit 0'
    )
    $expectedWithValidation = $expectedNormalized.Replace(
        "Option Explicit`n`nDim shell, command, exitCode",
        "Option Explicit`n`n$validationGuard`n`n" +
            "Dim shell, command, exitCode"
    )
    # Windows paths, PowerShell switches, and VBScript identifiers are all
    # case-insensitive. Accept only the exact launcher shape, with or without
    # the historical no-side-effect /validate shortcut used by deployment
    # probes. Any other added, removed, or reordered token still fails closed.
    if (
        $actualNormalized -ine $expectedNormalized -and
        $actualNormalized -ine $expectedWithValidation
    ) {
        throw "Legacy helper wrapper content is not the exact approved launcher."
    }
}

function Assert-HelperTaskSafety {
    param(
        [object]$Task,
        [string]$ExpectedCurrentSid,
        [string]$ExpectedDeploymentPath,
        [string]$ExpectedVbsPath,
        [bool]$AllowLegacyAction,
        [bool]$RequireDemandSafety = $true
    )

    if (
        [string]$Task.TaskPath -cne "\" -or
        [string]$Task.State -notin @("Ready", "Disabled") -or
        (Get-TaskAccountSid -UserId ([string]$Task.Principal.UserId)) -cne
            $ExpectedCurrentSid -or
        [string]$Task.Principal.RunLevel -ne "Limited" -or
        [string]$Task.Principal.LogonType -notin @(
            "Interactive",
            "InteractiveToken"
        ) -or
        @($Task.Triggers | Where-Object { $null -ne $_ }).Count -ne 0 -or
        $Task.Settings.StartWhenAvailable -or
        [int]$Task.Settings.RestartCount -ne 0 -or
        [string]$Task.Settings.MultipleInstances -ne "IgnoreNew" -or
        -not (Test-ZeroTaskDuration $Task.Settings.ExecutionTimeLimit) -or
        (
            $RequireDemandSafety -and
            (
                -not [bool]$Task.Settings.AllowDemandStart -or
                [bool]$Task.Settings.DisallowStartIfOnBatteries -or
                [bool]$Task.Settings.StopIfGoingOnBatteries -or
                [bool]$Task.Settings.RunOnlyIfNetworkAvailable
            )
        )
    ) {
        throw "Helper scheduled task policy is not the exact safe contract."
    }
    $actions = @($Task.Actions | Where-Object { $null -ne $_ })
    if ($actions.Count -ne 1) {
        throw "Helper scheduled task must have exactly one action."
    }
    $launcherPath = Join-Path $ExpectedDeploymentPath "Start-LanAssist.ps1"
    $powerShellPath = Join-Path `
        $env:SystemRoot `
        "System32\WindowsPowerShell\v1.0\powershell.exe"
    $directArguments = (
        '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
        '-WindowStyle Hidden -File "{0}"' -f $launcherPath
    )
    $isDirect = (
        [string]::Equals(
            [string]$actions[0].Execute,
            $powerShellPath,
            [StringComparison]::OrdinalIgnoreCase
        ) -and
        [string]$actions[0].Arguments -ceq $directArguments -and
        [string]::Equals(
            [string]$actions[0].WorkingDirectory,
            $ExpectedDeploymentPath,
            [StringComparison]::OrdinalIgnoreCase
        )
    )
    if ($isDirect) {
        return "Direct"
    }
    $wscriptPath = Join-Path $env:SystemRoot "System32\wscript.exe"
    $legacyArguments = '"{0}"' -f $ExpectedVbsPath
    $isLegacy = (
        $AllowLegacyAction -and
        [string]::Equals(
            [string]$actions[0].Execute,
            $wscriptPath,
            [StringComparison]::OrdinalIgnoreCase
        ) -and
        [string]$actions[0].Arguments -ceq $legacyArguments -and
        [string]::IsNullOrWhiteSpace(
            [string]$actions[0].WorkingDirectory
        )
    )
    if (-not $isLegacy) {
        throw "Helper scheduled task action is not the exact approved action."
    }
    Assert-LegacyHelperVbs $ExpectedVbsPath $launcherPath
    return "LegacyVbs"
}

function Assert-TrayTaskSafety {
    param(
        [object]$Task,
        [string]$ExpectedCurrentSid,
        [string]$ExpectedDeploymentPath,
        [string]$ExpectedTrayPath,
        [string]$ExpectedConfigPath,
        [bool]$RequireDemandSafety = $true
    )

    $triggers = @($Task.Triggers | Where-Object { $null -ne $_ })
    $actions = @($Task.Actions | Where-Object { $null -ne $_ })
    $expectedArguments = '--config "{0}"' -f $ExpectedConfigPath
    if (
        [string]$Task.TaskPath -cne "\" -or
        [string]$Task.State -notin @("Ready", "Disabled") -or
        (Get-TaskAccountSid -UserId ([string]$Task.Principal.UserId)) -cne
            $ExpectedCurrentSid -or
        [string]$Task.Principal.RunLevel -ne "Limited" -or
        [string]$Task.Principal.LogonType -notin @(
            "Interactive",
            "InteractiveToken"
        ) -or
        $triggers.Count -ne 1 -or
        [string]$triggers[0].CimClass.CimClassName -ne
            "MSFT_TaskLogonTrigger" -or
        (Get-TaskAccountSid -UserId ([string]$triggers[0].UserId)) -cne
            $ExpectedCurrentSid -or
        $actions.Count -ne 1 -or
        -not [string]::Equals(
            [string]$actions[0].Execute,
            $ExpectedTrayPath,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        [string]$actions[0].Arguments -cne $expectedArguments -or
        -not [string]::Equals(
            [string]$actions[0].WorkingDirectory,
            $ExpectedDeploymentPath,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        $Task.Settings.StartWhenAvailable -or
        [int]$Task.Settings.RestartCount -ne 0 -or
        [string]$Task.Settings.MultipleInstances -ne "IgnoreNew" -or
        -not (Test-ZeroTaskDuration $Task.Settings.ExecutionTimeLimit) -or
        (
            $RequireDemandSafety -and
            (
                -not [bool]$Task.Settings.AllowDemandStart -or
                [bool]$Task.Settings.DisallowStartIfOnBatteries -or
                [bool]$Task.Settings.StopIfGoingOnBatteries -or
                [bool]$Task.Settings.RunOnlyIfNetworkAvailable
            )
        )
    ) {
        throw "Tray scheduled task is not the exact safe contract."
    }
}

function Assert-NoHelperActivity {
    param(
        [string]$ExpectedLauncherPath,
        [string]$ExpectedVbsPath
    )

    $unsafe = @(
        Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object {
                $_.Name -in @(
                    "VideoTranscoderLanAssist.exe",
                    "VideoTranscoderLanTray.exe"
                ) -or
                (
                    $_.ProcessId -ne $PID -and
                    $_.Name -in @(
                        "powershell.exe",
                        "pwsh.exe",
                        "wscript.exe",
                        "cscript.exe"
                    ) -and
                    (
                        ([string]$_.CommandLine).IndexOf(
                            $ExpectedLauncherPath,
                            [StringComparison]::OrdinalIgnoreCase
                        ) -ge 0 -or
                        ([string]$_.CommandLine).IndexOf(
                            $ExpectedVbsPath,
                            [StringComparison]::OrdinalIgnoreCase
                        ) -ge 0
                    )
                )
            }
    )
    if ($unsafe.Count -ne 0) {
        throw "Refusing deployment while helper or tray activity is present."
    }
}

function Get-UnredirectedFilePresence {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return $false
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (
        $item.PSIsContainer -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw "A protected helper state path is redirected or not a file."
    }
    return $true
}

function Set-TaskXmlChildValue {
    param(
        [Xml.XmlDocument]$Document,
        [Xml.XmlNamespaceManager]$Namespace,
        [Xml.XmlNode]$Parent,
        [string]$Name,
        [string]$Value
    )

    $node = $Parent.SelectSingleNode("task:$Name", $Namespace)
    if ($null -eq $node) {
        $node = $Document.CreateElement(
            $Name,
            "http://schemas.microsoft.com/windows/2004/02/mit/task"
        )
        [void]$Parent.AppendChild($node)
    }
    $node.InnerText = $Value
}

function Get-ValidatedTrayControlSnapshot {
    param(
        [string]$TrayPath,
        [string]$ConfigPath,
        [string]$ReportDirectory,
        [string]$ExpectedControlId,
        [bool]$StatusWasPresent
    )

    $reportPath = Join-Path $ReportDirectory (
        ".tray-self-test.{0}.json" -f [Guid]::NewGuid().ToString("N")
    )
    $expectedStatusCategory = if ($StatusWasPresent) {
        ""
    } else {
        "StatusMissing"
    }
    try {
        $process = Start-Process `
            -FilePath $TrayPath `
            -ArgumentList @(
                "--config",
                "`"$ConfigPath`"",
                "--self-test-report",
                "`"$reportPath`""
            ) `
            -WindowStyle Hidden `
            -Wait `
            -PassThru
        if ($process.ExitCode -ne 0) {
            throw "Packaged tray rejected the helper control state."
        }
        $report = Get-Content -LiteralPath $reportPath -Raw |
            ConvertFrom-Json -ErrorAction Stop
        if (
            $report.event -ne "LanTraySelfTest" -or
            $report.status -ne "Ready" -or
            -not $report.tray_dependencies -or
            [string]$report.control_id -cne $ExpectedControlId -or
            (
                $report.revision -isnot [int] -and
                $report.revision -isnot [long]
            ) -or
            [int64]$report.revision -lt 1 -or
            $report.pc_in_use -isnot [bool] -or
            $report.status_readable -isnot [bool] -or
            [bool]$report.status_readable -ne $StatusWasPresent -or
            [string]$report.status_category -cne $expectedStatusCategory
        ) {
            throw "Packaged tray control safety evidence was invalid."
        }
        return $report
    } finally {
        Remove-Item `
            -LiteralPath $reportPath `
            -Force `
            -ErrorAction SilentlyContinue
    }
}

function Assert-PackagedControlMissing {
    param(
        [string]$TrayPath,
        [string]$ConfigPath,
        [string]$ReportDirectory
    )

    $reportPath = Join-Path $ReportDirectory (
        ".tray-missing-test.{0}.json" -f [Guid]::NewGuid().ToString("N")
    )
    try {
        $process = Start-Process `
            -FilePath $TrayPath `
            -ArgumentList @(
                "--config",
                "`"$ConfigPath`"",
                "--self-test-report",
                "`"$reportPath`""
            ) `
            -WindowStyle Hidden `
            -Wait `
            -PassThru
        if ($process.ExitCode -eq 0) {
            throw "Helper control appeared after the quiescent absence snapshot."
        }
        $report = Get-Content -LiteralPath $reportPath -Raw |
            ConvertFrom-Json -ErrorAction Stop
        if (
            $report.event -ne "LanTraySelfTest" -or
            $report.status -ne "Failed" -or
            [string]$report.category -cne "ControlMissing"
        ) {
            throw "Absent helper control path did not fail closed as missing."
        }
    } finally {
        Remove-Item `
            -LiteralPath $reportPath `
            -Force `
            -ErrorAction SilentlyContinue
    }
}

if (
    $TaskName -cne "VideoTranscoder LAN Helper" -or
    $TrayTaskName -cne "VideoTranscoder LAN Tray"
) {
    throw "Helper and tray task names are fixed by the packaged tray executable."
}

if ([string]::IsNullOrWhiteSpace($ReleaseRoot)) {
    $ReleaseRoot = Join-Path $PSScriptRoot "..\dist\lan-assist"
}
$deploymentPath = (
    Resolve-Path -LiteralPath $DeploymentRoot -ErrorAction Stop
).Path
$releasePath = (Resolve-Path -LiteralPath $ReleaseRoot -ErrorAction Stop).Path
$manifestPath = Join-Path $releasePath "build-manifest.json"
$manifest = Get-Content -LiteralPath $manifestPath -Raw |
    ConvertFrom-Json -ErrorAction Stop
$releaseExe = Join-Path $releasePath "VideoTranscoderLanAssist.exe"
$releaseTray = Join-Path $releasePath "VideoTranscoderLanTray.exe"
$releaseLauncher = Join-Path $releasePath "Start-LanAssist.ps1"
$releaseConnector = Join-Path $releasePath "Connect-LanHelperShare.ps1"
if (
    (Get-Sha256 $releaseExe) -ne [string]$manifest.sha256 -or
    (Get-Sha256 $releaseTray) -ne [string]$manifest.tray_sha256 -or
    (Get-Sha256 $releaseLauncher) -ne [string]$manifest.launcher_sha256 -or
    (Get-Sha256 $releaseConnector) -ne [string]$manifest.connector_sha256
) {
    throw "Release manifest verification failed."
}

$deployedExe = Join-Path $deploymentPath "VideoTranscoderLanAssist.exe"
$deployedTray = Join-Path $deploymentPath "VideoTranscoderLanTray.exe"
$deployedLauncher = Join-Path $deploymentPath "Start-LanAssist.ps1"
$deployedConnector = Join-Path $deploymentPath "Connect-LanHelperShare.ps1"
$deployedManifest = Join-Path $deploymentPath "build-manifest.json"
$configPath = Join-Path $deploymentPath "VideoTranscoderLanAssist.json"
$config = Get-Content -LiteralPath $configPath -Raw |
    ConvertFrom-Json -ErrorAction Stop
$existingControlId = ""
$existingControlProperty = $config.PSObject.Properties["control_id"]
if ($null -ne $existingControlProperty) {
    $existingControlId = [string]$existingControlProperty.Value
}
if ([string]::IsNullOrWhiteSpace($ControlId)) {
    $ControlId = $existingControlId
}
if (
    $ControlId -cnotmatch "^[0-9a-f]{32,64}$" -or
    (
        -not [string]::IsNullOrWhiteSpace($existingControlId) -and
        $existingControlId -cne $ControlId
    )
) {
    throw (
        "Supply the same lowercase 32-64 hex ControlId configured on the " +
        "coordinator. An existing helper identity cannot be replaced."
    )
}
$configUpdates = [ordered]@{
    event_log_file = ".\helper-events.jsonl"
    event_log_max_bytes = $EventLogMaxBytes
    event_log_backup_count = $EventLogBackupCount
    control_file = ".\helper-control.json"
    control_status_file = ".\helper-control-status.json"
    control_id = $ControlId
}
foreach ($entry in $configUpdates.GetEnumerator()) {
    $property = $config.PSObject.Properties[$entry.Key]
    if ($null -eq $property) {
        $config | Add-Member `
            -MemberType NoteProperty `
            -Name $entry.Key `
            -Value $entry.Value
    } else {
        $property.Value = $entry.Value
    }
}
$configStage = Join-Path $deploymentPath (
    ".VideoTranscoderLanAssist.{0}.config-new" -f (
        [Guid]::NewGuid().ToString("N")
    )
)
[IO.File]::WriteAllText(
    $configStage,
    ($config | ConvertTo-Json -Depth 10),
    [Text.UTF8Encoding]::new($false)
)
$tokenValue = [string]$config.token_file
if ([IO.Path]::IsPathRooted($tokenValue)) {
    $tokenPath = (Resolve-Path -LiteralPath $tokenValue -ErrorAction Stop).Path
} else {
    $tokenPath = (
        Resolve-Path `
            -LiteralPath (Join-Path $deploymentPath $tokenValue) `
            -ErrorAction Stop
    ).Path
}
$tokenHashBefore = Get-Sha256 $tokenPath

$currentWindowsIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentIdentity = $currentWindowsIdentity.Name
$currentSid = $currentWindowsIdentity.User.Value
$taskPath = "\"
$task = Get-ScheduledTask `
    -TaskName $TaskName `
    -TaskPath $taskPath `
    -ErrorAction Stop
$helperActionKind = Assert-HelperTaskSafety `
    -Task $task `
    -ExpectedCurrentSid $currentSid `
    -ExpectedDeploymentPath $deploymentPath `
    -ExpectedVbsPath $VbsLauncherPath `
    -AllowLegacyAction $true `
    -RequireDemandSafety $false
$helperTaskWasEnabled = [string]$task.State -eq "Ready"
$taskXml = Export-ScheduledTask -TaskName $TaskName -TaskPath $taskPath
$existingTrayTask = Get-ScheduledTask `
    -TaskName $TrayTaskName `
    -TaskPath "\" `
    -ErrorAction SilentlyContinue
$trayTaskWasPresent = $null -ne $existingTrayTask
$trayTaskWasEnabled = $false
$originalTrayTaskXml = ""
if ($trayTaskWasPresent) {
    Assert-TrayTaskSafety `
        -Task $existingTrayTask `
        -ExpectedCurrentSid $currentSid `
        -ExpectedDeploymentPath $deploymentPath `
        -ExpectedTrayPath $deployedTray `
        -ExpectedConfigPath $configPath `
        -RequireDemandSafety $false
    $trayTaskWasEnabled = [string]$existingTrayTask.State -eq "Ready"
    $originalTrayTaskXml = Export-ScheduledTask `
        -TaskName $TrayTaskName `
        -TaskPath "\"
}
Assert-NoHelperActivity $deployedLauncher $VbsLauncherPath

$controlPath = Join-Path $deploymentPath "helper-control.json"
$controlStatusPath = Join-Path $deploymentPath "helper-control-status.json"
$controlWasPresent = $false
$controlStatusWasPresent = $false
$controlPresenceCaptured = $false
$controlHashBefore = ""
$controlStatusHashBefore = ""

$backupParent = Join-Path $deploymentPath "deployment-backups"
if (-not (Test-Path -LiteralPath $backupParent -PathType Container)) {
    New-Item -ItemType Directory -Path $backupParent | Out-Null
}
$backupPath = Join-Path `
    $backupParent `
    ([DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss-fffffff"))
New-Item -ItemType Directory -Path $backupPath | Out-Null
$managedTargets = @(
    $deployedExe,
    $deployedTray,
    $deployedLauncher,
    $deployedConnector,
    $deployedManifest,
    $configPath
)
$originallyPresent = @{}
foreach ($path in $managedTargets) {
    $originallyPresent[$path] = Test-Path -LiteralPath $path -PathType Leaf
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        Copy-Item -LiteralPath $path -Destination $backupPath
    }
}
[IO.File]::WriteAllText(
    (Join-Path $backupPath "task-original.xml"),
    $taskXml,
    [Text.UTF8Encoding]::new($false)
)
if ($trayTaskWasPresent) {
    [IO.File]::WriteAllText(
        (Join-Path $backupPath "tray-task-original.xml"),
        $originalTrayTaskXml,
        [Text.UTF8Encoding]::new($false)
    )
}

$taskDocument = [Xml.XmlDocument]::new()
$taskDocument.PreserveWhitespace = $true
$taskDocument.LoadXml($taskXml)
$namespace = [Xml.XmlNamespaceManager]::new($taskDocument.NameTable)
$namespace.AddNamespace(
    "task",
    "http://schemas.microsoft.com/windows/2004/02/mit/task"
)
$triggers = $taskDocument.SelectSingleNode("//task:Triggers", $namespace)
if ($null -eq $triggers) {
    throw "Scheduled task XML has no Triggers element."
}
$triggers.RemoveAll()
$restart = $taskDocument.SelectSingleNode(
    "//task:Settings/task:RestartOnFailure",
    $namespace
)
if ($null -ne $restart) {
    [void]$restart.ParentNode.RemoveChild($restart)
}
$startWhenAvailable = $taskDocument.SelectSingleNode(
    "//task:Settings/task:StartWhenAvailable",
    $namespace
)
$settingsNode = $taskDocument.SelectSingleNode("//task:Settings", $namespace)
if ($null -eq $settingsNode) {
    throw "Scheduled task XML has no Settings element."
}
Set-TaskXmlChildValue `
    $taskDocument $namespace $settingsNode "StartWhenAvailable" "false"
Set-TaskXmlChildValue `
    $taskDocument $namespace $settingsNode "MultipleInstancesPolicy" "IgnoreNew"
Set-TaskXmlChildValue `
    $taskDocument $namespace $settingsNode "ExecutionTimeLimit" "PT0S"
Set-TaskXmlChildValue `
    $taskDocument $namespace $settingsNode "AllowStartOnDemand" "true"
Set-TaskXmlChildValue `
    $taskDocument $namespace $settingsNode "DisallowStartIfOnBatteries" "false"
Set-TaskXmlChildValue `
    $taskDocument $namespace $settingsNode "StopIfGoingOnBatteries" "false"
Set-TaskXmlChildValue `
    $taskDocument $namespace $settingsNode "RunOnlyIfNetworkAvailable" "false"
Set-TaskXmlChildValue `
    $taskDocument $namespace $settingsNode "Enabled" "false"

$execNodes = @(
    $taskDocument.SelectNodes("//task:Actions/task:Exec", $namespace)
)
if ($execNodes.Count -ne 1) {
    throw "Scheduled task XML must contain exactly one Exec action."
}
$directPowerShellPath = Join-Path `
    $env:SystemRoot `
    "System32\WindowsPowerShell\v1.0\powershell.exe"
$directHelperArguments = (
    '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
    '-WindowStyle Hidden -File "{0}"' -f $deployedLauncher
)
Set-TaskXmlChildValue `
    $taskDocument $namespace $execNodes[0] "Command" $directPowerShellPath
Set-TaskXmlChildValue `
    $taskDocument $namespace $execNodes[0] "Arguments" $directHelperArguments
Set-TaskXmlChildValue `
    $taskDocument $namespace $execNodes[0] "WorkingDirectory" $deploymentPath

$trayArguments = "--config `"$configPath`""
$trayAction = New-ScheduledTaskAction `
    -Execute $deployedTray `
    -Argument $trayArguments `
    -WorkingDirectory $deploymentPath
$trayTrigger = New-ScheduledTaskTrigger `
    -AtLogOn `
    -User $currentIdentity
$trayPrincipal = New-ScheduledTaskPrincipal `
    -UserId $currentIdentity `
    -LogonType Interactive `
    -RunLevel Limited
$traySettings = New-ScheduledTaskSettingsSet `
    -Disable `
    -StartWhenAvailable:$false `
    -DisallowDemandStart:$false `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$manualTask = $null
$manualTriggerCount = -1
$installedTrayTask = $null
$trayTriggerCount = -1
$controlCommand = $null
$controlHashExpected = ""
$controlWasValidated = $false
$controlStatusWasValidated = $false
$preflightControlSnapshot = $null
$postflightControlSnapshot = $null
try {
    if ([string]$task.State -eq "Ready") {
        Disable-ScheduledTask `
            -TaskName $TaskName `
            -TaskPath $taskPath `
            -ErrorAction Stop | Out-Null
    }
    if ($trayTaskWasPresent -and [string]$existingTrayTask.State -eq "Ready") {
        Disable-ScheduledTask `
            -TaskName $TrayTaskName `
            -TaskPath "\" `
            -ErrorAction Stop | Out-Null
    }
    $disabledHelperTask = Get-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath $taskPath `
        -ErrorAction Stop
    if ([string]$disabledHelperTask.State -ne "Disabled") {
        throw "Helper task could not be placed in the disabled state."
    }
    if ($trayTaskWasPresent) {
        $disabledTrayTask = Get-ScheduledTask `
            -TaskName $TrayTaskName `
            -TaskPath "\" `
            -ErrorAction Stop
        if ([string]$disabledTrayTask.State -ne "Disabled") {
            throw "Tray task could not be placed in the disabled state."
        }
    }
    Assert-NoHelperActivity $deployedLauncher $VbsLauncherPath

    $controlWasPresent = Get-UnredirectedFilePresence $controlPath
    $controlStatusWasPresent = Get-UnredirectedFilePresence $controlStatusPath
    $controlPresenceCaptured = $true

    $validationOutput = @(
        & $releaseExe "--config" $configStage "--validate-config" 2>$null
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Release executable rejected the staged helper configuration."
    }
    $validation = $validationOutput | Select-Object -Last 1 |
        ConvertFrom-Json -ErrorAction Stop
    if (
        $validation.Event -ne "ConfigValidated" -or
        $validation.Status -ne "Ready" -or
        $validation.Mode -ne "helper"
    ) {
        throw "Staged helper configuration validation evidence was invalid."
    }

    if (-not $controlWasPresent) {
        Assert-PackagedControlMissing `
            -TrayPath $releaseTray `
            -ConfigPath $configStage `
            -ReportDirectory $deploymentPath
        $controlCommand = Initialize-HelperControl $controlPath $ControlId
    }
    $preflightControlSnapshot = Get-ValidatedTrayControlSnapshot `
        -TrayPath $releaseTray `
        -ConfigPath $configStage `
        -ReportDirectory $deploymentPath `
        -ExpectedControlId $ControlId `
        -StatusWasPresent $controlStatusWasPresent
    $controlCommand = $preflightControlSnapshot
    $controlHashExpected = Get-Sha256 $controlPath
    $controlWasValidated = $true
    if ($controlWasPresent) {
        $controlHashBefore = $controlHashExpected
    }
    if ($controlStatusWasPresent) {
        $controlStatusHashBefore = Get-Sha256 $controlStatusPath
        $controlStatusWasValidated = $true
    }

    Install-VerifiedFile $releaseExe $deployedExe
    Install-VerifiedFile $releaseTray $deployedTray
    Install-VerifiedFile $releaseLauncher $deployedLauncher
    Install-VerifiedFile $releaseConnector $deployedConnector
    Install-VerifiedFile $manifestPath $deployedManifest
    Install-VerifiedFile $configStage $configPath

    if ((Get-Sha256 $tokenPath) -ne $tokenHashBefore) {
        throw "Token file changed during deployment."
    }

    Register-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath $taskPath `
        -Xml $taskDocument.OuterXml `
        -Force | Out-Null

    $manualTask = Get-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath $taskPath `
        -ErrorAction Stop
    $manualTriggerCount = @(
        $manualTask.Triggers | Where-Object { $null -ne $_ }
    ).Count
    $manualActionKind = Assert-HelperTaskSafety `
        -Task $manualTask `
        -ExpectedCurrentSid $currentSid `
        -ExpectedDeploymentPath $deploymentPath `
        -ExpectedVbsPath $VbsLauncherPath `
        -AllowLegacyAction $false
    if (
        $manualActionKind -cne "Direct" -or
        [string]$manualTask.State -ne "Disabled"
    ) {
        throw "Disabled direct helper task verification failed."
    }

    Register-ScheduledTask `
        -TaskName $TrayTaskName `
        -TaskPath "\" `
        -Action $trayAction `
        -Trigger $trayTrigger `
        -Principal $trayPrincipal `
        -Settings $traySettings `
        -Description "Optional travel/XPS transcoder availability control" `
        -Force | Out-Null
    $installedTrayTask = Get-ScheduledTask `
        -TaskName $TrayTaskName `
        -TaskPath "\" `
        -ErrorAction Stop
    $trayTriggers = @(
        $installedTrayTask.Triggers | Where-Object { $null -ne $_ }
    )
    $trayTriggerCount = $trayTriggers.Count
    Assert-TrayTaskSafety `
        -Task $installedTrayTask `
        -ExpectedCurrentSid $currentSid `
        -ExpectedDeploymentPath $deploymentPath `
        -ExpectedTrayPath $deployedTray `
        -ExpectedConfigPath $configPath
    if ([string]$installedTrayTask.State -ne "Disabled") {
        throw "Disabled limited interactive tray task verification failed."
    }

    $deployedConfig = Get-Content -LiteralPath $configPath -Raw |
        ConvertFrom-Json -ErrorAction Stop
    if (
        [string]$deployedConfig.event_log_file -ne ".\helper-events.jsonl" -or
        [int64]$deployedConfig.event_log_max_bytes -ne $EventLogMaxBytes -or
        [int]$deployedConfig.event_log_backup_count -ne $EventLogBackupCount -or
        [string]$deployedConfig.control_file -cne ".\helper-control.json" -or
        [string]$deployedConfig.control_status_file -cne (
            ".\helper-control-status.json"
        ) -or
        [string]$deployedConfig.control_id -cne $ControlId
    ) {
        throw "Deployed helper configuration verification failed."
    }
    $postflightControlSnapshot = Get-ValidatedTrayControlSnapshot `
        -TrayPath $deployedTray `
        -ConfigPath $configPath `
        -ReportDirectory $deploymentPath `
        -ExpectedControlId $ControlId `
        -StatusWasPresent $controlStatusWasPresent
    if (
        [int64]$postflightControlSnapshot.revision -ne
            [int64]$preflightControlSnapshot.revision -or
        [bool]$postflightControlSnapshot.pc_in_use -ne
            [bool]$preflightControlSnapshot.pc_in_use
    ) {
        throw "Helper control state changed between safe validation snapshots."
    }
    if (
        (Get-Sha256 $controlPath) -ne $controlHashExpected -or
        (
            $controlStatusWasPresent -and
            (Get-Sha256 $controlStatusPath) -ne $controlStatusHashBefore
        ) -or
        (
            -not $controlStatusWasPresent -and
            (Test-Path -LiteralPath $controlStatusPath)
        )
    ) {
        throw "Helper control state changed during deployment."
    }
    Assert-NoHelperActivity $deployedLauncher $VbsLauncherPath

    Enable-ScheduledTask `
        -TaskName $TrayTaskName `
        -TaskPath "\" `
        -ErrorAction Stop | Out-Null
    Enable-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath $taskPath `
        -ErrorAction Stop | Out-Null

    $manualTask = Get-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath $taskPath `
        -ErrorAction Stop
    $installedTrayTask = Get-ScheduledTask `
        -TaskName $TrayTaskName `
        -TaskPath "\" `
        -ErrorAction Stop
    $finalHelperActionKind = Assert-HelperTaskSafety `
        -Task $manualTask `
        -ExpectedCurrentSid $currentSid `
        -ExpectedDeploymentPath $deploymentPath `
        -ExpectedVbsPath $VbsLauncherPath `
        -AllowLegacyAction $false
    Assert-TrayTaskSafety `
        -Task $installedTrayTask `
        -ExpectedCurrentSid $currentSid `
        -ExpectedDeploymentPath $deploymentPath `
        -ExpectedTrayPath $deployedTray `
        -ExpectedConfigPath $configPath
    if (
        $finalHelperActionKind -cne "Direct" -or
        [string]$manualTask.State -ne "Ready" -or
        [string]$installedTrayTask.State -ne "Ready"
    ) {
        throw "Enabled helper/tray task verification failed."
    }
    Assert-NoHelperActivity $deployedLauncher $VbsLauncherPath
} catch {
    $deploymentFailure = $_
    $rollbackFailure = $null
    try {
        $rollbackHelperTask = Get-ScheduledTask `
            -TaskName $TaskName `
            -TaskPath $taskPath `
            -ErrorAction SilentlyContinue
        if (
            $null -ne $rollbackHelperTask -and
            [string]$rollbackHelperTask.State -ne "Disabled"
        ) {
            Disable-ScheduledTask `
                -TaskName $TaskName `
                -TaskPath $taskPath `
                -ErrorAction Stop | Out-Null
        }
        $rollbackTrayTask = Get-ScheduledTask `
            -TaskName $TrayTaskName `
            -TaskPath "\" `
            -ErrorAction SilentlyContinue
        if (
            $null -ne $rollbackTrayTask -and
            [string]$rollbackTrayTask.State -ne "Disabled"
        ) {
            Disable-ScheduledTask `
                -TaskName $TrayTaskName `
                -TaskPath "\" `
                -ErrorAction Stop | Out-Null
        }
        Assert-NoHelperActivity $deployedLauncher $VbsLauncherPath

        foreach ($target in $managedTargets) {
            $backupFile = Join-Path $backupPath (Split-Path -Leaf $target)
            if ([bool]$originallyPresent[$target]) {
                Install-VerifiedFile $backupFile $target
            } elseif (Test-Path -LiteralPath $target -PathType Leaf) {
                Remove-Item -LiteralPath $target -Force
            }
        }
        Register-ScheduledTask `
            -TaskName $TaskName `
            -TaskPath $taskPath `
            -Xml $taskXml `
            -Force | Out-Null
        $restoredHelperTask = Get-ScheduledTask `
            -TaskName $TaskName `
            -TaskPath $taskPath `
            -ErrorAction Stop
        if ([string]$restoredHelperTask.State -ne "Disabled") {
            Disable-ScheduledTask `
                -TaskName $TaskName `
                -TaskPath $taskPath `
                -ErrorAction Stop | Out-Null
        }
        if ($trayTaskWasPresent) {
            Register-ScheduledTask `
                -TaskName $TrayTaskName `
                -TaskPath "\" `
                -Xml $originalTrayTaskXml `
                -Force | Out-Null
            $restoredTrayTask = Get-ScheduledTask `
                -TaskName $TrayTaskName `
                -TaskPath "\" `
                -ErrorAction Stop
            if ([string]$restoredTrayTask.State -ne "Disabled") {
                Disable-ScheduledTask `
                    -TaskName $TrayTaskName `
                    -TaskPath "\" `
                    -ErrorAction Stop | Out-Null
            }
        } else {
            Unregister-ScheduledTask `
                -TaskName $TrayTaskName `
                -TaskPath "\" `
                -Confirm:$false `
                -ErrorAction SilentlyContinue
        }
        if ((Get-Sha256 $tokenPath) -ne $tokenHashBefore) {
            throw "Token verification failed during rollback."
        }
        if ($controlPresenceCaptured) {
            Restore-HelperControlState `
                -ControlPath $controlPath `
                -ControlWasPresent $controlWasPresent `
                -ControlWasValidated $controlWasValidated `
                -ControlHashBefore $controlHashBefore `
                -StatusPath $controlStatusPath `
                -StatusWasPresent $controlStatusWasPresent `
                -StatusWasValidated $controlStatusWasValidated `
                -StatusHashBefore $controlStatusHashBefore
        }

        if ($helperTaskWasEnabled) {
            Enable-ScheduledTask `
                -TaskName $TaskName `
                -TaskPath $taskPath `
                -ErrorAction Stop | Out-Null
        }
        if ($trayTaskWasPresent -and $trayTaskWasEnabled) {
            Enable-ScheduledTask `
                -TaskName $TrayTaskName `
                -TaskPath "\" `
                -ErrorAction Stop | Out-Null
        }
        $restoredHelperTask = Get-ScheduledTask `
            -TaskName $TaskName `
            -TaskPath $taskPath `
            -ErrorAction Stop
        [void](Assert-HelperTaskSafety `
            -Task $restoredHelperTask `
            -ExpectedCurrentSid $currentSid `
            -ExpectedDeploymentPath $deploymentPath `
            -ExpectedVbsPath $VbsLauncherPath `
            -AllowLegacyAction $true `
            -RequireDemandSafety $false)
        if (
            ([string]$restoredHelperTask.State -eq "Ready") -ne
                $helperTaskWasEnabled
        ) {
            throw "Helper task enabled state was not restored."
        }
        if ($trayTaskWasPresent) {
            $restoredTrayTask = Get-ScheduledTask `
                -TaskName $TrayTaskName `
                -TaskPath "\" `
                -ErrorAction Stop
            Assert-TrayTaskSafety `
                -Task $restoredTrayTask `
                -ExpectedCurrentSid $currentSid `
                -ExpectedDeploymentPath $deploymentPath `
                -ExpectedTrayPath $deployedTray `
                -ExpectedConfigPath $configPath `
                -RequireDemandSafety $false
            if (
                ([string]$restoredTrayTask.State -eq "Ready") -ne
                    $trayTaskWasEnabled
            ) {
                throw "Tray task enabled state was not restored."
            }
        }
    } catch {
        $rollbackFailure = $_
    }
    if ($null -ne $rollbackFailure) {
        throw "Helper deployment failed and rollback was incomplete."
    }
    throw $deploymentFailure
} finally {
    Remove-Item -LiteralPath $configStage -Force -ErrorAction SilentlyContinue
}

$postProcesses = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -in @(
                "VideoTranscoderLanAssist.exe",
                "VideoTranscoderLanTray.exe"
            )
        }
)
if ($postProcesses.Count -ne 0) {
    throw "Deployment unexpectedly started LAN Assist or its tray."
}

[ordered]@{
    Event = "TravelSafeHelperDeployed"
    Status = "Ready"
    TaskTriggerCount = $manualTriggerCount
    TrayTaskTriggerCount = $trayTriggerCount
    TrayTaskRunLevel = [string]$installedTrayTask.Principal.RunLevel
    StartWhenAvailable = $manualTask.Settings.StartWhenAvailable
    RestartCount = $manualTask.Settings.RestartCount
    HelperProcessCount = $postProcesses.Count
    BackupPath = $backupPath
    ExecutableSHA256 = Get-Sha256 $deployedExe
    TraySHA256 = Get-Sha256 $deployedTray
    LauncherSHA256 = Get-Sha256 $deployedLauncher
    ConnectorSHA256 = Get-Sha256 $deployedConnector
    TokenPreserved = $true
    ControlId = $ControlId
    ControlStatePreserved = $true
    ControlWasPresent = $controlWasPresent
    EventLogEnabled = $true
} | ConvertTo-Json -Compress
