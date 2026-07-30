$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
Set-StrictMode -Version Latest

$repositoryRoot = [IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot "..\.."))
$handoffScript = Join-Path `
    $repositoryRoot `
    "scripts\remote_batch\Invoke-LegacyHandoff.ps1"
. $handoffScript

$hostPath = (Get-Process -Id $PID -ErrorAction Stop).Path
$temporaryBase = [IO.Path]::GetFullPath(
    [IO.Path]::GetTempPath()).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar)
$testRoot = Join-Path `
    $temporaryBase `
    ("legacy-handoff-tests-" + [Guid]::NewGuid().ToString("N"))
[IO.Directory]::CreateDirectory($testRoot) | Out-Null

function Write-Utf8File {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Text
    )

    [IO.File]::WriteAllText(
        $Path,
        $Text,
        [Text.UTF8Encoding]::new($false))
}

function Start-SyntheticRunner {
    param(
        [Parameter(Mandatory = $true)][string]$RunnerPath,
        [Parameter(Mandatory = $true)][hashtable]$Argument
    )

    $arguments = New-Object Collections.Generic.List[string]
    foreach ($fixed in @(
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            $RunnerPath,
            "-Mode",
            "Batch")) {
        $arguments.Add($fixed)
    }
    foreach ($key in $Argument.Keys) {
        $arguments.Add("-$key")
        $arguments.Add([string]$Argument[$key])
    }
    return Start-Process `
        -FilePath $hostPath `
        -ArgumentList $arguments `
        -WindowStyle Hidden `
        -PassThru
}

function Write-State {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Process
    )

    $Process.Refresh()
    [pscustomobject]@{
        ProcessId = [int]$Process.Id
        ProcessStartUtc =
            $Process.StartTime.ToUniversalTime().ToString("o")
    } | ConvertTo-Json -Compress |
        ForEach-Object {
            Write-Utf8File -Path $Path -Text $_
        }
}

function Wait-ForFileState {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][bool]$Present,
        [int]$TimeoutSeconds = 20
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if ([IO.File]::Exists($Path) -eq $Present) {
            return
        }
        Start-Sleep -Milliseconds 25
    }
    while ([DateTime]::UtcNow -lt $deadline)
    throw "Timed out waiting for synthetic file state in case '$script:CurrentCase'."
}

function Stop-TestProcess {
    param($Process)

    if ($null -eq $Process) {
        return
    }
    try {
        $Process.Refresh()
        if (-not $Process.HasExited) {
            $Process.Kill()
            $Process.WaitForExit()
        }
    }
    catch {
    }
    finally {
        $Process.Dispose()
    }
}

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    return (Get-FileHash `
        -LiteralPath $Path `
        -Algorithm SHA256).Hash
}

function Read-SyntheticInteger {
    param([Parameter(Mandatory = $true)][string]$Path)

    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    do {
        try {
            $stream = [IO.FileStream]::new(
                $Path,
                [IO.FileMode]::Open,
                [IO.FileAccess]::Read,
                [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
            try {
                $reader = [IO.StreamReader]::new($stream)
                try {
                    return [int]$reader.ReadToEnd()
                }
                finally {
                    $reader.Dispose()
                }
            }
            finally {
                $stream.Dispose()
            }
        }
        catch {
            Start-Sleep -Milliseconds 20
        }
    }
    while ([DateTime]::UtcNow -lt $deadline)
    throw "Timed out reading a synthetic integer."
}

function Wait-ForSyntheticIntegerAdvance {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][int]$Before,
        [int]$TimeoutSeconds = 5
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        [int]$current = Read-SyntheticInteger -Path $Path
        if ($current -gt $Before) {
            return $current
        }
        Start-Sleep -Milliseconds 50
    }
    while ([DateTime]::UtcNow -lt $deadline)
    throw "A synthetic heartbeat did not advance."
}

$results = [ordered]@{}
$allProcesses = New-Object Collections.Generic.List[object]
try {
    $grammarRunner = Join-Path $testRoot "grammar-runner.ps1"
    Assert-PowerShellFileGrammar `
        -Arguments @(
            $hostPath,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            $grammarRunner,
            "-Mode",
            "Batch"
        ) `
        -RunnerPath $grammarRunner `
        -HostPath $hostPath
    Assert-PowerShellFileGrammar `
        -Arguments @(
            [IO.Path]::GetFileName($hostPath),
            "-NoProfile",
            "-File",
            $grammarRunner,
            "-Mode",
            "Batch"
        ) `
        -RunnerPath $grammarRunner `
        -HostPath $hostPath
    foreach ($forbiddenHostToken in @(
            "-c",
            "-co",
            "-command",
            "-e",
            "-enc",
            "-encodedcommand",
            "-f",
            "-fi")) {
        [bool]$grammarRejected = $false
        try {
            Assert-PowerShellFileGrammar `
                -Arguments @(
                    $hostPath,
                    $forbiddenHostToken,
                    "ignored",
                    "-File",
                    $grammarRunner,
                    "-Mode",
                    "Batch"
                ) `
                -RunnerPath $grammarRunner `
                -HostPath $hostPath
        }
        catch {
            $grammarRejected = $true
        }
        if (-not $grammarRejected) {
            throw "A forbidden PowerShell host abbreviation was accepted."
        }
    }
    if (-not [LegacyHandoffNative]::IsNoMoreFilesError(18) -or
        [LegacyHandoffNative]::IsNoMoreFilesError(5)) {
        throw "Toolhelp end-of-snapshot error classification is unsafe."
    }
    $results.ExactFileGrammar = $true
    $results.ToolhelpErrorsFailClosed = $true

    $script:CurrentCase = "boundary"
    $caseOne = Join-Path $testRoot "boundary"
    [IO.Directory]::CreateDirectory($caseOne) | Out-Null
    $runnerOnePath = Join-Path $caseOne "SyntheticRunner.ps1"
    $stateOne = Join-Path $caseOne "state.json"
    $journalOne = Join-Path $caseOne "active-transaction.json"
    $resourceOne = Join-Path $caseOne "runner.lock"
    $childMarkerOne = Join-Path $caseOne "child.pid"
    $runnerOneCode = @'
param(
    [string]$Mode,
    [string]$Journal,
    [string]$Resource,
    [string]$ChildMarker,
    [string]$HostExecutable
)
$ErrorActionPreference = "Stop"
$lock = [IO.FileStream]::new(
    $Resource,
    [IO.FileMode]::OpenOrCreate,
    [IO.FileAccess]::ReadWrite,
    [IO.FileShare]::None)
try {
    [IO.File]::WriteAllText($Journal, "active")
    Start-Sleep -Milliseconds 5000
    $child = Start-Process `
        -FilePath $HostExecutable `
        -ArgumentList @(
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Start-Sleep -Seconds 60"
        ) `
        -WindowStyle Hidden `
        -PassThru
    [IO.File]::WriteAllText($ChildMarker, [string]$child.Id)
    [IO.File]::Delete($Journal)
    $child.WaitForExit()
    [IO.File]::WriteAllText($Journal, "unsafe-next-transaction")
    Start-Sleep -Seconds 60
}
finally {
    $lock.Dispose()
}
'@
    Write-Utf8File -Path $runnerOnePath -Text $runnerOneCode
    $unrelated = Start-Process `
        -FilePath $hostPath `
        -ArgumentList @(
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Start-Sleep -Seconds 60"
        ) `
        -WindowStyle Hidden `
        -PassThru
    $allProcesses.Add($unrelated)
    $runnerOne = Start-SyntheticRunner `
        -RunnerPath $runnerOnePath `
        -Argument @{
            Journal = $journalOne
            Resource = $resourceOne
            ChildMarker = $childMarkerOne
            HostExecutable = $hostPath
        }
    $allProcesses.Add($runnerOne)
    Write-State -Path $stateOne -Process $runnerOne
    Wait-ForFileState -Path $journalOne -Present $true

    $outputOne = & $hostPath `
        -NoLogo `
        -NoProfile `
        -NonInteractive `
        -File $handoffScript `
        -StatePath $stateOne `
        -JournalPath $journalOne `
        -ExpectedRunnerPath $runnerOnePath `
        -ExpectedRunnerSha256 (Get-Sha256 -Path $runnerOnePath) `
        -ExpectedHostPath $hostPath `
        -ResourceProofPath $resourceOne `
        -PollMilliseconds 500 `
        -TimeoutSeconds 15
    $exitOne = $LASTEXITCODE
    $resultOne = $outputOne | ConvertFrom-Json
    Wait-ForFileState -Path $childMarkerOne -Present $true
    [int]$childOneId = [IO.File]::ReadAllText($childMarkerOne)
    Start-Sleep -Milliseconds 100
    $runnerOne.Refresh()
    $unrelated.Refresh()
    if ($exitOne -ne 0 -or
        $resultOne.Outcome -ne "StoppedAtBoundary" -or
        -not $resultOne.SafeToStartReplacement -or
        -not $resultOne.TransitionObserved -or
        -not $resultOne.ResourceReleaseProven -or
        $resultOne.PreStopResourceProofCount -ne 1 -or
        $resultOne.ResourceProbeCount -ne 1 -or
        $resultOne.DescendantCount -lt 1 -or
        -not $runnerOne.HasExited -or
        $unrelated.HasExited -or
        $null -ne (
            Get-Process -Id $childOneId -ErrorAction SilentlyContinue
        ) -or
        [IO.File]::Exists($journalOne)) {
        $diagnostic = [pscustomobject]@{
            ExitCode = $exitOne
            Result = $resultOne
            RunnerExited = $runnerOne.HasExited
            UnrelatedExited = $unrelated.HasExited
            ChildAlive = (
                $null -ne (
                    Get-Process `
                        -Id $childOneId `
                        -ErrorAction SilentlyContinue
                ))
            JournalPresent = [IO.File]::Exists($journalOne)
        } | ConvertTo-Json -Compress -Depth 4
        throw "Boundary stop or resource-release proof failed: $diagnostic"
    }
    $results.BoundaryStop = $resultOne.Outcome
    $results.FixedPointDescendants = [int]$resultOne.DescendantCount
    $results.ResourceReleaseProven =
        [bool]$resultOne.ResourceReleaseProven
    $results.UnrelatedProcessSurvived = $true
    Stop-TestProcess -Process $unrelated

    $script:CurrentCase = "reappearance"
    $caseTwo = Join-Path $testRoot "reappearance"
    [IO.Directory]::CreateDirectory($caseTwo) | Out-Null
    $runnerTwoPath = Join-Path $caseTwo "SyntheticRunner.ps1"
    $stateTwo = Join-Path $caseTwo "state.json"
    $journalTwo = Join-Path $caseTwo "active-transaction.json"
    $heartbeatTwo = Join-Path $caseTwo "heartbeat.txt"
    $resourceTwo = Join-Path $caseTwo "runner.lock"
    $runnerTwoCode = @'
param(
    [string]$Mode,
    [string]$Journal,
    [string]$Heartbeat,
    [string]$Resource
)
$ErrorActionPreference = "Stop"
$lock = [IO.FileStream]::new(
    $Resource,
    [IO.FileMode]::OpenOrCreate,
    [IO.FileAccess]::ReadWrite,
    [IO.FileShare]::None)
try {
    [IO.File]::WriteAllText($Journal, "active")
    Start-Sleep -Milliseconds 5000
    [IO.File]::Delete($Journal)
    $counter = 0
    while ($true) {
        $counter++
        [IO.File]::WriteAllText($Heartbeat, [string]$counter)
        Start-Sleep -Milliseconds 50
    }
}
finally {
    $lock.Dispose()
}
'@
    Write-Utf8File -Path $runnerTwoPath -Text $runnerTwoCode
    $runnerTwo = Start-SyntheticRunner `
        -RunnerPath $runnerTwoPath `
        -Argument @{
            Journal = $journalTwo
            Heartbeat = $heartbeatTwo
            Resource = $resourceTwo
        }
    $allProcesses.Add($runnerTwo)
    Write-State -Path $stateTwo -Process $runnerTwo
    Wait-ForFileState -Path $journalTwo -Present $true
    $resultTwo = Invoke-LegacyHandoffCore `
        -StatePath $stateTwo `
        -JournalPath $journalTwo `
        -ExpectedRunnerPath $runnerTwoPath `
        -ExpectedRunnerSha256 (Get-Sha256 -Path $runnerTwoPath) `
        -ExpectedHostPath $hostPath `
        -ResourceProofPath $resourceTwo `
        -PollMilliseconds 500 `
        -TimeoutSeconds 20 `
        -CandidateHook {
            Write-Utf8File `
                -Path $journalTwo `
                -Text "external-recovery-journal"
        }
    if ($resultTwo.Outcome -ne "RecoveryRequired" -or
        $resultTwo.SafeToStartReplacement -or
        -not [IO.File]::Exists($journalTwo)) {
        throw "Journal reappearance did not fail closed."
    }
    Wait-ForFileState -Path $heartbeatTwo -Present $true
    [int]$before = Read-SyntheticInteger -Path $heartbeatTwo
    [int]$after = Wait-ForSyntheticIntegerAdvance `
        -Path $heartbeatTwo `
        -Before $before
    $runnerTwo.Refresh()
    if ($runnerTwo.HasExited -or $after -le $before) {
        throw "The runner was not resumed after a recovery abort."
    }
    $results.JournalReappearance = $resultTwo.Outcome
    $results.TargetResumedOnAbort = $true
    Stop-TestProcess -Process $runnerTwo

    $script:CurrentCase = "transition"
    $caseThree = Join-Path $testRoot "transition"
    [IO.Directory]::CreateDirectory($caseThree) | Out-Null
    $runnerThreePath = Join-Path $caseThree "SyntheticRunner.ps1"
    $stateThree = Join-Path $caseThree "state.json"
    $journalThree = Join-Path $caseThree "active-transaction.json"
    $readyThree = Join-Path $caseThree "ready.txt"
    $resourceThree = Join-Path $caseThree "runner.lock"
    $runnerThreeCode = @'
param(
    [string]$Mode,
    [string]$Journal,
    [string]$Ready,
    [string]$Resource
)
$ErrorActionPreference = "Stop"
$lock = [IO.FileStream]::new(
    $Resource,
    [IO.FileMode]::OpenOrCreate,
    [IO.FileAccess]::ReadWrite,
    [IO.FileShare]::None)
try {
    [IO.File]::WriteAllText($Ready, "initially-absent")
    Start-Sleep -Milliseconds 1500
    [IO.File]::WriteAllText($Journal, "active")
    Start-Sleep -Milliseconds 3000
    [IO.File]::Delete($Journal)
    while ($true) {
        Start-Sleep -Milliseconds 100
    }
}
finally {
    $lock.Dispose()
}
'@
    Write-Utf8File -Path $runnerThreePath -Text $runnerThreeCode
    $runnerThree = Start-SyntheticRunner `
        -RunnerPath $runnerThreePath `
        -Argument @{
            Journal = $journalThree
            Ready = $readyThree
            Resource = $resourceThree
        }
    $allProcesses.Add($runnerThree)
    Write-State -Path $stateThree -Process $runnerThree
    Wait-ForFileState -Path $readyThree -Present $true
    $transitionTimer = [Diagnostics.Stopwatch]::StartNew()
    $resultThree = Invoke-LegacyHandoffCore `
        -StatePath $stateThree `
        -JournalPath $journalThree `
        -ExpectedRunnerPath $runnerThreePath `
        -ExpectedRunnerSha256 (Get-Sha256 -Path $runnerThreePath) `
        -ExpectedHostPath $hostPath `
        -ResourceProofPath $resourceThree `
        -PollMilliseconds 500 `
        -TimeoutSeconds 15
    $transitionTimer.Stop()
    $runnerThree.Refresh()
    if ($resultThree.Outcome -ne "StoppedAtBoundary" -or
        -not $resultThree.TransitionObserved -or
        -not $runnerThree.HasExited -or
        $transitionTimer.ElapsedMilliseconds -lt 3500) {
        throw "The initial-absence transition guard failed."
    }
    $results.InitialAbsenceGuarded = $true

    $script:CurrentCase = "identity"
    $caseFour = Join-Path $testRoot "identity"
    [IO.Directory]::CreateDirectory($caseFour) | Out-Null
    $runnerFourPath = Join-Path $caseFour "SyntheticRunner.ps1"
    $stateFour = Join-Path $caseFour "state.json"
    $journalFour = Join-Path $caseFour "active-transaction.json"
    $heartbeatFour = Join-Path $caseFour "heartbeat.txt"
    $resourceFour = Join-Path $caseFour "runner.lock"
    $runnerFourCode = @'
param(
    [string]$Mode,
    [string]$Journal,
    [string]$Heartbeat
)
$counter = 0
while ($true) {
    $counter++
    [IO.File]::WriteAllText($Heartbeat, [string]$counter)
    Start-Sleep -Milliseconds 50
}
'@
    Write-Utf8File -Path $runnerFourPath -Text $runnerFourCode
    Write-Utf8File -Path $resourceFour -Text "unlocked"
    $runnerFour = Start-SyntheticRunner `
        -RunnerPath $runnerFourPath `
        -Argument @{
            Journal = $journalFour
            Heartbeat = $heartbeatFour
        }
    $allProcesses.Add($runnerFour)
    Write-State -Path $stateFour -Process $runnerFour
    Wait-ForFileState -Path $heartbeatFour -Present $true
    [bool]$zeroProofRejected = $false
    try {
        Invoke-LegacyHandoffCore `
            -StatePath $stateFour `
            -JournalPath $journalFour `
            -ExpectedRunnerPath $runnerFourPath `
            -ExpectedRunnerSha256 (Get-Sha256 -Path $runnerFourPath) `
            -ExpectedHostPath $hostPath `
            -PollMilliseconds 500 `
            -TimeoutSeconds 2 | Out-Null
    }
    catch {
        $zeroProofRejected = $true
    }
    if (-not $zeroProofRejected) {
        throw "A zero-resource-proof handoff was accepted."
    }

    $runnerFour.Refresh()
    $shiftedStartUtc = $runnerFour.StartTime.ToUniversalTime().AddMilliseconds(
        50).ToString("o")
    [pscustomobject]@{
        ProcessId = [int]$runnerFour.Id
        ProcessStartUtc = $shiftedStartUtc
    } | ConvertTo-Json -Compress |
        ForEach-Object {
            Write-Utf8File -Path $stateFour -Text $_
        }
    [bool]$impreciseIdentityRejected = $false
    try {
        Invoke-LegacyHandoffCore `
            -StatePath $stateFour `
            -JournalPath $journalFour `
            -ExpectedRunnerPath $runnerFourPath `
            -ExpectedRunnerSha256 (Get-Sha256 -Path $runnerFourPath) `
            -ExpectedHostPath $hostPath `
            -ResourceProofPath $resourceFour `
            -PollMilliseconds 500 `
            -TimeoutSeconds 2 | Out-Null
    }
    catch {
        $impreciseIdentityRejected = $true
    }
    Write-State -Path $stateFour -Process $runnerFour
    if (-not $impreciseIdentityRejected) {
        throw "An imprecise native process identity was accepted."
    }

    [bool]$identityRejected = $false
    try {
        Invoke-LegacyHandoffCore `
            -StatePath $stateFour `
            -JournalPath $journalFour `
            -ExpectedRunnerPath $runnerFourPath `
            -ExpectedRunnerSha256 ("0" * 64) `
            -ExpectedHostPath $hostPath `
            -ResourceProofPath $resourceFour `
            -PollMilliseconds 500 `
            -TimeoutSeconds 2 | Out-Null
    }
    catch {
        $identityRejected = $true
    }
    [int]$identityBefore =
        Read-SyntheticInteger -Path $heartbeatFour
    [int]$identityAfter =
        Wait-ForSyntheticIntegerAdvance `
            -Path $heartbeatFour `
            -Before $identityBefore
    $runnerFour.Refresh()
    if (-not $identityRejected -or
        $runnerFour.HasExited -or
        $identityAfter -le $identityBefore) {
        throw "An identity mismatch did not leave the target untouched."
    }
    $results.IdentityMismatchRejected = $true
    $results.IdentityMismatchTargetUntouched = $true
    $results.ZeroProofRejected = $true
    $results.NativeStartIdentityTight = $true
    Stop-TestProcess -Process $runnerFour

    $script:CurrentCase = "command-bypass"
    $caseFive = Join-Path $testRoot "command-bypass"
    [IO.Directory]::CreateDirectory($caseFive) | Out-Null
    $runnerFivePath = Join-Path $caseFive "SyntheticRunner.ps1"
    $stateFive = Join-Path $caseFive "state.json"
    $journalFive = Join-Path $caseFive "active-transaction.json"
    $resourceFive = Join-Path $caseFive "runner.lock"
    Write-Utf8File `
        -Path $runnerFivePath `
        -Text "param([string]`$Mode)`nStart-Sleep -Seconds 60"
    Write-Utf8File -Path $resourceFive -Text "unlocked"
    $bypassStart = [Diagnostics.ProcessStartInfo]::new()
    $bypassStart.FileName = $hostPath
    $bypassStart.Arguments =
        '-NoLogo -NoProfile -c "Start-Sleep -Seconds 60; #" ' +
        '-File "' + $runnerFivePath + '" -Mode Batch'
    $bypassStart.UseShellExecute = $false
    $bypassStart.CreateNoWindow = $true
    $runnerFive = [Diagnostics.Process]::Start($bypassStart)
    $allProcesses.Add($runnerFive)
    Write-State -Path $stateFive -Process $runnerFive
    [bool]$bypassRejected = $false
    try {
        Invoke-LegacyHandoffCore `
            -StatePath $stateFive `
            -JournalPath $journalFive `
            -ExpectedRunnerPath $runnerFivePath `
            -ExpectedRunnerSha256 (Get-Sha256 -Path $runnerFivePath) `
            -ExpectedHostPath $hostPath `
            -ResourceProofPath $resourceFive `
            -PollMilliseconds 500 `
            -TimeoutSeconds 2 | Out-Null
    }
    catch {
        $bypassRejected = $true
    }
    $runnerFive.Refresh()
    if (-not $bypassRejected -or $runnerFive.HasExited) {
        throw "The -Command/-File grammar bypass was not rejected safely."
    }

    $unrelatedAncestry = Start-Process `
        -FilePath $hostPath `
        -ArgumentList @(
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Start-Sleep -Seconds 60"
        ) `
        -WindowStyle Hidden `
        -PassThru
    $allProcesses.Add($unrelatedAncestry)
    $rootIdentityHandle =
        [LegacyHandoffNative]::Open([int]$runnerFive.Id)
    $unrelatedIdentityHandle =
        [LegacyHandoffNative]::Open([int]$unrelatedAncestry.Id)
    [bool]$ancestryRejected = $false
    try {
        Assert-RetainedTreeAncestry `
            -RootHandle $rootIdentityHandle `
            -Descendants @($unrelatedIdentityHandle)
    }
    catch {
        $ancestryRejected = $true
    }
    finally {
        $unrelatedIdentityHandle.Dispose()
        $rootIdentityHandle.Dispose()
    }
    if (-not $ancestryRejected) {
        throw "A retained handle with false ancestry was accepted."
    }
    $results.CommandBypassRejected = $true
    $results.RetainedAncestryRequired = $true
    Stop-TestProcess -Process $unrelatedAncestry
    Stop-TestProcess -Process $runnerFive

    $script:CurrentCase = "unlocked-proof"
    $caseSix = Join-Path $testRoot "unlocked-proof"
    [IO.Directory]::CreateDirectory($caseSix) | Out-Null
    $runnerSixPath = Join-Path $caseSix "SyntheticRunner.ps1"
    $stateSix = Join-Path $caseSix "state.json"
    $journalSix = Join-Path $caseSix "active-transaction.json"
    $resourceSix = Join-Path $caseSix "runner.lock"
    $heartbeatSix = Join-Path $caseSix "heartbeat.txt"
    $runnerSixCode = @'
param(
    [string]$Mode,
    [string]$Journal,
    [string]$Heartbeat
)
[IO.File]::WriteAllText($Journal, "active")
Start-Sleep -Milliseconds 4000
[IO.File]::Delete($Journal)
$counter = 0
while ($true) {
    $counter++
    [IO.File]::WriteAllText($Heartbeat, [string]$counter)
    Start-Sleep -Milliseconds 50
}
'@
    Write-Utf8File -Path $runnerSixPath -Text $runnerSixCode
    Write-Utf8File -Path $resourceSix -Text "never-locked"
    $runnerSix = Start-SyntheticRunner `
        -RunnerPath $runnerSixPath `
        -Argument @{
            Journal = $journalSix
            Heartbeat = $heartbeatSix
        }
    $allProcesses.Add($runnerSix)
    Write-State -Path $stateSix -Process $runnerSix
    Wait-ForFileState -Path $journalSix -Present $true
    [bool]$unlockedRejected = $false
    try {
        Invoke-LegacyHandoffCore `
            -StatePath $stateSix `
            -JournalPath $journalSix `
            -ExpectedRunnerPath $runnerSixPath `
            -ExpectedRunnerSha256 (Get-Sha256 -Path $runnerSixPath) `
            -ExpectedHostPath $hostPath `
            -ResourceProofPath $resourceSix `
            -PollMilliseconds 500 `
            -TimeoutSeconds 15 | Out-Null
    }
    catch {
        $unlockedRejected = $true
    }
    Wait-ForFileState -Path $heartbeatSix -Present $true
    [int]$unlockedBefore =
        Read-SyntheticInteger -Path $heartbeatSix
    [int]$unlockedAfter =
        Wait-ForSyntheticIntegerAdvance `
            -Path $heartbeatSix `
            -Before $unlockedBefore
    $runnerSix.Refresh()
    if (-not $unlockedRejected -or
        $runnerSix.HasExited -or
        $unlockedAfter -le $unlockedBefore) {
        throw "An unlocked resource proof was accepted or left suspended."
    }
    $results.UnlockedProofRejected = $true
    Stop-TestProcess -Process $runnerSix

    $script:CurrentCase = "timeout"
    $caseSeven = Join-Path $testRoot "timeout"
    [IO.Directory]::CreateDirectory($caseSeven) | Out-Null
    $runnerSevenPath = Join-Path $caseSeven "SyntheticRunner.ps1"
    $stateSeven = Join-Path $caseSeven "state.json"
    $journalSeven = Join-Path $caseSeven "active-transaction.json"
    $resourceSeven = Join-Path $caseSeven "runner.lock"
    $heartbeatSeven = Join-Path $caseSeven "heartbeat.txt"
    $runnerSevenCode = @'
param(
    [string]$Mode,
    [string]$Journal,
    [string]$Resource,
    [string]$Heartbeat
)
$lock = [IO.FileStream]::new(
    $Resource,
    [IO.FileMode]::OpenOrCreate,
    [IO.FileAccess]::ReadWrite,
    [IO.FileShare]::None)
try {
    [IO.File]::WriteAllText($Journal, "active")
    $counter = 0
    while ($true) {
        $counter++
        [IO.File]::WriteAllText($Heartbeat, [string]$counter)
        Start-Sleep -Milliseconds 50
    }
}
finally {
    $lock.Dispose()
}
'@
    Write-Utf8File -Path $runnerSevenPath -Text $runnerSevenCode
    $runnerSeven = Start-SyntheticRunner `
        -RunnerPath $runnerSevenPath `
        -Argument @{
            Journal = $journalSeven
            Resource = $resourceSeven
            Heartbeat = $heartbeatSeven
        }
    $allProcesses.Add($runnerSeven)
    Write-State -Path $stateSeven -Process $runnerSeven
    Wait-ForFileState -Path $journalSeven -Present $true
    $resultSeven = Invoke-LegacyHandoffCore `
        -StatePath $stateSeven `
        -JournalPath $journalSeven `
        -ExpectedRunnerPath $runnerSevenPath `
        -ExpectedRunnerSha256 (Get-Sha256 -Path $runnerSevenPath) `
        -ExpectedHostPath $hostPath `
        -ResourceProofPath $resourceSeven `
        -PollMilliseconds 500 `
        -TimeoutSeconds 2
    [int]$timeoutBefore =
        Read-SyntheticInteger -Path $heartbeatSeven
    [int]$timeoutAfter =
        Wait-ForSyntheticIntegerAdvance `
            -Path $heartbeatSeven `
            -Before $timeoutBefore
    $runnerSeven.Refresh()
    if ($resultSeven.Outcome -ne "TimedOut" -or
        $resultSeven.SafeToStartReplacement -or
        $runnerSeven.HasExited -or
        $timeoutAfter -le $timeoutBefore) {
        throw "Timeout did not leave the exact target running and resumed."
    }
    $results.TimeoutLeavesTargetRunning = $true
    Stop-TestProcess -Process $runnerSeven

    $script:CurrentCase = "early-exit"
    $caseEight = Join-Path $testRoot "early-exit"
    [IO.Directory]::CreateDirectory($caseEight) | Out-Null
    $runnerEightPath = Join-Path $caseEight "SyntheticRunner.ps1"
    $stateEight = Join-Path $caseEight "state.json"
    $journalEight = Join-Path $caseEight "active-transaction.json"
    $resourceEight = Join-Path $caseEight "runner.lock"
    $runnerEightCode = @'
param(
    [string]$Mode,
    [string]$Journal,
    [string]$Resource
)
$lock = [IO.FileStream]::new(
    $Resource,
    [IO.FileMode]::OpenOrCreate,
    [IO.FileAccess]::ReadWrite,
    [IO.FileShare]::None)
try {
    [IO.File]::WriteAllText($Journal, "active")
    Start-Sleep -Milliseconds 5000
}
finally {
    $lock.Dispose()
}
'@
    Write-Utf8File -Path $runnerEightPath -Text $runnerEightCode
    $runnerEight = Start-SyntheticRunner `
        -RunnerPath $runnerEightPath `
        -Argument @{
            Journal = $journalEight
            Resource = $resourceEight
        }
    $allProcesses.Add($runnerEight)
    Write-State -Path $stateEight -Process $runnerEight
    Wait-ForFileState -Path $journalEight -Present $true
    $resultEight = Invoke-LegacyHandoffCore `
        -StatePath $stateEight `
        -JournalPath $journalEight `
        -ExpectedRunnerPath $runnerEightPath `
        -ExpectedRunnerSha256 (Get-Sha256 -Path $runnerEightPath) `
        -ExpectedHostPath $hostPath `
        -ResourceProofPath $resourceEight `
        -PollMilliseconds 500 `
        -TimeoutSeconds 15
    if ($resultEight.Outcome -ne "TargetExitedBeforeBoundary" -or
        $resultEight.SafeToStartReplacement -or
        -not $resultEight.JournalPresent) {
        throw "Early target exit was not reported fail closed."
    }
    $results.EarlyExitFailsClosed = $true

    $script:CurrentCase = "held-after-stop"
    $caseNine = Join-Path $testRoot "held-after-stop"
    [IO.Directory]::CreateDirectory($caseNine) | Out-Null
    $runnerNinePath = Join-Path $caseNine "SyntheticRunner.ps1"
    $lockerNinePath = Join-Path $caseNine "SyntheticLocker.ps1"
    $stateNine = Join-Path $caseNine "state.json"
    $journalNine = Join-Path $caseNine "active-transaction.json"
    $resourceNine = Join-Path $caseNine "runner.lock"
    $lockerReadyNine = Join-Path $caseNine "locker-ready.txt"
    Write-Utf8File -Path $resourceNine -Text "shared-lock"
    $runnerNineCode = @'
param(
    [string]$Mode,
    [string]$Journal,
    [string]$Resource
)
$lock = [IO.FileStream]::new(
    $Resource,
    [IO.FileMode]::Open,
    [IO.FileAccess]::Read,
    [IO.FileShare]::Read)
try {
    [IO.File]::WriteAllText($Journal, "active")
    Start-Sleep -Milliseconds 4000
    [IO.File]::Delete($Journal)
    while ($true) {
        Start-Sleep -Milliseconds 100
    }
}
finally {
    $lock.Dispose()
}
'@
    $lockerNineCode = @'
param(
    [string]$Resource,
    [string]$Ready
)
$lock = [IO.FileStream]::new(
    $Resource,
    [IO.FileMode]::Open,
    [IO.FileAccess]::Read,
    [IO.FileShare]::Read)
try {
    [IO.File]::WriteAllText($Ready, "locked")
    while ($true) {
        Start-Sleep -Milliseconds 100
    }
}
finally {
    $lock.Dispose()
}
'@
    Write-Utf8File -Path $runnerNinePath -Text $runnerNineCode
    Write-Utf8File -Path $lockerNinePath -Text $lockerNineCode
    $runnerNine = Start-SyntheticRunner `
        -RunnerPath $runnerNinePath `
        -Argument @{
            Journal = $journalNine
            Resource = $resourceNine
        }
    $allProcesses.Add($runnerNine)
    Write-State -Path $stateNine -Process $runnerNine
    Wait-ForFileState -Path $journalNine -Present $true
    $lockerNine = Start-Process `
        -FilePath $hostPath `
        -ArgumentList @(
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            $lockerNinePath,
            "-Resource",
            $resourceNine,
            "-Ready",
            $lockerReadyNine
        ) `
        -WindowStyle Hidden `
        -PassThru
    $allProcesses.Add($lockerNine)
    Wait-ForFileState -Path $lockerReadyNine -Present $true
    $resultNine = Invoke-LegacyHandoffCore `
        -StatePath $stateNine `
        -JournalPath $journalNine `
        -ExpectedRunnerPath $runnerNinePath `
        -ExpectedRunnerSha256 (Get-Sha256 -Path $runnerNinePath) `
        -ExpectedHostPath $hostPath `
        -ResourceProofPath $resourceNine `
        -PollMilliseconds 500 `
        -TimeoutSeconds 15
    $runnerNine.Refresh()
    $lockerNine.Refresh()
    if ($resultNine.Outcome -ne "ResourceReleaseIncomplete" -or
        $resultNine.SafeToStartReplacement -or
        $resultNine.PreStopResourceProofCount -ne 1 -or
        -not $runnerNine.HasExited -or
        $lockerNine.HasExited) {
        throw "A resource still held after stop was accepted."
    }
    $results.PostStopHeldResourceRejected = $true
    Stop-TestProcess -Process $lockerNine

    $script:CurrentCase = "partial-termination"
    $caseTen = Join-Path $testRoot "partial-termination"
    [IO.Directory]::CreateDirectory($caseTen) | Out-Null
    $runnerTenPath = Join-Path $caseTen "SyntheticRunner.ps1"
    $stateTen = Join-Path $caseTen "state.json"
    $journalTen = Join-Path $caseTen "active-transaction.json"
    $resourceTen = Join-Path $caseTen "runner.lock"
    $heartbeatTen = Join-Path $caseTen "heartbeat.txt"
    $childMarkerTen = Join-Path $caseTen "child.pid"
    $runnerTenCode = @'
param(
    [string]$Mode,
    [string]$Journal,
    [string]$Resource,
    [string]$Heartbeat,
    [string]$ChildMarker,
    [string]$HostExecutable
)
$lock = [IO.FileStream]::new(
    $Resource,
    [IO.FileMode]::OpenOrCreate,
    [IO.FileAccess]::ReadWrite,
    [IO.FileShare]::None)
try {
    [IO.File]::WriteAllText($Journal, "active")
    $child = Start-Process `
        -FilePath $HostExecutable `
        -ArgumentList @(
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Start-Sleep -Seconds 60"
        ) `
        -WindowStyle Hidden `
        -PassThru
    [IO.File]::WriteAllText($ChildMarker, [string]$child.Id)
    Start-Sleep -Milliseconds 4000
    [IO.File]::Delete($Journal)
    $counter = 0
    while ($true) {
        $counter++
        [IO.File]::WriteAllText($Heartbeat, [string]$counter)
        Start-Sleep -Milliseconds 50
    }
}
finally {
    $lock.Dispose()
}
'@
    Write-Utf8File -Path $runnerTenPath -Text $runnerTenCode
    $runnerTen = Start-SyntheticRunner `
        -RunnerPath $runnerTenPath `
        -Argument @{
            Journal = $journalTen
            Resource = $resourceTen
            Heartbeat = $heartbeatTen
            ChildMarker = $childMarkerTen
            HostExecutable = $hostPath
        }
    $allProcesses.Add($runnerTen)
    Write-State -Path $stateTen -Process $runnerTen
    Wait-ForFileState -Path $journalTen -Present $true
    Wait-ForFileState -Path $childMarkerTen -Present $true
    [int]$childTenId = [IO.File]::ReadAllText($childMarkerTen)
    $resultTen = Invoke-LegacyHandoffCore `
        -StatePath $stateTen `
        -JournalPath $journalTen `
        -ExpectedRunnerPath $runnerTenPath `
        -ExpectedRunnerSha256 (Get-Sha256 -Path $runnerTenPath) `
        -ExpectedHostPath $hostPath `
        -ResourceProofPath $resourceTen `
        -PollMilliseconds 500 `
        -TimeoutSeconds 15 `
        -SimulatedTerminationFailureProcessId $runnerTen.Id
    Wait-ForFileState -Path $heartbeatTen -Present $true
    [int]$partialBefore =
        Read-SyntheticInteger -Path $heartbeatTen
    [int]$partialAfter =
        Wait-ForSyntheticIntegerAdvance `
            -Path $heartbeatTen `
            -Before $partialBefore
    $runnerTen.Refresh()
    if ($resultTen.Outcome -ne "TerminationIncomplete" -or
        $resultTen.SafeToStartReplacement -or
        $resultTen.DescendantCount -lt 1 -or
        $runnerTen.HasExited -or
        $partialAfter -le $partialBefore -or
        $null -ne (
            Get-Process -Id $childTenId -ErrorAction SilentlyContinue
        )) {
        $partialDiagnostic = [pscustomobject]@{
            Result = $resultTen
            RunnerExited = $runnerTen.HasExited
            HeartbeatAdvanced = ($partialAfter -gt $partialBefore)
            ChildAlive = (
                $null -ne (
                    Get-Process `
                        -Id $childTenId `
                        -ErrorAction SilentlyContinue
                ))
        } | ConvertTo-Json -Compress -Depth 3
        throw "Partial termination failed: $partialDiagnostic"
    }
    $results.PartialTerminationFailsClosed = $true
    $results.PartialTerminationSurvivorResumed = $true
    Stop-TestProcess -Process $runnerTen
}
finally {
    foreach ($process in $allProcesses) {
        Stop-TestProcess -Process $process
    }
    $resolvedTestRoot = [IO.Path]::GetFullPath($testRoot)
    $expectedPrefix = $temporaryBase +
        [IO.Path]::DirectorySeparatorChar
    if (-not $resolvedTestRoot.StartsWith(
            $expectedPrefix,
            [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove an unexpected synthetic test directory."
    }
    if ([IO.Directory]::Exists($resolvedTestRoot)) {
        Remove-Item `
            -LiteralPath $resolvedTestRoot `
            -Recurse `
            -Force
    }
}

[pscustomobject]$results | ConvertTo-Json -Compress
