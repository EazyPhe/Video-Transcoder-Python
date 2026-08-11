[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$CoordinatorConfigPath,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ExpectedWorkRoot,

    [string]$ReleaseRoot = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# These values deliberately are not parameters.  This deployer has authority
# for one coordinator on one host; it must not become a general remote copier.
$sshDestination = "codex-remote"
$taskName = "VideoTranscoder LAN Coordinator"
$taskPath = "\"
$deploymentRoot = (
    "D:\Development\VideoTranscoderToolchain\LAN Assist\coordinator"
)
$deployedConfigPath = Join-Path `
    $deploymentRoot `
    "VideoTranscoderLanAssist.json"
$deployedExecutablePath = Join-Path `
    $deploymentRoot `
    "VideoTranscoderLanAssist.exe"
$deployedLauncherPath = Join-Path `
    $deploymentRoot `
    "Start-Coordinator.ps1"
$deployedManifestPath = Join-Path $deploymentRoot "build-manifest.json"

function Get-Sha256 {
    param([string]$Path)

    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash
}

function ConvertTo-EncodedPowerShell {
    param([string]$ScriptText)

    return [Convert]::ToBase64String(
        [Text.Encoding]::Unicode.GetBytes($ScriptText)
    )
}

function ConvertTo-PowerShellLiteral {
    param([string]$Value)

    return "'{0}'" -f $Value.Replace("'", "''")
}

function New-SshArgumentList {
    param(
        [string]$EncodedCommand,
        [string]$Destination = "codex-remote"
    )

    return @(
        "-n",
        "-T",
        "-o", "BatchMode=yes",
        "-o", "PreferredAuthentications=publickey",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "NumberOfPasswordPrompts=0",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectionAttempts=1",
        "-o", "ConnectTimeout=10",
        $Destination,
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-EncodedCommand", $EncodedCommand
    )
}

function New-SshStdinArgumentList {
    param([string]$Destination = "codex-remote")

    return @(
        "-T",
        "-o", "BatchMode=yes",
        "-o", "PreferredAuthentications=publickey",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "NumberOfPasswordPrompts=0",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectionAttempts=1",
        "-o", "ConnectTimeout=10",
        $Destination,
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-Command", "-"
    )
}

function New-ScpArgumentList {
    param(
        [string]$Source,
        [string]$RemotePath,
        [string]$Destination = "codex-remote"
    )

    return @(
        "-B",
        "-q",
        "-o", "BatchMode=yes",
        "-o", "PreferredAuthentications=publickey",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "NumberOfPasswordPrompts=0",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectionAttempts=1",
        "-o", "ConnectTimeout=10",
        $Source,
        ("{0}:{1}" -f $Destination, $RemotePath)
    )
}

function Invoke-CheckedNativeCommand {
    param(
        [string]$Executable,
        [string[]]$Arguments,
        [string]$FailureMessage
    )

    $output = @(& $Executable @Arguments 2>&1)
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        $detail = @(
            $output |
                Select-Object -Last 5 |
                ForEach-Object { ([string]$_).Trim() } |
                Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
        ) -join " | "
        if ([string]::IsNullOrWhiteSpace($detail)) {
            throw ("{0} (exit {1})." -f $FailureMessage, $exitCode)
        }
        throw ("{0} (exit {1}): {2}" -f $FailureMessage, $exitCode, $detail)
    }
    return $output
}

function Invoke-RemotePowerShell {
    param([string]$ScriptText)

    $arguments = New-SshStdinArgumentList -Destination $sshDestination
    $encodedPayload = ConvertTo-EncodedPowerShell $ScriptText
    $stdinCommand = (
        '$ErrorActionPreference="Stop";' +
        '$payload=[Text.Encoding]::Unicode.GetString(' +
        '[Convert]::FromBase64String("' + $encodedPayload + '"));' +
        'try{& ([ScriptBlock]::Create($payload))}' +
        'catch{[Console]::Error.WriteLine(' +
        '"RemoteCoordinatorCommandFailed");exit 1}'
    )
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = @(
            $stdinCommand |
                & $script:sshExecutable @arguments 2>&1
        )
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($exitCode -ne 0) {
        $detail = @(
            $output |
                Select-Object -Last 5 |
                ForEach-Object { ([string]$_).Trim() } |
                Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
        ) -join " | "
        if ([string]::IsNullOrWhiteSpace($detail)) {
            throw (
                "INSPIRON coordinator deployment command failed " +
                "(exit $exitCode)."
            )
        }
        throw (
            "INSPIRON coordinator deployment command failed " +
            "(exit $exitCode): $detail"
        )
    }
    return $output
}

function ConvertFrom-RemoteJsonResult {
    param([object[]]$Output)

    $lines = @(
        $Output |
            ForEach-Object { ([string]$_).Trim() } |
            Where-Object {
                $_.StartsWith("{", [StringComparison]::Ordinal) -and
                $_.EndsWith("}", [StringComparison]::Ordinal)
            }
    )
    for ($index = $lines.Count - 1; $index -ge 0; $index--) {
        try {
            $candidate = $lines[$index] |
                ConvertFrom-Json -ErrorAction Stop
            if ($null -ne $candidate.PSObject.Properties["Event"]) {
                return $candidate
            }
        } catch {
        }
    }
    throw "Remote coordinator command returned no valid event evidence."
}

function Assert-ReleaseManifest {
    param(
        [string]$ManifestPath,
        [string]$ExecutablePath,
        [string]$LauncherPath
    )

    $manifest = Get-Content -LiteralPath $ManifestPath -Raw |
        ConvertFrom-Json -ErrorAction Stop
    if (
        $manifest.schema_version -ne 1 -or
        [string]$manifest.artifact -ne "VideoTranscoderLanAssist.exe" -or
        [string]::IsNullOrWhiteSpace([string]$manifest.sha256) -or
        [string]::IsNullOrWhiteSpace(
            [string]$manifest.coordinator_launcher_sha256
        ) -or
        (Get-Sha256 $ExecutablePath) -ne [string]$manifest.sha256 -or
        (Get-Sha256 $LauncherPath) -ne
            [string]$manifest.coordinator_launcher_sha256
    ) {
        throw "Coordinator release manifest verification failed."
    }
    return $manifest
}

function Get-RemotePreflightScript {
    param(
        [string]$StageName,
        [string]$ExpectedConfiguredWorkRoot
    )

    $template = @'
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ([string]$env:COMPUTERNAME -cne "INSPIRON") {
    throw "Coordinator remote host identity is not INSPIRON."
}

$TaskName = __TASK_NAME__
$TaskPath = __TASK_PATH__
$DeploymentRoot = __DEPLOYMENT_ROOT__
$ConfigPath = __CONFIG_PATH__
$LauncherPath = __LAUNCHER_PATH__
$ExpectedWorkRoot = __EXPECTED_WORK_ROOT__
$StageName = __STAGE_NAME__
$StageRoot = Join-Path $DeploymentRoot $StageName
$RequiredPorts = @(41800, 41802)

function Get-CanonicalConfiguredPath {
    param(
        [string]$ConfigurationPath,
        [string]$ConfiguredValue,
        [string]$Label
    )

    if ([string]::IsNullOrWhiteSpace($ConfiguredValue)) {
        throw "$Label is missing from the coordinator configuration."
    }
    $candidate = if ([IO.Path]::IsPathRooted($ConfiguredValue)) {
        [IO.Path]::GetFullPath($ConfiguredValue)
    } else {
        [IO.Path]::GetFullPath((Join-Path `
            (Split-Path -Parent $ConfigurationPath) `
            $ConfiguredValue))
    }
    return (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path
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
        throw "Coordinator scheduled task identity is missing."
    }
    try {
        if ($UserId -match '^S-1-') {
            return ([Security.Principal.SecurityIdentifier]::new($UserId)).Value
        }
        return ([Security.Principal.NTAccount]::new($UserId)).Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    } catch {
        throw "Coordinator scheduled task identity could not be resolved."
    }
}

function Assert-ExactTask {
    param(
        [object]$Task,
        [bool]$RequireNormalizedSettings = $false
    )

    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $actions = @($Task.Actions | Where-Object { $null -ne $_ })
    $triggers = @($Task.Triggers | Where-Object { $null -ne $_ })
    if (
        [string]$Task.TaskPath -cne $TaskPath -or
        (Get-TaskAccountSid ([string]$Task.Principal.UserId)) -cne $currentSid -or
        [string]$Task.Principal.LogonType -notin @(
            "Interactive",
            "InteractiveToken"
        ) -or
        [string]$Task.Principal.RunLevel -ne "Highest" -or
        $triggers.Count -ne 1 -or
        [string]$triggers[0].CimClass.CimClassName -ne
            "MSFT_TaskLogonTrigger" -or
        -not [bool]$triggers[0].Enabled -or
        [string]$triggers[0].StartBoundary -cne "" -or
        (Get-TaskAccountSid ([string]$triggers[0].UserId)) -cne $currentSid
    ) {
        throw "Coordinator scheduled task principal or trigger is unexpected."
    }
    if ($actions.Count -ne 1) {
        throw "Coordinator scheduled task must have exactly one action."
    }
    $expectedArguments = (
        '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
        '-WindowStyle Hidden -File "{0}"' -f $LauncherPath
    )
    if ([string]$actions[0].Arguments -cne $expectedArguments) {
        throw "Coordinator scheduled task arguments are unexpected."
    }
    $systemPowerShell = Join-Path `
        $env:SystemRoot `
        "System32\WindowsPowerShell\v1.0\powershell.exe"
    $isLegacyAction = (
        [string]::Equals(
            [string]$actions[0].Execute,
            "powershell.exe",
            [StringComparison]::OrdinalIgnoreCase
        ) -and
        [string]$actions[0].WorkingDirectory -ceq ""
    )
    $isNormalizedAction = (
        [string]::Equals(
            [string]$actions[0].Execute,
            $systemPowerShell,
            [StringComparison]::OrdinalIgnoreCase
        ) -and
        [string]::Equals(
            [string]$actions[0].WorkingDirectory,
            $DeploymentRoot,
            [StringComparison]::OrdinalIgnoreCase
        )
    )
    if (
        -not $isNormalizedAction -and
        ($RequireNormalizedSettings -or -not $isLegacyAction)
    ) {
        throw "Coordinator scheduled task action is unexpected."
    }
    if (
        $RequireNormalizedSettings -and
        (
            $Task.Settings.StartWhenAvailable -or
            -not [bool]$Task.Settings.AllowDemandStart -or
            [int]$Task.Settings.RestartCount -ne 0 -or
            [string]$Task.Settings.MultipleInstances -ne "IgnoreNew" -or
            -not (Test-ZeroTaskDuration $Task.Settings.ExecutionTimeLimit)
        )
    ) {
        throw "Coordinator scheduled task settings are not normalized."
    }
}

function Assert-SafeStoppedState {
    $task = Get-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath $TaskPath `
        -ErrorAction Stop
    Assert-ExactTask $task
    if ([string]$task.State -ne "Disabled") {
        throw "Coordinator task must be disabled before deployment staging."
    }

    $unsafeProcesses = @(
        Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object {
                $_.Name -in @(
                    "VideoTranscoderLanAssist.exe",
                    "ffmpeg.exe",
                    "ffprobe.exe"
                ) -or
                (
                    $_.ProcessId -ne $PID -and
                    $_.Name -in @(
                        "powershell.exe",
                        "pwsh.exe",
                        "wscript.exe",
                        "cscript.exe"
                    ) -and
                    ([string]$_.CommandLine).IndexOf(
                        $LauncherPath,
                        [StringComparison]::OrdinalIgnoreCase
                    ) -ge 0
                )
            }
    )
    if ($unsafeProcesses.Count -ne 0) {
        throw "Coordinator, FFmpeg, FFprobe, or launcher process is still active."
    }

    $listeners = @(
        Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
            Where-Object { [int]$_.LocalPort -in $RequiredPorts }
    )
    if ($listeners.Count -ne 0) {
        throw "Coordinator API or dashboard port is still listening."
    }

    $config = Get-Content -LiteralPath $ConfigPath -Raw |
        ConvertFrom-Json -ErrorAction Stop
    if ($config.schema_version -ne 1 -or $config.mode -ne "coordinator") {
        throw "Live coordinator configuration identity is invalid."
    }
    if ([int]$config.api_port -ne 41800 -or [int]$config.dashboard_port -ne 41802) {
        throw "Live coordinator ports do not match the exact deployment contract."
    }
    $workRoot = Get-CanonicalConfiguredPath `
        $ConfigPath `
        ([string]$config.work_root) `
        "work_root"
    $expected = (Resolve-Path -LiteralPath $ExpectedWorkRoot -ErrorAction Stop).Path
    if (-not $workRoot.Equals($expected, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Configured work_root is outside the exact approved target."
    }
    $journalPath = Join-Path $workRoot "active-transaction.json"
    if (Test-Path -LiteralPath $journalPath) {
        throw "An active coordinator transaction journal is present."
    }
}

$resolvedDeploymentRoot = (
    Resolve-Path -LiteralPath $DeploymentRoot -ErrorAction Stop
).Path
if (-not $resolvedDeploymentRoot.Equals(
    $DeploymentRoot,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "Coordinator deployment root did not resolve exactly."
}
if (
    (Split-Path -Parent $StageRoot) -ine $DeploymentRoot -or
    (Split-Path -Leaf $StageRoot) -notlike ".codex-coordinator-deploy-*"
) {
    throw "Coordinator staging path is outside the exact deployment root."
}
Assert-SafeStoppedState
if (Test-Path -LiteralPath $StageRoot) {
    throw "Coordinator staging path already exists."
}
New-Item -ItemType Directory -Path $StageRoot | Out-Null

[ordered]@{
    Event = "CoordinatorDeploymentStagingReady"
    Status = "Ready"
    TaskState = [string](
        Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath
    ).State
    RuntimeProcessCount = 0
    ListenerCount = 0
    JournalPresent = $false
} | ConvertTo-Json -Compress
'@

    return $template.
        Replace("__TASK_NAME__", (ConvertTo-PowerShellLiteral $taskName)).
        Replace("__TASK_PATH__", (ConvertTo-PowerShellLiteral $taskPath)).
        Replace(
            "__DEPLOYMENT_ROOT__",
            (ConvertTo-PowerShellLiteral $deploymentRoot)
        ).
        Replace(
            "__CONFIG_PATH__",
            (ConvertTo-PowerShellLiteral $deployedConfigPath)
        ).
        Replace(
            "__LAUNCHER_PATH__",
            (ConvertTo-PowerShellLiteral $deployedLauncherPath)
        ).
        Replace(
            "__EXPECTED_WORK_ROOT__",
            (ConvertTo-PowerShellLiteral $ExpectedConfiguredWorkRoot)
        ).
        Replace("__STAGE_NAME__", (ConvertTo-PowerShellLiteral $StageName))
}

function Get-RemoteDeploymentScript {
    param(
        [string]$StageName,
        [string]$ExpectedConfiguredWorkRoot,
        [string]$ExecutableHash,
        [string]$ConfigHash,
        [string]$LauncherHash,
        [string]$ManifestHash
    )

    $template = @'
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ([string]$env:COMPUTERNAME -cne "INSPIRON") {
    throw "Coordinator remote host identity is not INSPIRON."
}

$TaskName = __TASK_NAME__
$TaskPath = __TASK_PATH__
$DeploymentRoot = __DEPLOYMENT_ROOT__
$ConfigPath = __CONFIG_PATH__
$ExecutablePath = __EXECUTABLE_PATH__
$LauncherPath = __LAUNCHER_PATH__
$ManifestPath = __MANIFEST_PATH__
$ExpectedWorkRoot = __EXPECTED_WORK_ROOT__
$StageName = __STAGE_NAME__
$StageRoot = Join-Path $DeploymentRoot $StageName
$ConfigStage = Join-Path $DeploymentRoot (".{0}.config-new" -f $StageName)
$ExpectedExecutableHash = __EXECUTABLE_HASH__
$ExpectedConfigHash = __CONFIG_HASH__
$ExpectedLauncherHash = __LAUNCHER_HASH__
$ExpectedManifestHash = __MANIFEST_HASH__
$RequiredPorts = @(41800, 41802)

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash
}

function Get-CanonicalConfiguredPath {
    param(
        [string]$ConfigurationPath,
        [string]$ConfiguredValue,
        [string]$Label
    )

    if ([string]::IsNullOrWhiteSpace($ConfiguredValue)) {
        throw "$Label is missing from the coordinator configuration."
    }
    $candidate = if ([IO.Path]::IsPathRooted($ConfiguredValue)) {
        [IO.Path]::GetFullPath($ConfiguredValue)
    } else {
        [IO.Path]::GetFullPath((Join-Path `
            (Split-Path -Parent $ConfigurationPath) `
            $ConfiguredValue))
    }
    return (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path
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

function Assert-ExactTask {
    param(
        [object]$Task,
        [bool]$RequireNormalizedSettings = $false
    )

    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $actions = @($Task.Actions | Where-Object { $null -ne $_ })
    $triggers = @($Task.Triggers | Where-Object { $null -ne $_ })
    if (
        [string]$Task.TaskPath -cne $TaskPath -or
        (Get-TaskAccountSid ([string]$Task.Principal.UserId)) -cne $currentSid -or
        [string]$Task.Principal.LogonType -notin @(
            "Interactive",
            "InteractiveToken"
        ) -or
        [string]$Task.Principal.RunLevel -ne "Highest" -or
        $triggers.Count -ne 1 -or
        [string]$triggers[0].CimClass.CimClassName -ne
            "MSFT_TaskLogonTrigger" -or
        -not [bool]$triggers[0].Enabled -or
        [string]$triggers[0].StartBoundary -cne "" -or
        (Get-TaskAccountSid ([string]$triggers[0].UserId)) -cne $currentSid
    ) {
        throw "Coordinator scheduled task principal or trigger is unexpected."
    }
    if ($actions.Count -ne 1) {
        throw "Coordinator scheduled task must have exactly one action."
    }
    $expectedArguments = (
        '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
        '-WindowStyle Hidden -File "{0}"' -f $LauncherPath
    )
    if ([string]$actions[0].Arguments -cne $expectedArguments) {
        throw "Coordinator scheduled task arguments are unexpected."
    }
    $systemPowerShell = Join-Path `
        $env:SystemRoot `
        "System32\WindowsPowerShell\v1.0\powershell.exe"
    $isLegacyAction = (
        [string]::Equals(
            [string]$actions[0].Execute,
            "powershell.exe",
            [StringComparison]::OrdinalIgnoreCase
        ) -and
        [string]$actions[0].WorkingDirectory -ceq ""
    )
    $isNormalizedAction = (
        [string]::Equals(
            [string]$actions[0].Execute,
            $systemPowerShell,
            [StringComparison]::OrdinalIgnoreCase
        ) -and
        [string]::Equals(
            [string]$actions[0].WorkingDirectory,
            $DeploymentRoot,
            [StringComparison]::OrdinalIgnoreCase
        )
    )
    if (
        -not $isNormalizedAction -and
        ($RequireNormalizedSettings -or -not $isLegacyAction)
    ) {
        throw "Coordinator scheduled task action is unexpected."
    }
    if (
        $RequireNormalizedSettings -and
        (
            $Task.Settings.StartWhenAvailable -or
            -not [bool]$Task.Settings.AllowDemandStart -or
            [int]$Task.Settings.RestartCount -ne 0 -or
            [string]$Task.Settings.MultipleInstances -ne "IgnoreNew" -or
            -not (Test-ZeroTaskDuration $Task.Settings.ExecutionTimeLimit)
        )
    ) {
        throw "Coordinator scheduled task settings are not normalized."
    }
}

function Get-TaskAccountSid {
    param([string]$UserId)

    if ([string]::IsNullOrWhiteSpace($UserId)) {
        throw "Coordinator scheduled task identity is missing."
    }
    try {
        if ($UserId -match '^S-1-') {
            return ([Security.Principal.SecurityIdentifier]::new($UserId)).Value
        }
        return ([Security.Principal.NTAccount]::new($UserId)).Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    } catch {
        throw "Coordinator scheduled task identity could not be resolved."
    }
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

function Get-DisabledTaskXml {
    param(
        [string]$Xml,
        [bool]$NormalizeSettings
    )

    $document = [Xml.XmlDocument]::new()
    $document.PreserveWhitespace = $true
    $document.LoadXml($Xml)
    $namespace = [Xml.XmlNamespaceManager]::new($document.NameTable)
    $namespace.AddNamespace(
        "task",
        "http://schemas.microsoft.com/windows/2004/02/mit/task"
    )
    $settings = $document.SelectSingleNode("//task:Settings", $namespace)
    if ($null -eq $settings) {
        throw "Coordinator scheduled task XML has no Settings element."
    }
    Set-TaskXmlChildValue `
        $document $namespace $settings "Enabled" "false"
    if ($NormalizeSettings) {
        $restart = $settings.SelectSingleNode(
            "task:RestartOnFailure",
            $namespace
        )
        if ($null -ne $restart) {
            [void]$settings.RemoveChild($restart)
        }
        Set-TaskXmlChildValue `
            $document $namespace $settings "StartWhenAvailable" "false"
        Set-TaskXmlChildValue `
            $document $namespace $settings "AllowStartOnDemand" "true"
        Set-TaskXmlChildValue `
            $document $namespace $settings "MultipleInstancesPolicy" "IgnoreNew"
        Set-TaskXmlChildValue `
            $document $namespace $settings "ExecutionTimeLimit" "PT0S"
        $execNodes = @(
            $document.SelectNodes("//task:Actions/task:Exec", $namespace)
        )
        if ($execNodes.Count -ne 1) {
            throw "Coordinator scheduled task XML must have one Exec action."
        }
        $systemPowerShell = Join-Path `
            $env:SystemRoot `
            "System32\WindowsPowerShell\v1.0\powershell.exe"
        $exactArguments = (
            '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
            '-WindowStyle Hidden -File "{0}"' -f $LauncherPath
        )
        Set-TaskXmlChildValue `
            $document $namespace $execNodes[0] "Command" $systemPowerShell
        Set-TaskXmlChildValue `
            $document $namespace $execNodes[0] "Arguments" $exactArguments
        Set-TaskXmlChildValue `
            $document $namespace $execNodes[0] "WorkingDirectory" $DeploymentRoot
    }
    return $document.OuterXml
}

function Get-ExactCoordinatorTask {
    $task = Get-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath $TaskPath `
        -ErrorAction Stop
    Assert-ExactTask $task
    if ([string]$task.State -notin @("Ready", "Disabled")) {
        throw "Coordinator task is not safely stopped."
    }
    return $task
}

function Assert-NoCoordinatorActivity {
    param([string]$WorkRoot)

    $unsafeProcesses = @(
        Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object {
                $_.Name -in @(
                    "VideoTranscoderLanAssist.exe",
                    "ffmpeg.exe",
                    "ffprobe.exe"
                ) -or
                (
                    $_.ProcessId -ne $PID -and
                    $_.Name -in @(
                        "powershell.exe",
                        "pwsh.exe",
                        "wscript.exe",
                        "cscript.exe"
                    ) -and
                    ([string]$_.CommandLine).IndexOf(
                        $LauncherPath,
                        [StringComparison]::OrdinalIgnoreCase
                    ) -ge 0
                )
            }
    )
    if ($unsafeProcesses.Count -ne 0) {
        throw "Coordinator, FFmpeg, FFprobe, or launcher process is still active."
    }
    $listeners = @(
        Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
            Where-Object { [int]$_.LocalPort -in $RequiredPorts }
    )
    if ($listeners.Count -ne 0) {
        throw "Coordinator API or dashboard port is still listening."
    }
    if (Test-Path -LiteralPath (Join-Path $WorkRoot "active-transaction.json")) {
        throw "An active coordinator transaction journal is present."
    }
}

function Install-AtomicVerifiedFile {
    param(
        [string]$Source,
        [string]$Destination,
        [string]$ExpectedHash
    )

    $destinationParent = (Resolve-Path `
        -LiteralPath (Split-Path -Parent $Destination) `
        -ErrorAction Stop).Path
    if (-not $destinationParent.Equals(
        $DeploymentRoot,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Atomic replacement target is outside the deployment root."
    }
    if ((Get-Sha256 $Source) -ne $ExpectedHash) {
        throw "Atomic replacement source hash is invalid."
    }
    $newPath = Join-Path $destinationParent (
        ".{0}.{1}.new" -f (
            Split-Path -Leaf $Destination
        ), [Guid]::NewGuid().ToString("N")
    )
    $replaceBackup = Join-Path $destinationParent (
        ".{0}.{1}.replaced" -f (
            Split-Path -Leaf $Destination
        ), [Guid]::NewGuid().ToString("N")
    )
    try {
        Copy-Item -LiteralPath $Source -Destination $newPath
        if ((Get-Sha256 $newPath) -ne $ExpectedHash) {
            throw "Atomic replacement staging hash is invalid."
        }
        if (Test-Path -LiteralPath $Destination -PathType Leaf) {
            [IO.File]::Replace($newPath, $Destination, $replaceBackup, $true)
        } else {
            [IO.File]::Move($newPath, $Destination)
        }
        if ((Get-Sha256 $Destination) -ne $ExpectedHash) {
            throw "Atomic replacement destination hash is invalid."
        }
    } finally {
        Remove-Item `
            -LiteralPath $newPath, $replaceBackup `
            -Force `
            -ErrorAction SilentlyContinue
    }
}

function Assert-ConfigIdentity {
    param(
        [object]$LiveConfig,
        [object]$CandidateConfig,
        [string]$LiveConfigPath,
        [string]$CandidateConfigPath
    )

    foreach ($config in @($LiveConfig, $CandidateConfig)) {
        if ($config.schema_version -ne 1 -or $config.mode -ne "coordinator") {
            throw "Coordinator configuration identity is invalid."
        }
        if (
            [int]$config.api_port -ne 41800 -or
            [int]$config.dashboard_port -ne 41802
        ) {
            throw "Coordinator ports do not match the exact deployment contract."
        }
    }
    $expected = (Resolve-Path `
        -LiteralPath $ExpectedWorkRoot `
        -ErrorAction Stop).Path
    $liveWorkRoot = Get-CanonicalConfiguredPath `
        $LiveConfigPath `
        ([string]$LiveConfig.work_root) `
        "live work_root"
    $candidateWorkRoot = Get-CanonicalConfiguredPath `
        $CandidateConfigPath `
        ([string]$CandidateConfig.work_root) `
        "candidate work_root"
    if (
        -not $liveWorkRoot.Equals(
            $expected,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        -not $candidateWorkRoot.Equals(
            $expected,
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "Coordinator work_root changed or is not the exact approved path."
    }
    $liveMediaRoot = Get-CanonicalConfiguredPath `
        $LiveConfigPath `
        ([string]$LiveConfig.root) `
        "live root"
    $candidateMediaRoot = Get-CanonicalConfiguredPath `
        $CandidateConfigPath `
        ([string]$CandidateConfig.root) `
        "candidate root"
    if (-not $liveMediaRoot.Equals(
        $candidateMediaRoot,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Coordinator media root changed during deployment."
    }
    $liveToken = Get-CanonicalConfiguredPath `
        $LiveConfigPath `
        ([string]$LiveConfig.token_file) `
        "live token_file"
    $candidateToken = Get-CanonicalConfiguredPath `
        $CandidateConfigPath `
        ([string]$CandidateConfig.token_file) `
        "candidate token_file"
    if (-not $liveToken.Equals(
        $candidateToken,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Coordinator token path changed during deployment."
    }
    [pscustomobject]@{
        WorkRoot = $liveWorkRoot
        TokenPath = $liveToken
    }
}

$resolvedDeploymentRoot = (
    Resolve-Path -LiteralPath $DeploymentRoot -ErrorAction Stop
).Path
if (-not $resolvedDeploymentRoot.Equals(
    $DeploymentRoot,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "Coordinator deployment root did not resolve exactly."
}
if (
    (Split-Path -Parent $StageRoot) -ine $DeploymentRoot -or
    (Split-Path -Leaf $StageRoot) -notlike ".codex-coordinator-deploy-*" -or
    (Split-Path -Parent $ConfigStage) -ine $DeploymentRoot
) {
    throw "Coordinator staging path is outside the exact deployment root."
}

$stageExecutable = Join-Path $StageRoot "VideoTranscoderLanAssist.exe"
$stageConfigSource = Join-Path $StageRoot "VideoTranscoderLanAssist.json"
$stageLauncher = Join-Path $StageRoot "Start-Coordinator.ps1"
$stageManifest = Join-Path $StageRoot "build-manifest.json"
$backupPath = $null
$tokenPath = $null
$tokenHashBefore = $null
$deploymentAttempted = $false
$taskCaptured = $false
$taskWasEnabled = $false
$taskXml = ""
$originalDisabledTaskXml = ""
$normalizedTaskXml = ""
$identity = $null

try {
    $task = Get-ExactCoordinatorTask
    $taskXml = Export-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath $TaskPath `
        -ErrorAction Stop
    $taskWasEnabled = [string]$task.State -eq "Ready"
    $originalDisabledTaskXml = Get-DisabledTaskXml `
        -Xml $taskXml `
        -NormalizeSettings $false
    $normalizedTaskXml = Get-DisabledTaskXml `
        -Xml $taskXml `
        -NormalizeSettings $true
    $taskCaptured = $true
    if ($taskWasEnabled) {
        Disable-ScheduledTask `
            -TaskName $TaskName `
            -TaskPath $TaskPath `
            -ErrorAction Stop | Out-Null
    }
    $disabledTask = Get-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath $TaskPath `
        -ErrorAction Stop
    if ([string]$disabledTask.State -ne "Disabled") {
        throw "Coordinator task could not be placed in the disabled state."
    }

    foreach ($stageFile in @(
        @{ Path = $stageExecutable; Hash = $ExpectedExecutableHash },
        @{ Path = $stageConfigSource; Hash = $ExpectedConfigHash },
        @{ Path = $stageLauncher; Hash = $ExpectedLauncherHash },
        @{ Path = $stageManifest; Hash = $ExpectedManifestHash }
    )) {
        if (
            -not (Test-Path -LiteralPath $stageFile.Path -PathType Leaf) -or
            (Get-Sha256 $stageFile.Path) -ne $stageFile.Hash
        ) {
            throw "A staged coordinator artifact failed SHA-256 verification."
        }
    }

    $stageManifestRecord = Get-Content -LiteralPath $stageManifest -Raw |
        ConvertFrom-Json -ErrorAction Stop
    if (
        $stageManifestRecord.schema_version -ne 1 -or
        [string]$stageManifestRecord.sha256 -ne $ExpectedExecutableHash -or
        [string]$stageManifestRecord.coordinator_launcher_sha256 -ne
            $ExpectedLauncherHash
    ) {
        throw "Staged coordinator manifest evidence is invalid."
    }

    Copy-Item -LiteralPath $stageConfigSource -Destination $ConfigStage
    if ((Get-Sha256 $ConfigStage) -ne $ExpectedConfigHash) {
        throw "Adjacent coordinator configuration staging hash is invalid."
    }

    $liveConfig = Get-Content -LiteralPath $ConfigPath -Raw |
        ConvertFrom-Json -ErrorAction Stop
    $candidateConfig = Get-Content -LiteralPath $ConfigStage -Raw |
        ConvertFrom-Json -ErrorAction Stop
    $identity = Assert-ConfigIdentity `
        $liveConfig `
        $candidateConfig `
        $ConfigPath `
        $ConfigStage
    $tokenPath = $identity.TokenPath
    $tokenHashBefore = Get-Sha256 $tokenPath
    Assert-NoCoordinatorActivity $identity.WorkRoot

    $validationOutput = @(
        & $stageExecutable `
            "--config" $ConfigStage `
            "--validate-config" 2>$null
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Staged executable rejected the coordinator configuration."
    }
    $validation = $validationOutput | Select-Object -Last 1 |
        ConvertFrom-Json -ErrorAction Stop
    if (
        $validation.Event -ne "ConfigValidated" -or
        $validation.Status -ne "Ready" -or
        $validation.Mode -ne "coordinator"
    ) {
        throw "Staged coordinator configuration validation evidence is invalid."
    }

    $backupParent = Join-Path $DeploymentRoot "deployment-backups"
    if (-not (Test-Path -LiteralPath $backupParent -PathType Container)) {
        New-Item -ItemType Directory -Path $backupParent | Out-Null
    }
    $backupPath = Join-Path $backupParent (
        "{0}-{1}" -f (
            [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss-fffffff")
        ), [Guid]::NewGuid().ToString("N")
    )
    New-Item -ItemType Directory -Path $backupPath | Out-Null
    foreach ($managedPath in @(
        $ExecutablePath,
        $ConfigPath,
        $LauncherPath,
        $ManifestPath
    )) {
        if (-not (Test-Path -LiteralPath $managedPath -PathType Leaf)) {
            throw "An exact managed coordinator artifact is missing."
        }
        Copy-Item -LiteralPath $managedPath -Destination $backupPath
    }
    [IO.File]::WriteAllText(
        (Join-Path $backupPath "task-original.xml"),
        $taskXml,
        [Text.UTF8Encoding]::new($false)
    )
    # Only the token SHA-256 is retained.  Token bytes are never copied.
    [IO.File]::WriteAllText(
        (Join-Path $backupPath "token-sha256.txt"),
        ($tokenHashBefore + [Environment]::NewLine),
        [Text.Encoding]::ASCII
    )

    Assert-NoCoordinatorActivity $identity.WorkRoot
    if ((Get-Sha256 $tokenPath) -ne $tokenHashBefore) {
        throw "Coordinator token changed before artifact replacement."
    }

    $deploymentAttempted = $true
    Install-AtomicVerifiedFile `
        $stageExecutable `
        $ExecutablePath `
        $ExpectedExecutableHash
    Install-AtomicVerifiedFile `
        $ConfigStage `
        $ConfigPath `
        $ExpectedConfigHash
    Install-AtomicVerifiedFile `
        $stageLauncher `
        $LauncherPath `
        $ExpectedLauncherHash

    $installedValidationOutput = @(
        & $ExecutablePath `
            "--config" $ConfigPath `
            "--validate-config" 2>$null
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Installed executable rejected the coordinator configuration."
    }
    $installedValidation = $installedValidationOutput |
        Select-Object -Last 1 |
        ConvertFrom-Json -ErrorAction Stop
    if (
        $installedValidation.Event -ne "ConfigValidated" -or
        $installedValidation.Status -ne "Ready" -or
        $installedValidation.Mode -ne "coordinator"
    ) {
        throw "Installed coordinator validation evidence is invalid."
    }
    if ((Get-Sha256 $tokenPath) -ne $tokenHashBefore) {
        throw "Coordinator token changed during artifact replacement."
    }
    Assert-NoCoordinatorActivity $identity.WorkRoot

    # Install the manifest last.  Every operation that can require binary,
    # configuration, or launcher rollback has already passed at this point.
    Install-AtomicVerifiedFile `
        $stageManifest `
        $ManifestPath `
        $ExpectedManifestHash

    Register-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath $TaskPath `
        -Xml $normalizedTaskXml `
        -Force | Out-Null
    $normalizedTask = Get-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath $TaskPath `
        -ErrorAction Stop
    Assert-ExactTask $normalizedTask $true
    if ([string]$normalizedTask.State -ne "Disabled") {
        throw "Normalized coordinator task was not registered disabled."
    }
    Assert-NoCoordinatorActivity $identity.WorkRoot

    Enable-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath $TaskPath `
        -ErrorAction Stop | Out-Null
    $enabledTask = Get-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath $TaskPath `
        -ErrorAction Stop
    Assert-ExactTask $enabledTask $true
    if ([string]$enabledTask.State -ne "Ready") {
        throw "Normalized coordinator task was not enabled after deployment."
    }
    Assert-NoCoordinatorActivity $identity.WorkRoot

    [ordered]@{
        Event = "LanCoordinatorSafelyDeployed"
        Status = "Ready"
        TaskState = [string]$enabledTask.State
        BackupPath = $backupPath
        ExecutableSHA256 = Get-Sha256 $ExecutablePath
        ConfigSHA256 = Get-Sha256 $ConfigPath
        LauncherSHA256 = Get-Sha256 $LauncherPath
        ManifestSHA256 = Get-Sha256 $ManifestPath
        TokenPreserved = $true
        JournalPresent = $false
        RuntimeProcessCount = 0
        ListenerCount = 0
        StartRequired = $true
    } | ConvertTo-Json -Compress
} catch {
    $deploymentFailure = $_
    $rollbackFailure = $null
    try {
        if ($taskCaptured) {
            $rollbackTask = Get-ScheduledTask `
                -TaskName $TaskName `
                -TaskPath $TaskPath `
                -ErrorAction SilentlyContinue
            if (
                $null -ne $rollbackTask -and
                [string]$rollbackTask.State -ne "Disabled"
            ) {
                Disable-ScheduledTask `
                    -TaskName $TaskName `
                    -TaskPath $TaskPath `
                    -ErrorAction Stop | Out-Null
            }
        }
        if ($null -ne $identity) {
            Assert-NoCoordinatorActivity $identity.WorkRoot
        }
        if ($deploymentAttempted -and $null -ne $backupPath) {
            # Work data, ledger, token, journal, and candidates are never
            # rollback targets. Only the exact deployment artifacts are.
            foreach ($rollbackTarget in @(
                @{ Path = $ExecutablePath; Name = "VideoTranscoderLanAssist.exe" },
                @{ Path = $ConfigPath; Name = "VideoTranscoderLanAssist.json" },
                @{ Path = $LauncherPath; Name = "Start-Coordinator.ps1" },
                @{ Path = $ManifestPath; Name = "build-manifest.json" }
            )) {
                $backupSource = Join-Path $backupPath $rollbackTarget.Name
                Install-AtomicVerifiedFile `
                    $backupSource `
                    $rollbackTarget.Path `
                    (Get-Sha256 $backupSource)
            }
        }
        if (
            $null -ne $tokenPath -and
            (Get-Sha256 $tokenPath) -ne $tokenHashBefore
        ) {
            throw "Token verification failed after coordinator rollback."
        }
        if ($taskCaptured) {
            Register-ScheduledTask `
                -TaskName $TaskName `
                -TaskPath $TaskPath `
                -Xml $originalDisabledTaskXml `
                -Force | Out-Null
            $restoredTask = Get-ScheduledTask `
                -TaskName $TaskName `
                -TaskPath $TaskPath `
                -ErrorAction Stop
            Assert-ExactTask $restoredTask
            if ([string]$restoredTask.State -ne "Disabled") {
                throw "Original coordinator task was not restored disabled."
            }
            if ($taskWasEnabled) {
                Enable-ScheduledTask `
                    -TaskName $TaskName `
                    -TaskPath $TaskPath `
                    -ErrorAction Stop | Out-Null
            }
            $restoredTask = Get-ScheduledTask `
                -TaskName $TaskName `
                -TaskPath $TaskPath `
                -ErrorAction Stop
            Assert-ExactTask $restoredTask
            if (
                ([string]$restoredTask.State -eq "Ready") -ne $taskWasEnabled
            ) {
                throw "Original coordinator task enabled state was not restored."
            }
        }
    } catch {
        $rollbackFailure = $_
    }
    if ($null -ne $rollbackFailure) {
        throw "Coordinator deployment failed; rollback was incomplete."
    }
    throw $deploymentFailure
} finally {
    Remove-Item -LiteralPath $ConfigStage -Force -ErrorAction SilentlyContinue
    if (
        (Split-Path -Parent $StageRoot) -ieq $DeploymentRoot -and
        (Split-Path -Leaf $StageRoot) -like ".codex-coordinator-deploy-*"
    ) {
        Remove-Item `
            -LiteralPath $StageRoot `
            -Recurse `
            -Force `
            -ErrorAction SilentlyContinue
    }
}
'@

    return $template.
        Replace("__TASK_NAME__", (ConvertTo-PowerShellLiteral $taskName)).
        Replace("__TASK_PATH__", (ConvertTo-PowerShellLiteral $taskPath)).
        Replace(
            "__DEPLOYMENT_ROOT__",
            (ConvertTo-PowerShellLiteral $deploymentRoot)
        ).
        Replace(
            "__CONFIG_PATH__",
            (ConvertTo-PowerShellLiteral $deployedConfigPath)
        ).
        Replace(
            "__EXECUTABLE_PATH__",
            (ConvertTo-PowerShellLiteral $deployedExecutablePath)
        ).
        Replace(
            "__LAUNCHER_PATH__",
            (ConvertTo-PowerShellLiteral $deployedLauncherPath)
        ).
        Replace(
            "__MANIFEST_PATH__",
            (ConvertTo-PowerShellLiteral $deployedManifestPath)
        ).
        Replace(
            "__EXPECTED_WORK_ROOT__",
            (ConvertTo-PowerShellLiteral $ExpectedConfiguredWorkRoot)
        ).
        Replace("__STAGE_NAME__", (ConvertTo-PowerShellLiteral $StageName)).
        Replace(
            "__EXECUTABLE_HASH__",
            (ConvertTo-PowerShellLiteral $ExecutableHash)
        ).
        Replace("__CONFIG_HASH__", (ConvertTo-PowerShellLiteral $ConfigHash)).
        Replace(
            "__LAUNCHER_HASH__",
            (ConvertTo-PowerShellLiteral $LauncherHash)
        ).
        Replace("__MANIFEST_HASH__", (ConvertTo-PowerShellLiteral $ManifestHash))
}

function Get-RemoteCleanupScript {
    param([string]$StageName)

    $template = @'
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ([string]$env:COMPUTERNAME -cne "INSPIRON") {
    throw "Coordinator remote host identity is not INSPIRON."
}
$DeploymentRoot = __DEPLOYMENT_ROOT__
$StageName = __STAGE_NAME__
$StageRoot = Join-Path $DeploymentRoot $StageName
$ConfigStage = Join-Path $DeploymentRoot (".{0}.config-new" -f $StageName)
if (
    (Split-Path -Parent $StageRoot) -ine $DeploymentRoot -or
    (Split-Path -Leaf $StageRoot) -notlike ".codex-coordinator-deploy-*" -or
    (Split-Path -Parent $ConfigStage) -ine $DeploymentRoot
) {
    throw "Refusing unsafe coordinator staging cleanup."
}
Remove-Item -LiteralPath $ConfigStage -Force -ErrorAction SilentlyContinue
Remove-Item `
    -LiteralPath $StageRoot `
    -Recurse `
    -Force `
    -ErrorAction SilentlyContinue
'@
    return $template.
        Replace(
            "__DEPLOYMENT_ROOT__",
            (ConvertTo-PowerShellLiteral $deploymentRoot)
        ).
        Replace("__STAGE_NAME__", (ConvertTo-PowerShellLiteral $StageName))
}

function Get-RemotePayloadBootstrapScript {
    param(
        [string]$StageName,
        [string]$PayloadHash
    )

    $template = @'
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ([string]$env:COMPUTERNAME -cne "INSPIRON") {
    throw "Coordinator remote host identity is not INSPIRON."
}
$DeploymentRoot = __DEPLOYMENT_ROOT__
$StageName = __STAGE_NAME__
$ExpectedPayloadHash = __PAYLOAD_HASH__
$StageRoot = Join-Path $DeploymentRoot $StageName
$PayloadPath = Join-Path $StageRoot "Invoke-CoordinatorDeployment.ps1"
if (
    (Split-Path -Parent $StageRoot) -ine $DeploymentRoot -or
    (Split-Path -Leaf $StageRoot) -notlike ".codex-coordinator-deploy-*" -or
    (Split-Path -Parent $PayloadPath) -ine $StageRoot
) {
    throw "Coordinator deployment payload path is outside exact staging."
}
$payloadBytes = [IO.File]::ReadAllBytes($PayloadPath)
$sha256 = [Security.Cryptography.SHA256]::Create()
try {
    $payloadHash = [BitConverter]::ToString(
        $sha256.ComputeHash($payloadBytes)
    ).Replace("-", "")
} finally {
    $sha256.Dispose()
}
if ($payloadHash -ne $ExpectedPayloadHash) {
    throw "Coordinator deployment payload failed SHA-256 verification."
}
$payloadText = [Text.UTF8Encoding]::new($false, $true).GetString($payloadBytes)
$payload = [ScriptBlock]::Create($payloadText)
& $payload
'@
    return $template.
        Replace(
            "__DEPLOYMENT_ROOT__",
            (ConvertTo-PowerShellLiteral $deploymentRoot)
        ).
        Replace("__STAGE_NAME__", (ConvertTo-PowerShellLiteral $StageName)).
        Replace("__PAYLOAD_HASH__", (ConvertTo-PowerShellLiteral $PayloadHash))
}

if ([string]::IsNullOrWhiteSpace($ReleaseRoot)) {
    $ReleaseRoot = Join-Path $PSScriptRoot "..\dist\lan-assist"
}
$releasePath = (Resolve-Path -LiteralPath $ReleaseRoot -ErrorAction Stop).Path
$candidateConfig = (
    Resolve-Path -LiteralPath $CoordinatorConfigPath -ErrorAction Stop
).Path
$releaseExecutable = Join-Path $releasePath "VideoTranscoderLanAssist.exe"
$releaseLauncher = Join-Path $releasePath "Start-Coordinator.ps1"
$releaseManifest = Join-Path $releasePath "build-manifest.json"
foreach ($releaseFile in @(
    $releaseExecutable,
    $releaseLauncher,
    $releaseManifest,
    $candidateConfig
)) {
    if (-not (Test-Path -LiteralPath $releaseFile -PathType Leaf)) {
        throw "A required coordinator release input is missing."
    }
}
[void](Assert-ReleaseManifest `
    $releaseManifest `
    $releaseExecutable `
    $releaseLauncher)
$candidateConfigRecord = Get-Content -LiteralPath $candidateConfig -Raw |
    ConvertFrom-Json -ErrorAction Stop
if (
    $candidateConfigRecord.schema_version -ne 1 -or
    $candidateConfigRecord.mode -ne "coordinator" -or
    [int]$candidateConfigRecord.api_port -ne 41800 -or
    [int]$candidateConfigRecord.dashboard_port -ne 41802
) {
    throw "Candidate coordinator configuration identity is invalid."
}
if (-not [IO.Path]::IsPathRooted($ExpectedWorkRoot)) {
    throw "ExpectedWorkRoot must be a fully qualified INSPIRON path."
}

$script:sshExecutable = (Get-Command ssh.exe -ErrorAction Stop).Source
$scpExecutable = (Get-Command scp.exe -ErrorAction Stop).Source
$stageName = ".codex-coordinator-deploy-{0}" -f (
    [Guid]::NewGuid().ToString("N")
)
$remoteStageRoot = Join-Path $deploymentRoot $stageName
$remoteStageTransferRoot = $remoteStageRoot.Replace("\", "/")
$deploymentCommandStarted = $false
$remoteStageCreated = $false
$releaseExecutableHash = Get-Sha256 $releaseExecutable
$candidateConfigHash = Get-Sha256 $candidateConfig
$releaseLauncherHash = Get-Sha256 $releaseLauncher
$releaseManifestHash = Get-Sha256 $releaseManifest
$deploymentPayloadText = Get-RemoteDeploymentScript `
    $stageName `
    $ExpectedWorkRoot `
    $releaseExecutableHash `
    $candidateConfigHash `
    $releaseLauncherHash `
    $releaseManifestHash
$localPayloadPath = Join-Path ([IO.Path]::GetTempPath()) (
    "video-transcoder-coordinator-deployment-{0}.ps1" -f (
        [Guid]::NewGuid().ToString("N")
    )
)
$deploymentPayloadHash = $null

try {
    [IO.File]::WriteAllText(
        $localPayloadPath,
        $deploymentPayloadText,
        [Text.UTF8Encoding]::new($false)
    )
    $deploymentPayloadHash = Get-Sha256 $localPayloadPath
    $preflightOutput = Invoke-RemotePowerShell (
        Get-RemotePreflightScript $stageName $ExpectedWorkRoot
    )
    $remoteStageCreated = $true
    $preflight = ConvertFrom-RemoteJsonResult $preflightOutput
    if (
        $preflight.Event -ne "CoordinatorDeploymentStagingReady" -or
        $preflight.Status -ne "Ready" -or
        $preflight.TaskState -ne "Disabled" -or
        $preflight.RuntimeProcessCount -ne 0 -or
        $preflight.ListenerCount -ne 0 -or
        $preflight.JournalPresent
    ) {
        throw "Remote coordinator deployment preflight evidence is invalid."
    }
    $uploads = @(
        @{ Source = $releaseExecutable; Name = "VideoTranscoderLanAssist.exe" },
        @{ Source = $candidateConfig; Name = "VideoTranscoderLanAssist.json" },
        @{ Source = $releaseLauncher; Name = "Start-Coordinator.ps1" },
        @{ Source = $releaseManifest; Name = "build-manifest.json" },
        @{
            Source = $localPayloadPath
            Name = "Invoke-CoordinatorDeployment.ps1"
        }
    )
    foreach ($upload in $uploads) {
        $remotePath = "{0}/{1}" -f $remoteStageTransferRoot, $upload.Name
        $scpArguments = New-ScpArgumentList `
            -Source $upload.Source `
            -RemotePath $remotePath `
            -Destination $sshDestination
        [void](Invoke-CheckedNativeCommand `
            -Executable $scpExecutable `
            -Arguments $scpArguments `
            -FailureMessage "Coordinator release staging upload failed")
    }

    $deploymentCommandStarted = $true
    $deploymentOutput = Invoke-RemotePowerShell (
        Get-RemotePayloadBootstrapScript `
            $stageName `
            $deploymentPayloadHash
    )
    $result = ConvertFrom-RemoteJsonResult $deploymentOutput
    if (
        $result.Event -ne "LanCoordinatorSafelyDeployed" -or
        $result.Status -ne "Ready" -or
        $result.TaskState -ne "Ready" -or
        -not $result.TokenPreserved -or
        $result.JournalPresent -or
        $result.RuntimeProcessCount -ne 0 -or
        $result.ListenerCount -ne 0 -or
        -not $result.StartRequired
    ) {
        throw "Remote coordinator deployment evidence is invalid."
    }
    $result | ConvertTo-Json -Compress
} catch {
    if ($remoteStageCreated -and -not $deploymentCommandStarted) {
        try {
            [void](Invoke-RemotePowerShell (Get-RemoteCleanupScript $stageName))
        } catch {
            # Cleanup is best-effort only before deployment begins.  Once the
            # deployment command begins, its own guarded finally block owns
            # cleanup so a lost SSH session cannot race atomic replacement.
        }
    }
    throw
} finally {
    Remove-Item `
        -LiteralPath $localPayloadPath `
        -Force `
        -ErrorAction SilentlyContinue
}
