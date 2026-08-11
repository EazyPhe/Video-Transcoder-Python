[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$scriptsRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$deployment = Join-Path $scriptsRoot "Deploy-LanCoordinatorSafely.ps1"

$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $deployment,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) {
    $message = @($errors | ForEach-Object { $_.Message }) -join " | "
    throw "PowerShell parser errors were found in coordinator deployer: $message"
}

$deploymentText = Get-Content -LiteralPath $deployment -Raw
foreach ($required in @(
    'sshDestination = "codex-remote"',
    'taskName = "VideoTranscoder LAN Coordinator"',
    'taskPath = "\"',
    'D:\Development\VideoTranscoderToolchain\LAN Assist\coordinator',
    'Start-Coordinator.ps1',
    'ExpectedWorkRoot',
    'active-transaction.json',
    'VideoTranscoderLanAssist.exe',
    'ffmpeg.exe',
    'ffprobe.exe',
    'Get-NetTCPConnection -State Listen',
    '$RequiredPorts = @(41800, 41802)',
    'Export-ScheduledTask',
    'Disable-ScheduledTask',
    'Register-ScheduledTask',
    'Enable-ScheduledTask',
    'Get-DisabledTaskXml',
    'StartWhenAvailable',
    'AllowStartOnDemand',
    'MultipleInstancesPolicy',
    'ExecutionTimeLimit',
    'PT0S',
    'Coordinator task must be disabled before deployment staging.',
    'Coordinator remote host identity is not INSPIRON.',
    'task-original.xml',
    'token-sha256.txt',
    'Invoke-CoordinatorDeployment.ps1',
    '[IO.File]::ReadAllBytes($PayloadPath)',
    'Coordinator deployment payload failed SHA-256 verification',
    'deployment-backups',
    '[IO.File]::Replace',
    '--validate-config',
    'rollback was incomplete',
    'StartRequired = $true'
)) {
    if ($deploymentText -notmatch [regex]::Escape($required)) {
        throw "Coordinator deployer is missing required guard: $required"
    }
}
$hostGuard = 'if ([string]$env:COMPUTERNAME -cne "INSPIRON")'
if (
    [regex]::Matches(
        $deploymentText,
        [regex]::Escape($hostGuard)
    ).Count -ne 4
) {
    throw "Every remote coordinator script must pin the INSPIRON host."
}

foreach ($requiredSshGuard in @(
    "-n",
    "BatchMode=yes",
    "PreferredAuthentications=publickey",
    "PasswordAuthentication=no",
    "KbdInteractiveAuthentication=no",
    "NumberOfPasswordPrompts=0",
    "StrictHostKeyChecking=yes",
    "ConnectionAttempts=1",
    "ConnectTimeout=10"
)) {
    if ($deploymentText -notmatch [regex]::Escape($requiredSshGuard)) {
        throw "Coordinator deployer is missing SSH guard $requiredSshGuard."
    }
}

foreach ($forbidden in @(
    "Start-ScheduledTask",
    "Stop-ScheduledTask",
    "Stop-Process",
    "taskkill",
    "net use",
    "Get-Credential"
)) {
    if ($deploymentText -match [regex]::Escape($forbidden)) {
        throw "Coordinator deployer contains forbidden live action $forbidden."
    }
}

$firstArtifactInstall = [regex]::Match(
    $deploymentText,
    'Install-AtomicVerifiedFile\s+`\s*\r?\n\s+\$stageExecutable'
).Index
$activityChecks = @(
    [regex]::Matches(
        $deploymentText,
        'Assert-NoCoordinatorActivity \$identity\.WorkRoot'
    ) | ForEach-Object { $_.Index }
)
if (
    $firstArtifactInstall -le 0 -or
    @($activityChecks | Where-Object { $_ -lt $firstArtifactInstall }).Count -lt 2
) {
    throw "Coordinator activity must be rechecked before atomic replacement."
}
$manifestInstall = [regex]::Match(
    $deploymentText,
    'Install-AtomicVerifiedFile\s+`\s*\r?\n\s+\$stageManifest'
).Index
$installedValidation = $deploymentText.IndexOf(
    '$installedValidation.Event',
    [StringComparison]::Ordinal
)
if ($manifestInstall -le $installedValidation -or $installedValidation -le 0) {
    throw "Coordinator manifest must be installed after binary/config validation."
}

function Get-FunctionDefinitionText {
    param([string]$Name)

    $definition = $ast.Find(
        {
            param($node)
            return (
                $node -is
                    [Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq $Name
            )
        },
        $true
    )
    if ($null -eq $definition) {
        throw "Required coordinator deployer function $Name was not found."
    }
    return $definition.Extent.Text
}

function Get-InputFunctionDefinitionText {
    param(
        [string]$ScriptText,
        [string]$Name
    )

    $tokens = $null
    $errors = $null
    $inputAst = [Management.Automation.Language.Parser]::ParseInput(
        $ScriptText,
        [ref]$tokens,
        [ref]$errors
    )
    if ($errors.Count -ne 0) {
        throw "Generated coordinator script could not be parsed."
    }
    $definition = $inputAst.Find(
        {
            param($node)
            return (
                $node -is
                    [Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq $Name
            )
        },
        $true
    )
    if ($null -eq $definition) {
        throw "Generated coordinator function $Name was not found."
    }
    return $definition.Extent.Text
}

foreach ($functionName in @(
    "Get-Sha256",
    "ConvertTo-EncodedPowerShell",
    "ConvertTo-PowerShellLiteral",
    "New-SshArgumentList",
    "New-SshStdinArgumentList",
    "New-ScpArgumentList",
    "ConvertFrom-RemoteJsonResult",
    "Assert-ReleaseManifest",
    "Get-RemotePreflightScript",
    "Get-RemoteDeploymentScript",
    "Get-RemoteCleanupScript",
    "Get-RemotePayloadBootstrapScript"
)) {
    Invoke-Expression (Get-FunctionDefinitionText $functionName)
}

$encodedFixture = ConvertTo-EncodedPowerShell 'Write-Output "safe fixture"'
$decodedFixture = [Text.Encoding]::Unicode.GetString(
    [Convert]::FromBase64String($encodedFixture)
)
if ($decodedFixture -ne 'Write-Output "safe fixture"') {
    throw "Encoded remote PowerShell did not round-trip exactly."
}
if ((ConvertTo-PowerShellLiteral "a'b") -ne "'a''b'") {
    throw "Remote PowerShell literal escaping is unsafe."
}

$sshArguments = @(
    New-SshArgumentList -EncodedCommand "fixture" -Destination "codex-remote"
)
$stdinArguments = @(
    New-SshStdinArgumentList -Destination "codex-remote"
)
$scpArguments = @(
    New-ScpArgumentList `
        -Source "C:\safe fixture\file.bin" `
        -RemotePath "D:/safe fixture/file.bin" `
        -Destination "codex-remote"
)
if (
    $sshArguments[0] -ne "-n" -or
    $sshArguments -notcontains "-T" -or
    $sshArguments -notcontains "BatchMode=yes" -or
    $sshArguments -notcontains "PreferredAuthentications=publickey" -or
    $sshArguments -notcontains "PasswordAuthentication=no" -or
    $sshArguments -notcontains "KbdInteractiveAuthentication=no" -or
    $sshArguments -notcontains "NumberOfPasswordPrompts=0" -or
    $sshArguments -notcontains "StrictHostKeyChecking=yes" -or
    $sshArguments -notcontains "codex-remote" -or
    $sshArguments -notcontains "-EncodedCommand"
) {
    throw "SSH argument construction is not noninteractive key-only."
}
if (
    $stdinArguments -contains "-n" -or
    $stdinArguments -notcontains "-T" -or
    $stdinArguments -notcontains "BatchMode=yes" -or
    $stdinArguments -notcontains "PreferredAuthentications=publickey" -or
    $stdinArguments -notcontains "PasswordAuthentication=no" -or
    $stdinArguments -notcontains "StrictHostKeyChecking=yes" -or
    $stdinArguments -notcontains "codex-remote" -or
    $stdinArguments[-2] -ne "-Command" -or
    $stdinArguments[-1] -ne "-"
) {
    throw "SSH stdin transport is not noninteractive key-only."
}
if (
    $scpArguments[0] -ne "-B" -or
    $scpArguments -notcontains "BatchMode=yes" -or
    $scpArguments -notcontains "PasswordAuthentication=no" -or
    $scpArguments -notcontains "StrictHostKeyChecking=yes" -or
    $scpArguments[-1] -ne "codex-remote:D:/safe fixture/file.bin"
) {
    throw "SCP argument construction is not noninteractive key-only."
}

$remoteResult = ConvertFrom-RemoteJsonResult @(
    '{"Event":"FixtureReady","Status":"Ready"}',
    '#< CLIXML progress noise'
)
if (
    $remoteResult.Event -ne "FixtureReady" -or
    $remoteResult.Status -ne "Ready"
) {
    throw "Remote JSON evidence selection did not ignore progress noise."
}

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
$fixtureStageName = ".codex-coordinator-deploy-" + ("a" * 32)
$fixtureWorkRoot = (
    "D:\Development\VideoTranscoderToolchain\jobs\fixture-work"
)
$remoteScripts = [Collections.Generic.List[string]]::new()
$remoteScripts.Add(
    (Get-RemotePreflightScript $fixtureStageName $fixtureWorkRoot)
)
$remoteScripts.Add((Get-RemoteDeploymentScript `
    $fixtureStageName `
    $fixtureWorkRoot `
    ("1" * 64) `
    ("2" * 64) `
    ("3" * 64) `
    ("4" * 64)))
$remoteScripts.Add((Get-RemoteCleanupScript $fixtureStageName))
$remoteScripts.Add((Get-RemotePayloadBootstrapScript `
    $fixtureStageName `
    ("5" * 64)))
if ($remoteScripts.Count -ne 4) {
    throw "Remote coordinator payload generator count is invalid."
}
foreach ($remoteScript in $remoteScripts) {
    $remoteTokens = $null
    $remoteErrors = $null
    [void][Management.Automation.Language.Parser]::ParseInput(
        [string]$remoteScript,
        [ref]$remoteTokens,
        [ref]$remoteErrors
    )
    if ($remoteErrors.Count -ne 0 -or $remoteScript -match '__[A-Z_]+__') {
        throw "Generated remote coordinator deployment payload is invalid."
    }
    $hostGuardIndex = $remoteScript.IndexOf($hostGuard)
    $deploymentRootIndex = $remoteScript.IndexOf('$DeploymentRoot =')
    if (
        $hostGuardIndex -lt 0 -or
        $deploymentRootIndex -le $hostGuardIndex
    ) {
        throw "Remote coordinator host pin must precede path or state access."
    }
}
$remoteDeployment = [string]$remoteScripts[1]
$remotePreflight = [string]$remoteScripts[0]
foreach ($required in @(
    'Get-TaskAccountSid',
    'Set-TaskXmlChildValue',
    'Get-DisabledTaskXml',
    'MSFT_TaskLogonTrigger',
    'RunLevel -ne "Highest"',
    'WorkingDirectory',
    'System32\WindowsPowerShell\v1.0\powershell.exe',
    '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass',
    '"StartWhenAvailable" "false"',
    '"AllowStartOnDemand" "true"',
    '"MultipleInstancesPolicy" "IgnoreNew"',
    '"ExecutionTimeLimit" "PT0S"',
    'Assert-ExactTask $normalizedTask $true',
    'Assert-ExactTask $enabledTask $true',
    'Register-ScheduledTask',
    'Enable-ScheduledTask'
)) {
    if ($remoteDeployment -notmatch [regex]::Escape($required)) {
        throw "Generated coordinator deployment is missing task guard $required."
    }
}
$preflightAssert = Get-InputFunctionDefinitionText `
    $remotePreflight `
    "Assert-ExactTask"
$deploymentAssert = Get-InputFunctionDefinitionText `
    $remoteDeployment `
    "Assert-ExactTask"
if ($preflightAssert -cne $deploymentAssert) {
    throw "Preflight and deployment exact-task contracts diverged."
}
$disableIndex = $remoteDeployment.IndexOf("Disable-ScheduledTask")
$stageValidationIndex = $remoteDeployment.IndexOf('    $validationOutput = @(')
$artifactIndex = $remoteDeployment.IndexOf(
    'Install-AtomicVerifiedFile `',
    $stageValidationIndex
)
$manifestIndex = $remoteDeployment.IndexOf(
    '        $stageManifest `',
    $artifactIndex
)
$registerIndex = $remoteDeployment.IndexOf(
    "Register-ScheduledTask",
    $manifestIndex
)
$enableIndex = $remoteDeployment.IndexOf(
    "Enable-ScheduledTask",
    $registerIndex
)
if (
    $disableIndex -lt 0 -or
    $stageValidationIndex -le $disableIndex -or
    $artifactIndex -le $stageValidationIndex -or
    $manifestIndex -le $artifactIndex -or
    $registerIndex -le $manifestIndex -or
    $enableIndex -le $registerIndex
) {
    throw "Coordinator task did not remain disabled through deployment verification."
}

foreach ($functionName in @(
    "Test-ZeroTaskDuration",
    "Get-TaskAccountSid",
    "Assert-ExactTask",
    "Set-TaskXmlChildValue",
    "Get-DisabledTaskXml"
)) {
    Invoke-Expression (
        Get-InputFunctionDefinitionText $remoteDeployment $functionName
    )
}
if (
    -not (Test-ZeroTaskDuration "PT0S") -or
    (Test-ZeroTaskDuration "PT72H")
) {
    throw "Coordinator zero execution-time-limit check is invalid."
}
$LauncherPath = $deployedLauncherPath
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$otherSid = if ($currentSid -cne "S-1-5-18") {
    "S-1-5-18"
} else {
    "S-1-5-19"
}
$exactArguments = (
    '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
    '-WindowStyle Hidden -File "{0}"' -f $LauncherPath
)
$systemPowerShell = Join-Path `
    $env:SystemRoot `
    "System32\WindowsPowerShell\v1.0\powershell.exe"

function New-TestCoordinatorTask {
    param([ValidateSet("Legacy", "Normalized")][string]$ActionKind)

    $execute = if ($ActionKind -ceq "Legacy") {
        "powershell.exe"
    } else {
        $systemPowerShell
    }
    $workingDirectory = if ($ActionKind -ceq "Legacy") {
        ""
    } else {
        $DeploymentRoot
    }
    return [pscustomobject]@{
        TaskPath = $TaskPath
        Principal = [pscustomobject]@{
            UserId = $currentSid
            LogonType = "Interactive"
            RunLevel = "Highest"
        }
        Triggers = @([pscustomobject]@{
            UserId = $currentSid
            Enabled = $true
            StartBoundary = ""
            CimClass = [pscustomobject]@{
                CimClassName = "MSFT_TaskLogonTrigger"
            }
        })
        Actions = @([pscustomobject]@{
            Execute = $execute
            Arguments = $exactArguments
            WorkingDirectory = $workingDirectory
        })
        Settings = [pscustomobject]@{
            StartWhenAvailable = $false
            AllowDemandStart = $true
            RestartCount = 0
            MultipleInstances = "IgnoreNew"
            ExecutionTimeLimit = "PT0S"
        }
    }
}

function Assert-TestCoordinatorTaskRejected {
    param(
        [object]$Task,
        [bool]$RequireNormalizedSettings,
        [string]$Message
    )

    $rejected = $false
    try {
        Assert-ExactTask $Task $RequireNormalizedSettings
    } catch {
        $rejected = $true
    }
    if (-not $rejected) {
        throw $Message
    }
}

$legacyTask = New-TestCoordinatorTask "Legacy"
$normalizedTaskFixture = New-TestCoordinatorTask "Normalized"
Assert-ExactTask $legacyTask $false
Assert-ExactTask $normalizedTaskFixture $false
Assert-ExactTask $normalizedTaskFixture $true
Assert-TestCoordinatorTaskRejected `
    $legacyTask $true `
    "Normalized task verification accepted the legacy action."

$wrongPathTask = New-TestCoordinatorTask "Normalized"
$wrongPathTask.Actions[0].Execute = Join-Path `
    $env:SystemRoot `
    "System32\powershell.exe"
Assert-TestCoordinatorTaskRejected `
    $wrongPathTask $false `
    "Coordinator task accepted a different full PowerShell path."
$prefixArgumentsTask = New-TestCoordinatorTask "Normalized"
$prefixArgumentsTask.Actions[0].Arguments = "-NoProfile $exactArguments"
Assert-TestCoordinatorTaskRejected `
    $prefixArgumentsTask $false `
    "Coordinator task accepted prefixed launcher arguments."
$normalizedWorkDirTask = New-TestCoordinatorTask "Normalized"
$normalizedWorkDirTask.Actions[0].WorkingDirectory = ""
Assert-TestCoordinatorTaskRejected `
    $normalizedWorkDirTask $false `
    "Coordinator task accepted an empty normalized working directory."
$legacyWorkDirTask = New-TestCoordinatorTask "Legacy"
$legacyWorkDirTask.Actions[0].WorkingDirectory = $DeploymentRoot
Assert-TestCoordinatorTaskRejected `
    $legacyWorkDirTask $false `
    "Coordinator task accepted a nonempty legacy working directory."
$wrongPrincipalTask = New-TestCoordinatorTask "Normalized"
$wrongPrincipalTask.Principal.UserId = $otherSid
Assert-TestCoordinatorTaskRejected `
    $wrongPrincipalTask $false `
    "Coordinator task accepted a different principal SID."
$wrongRunLevelTask = New-TestCoordinatorTask "Normalized"
$wrongRunLevelTask.Principal.RunLevel = "Limited"
Assert-TestCoordinatorTaskRejected `
    $wrongRunLevelTask $false `
    "Coordinator task accepted a non-Highest principal."
$wrongLogonTypeTask = New-TestCoordinatorTask "Normalized"
$wrongLogonTypeTask.Principal.LogonType = "Password"
Assert-TestCoordinatorTaskRejected `
    $wrongLogonTypeTask $false `
    "Coordinator task accepted a noninteractive principal."
$extraTriggerTask = New-TestCoordinatorTask "Normalized"
$extraTriggerTask.Triggers = @(
    $extraTriggerTask.Triggers[0],
    $extraTriggerTask.Triggers[0]
)
Assert-TestCoordinatorTaskRejected `
    $extraTriggerTask $false `
    "Coordinator task accepted multiple triggers."
$wrongTriggerSidTask = New-TestCoordinatorTask "Normalized"
$wrongTriggerSidTask.Triggers[0].UserId = $otherSid
Assert-TestCoordinatorTaskRejected `
    $wrongTriggerSidTask $false `
    "Coordinator task accepted a different trigger SID."
$wrongTriggerClassTask = New-TestCoordinatorTask "Normalized"
$wrongTriggerClassTask.Triggers[0].CimClass.CimClassName = "MSFT_TaskBootTrigger"
Assert-TestCoordinatorTaskRejected `
    $wrongTriggerClassTask $false `
    "Coordinator task accepted a non-logon trigger."
$disabledTriggerTask = New-TestCoordinatorTask "Normalized"
$disabledTriggerTask.Triggers[0].Enabled = $false
Assert-TestCoordinatorTaskRejected `
    $disabledTriggerTask $false `
    "Coordinator task accepted a disabled logon trigger."
$boundedTriggerTask = New-TestCoordinatorTask "Normalized"
$boundedTriggerTask.Triggers[0].StartBoundary = "2026-08-06T00:00:00"
Assert-TestCoordinatorTaskRejected `
    $boundedTriggerTask $false `
    "Coordinator task accepted a bounded logon trigger."
$demandStartTask = New-TestCoordinatorTask "Normalized"
$demandStartTask.Settings.AllowDemandStart = $false
Assert-TestCoordinatorTaskRejected `
    $demandStartTask $true `
    "Coordinator task accepted disabled demand start."
$taskFixtureXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$currentSid</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$currentSid</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy>
    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowStartOnDemand>false</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT72H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>$exactArguments</Arguments>
    </Exec>
  </Actions>
</Task>
"@
$originalDocument = [xml]$taskFixtureXml
$originalNamespace = [Xml.XmlNamespaceManager]::new($originalDocument.NameTable)
$originalNamespace.AddNamespace(
    "task",
    "http://schemas.microsoft.com/windows/2004/02/mit/task"
)
$originalPrincipalXml = $originalDocument.SelectSingleNode(
    "//task:Principals",
    $originalNamespace
).OuterXml
$originalTriggerXml = $originalDocument.SelectSingleNode(
    "//task:Triggers",
    $originalNamespace
).OuterXml
$normalizedXml = Get-DisabledTaskXml $taskFixtureXml $true
$normalizedDocument = [xml]$normalizedXml
$normalizedNamespace = [Xml.XmlNamespaceManager]::new(
    $normalizedDocument.NameTable
)
$normalizedNamespace.AddNamespace(
    "task",
    "http://schemas.microsoft.com/windows/2004/02/mit/task"
)
function Get-NormalizedSetting {
    param([string]$Name)

    return [string]$normalizedDocument.SelectSingleNode(
        "//task:Settings/task:$Name",
        $normalizedNamespace
    ).InnerText
}
$normalizedExec = $normalizedDocument.SelectSingleNode(
    "//task:Actions/task:Exec",
    $normalizedNamespace
)
if (
    (Get-NormalizedSetting "Enabled") -cne "false" -or
    (Get-NormalizedSetting "StartWhenAvailable") -cne "false" -or
    (Get-NormalizedSetting "AllowStartOnDemand") -cne "true" -or
    (Get-NormalizedSetting "MultipleInstancesPolicy") -cne "IgnoreNew" -or
    (Get-NormalizedSetting "ExecutionTimeLimit") -cne "PT0S" -or
    [string]$normalizedExec.Command -cne $systemPowerShell -or
    [string]$normalizedExec.Arguments -cne $exactArguments -or
    [string]$normalizedExec.WorkingDirectory -cne $DeploymentRoot -or
    $normalizedDocument.SelectSingleNode(
        "//task:Principals",
        $normalizedNamespace
    ).OuterXml -cne $originalPrincipalXml -or
    $normalizedDocument.SelectSingleNode(
        "//task:Triggers",
        $normalizedNamespace
    ).OuterXml -cne $originalTriggerXml -or
    $null -ne $normalizedDocument.SelectSingleNode(
        "//task:Settings/task:RestartOnFailure",
        $normalizedNamespace
    )
) {
    throw "Coordinator task XML normalization is incomplete."
}
$rollbackXml = Get-DisabledTaskXml $taskFixtureXml $false
$rollbackDocument = [xml]$rollbackXml
$rollbackNamespace = [Xml.XmlNamespaceManager]::new($rollbackDocument.NameTable)
$rollbackNamespace.AddNamespace(
    "task",
    "http://schemas.microsoft.com/windows/2004/02/mit/task"
)
$rollbackExec = $rollbackDocument.SelectSingleNode(
    "//task:Actions/task:Exec",
    $rollbackNamespace
)
if (
    [string]$rollbackDocument.SelectSingleNode(
        "//task:Settings/task:Enabled",
        $rollbackNamespace
    ).InnerText -cne "false" -or
    [string]$rollbackDocument.SelectSingleNode(
        "//task:Settings/task:StartWhenAvailable",
        $rollbackNamespace
    ).InnerText -cne "true" -or
    [string]$rollbackExec.Command -cne "powershell.exe" -or
    [string]$rollbackExec.Arguments -cne $exactArguments -or
    $null -ne $rollbackExec.SelectSingleNode(
        "task:WorkingDirectory",
        $rollbackNamespace
    ) -or
    $rollbackDocument.SelectSingleNode(
        "//task:Principals",
        $rollbackNamespace
    ).OuterXml -cne $originalPrincipalXml -or
    $rollbackDocument.SelectSingleNode(
        "//task:Triggers",
        $rollbackNamespace
    ).OuterXml -cne $originalTriggerXml -or
    $null -eq $rollbackDocument.SelectSingleNode(
        "//task:Settings/task:RestartOnFailure",
        $rollbackNamespace
    )
) {
    throw "Coordinator rollback task XML did not preserve original settings."
}
$encodedPreflightLength = (ConvertTo-EncodedPowerShell $remoteScripts[0]).Length
$encodedBootstrapLength = (ConvertTo-EncodedPowerShell $remoteScripts[3]).Length
if ($encodedPreflightLength -gt 30000 -or $encodedBootstrapLength -gt 30000) {
    throw "An SSH encoded command exceeds the conservative Windows limit."
}

$testRoot = Join-Path ([IO.Path]::GetTempPath()) (
    "video-transcoder-coordinator-deployer-{0}" -f (
        [Guid]::NewGuid().ToString("N")
    )
)
New-Item -ItemType Directory -Path $testRoot | Out-Null
try {
    $testExecutable = Join-Path $testRoot "VideoTranscoderLanAssist.exe"
    $testLauncher = Join-Path $testRoot "Start-Coordinator.ps1"
    $testManifest = Join-Path $testRoot "build-manifest.json"
    [IO.File]::WriteAllText(
        $testExecutable,
        "offline executable fixture",
        [Text.UTF8Encoding]::new($false)
    )
    [IO.File]::WriteAllText(
        $testLauncher,
        "# offline launcher fixture",
        [Text.UTF8Encoding]::new($false)
    )
    $manifestRecord = [ordered]@{
        schema_version = 1
        artifact = "VideoTranscoderLanAssist.exe"
        sha256 = Get-Sha256 $testExecutable
        coordinator_launcher_sha256 = Get-Sha256 $testLauncher
    }
    [IO.File]::WriteAllText(
        $testManifest,
        ($manifestRecord | ConvertTo-Json),
        [Text.UTF8Encoding]::new($false)
    )
    [void](Assert-ReleaseManifest `
        $testManifest `
        $testExecutable `
        $testLauncher)

    $manifestRecord.sha256 = "0" * 64
    [IO.File]::WriteAllText(
        $testManifest,
        ($manifestRecord | ConvertTo-Json),
        [Text.UTF8Encoding]::new($false)
    )
    $badHashRejected = $false
    try {
        [void](Assert-ReleaseManifest `
            $testManifest `
            $testExecutable `
            $testLauncher)
    } catch {
        $badHashRejected = $true
    }
    if (-not $badHashRejected) {
        throw "Release manifest accepted a mismatched executable hash."
    }
} finally {
    Remove-Item `
        -LiteralPath $testRoot `
        -Recurse `
        -Force `
        -ErrorAction SilentlyContinue
}

# These tests deliberately never dot-source or invoke the deployer.  Native
# SSH/SCP execution is represented only by argument-list fixtures above, so no
# scheduled task, process, port, journal, token, ledger, or work file can be
# affected by the test run.
Write-Host "LAN coordinator deployment script tests passed."
