[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$scriptsRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$launcher = Join-Path $scriptsRoot "Start-LanCoordinatorSafely.ps1"
$tokens = $null
$errors = $null
[void][Management.Automation.Language.Parser]::ParseFile(
    $launcher,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) {
    throw "PowerShell parser errors were found in $launcher."
}

$launcherText = Get-Content -LiteralPath $launcher -Raw
foreach ($forbidden in @(
    "Jellyfin",
    "Stop-Service",
    "Restart-Service",
    "Stop-Process",
    "Get-Credential",
    "New-SmbMapping",
    "net.exe"
)) {
    if ($launcherText -match [regex]::Escape($forbidden)) {
        throw "Coordinator launcher contains forbidden operation $forbidden."
    }
}
foreach ($required in @(
    "ResumeSourceBusy",
    "CoordinatorStartupStatus",
    "MaxSourceBusyRetries",
    "SourceBusyRetrySeconds"
)) {
    if ($launcherText -notmatch [regex]::Escape($required)) {
        throw "Coordinator launcher is missing required guard $required."
    }
}

$testRoot = Join-Path ([IO.Path]::GetTempPath()) (
    "video-transcoder-coordinator-safety-{0}" -f
        [Guid]::NewGuid().ToString("N")
)
New-Item -ItemType Directory -Path $testRoot | Out-Null
try {
    $configPath = Join-Path $testRoot "VideoTranscoderLanAssist.json"
    $fakeExecutable = Join-Path $testRoot "fake-coordinator.ps1"
    $attemptPath = Join-Path $testRoot "attempts.txt"
    $casePath = Join-Path $testRoot "case.txt"
    $statusPath = Join-Path $testRoot "coordinator-startup-status.json"
    $stdoutPath = Join-Path $testRoot "stdout.txt"
    $stderrPath = Join-Path $testRoot "stderr.txt"

    @{
        schema_version = 1
        mode = "coordinator"
        root = (Join-Path $testRoot "media")
        work_root = (Join-Path $testRoot "work")
        token_file = (Join-Path $testRoot "token.txt")
    } | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding UTF8

    @'
if ($args -contains "--validate-config") {
    '{"Event":"ConfigValidated","Mode":"coordinator","Status":"Ready"}'
    exit 0
}
$attemptPath = Join-Path $PSScriptRoot "attempts.txt"
$casePath = Join-Path $PSScriptRoot "case.txt"
$count = if (Test-Path -LiteralPath $attemptPath) {
    [int](Get-Content -LiteralPath $attemptPath -Raw).Trim()
} else {
    0
}
$count++
Set-Content -LiteralPath $attemptPath -Value $count -Encoding ASCII
$scenario = (Get-Content -LiteralPath $casePath -Raw).Trim()
if ($scenario -eq "busy_then_success") {
    if ($count -le 2) {
        '{"Event":"ServiceStopped","Status":"Failed","FailureCategory":"ResumeSourceBusy"}'
        exit 1
    }
    '{"Event":"CoordinatorStatus","Status":"Running","FailureCategory":""}'
    exit 0
}
if ($scenario -eq "always_busy") {
    '{"Event":"ServiceStopped","Status":"Failed","FailureCategory":"ResumeSourceBusy"}'
    exit 1
}
if ($scenario -eq "source_changed") {
    '{"Event":"ServiceStopped","Status":"Failed","FailureCategory":"SourceChanged"}'
    exit 1
}
if ($scenario -eq "workers_active") {
    '{"Event":"ServiceStopped","Status":"Failed","FailureCategory":"ResumeWorkersActive"}'
    exit 1
}
if ($scenario -eq "busy_after_status") {
    '{"Event":"CoordinatorStatus","Status":"Running","FailureCategory":""}'
    '{"Event":"ServiceStopped","Status":"Failed","FailureCategory":"ResumeSourceBusy"}'
    exit 1
}
if ($scenario -eq "busy_wrong_status") {
    '{"Event":"ServiceStopped","Status":"Blocked","FailureCategory":"ResumeSourceBusy"}'
    exit 1
}
if ($scenario -eq "busy_wrong_event") {
    '{"Event":"CoordinatorStopped","Status":"Failed","FailureCategory":"ResumeSourceBusy"}'
    exit 1
}
if ($scenario -eq "busy_exit_two") {
    '{"Event":"ServiceStopped","Status":"Failed","FailureCategory":"ResumeSourceBusy"}'
    exit 2
}
if ($scenario -eq "malformed") {
    "not-json"
    exit 1
}
if ($scenario -eq "json_null") {
    "null"
    exit 1
}
if ($scenario -eq "json_scalar") {
    "7"
    exit 1
}
if ($scenario -eq "json_array") {
    '[{"Event":"ServiceStopped","Status":"Failed","FailureCategory":"ResumeSourceBusy"}]'
    exit 1
}
if ($scenario -eq "stale_busy_success") {
    '{"Event":"ServiceStopped","Status":"Failed","FailureCategory":"ResumeSourceBusy"}'
    exit 0
}
'{"Event":"ServiceStopped","Status":"Failed","FailureCategory":"RootUnavailable"}'
exit 1
'@ | Set-Content -LiteralPath $fakeExecutable -Encoding ASCII

    function Invoke-LauncherCase {
        param(
            [string]$Case,
            [int]$Retries,
            [switch]$ValidateOnly
        )

        Remove-Item `
            -LiteralPath $attemptPath, $statusPath, $stdoutPath, $stderrPath `
            -Force `
            -ErrorAction SilentlyContinue
        Set-Content -LiteralPath $casePath -Value $Case -Encoding ASCII
        $arguments = @(
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "`"$launcher`"",
            "-ConfigPath",
            "`"$configPath`"",
            "-ExecutablePath",
            "`"$fakeExecutable`"",
            "-MaxSourceBusyRetries",
            [string]$Retries,
            "-SourceBusyRetrySeconds",
            "0"
        )
        if ($ValidateOnly) {
            $arguments += "-ValidateOnly"
        }
        $process = Start-Process `
            -FilePath "powershell.exe" `
            -ArgumentList $arguments `
            -WindowStyle Hidden `
            -Wait `
            -PassThru `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath
        $attempts = if (Test-Path -LiteralPath $attemptPath) {
            [int](Get-Content -LiteralPath $attemptPath -Raw).Trim()
        } else {
            0
        }
        $status = if (Test-Path -LiteralPath $statusPath) {
            Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
        } else {
            $null
        }
        [pscustomobject]@{
            ExitCode = $process.ExitCode
            Attempts = $attempts
            Status = $status
            Stdout = Get-Content -LiteralPath $stdoutPath -Raw
            Stderr = Get-Content -LiteralPath $stderrPath -Raw
        }
    }

    $validated = Invoke-LauncherCase "busy_then_success" 2 -ValidateOnly
    if (
        $validated.ExitCode -ne 0 -or
        $validated.Attempts -ne 0 -or
        $null -ne $validated.Status -or
        $validated.Stdout -notmatch "CoordinatorLauncherValidated"
    ) {
        throw "Coordinator launcher ValidateOnly evidence is invalid."
    }

    $released = Invoke-LauncherCase "busy_then_success" 2
    if (
        $released.ExitCode -ne 0 -or
        $released.Attempts -ne 3 -or
        $released.Status.Status -ne "Stopped" -or
        $released.Status.FailureCategory -ne ""
    ) {
        throw (
            "ResumeSourceBusy retry evidence was invalid: " +
            "exit={0}, attempts={1}, status={2}, category={3}, stderr={4}" -f
                $released.ExitCode,
                $released.Attempts,
                $released.Status.Status,
                $released.Status.FailureCategory,
                ([string]$released.Stderr)
        )
    }

    $exhausted = Invoke-LauncherCase "always_busy" 2
    if (
        $exhausted.ExitCode -ne 1 -or
        $exhausted.Attempts -ne 3 -or
        $exhausted.Status.Status -ne "Blocked" -or
        $exhausted.Status.FailureCategory -ne "ResumeSourceBusy"
    ) {
        throw "ResumeSourceBusy exhaustion was not fail-closed."
    }

    foreach ($case in @(
        "source_changed",
        "workers_active",
        "busy_after_status",
        "busy_wrong_status",
        "busy_wrong_event",
        "malformed",
        "json_null",
        "json_scalar",
        "json_array"
    )) {
        $result = Invoke-LauncherCase $case 5
        if ($result.ExitCode -ne 1 -or $result.Attempts -ne 1) {
            throw "Non-retryable case $case was retried."
        }
    }

    $wrongExit = Invoke-LauncherCase "busy_exit_two" 5
    if ($wrongExit.ExitCode -ne 2 -or $wrongExit.Attempts -ne 1) {
        throw "ResumeSourceBusy with a non-contract exit code was retried."
    }

    $staleText = Invoke-LauncherCase "stale_busy_success" 5
    if ($staleText.ExitCode -ne 0 -or $staleText.Attempts -ne 1) {
        throw "A successful exit was retried because of stale busy text."
    }

    $expectedProperties = @(
        "SchemaVersion",
        "Event",
        "Status",
        "FailureCategory",
        "Attempt",
        "MaximumAttempts",
        "RetryDelaySeconds",
        "UpdatedUtc"
    ) | Sort-Object
    $actualProperties = @(
        $exhausted.Status.PSObject.Properties.Name
    ) | Sort-Object
    if (
        @(Compare-Object $expectedProperties $actualProperties).Count -ne 0 -or
        ($exhausted.Status | ConvertTo-Json -Compress) -match
            [regex]::Escape($testRoot)
    ) {
        throw "Coordinator startup telemetry is not path-redacted."
    }
} finally {
    Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "LAN coordinator safety script tests passed."
