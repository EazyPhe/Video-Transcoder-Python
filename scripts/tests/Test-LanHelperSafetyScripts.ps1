[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$scriptsRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$launcher = Join-Path $scriptsRoot "Start-LanHelperSafely.ps1"
$connector = Join-Path $scriptsRoot "Connect-LanHelperShare.ps1"
$deployment = Join-Path $scriptsRoot "Deploy-TravelSafeLanHelper.ps1"
$mainWorker = Join-Path $scriptsRoot "Start-HotBoxMainWorker.ps1"

foreach ($path in @($launcher, $connector, $deployment, $mainWorker)) {
    $tokens = $null
    $errors = $null
    [void][Management.Automation.Language.Parser]::ParseFile(
        $path,
        [ref]$tokens,
        [ref]$errors
    )
    if ($errors.Count -ne 0) {
        throw "PowerShell parser errors were found in $path."
    }
}

$launcherText = Get-Content -LiteralPath $launcher -Raw
$connectorText = Get-Content -LiteralPath $connector -Raw
$deploymentText = Get-Content -LiteralPath $deployment -Raw
$mainWorkerText = Get-Content -LiteralPath $mainWorker -Raw
foreach ($required in @(
    "BatchMode=yes",
    "PreferredAuthentications=publickey",
    "PasswordAuthentication=no",
    "KbdInteractiveAuthentication=no",
    "NumberOfPasswordPrompts=0",
    "StrictHostKeyChecking=yes",
    "ConnectionAttempts=1"
)) {
    if ($launcherText -notmatch [regex]::Escape($required)) {
        throw "Safe launcher is missing SSH guard $required."
    }
}
if ($launcherText -match "Get-Credential|New-SmbMapping") {
    throw "Scheduled safe launcher must never prompt or create SMB sessions."
}
if ($mainWorkerText -match '(?m)^\s*-LocalPort\s+\$tunnelLocalPort') {
    throw (
        "HOT-BOX listener discovery must not use an exact-port CIM query; " +
        "Windows reports a normal zero-match result as an error."
    )
}
if ($launcherText -match [regex]::Escape('-Category "TravelSafeStart"')) {
    throw "Safe launcher must not mutate its log between validation and startup."
}
foreach ($required in @(
    "exit 20",
    "ExitCode = 21",
    "ExitCode = 22",
    "ExitCode = 23",
    "Write-HelperEventLog"
)) {
    if ($launcherText -notmatch [regex]::Escape($required)) {
        throw "Safe launcher is missing blocked-state/logging guard $required."
    }
}
if ($launcherText -notmatch [regex]::Escape('ConfigPath = ""')) {
    throw "Safe launcher must support its beside-script deployment config."
}
foreach ($required in @(
    'event_log_file = ".\helper-events.jsonl"',
    'control_file = ".\helper-control.json"',
    'control_status_file = ".\helper-control-status.json"',
    '"VideoTranscoderLanTray.exe"',
    'manifest.tray_sha256',
    "Initialize-HelperControl",
    "Restore-HelperControlState",
    "Get-ValidatedTrayControlSnapshot",
    "Assert-PackagedControlMissing",
    "Assert-HelperTaskSafety",
    "Assert-TrayTaskSafety",
    "Assert-NoHelperActivity",
    "status_readable",
    "status_category",
    "ControlWasValidated",
    "StatusWasValidated",
    'DisallowDemandStart:$false',
    'New-ScheduledTaskSettingsSet `',
    '-Disable `',
    "AtLogOn",
    "RunLevel Limited",
    'MultipleInstances IgnoreNew',
    "Install-VerifiedFile `$configStage `$configPath",
    "rollback was incomplete",
    'task.State -ne "Ready"'
)) {
    if ($deploymentText -notmatch [regex]::Escape($required)) {
        throw "Helper deployer is missing transactional guard $required."
    }
}
foreach ($forbidden in @(
    "Start-ScheduledTask",
    "Stop-ScheduledTask",
    "Stop-Process",
    "taskkill"
)) {
    if ($deploymentText -match [regex]::Escape($forbidden)) {
        throw "Helper deployer contains forbidden task/process action $forbidden."
    }
}
if ($deploymentText -match '(?m)^\s+-AllowDemandStart(?:\s|`|$)') {
    throw "Helper deployer uses an unsupported AllowDemandStart switch."
}
$managedTargetsStart = $deploymentText.IndexOf('$managedTargets = @(')
if ($managedTargetsStart -lt 0) {
    throw "Helper deployer managed-target start was not found."
}
$managedTargetsEnd = $deploymentText.IndexOf(
    '$originallyPresent = @{}',
    $managedTargetsStart
)
if ($managedTargetsEnd -lt 0) {
    throw "Helper deployer managed-target boundary was not found."
}
$managedTargetsText = $deploymentText.Substring(
    $managedTargetsStart,
    $managedTargetsEnd - $managedTargetsStart
)
if ($managedTargetsText -match "helper-control") {
    throw "Helper control state must not be managed or rolled back as an artifact."
}
if (
    $deploymentText.IndexOf("Register-ScheduledTask") -lt
    $deploymentText.IndexOf("Install-VerifiedFile `$configStage `$configPath")
) {
    throw "Helper deployer must register task policy after artifact install."
}
$mainTry = $deploymentText.IndexOf("try {", $managedTargetsEnd)
$firstDisable = $deploymentText.IndexOf("Disable-ScheduledTask", $mainTry)
$configValidation = $deploymentText.IndexOf('    $validationOutput = @(', $mainTry)
$firstInstall = $deploymentText.IndexOf(
    "Install-VerifiedFile `$releaseExe `$deployedExe",
    $mainTry
)
$firstRegister = $deploymentText.IndexOf("Register-ScheduledTask", $mainTry)
$presenceCapture = $deploymentText.IndexOf(
    '$controlWasPresent = Get-UnredirectedFilePresence $controlPath',
    $mainTry
)
$activityBeforePresence = $deploymentText.LastIndexOf(
    'Assert-NoHelperActivity $deployedLauncher $VbsLauncherPath',
    $presenceCapture
)
$postflightSnapshot = $deploymentText.IndexOf(
    '$postflightControlSnapshot = Get-ValidatedTrayControlSnapshot',
    $mainTry
)
$firstEnable = $deploymentText.IndexOf("Enable-ScheduledTask", $mainTry)
if (
    $mainTry -lt 0 -or
    $firstDisable -le $mainTry -or
    $configValidation -le $firstDisable -or
    $activityBeforePresence -le $firstDisable -or
    $presenceCapture -le $activityBeforePresence -or
    $presenceCapture -ge $configValidation -or
    $firstInstall -le $configValidation -or
    $firstRegister -le $firstInstall -or
    $postflightSnapshot -le $firstRegister -or
    $firstEnable -le $postflightSnapshot
) {
    throw "Helper tasks must stay disabled through validation and verification."
}
if ($connectorText -notmatch "Get-Credential") {
    throw "Interactive connector is missing its secure credential prompt."
}
if ($connectorText -notmatch [regex]::Escape("/persistent:no")) {
    throw "Interactive connector must create only a non-persistent session."
}
if (
    $connectorText.IndexOf("Remove-ScopedConnection") -gt
    $connectorText.IndexOf("Get-Credential")
) {
    throw "Interactive connector must clean stale state before prompting."
}
foreach ($required in @(
    "The scoped SMB session is still present after cleanup.",
    "A scoped Windows credential is still present after cleanup.",
    "SessionNotReady"
)) {
    if ($connectorText -notmatch [regex]::Escape($required)) {
        throw "Interactive connector is missing fail-closed cleanup evidence."
    }
}

function Get-FunctionDefinitionText {
    param(
        [string]$Path,
        [string]$Name
    )

    $tokens = $null
    $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile(
        $Path,
        [ref]$tokens,
        [ref]$errors
    )
    $definition = $ast.Find(
        {
            param($node)
            return (
                $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq $Name
            )
        },
        $true
    )
    if ($null -eq $definition) {
        throw "Required function $Name was not found in $Path."
    }
    return $definition.Extent.Text
}

$listenerFunctionText = Get-FunctionDefinitionText `
    $mainWorker `
    "Get-TunnelPortListeners"
& {
    param([string]$Definition)

    $tunnelLocalPort = 41801
    $mockTunnelFailure = $false
    $mockTunnelConnections = @()
    function Get-NetTCPConnection {
        param(
            [string]$State,
            [object]$ErrorAction
        )

        if ($mockTunnelFailure) {
            throw "listener query failed"
        }
        return @($mockTunnelConnections)
    }
    Invoke-Expression $Definition

    if (@(Get-TunnelPortListeners).Count -ne 0) {
        throw "HOT-BOX listener discovery did not accept a clean zero result."
    }
    $mockTunnelConnections = @(
        [pscustomobject]@{ LocalAddress = "0.0.0.0"; LocalPort = 41801 },
        [pscustomobject]@{ LocalAddress = "127.0.0.1"; LocalPort = 41800 },
        [pscustomobject]@{ LocalAddress = "::"; LocalPort = 41801 }
    )
    $matchingListeners = @(Get-TunnelPortListeners)
    if (
        $matchingListeners.Count -ne 2 -or
        @($matchingListeners | Where-Object {
            [int]$_.LocalPort -ne $tunnelLocalPort
        }).Count -ne 0
    ) {
        throw "HOT-BOX listener discovery did not filter the broad query."
    }
    $mockTunnelFailure = $true
    $queryFailureObserved = $false
    try {
        [void](Get-TunnelPortListeners)
    } catch {
        $queryFailureObserved = $true
    }
    if (-not $queryFailureObserved) {
        throw "HOT-BOX listener discovery did not fail closed on query errors."
    }
} $listenerFunctionText

Invoke-Expression (
    Get-FunctionDefinitionText $connector "Get-ShareConnectionRecord"
)
Invoke-Expression (
    Get-FunctionDefinitionText $connector "Get-ScopedCredentialTargets"
)
Invoke-Expression (
    Get-FunctionDefinitionText $launcher "Write-AtomicHelperStatus"
)
Invoke-Expression (
    Get-FunctionDefinitionText $launcher "Write-HelperEventLog"
)
Invoke-Expression (
    Get-FunctionDefinitionText $launcher "Write-HelperStatus"
)
Invoke-Expression (
    Get-FunctionDefinitionText $launcher "Get-StagingProbeFailure"
)
Invoke-Expression (
    Get-FunctionDefinitionText $deployment "Get-Sha256"
)
Invoke-Expression (
    Get-FunctionDefinitionText $deployment "Get-ValidatedHelperControl"
)
Invoke-Expression (
    Get-FunctionDefinitionText $deployment "Initialize-HelperControl"
)
Invoke-Expression (
    Get-FunctionDefinitionText $deployment "Restore-HelperControlState"
)
Invoke-Expression (
    Get-FunctionDefinitionText $deployment "Test-ZeroTaskDuration"
)
Invoke-Expression (
    Get-FunctionDefinitionText $deployment "Get-TaskAccountSid"
)
Invoke-Expression (
    Get-FunctionDefinitionText $deployment "Assert-LegacyHelperVbs"
)
Invoke-Expression (
    Get-FunctionDefinitionText $deployment "Assert-HelperTaskSafety"
)
Invoke-Expression (
    Get-FunctionDefinitionText $deployment "Assert-TrayTaskSafety"
)

$settingsParametersBound = $false
try {
    [void](New-ScheduledTaskSettingsSet `
        -Disable `
        -StartWhenAvailable:$false `
        -DisallowDemandStart:$false `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero))
    $settingsParametersBound = $true
} catch [Management.Automation.ParameterBindingException] {
    throw "Tray scheduled-task settings parameters do not bind."
} catch {
    # A restricted test account can be denied by the ScheduledTasks CIM
    # provider only after PowerShell has successfully bound every parameter.
    $settingsParametersBound = $true
}
if (-not $settingsParametersBound) {
    throw "Tray scheduled-task settings parameter binding was not exercised."
}

function Assert-TestThrows {
    param(
        [scriptblock]$Action,
        [string]$Message
    )

    $rejected = $false
    try {
        & $Action
    } catch {
        $rejected = $true
    }
    if (-not $rejected) {
        throw $Message
    }
}

function New-TestHelperTask {
    param(
        [string]$Sid,
        [string]$DeploymentPath,
        [string]$VbsPath,
        [string]$State = "Disabled",
        [string]$RunLevel = "Limited",
        [string]$LogonType = "Interactive",
        [bool]$StartWhenAvailable = $false,
        [int]$RestartCount = 0,
        [string]$MultipleInstances = "IgnoreNew",
        [object]$ExecutionTimeLimit = "PT0S",
        [string]$ActionKind = "Direct",
        [bool]$AddExtraAction = $false
    )

    $launcherPath = Join-Path $DeploymentPath "Start-LanAssist.ps1"
    $powerShellPath = Join-Path `
        $env:SystemRoot `
        "System32\WindowsPowerShell\v1.0\powershell.exe"
    $action = if ($ActionKind -ceq "Legacy") {
        [pscustomobject]@{
            Execute = Join-Path $env:SystemRoot "System32\wscript.exe"
            Arguments = '"{0}"' -f $VbsPath
            WorkingDirectory = ""
        }
    } elseif ($ActionKind -ceq "Direct") {
        [pscustomobject]@{
            Execute = $powerShellPath
            Arguments = (
                '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
                '-WindowStyle Hidden -File "{0}"' -f $launcherPath
            )
            WorkingDirectory = $DeploymentPath
        }
    } else {
        [pscustomobject]@{
            Execute = "C:\Windows\System32\cmd.exe"
            Arguments = "/c exit 0"
            WorkingDirectory = $DeploymentPath
        }
    }
    $actions = @($action)
    if ($AddExtraAction) {
        $actions += $action
    }
    return [pscustomobject]@{
        TaskPath = "\"
        State = $State
        Principal = [pscustomobject]@{
            UserId = $Sid
            RunLevel = $RunLevel
            LogonType = $LogonType
        }
        Triggers = @()
        Actions = $actions
        Settings = [pscustomobject]@{
            StartWhenAvailable = $StartWhenAvailable
            AllowDemandStart = $true
            DisallowStartIfOnBatteries = $false
            StopIfGoingOnBatteries = $false
            RunOnlyIfNetworkAvailable = $false
            RestartCount = $RestartCount
            MultipleInstances = $MultipleInstances
            ExecutionTimeLimit = $ExecutionTimeLimit
        }
    }
}

function New-TestTrayTask {
    param(
        [string]$Sid,
        [string]$DeploymentPath,
        [string]$TrayPath,
        [string]$ConfigPath,
        [string]$State = "Disabled",
        [string]$TriggerClass = "MSFT_TaskLogonTrigger",
        [string]$TriggerSid = "",
        [bool]$AddExtraTrigger = $false,
        [string]$RunLevel = "Limited",
        [object]$ExecutionTimeLimit = "PT0S"
    )

    if ([string]::IsNullOrWhiteSpace($TriggerSid)) {
        $TriggerSid = $Sid
    }
    $trigger = [pscustomobject]@{
        UserId = $TriggerSid
        CimClass = [pscustomobject]@{ CimClassName = $TriggerClass }
    }
    $triggers = @($trigger)
    if ($AddExtraTrigger) {
        $triggers += $trigger
    }
    return [pscustomobject]@{
        TaskPath = "\"
        State = $State
        Principal = [pscustomobject]@{
            UserId = $Sid
            RunLevel = $RunLevel
            LogonType = "Interactive"
        }
        Triggers = $triggers
        Actions = @([pscustomobject]@{
            Execute = $TrayPath
            Arguments = '--config "{0}"' -f $ConfigPath
            WorkingDirectory = $DeploymentPath
        })
        Settings = [pscustomobject]@{
            StartWhenAvailable = $false
            AllowDemandStart = $true
            DisallowStartIfOnBatteries = $false
            StopIfGoingOnBatteries = $false
            RunOnlyIfNetworkAvailable = $false
            RestartCount = 0
            MultipleInstances = "IgnoreNew"
            ExecutionTimeLimit = $ExecutionTimeLimit
        }
    }
}

$authRecord = [Management.Automation.ErrorRecord]::new(
    [ComponentModel.Win32Exception]::new(5),
    "synthetic-auth",
    [Management.Automation.ErrorCategory]::PermissionDenied,
    $null
)
$networkRecord = [Management.Automation.ErrorRecord]::new(
    [ComponentModel.Win32Exception]::new(53),
    "synthetic-network",
    [Management.Automation.ErrorCategory]::ConnectionError,
    $null
)
$otherRecord = [Management.Automation.ErrorRecord]::new(
    [ComponentModel.Win32Exception]::new(112),
    "synthetic-disk",
    [Management.Automation.ErrorCategory]::ResourceUnavailable,
    $null
)
$authFailure = Get-StagingProbeFailure $authRecord
$networkFailure = Get-StagingProbeFailure $networkRecord
$otherFailure = Get-StagingProbeFailure $otherRecord
if (
    $authFailure.Category -ne "AuthBlocked" -or
    $authFailure.ExitCode -ne 21 -or
    $networkFailure.Category -ne "CoordinatorDisconnected" -or
    $networkFailure.ExitCode -ne 22 -or
    $otherFailure.Category -ne "StagingAccessBlocked" -or
    $otherFailure.ExitCode -ne 23
) {
    throw "Staging probe failures were not classified narrowly."
}

$testShare = "\\INSPIRON\Inspiron"
$okRecord = Get-ShareConnectionRecord $testShare @(
    "OK                     \\INSPIRON\Inspiron    Microsoft Windows Network"
)
if ($null -eq $okRecord -or $okRecord.Status -ne "OK") {
    throw "Exact healthy SMB session was not recognized."
}
$mappedRecord = Get-ShareConnectionRecord $testShare @(
    "OK           Z:        \\INSPIRON\Inspiron    Microsoft Windows Network"
)
if ($null -eq $mappedRecord -or $mappedRecord.Status -ne "OK") {
    throw "Exact healthy mapped SMB session was not recognized."
}
$staleRecord = Get-ShareConnectionRecord $testShare @(
    "Disconnected           \\INSPIRON\Inspiron    Microsoft Windows Network"
)
if ($null -eq $staleRecord -or $staleRecord.Status -eq "OK") {
    throw "Disconnected SMB session was accepted as healthy."
}
$prefixCollision = Get-ShareConnectionRecord $testShare @(
    "OK                     \\INSPIRON\Inspiron-old    Microsoft Windows Network"
)
if ($null -ne $prefixCollision) {
    throw "A different share with a common prefix matched the configured share."
}
$credentialTargets = @(
    Get-ScopedCredentialTargets "INSPIRON" @(
        "Target: Domain:target=INSPIRON",
        "Target: LegacyGeneric:target=INSPIRON-old",
        "Target: TERMSRV/INSPIRON",
        "Target: LegacyGeneric:target=cifs/INSPIRON"
    )
)
if (
    $credentialTargets.Count -ne 2 -or
    $credentialTargets -notcontains "Domain:target=INSPIRON" -or
    $credentialTargets -notcontains "LegacyGeneric:target=cifs/INSPIRON"
) {
    throw "Scoped credential target matching was not exact."
}

$testRoot = Join-Path ([IO.Path]::GetTempPath()) (
    "video-transcoder-helper-safety-{0}" -f [Guid]::NewGuid().ToString("N")
)
New-Item -ItemType Directory -Path $testRoot | Out-Null
try {
    $configPath = Join-Path $testRoot "VideoTranscoderLanAssist.json"
    $statusPath = Join-Path $testRoot "status.json"
    $eventLogPath = Join-Path $testRoot "helper-events.jsonl"
    $controlPath = Join-Path $testRoot "helper-control.json"
    $controlStatusPath = Join-Path $testRoot "helper-control-status.json"
    $controlId = "b37609d869b84f8b8e05744ee428adb2"
    $fakeExecutable = Join-Path $testRoot "fake-lan-assist.cmd"
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $otherSid = if ($currentSid -cne "S-1-5-18") {
        "S-1-5-18"
    } else {
        "S-1-5-19"
    }
    $legacyVbsPath = Join-Path $testRoot "VideoTranscoder-LAN-Helper.vbs"
    $launcherPath = Join-Path $testRoot "Start-LanAssist.ps1"
    $powerShellPath = Join-Path `
        $env:SystemRoot `
        "System32\WindowsPowerShell\v1.0\powershell.exe"
    $legacyVbsContent = @"
Option Explicit

Dim shell, command, exitCode
Set shell = CreateObject("WScript.Shell")

command = Quote("$powerShellPath") & _
    " -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File " & _
    Quote("$launcherPath")

exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode

Function Quote(value)
    Quote = Chr(34) & value & Chr(34)
End Function
"@
    [IO.File]::WriteAllText(
        $legacyVbsPath,
        $legacyVbsContent,
        [Text.UTF8Encoding]::new($false)
    )

    if (-not (Test-ZeroTaskDuration "PT0S")) {
        throw "Zero scheduled-task duration was not accepted."
    }
    if (Test-ZeroTaskDuration "PT72H") {
        throw "Nonzero scheduled-task duration was accepted."
    }
    $directTask = New-TestHelperTask `
        $currentSid $testRoot $legacyVbsPath
    $directKind = Assert-HelperTaskSafety `
        $directTask $currentSid $testRoot $legacyVbsPath $false
    if ($directKind -cne "Direct") {
        throw "Exact direct helper task was not accepted."
    }
    $legacyTask = New-TestHelperTask `
        $currentSid $testRoot $legacyVbsPath `
        -ActionKind "Legacy"
    $legacyKind = Assert-HelperTaskSafety `
        $legacyTask $currentSid $testRoot $legacyVbsPath $true
    if ($legacyKind -cne "LegacyVbs") {
        throw "Exact legacy helper wrapper was not accepted for migration."
    }
    $caseVariantContent = $legacyVbsContent.Replace(
        $powerShellPath,
        $powerShellPath.ToLowerInvariant()
    )
    [IO.File]::WriteAllText(
        $legacyVbsPath,
        $caseVariantContent,
        [Text.UTF8Encoding]::new($false)
    )
    $caseVariantKind = Assert-HelperTaskSafety `
        $legacyTask $currentSid $testRoot $legacyVbsPath $true
    if ($caseVariantKind -cne "LegacyVbs") {
        throw "Case-only Windows path variation was not accepted."
    }
    $validationGuardContent = $legacyVbsContent.Replace(
        "Option Explicit`r`n`r`nDim shell, command, exitCode",
        "Option Explicit`r`n`r`n" +
            'If WScript.Arguments.Named.Exists("validate") Then WScript.Quit 0' +
            "`r`n`r`nDim shell, command, exitCode"
    ).Replace(
        "Option Explicit`n`nDim shell, command, exitCode",
        "Option Explicit`n`n" +
            'If WScript.Arguments.Named.Exists("validate") Then WScript.Quit 0' +
            "`n`nDim shell, command, exitCode"
    )
    [IO.File]::WriteAllText(
        $legacyVbsPath,
        $validationGuardContent,
        [Text.UTF8Encoding]::new($false)
    )
    $validationGuardKind = Assert-HelperTaskSafety `
        $legacyTask $currentSid $testRoot $legacyVbsPath $true
    if ($validationGuardKind -cne "LegacyVbs") {
        throw "Exact validation-guard legacy wrapper was not accepted."
    }
    [IO.File]::WriteAllText(
        $legacyVbsPath,
        $legacyVbsContent,
        [Text.UTF8Encoding]::new($false)
    )
    Assert-TestThrows {
        $wrongSidTask = New-TestHelperTask `
            $otherSid $testRoot $legacyVbsPath
        [void](Assert-HelperTaskSafety `
            $wrongSidTask $currentSid $testRoot $legacyVbsPath $false)
    } "Helper task accepted a different user SID."
    Assert-TestThrows {
        $extraActionTask = New-TestHelperTask `
            $currentSid $testRoot $legacyVbsPath `
            -AddExtraAction $true
        [void](Assert-HelperTaskSafety `
            $extraActionTask $currentSid $testRoot $legacyVbsPath $false)
    } "Helper task accepted multiple actions."
    Assert-TestThrows {
        $restartTask = New-TestHelperTask `
            $currentSid $testRoot $legacyVbsPath `
            -RestartCount 1
        [void](Assert-HelperTaskSafety `
            $restartTask $currentSid $testRoot $legacyVbsPath $false)
    } "Helper task accepted restart-on-failure."
    Assert-TestThrows {
        $boundedTask = New-TestHelperTask `
            $currentSid $testRoot $legacyVbsPath `
            -ExecutionTimeLimit "PT72H"
        [void](Assert-HelperTaskSafety `
            $boundedTask $currentSid $testRoot $legacyVbsPath $false)
    } "Helper task accepted a nonzero execution time limit."
    $batteryUnsafeTask = New-TestHelperTask `
        $currentSid $testRoot $legacyVbsPath
    $batteryUnsafeTask.Settings.DisallowStartIfOnBatteries = $true
    Assert-TestThrows {
        [void](Assert-HelperTaskSafety `
            $batteryUnsafeTask $currentSid $testRoot $legacyVbsPath $false)
    } "Helper task accepted battery-blocked demand start."
    [IO.File]::AppendAllText($legacyVbsPath, "`r`n' changed")
    Assert-TestThrows {
        [void](Assert-HelperTaskSafety `
            $legacyTask $currentSid $testRoot $legacyVbsPath $true)
    } "Helper task accepted a modified legacy wrapper."
    [IO.File]::WriteAllText(
        $legacyVbsPath,
        $legacyVbsContent,
        [Text.UTF8Encoding]::new($false)
    )

    $trayPath = Join-Path $testRoot "VideoTranscoderLanTray.exe"
    $trayTask = New-TestTrayTask `
        $currentSid $testRoot $trayPath $configPath
    Assert-TrayTaskSafety `
        $trayTask $currentSid $testRoot $trayPath $configPath
    Assert-TestThrows {
        $wrongTriggerTask = New-TestTrayTask `
            $currentSid $testRoot $trayPath $configPath `
            -TriggerSid $otherSid
        Assert-TrayTaskSafety `
            $wrongTriggerTask $currentSid $testRoot $trayPath $configPath
    } "Tray task accepted a different logon-trigger SID."
    Assert-TestThrows {
        $extraTriggerTask = New-TestTrayTask `
            $currentSid $testRoot $trayPath $configPath `
            -AddExtraTrigger $true
        Assert-TrayTaskSafety `
            $extraTriggerTask $currentSid $testRoot $trayPath $configPath
    } "Tray task accepted multiple triggers."
    $noDemandTrayTask = New-TestTrayTask `
        $currentSid $testRoot $trayPath $configPath
    $noDemandTrayTask.Settings.AllowDemandStart = $false
    Assert-TestThrows {
        Assert-TrayTaskSafety `
            $noDemandTrayTask $currentSid $testRoot $trayPath $configPath
    } "Tray task accepted disabled demand start."

    @{
        schema_version = 1
        mode = "helper"
        token_file = ".\lan-token.txt"
        status_file = ".\status.json"
        event_log_file = ".\helper-events.jsonl"
        event_log_max_bytes = 4096
        event_log_backup_count = 2
        control_file = ".\helper-control.json"
        control_status_file = ".\helper-control-status.json"
        control_id = $controlId
        ssh_destination = "unreachable-test-host"
        staging_root = "\\unreachable-test-host\Share\staging"
        cache_root = ".\cache"
        fallback_root = ".\fallback"
        worker_id = "test-helper"
    } | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding UTF8
    $initialControl = Initialize-HelperControl $controlPath $controlId
    $initialControlHash = (
        Get-FileHash -Algorithm SHA256 -LiteralPath $controlPath
    ).Hash
    $secondControl = Initialize-HelperControl $controlPath $controlId
    if (
        $initialControl.revision -ne 1 -or
        $initialControl.pc_in_use -ne $false -or
        $secondControl.control_id -cne $controlId -or
        (
            Get-FileHash -Algorithm SHA256 -LiteralPath $controlPath
        ).Hash -ne $initialControlHash
    ) {
        throw "Helper control initialization was not stable and idempotent."
    }
    $badControlPath = Join-Path $testRoot "bad-control.json"
    $badControlPayload = [ordered]@{
        schema_version = 1
        control_id = $controlId.ToUpperInvariant()
        revision = 1
        pc_in_use = $false
    } | ConvertTo-Json -Compress
    [IO.File]::WriteAllText(
        $badControlPath,
        $badControlPayload,
        [Text.UTF8Encoding]::new($false)
    )
    $invalidControlRejected = $false
    try {
        [void](Get-ValidatedHelperControl $badControlPath $controlId)
    } catch {
        $invalidControlRejected = $true
    }
    if (-not $invalidControlRejected) {
        throw "Helper control validation accepted a mismatched identity."
    }
    [IO.File]::WriteAllText(
        $controlStatusPath,
        "deployment-created-status",
        [Text.UTF8Encoding]::new($false)
    )
    Restore-HelperControlState `
        -ControlPath $controlPath `
        -ControlWasPresent $true `
        -ControlWasValidated $true `
        -ControlHashBefore $initialControlHash `
        -StatusPath $controlStatusPath `
        -StatusWasPresent $false `
        -StatusWasValidated $false `
        -StatusHashBefore ""
    if (
        -not (Test-Path -LiteralPath $controlPath -PathType Leaf) -or
        (Get-Sha256 $controlPath) -ne $initialControlHash -or
        (Test-Path -LiteralPath $controlStatusPath)
    ) {
        throw "Rollback did not preserve initially-present control state."
    }
    $newControlPath = Join-Path $testRoot "rollback-new-control.json"
    $newStatusPath = Join-Path $testRoot "rollback-new-status.json"
    [void](Initialize-HelperControl $newControlPath $controlId)
    [IO.File]::WriteAllText(
        $newStatusPath,
        "deployment-created-status",
        [Text.UTF8Encoding]::new($false)
    )
    Restore-HelperControlState `
        -ControlPath $newControlPath `
        -ControlWasPresent $false `
        -ControlWasValidated $false `
        -ControlHashBefore "" `
        -StatusPath $newStatusPath `
        -StatusWasPresent $false `
        -StatusWasValidated $false `
        -StatusHashBefore ""
    if (
        (Test-Path -LiteralPath $newControlPath) -or
        (Test-Path -LiteralPath $newStatusPath)
    ) {
        throw "Rollback did not restore initially-absent control state."
    }
    @'
@echo off
echo {"Event":"ConfigValidated","Status":"Ready","Mode":"helper"}
exit /b 0
'@ | Set-Content -LiteralPath $fakeExecutable -Encoding ASCII

    $launcherOutput = & $launcher `
        -ConfigPath $configPath `
        -ExecutablePath $fakeExecutable `
        -ValidateOnly | ConvertFrom-Json
    if (
        $launcherOutput.Event -ne "TravelSafeLauncherValidated" -or
        $launcherOutput.Status -ne "Ready"
    ) {
        throw "Safe launcher ValidateOnly evidence is invalid."
    }

    $connectorOutput = & $connector `
        -ConfigPath $configPath `
        -ValidateOnly | ConvertFrom-Json
    if (
        $connectorOutput.Event -ne "TravelSafeConnectorValidated" -or
        $connectorOutput.Status -ne "Ready"
    ) {
        throw "Safe connector ValidateOnly evidence is invalid."
    }
    if (Test-Path -LiteralPath $statusPath) {
        throw "ValidateOnly unexpectedly touched helper status."
    }
    if (Test-Path -LiteralPath $eventLogPath) {
        throw "ValidateOnly unexpectedly touched helper event log."
    }
    if (Test-Path -LiteralPath $controlStatusPath) {
        throw "ValidateOnly unexpectedly touched helper control status."
    }

    for ($index = 0; $index -lt 8; $index++) {
        Write-HelperStatus `
            -Path $statusPath `
            -Kind "Blocked" `
            -Category "StagingAccessBlocked" `
            -EventLogPath $eventLogPath `
            -EventLogMaxBytes 420 `
            -EventLogBackupCount 2
    }
    $logFiles = @(
        Get-ChildItem `
            -LiteralPath $testRoot `
            -Filter "helper-events.jsonl*" `
            -File
    )
    $expectedLogNames = @(
        "helper-events.jsonl",
        "helper-events.jsonl.1",
        "helper-events.jsonl.2"
    )
    if (
        $logFiles.Count -lt 1 -or
        @($logFiles | Where-Object {
            $_.Name -notin $expectedLogNames
        }).Count -ne 0
    ) {
        throw "Helper event log rotation touched an unexpected path."
    }
    foreach ($logFile in $logFiles) {
        foreach ($line in Get-Content -LiteralPath $logFile.FullName) {
            $record = $line | ConvertFrom-Json -ErrorAction Stop
            if (
                $record.Event -ne "HelperStatus" -or
                $record.Category -ne "StagingAccessBlocked" -or
                $record.PSObject.Properties.Name -contains "Path"
            ) {
                throw "Helper event log contained unsafe or invalid content."
            }
        }
    }

    $badLogPath = Join-Path $testRoot "bad-log-target"
    New-Item -ItemType Directory -Path $badLogPath | Out-Null
    $logFailureObserved = $false
    try {
        Write-HelperStatus `
            -Path $statusPath `
            -Kind "Starting" `
            -Category "TravelSafeStart" `
            -EventLogPath $badLogPath `
            -EventLogMaxBytes 4096 `
            -EventLogBackupCount 2
    } catch {
        $logFailureObserved = $true
    }
    $failureStatus = Get-Content -LiteralPath $statusPath -Raw |
        ConvertFrom-Json
    if (
        -not $logFailureObserved -or
        $failureStatus.Category -ne "HelperEventLogWriteFailed"
    ) {
        throw "Event-log failure was not reflected in latest helper status."
    }
} finally {
    Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "LAN helper safety script tests passed."
