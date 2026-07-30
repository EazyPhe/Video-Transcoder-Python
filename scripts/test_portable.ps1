[CmdletBinding()]
param(
    [string]$ExePath = "",
    [string]$EvidenceDirectory = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $ExePath) {
    $ExePath = Join-Path $projectRoot "dist\portable\VideoTranscoderPortable.exe"
}
$ExePath = (Resolve-Path -LiteralPath $ExePath).Path

if (-not $EvidenceDirectory) {
    $stamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
    $EvidenceDirectory = Join-Path $projectRoot "build\portable-smoke\$stamp"
}
$evidence = [System.IO.Path]::GetFullPath($EvidenceDirectory)
$stateDirectory = Join-Path $evidence "state"
$reportPath = Join-Path $evidence "report.json"
New-Item -ItemType Directory -Force -Path $evidence | Out-Null

$priorStateDirectory = [Environment]::GetEnvironmentVariable(
    "VIDEO_TRANSCODER_STATE_DIR",
    "Process"
)
$env:VIDEO_TRANSCODER_STATE_DIR = $stateDirectory
try {
    $process = Start-Process `
        -FilePath $ExePath `
        -ArgumentList @("--portable-self-test", "`"$reportPath`"") `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
} finally {
    if ($null -eq $priorStateDirectory) {
        Remove-Item Env:VIDEO_TRANSCODER_STATE_DIR -ErrorAction SilentlyContinue
    } else {
        $env:VIDEO_TRANSCODER_STATE_DIR = $priorStateDirectory
    }
}

if ($process.ExitCode -ne 0) {
    throw "Portable self-test exited with code $($process.ExitCode)."
}
if (-not (Test-Path -LiteralPath $reportPath -PathType Leaf)) {
    throw "Portable self-test did not write its report: $reportPath"
}

$report = Get-Content -LiteralPath $reportPath -Raw | ConvertFrom-Json
if (-not $report.success) {
    throw "Portable self-test report indicates failure: $reportPath"
}
$expectedOutputDirectory = Split-Path -Parent $ExePath
if (
    [System.IO.Path]::GetFullPath(
        [string]$report.runtime.default_output_directory
    ) -ne [System.IO.Path]::GetFullPath($expectedOutputDirectory)
) {
    throw (
        "Portable default output directory does not match the EXE folder. " +
        "Expected: $expectedOutputDirectory; " +
        "actual: $($report.runtime.default_output_directory)"
    )
}
if (-not (Test-Path -LiteralPath $report.artifacts.encoded -PathType Leaf)) {
    throw "Portable self-test did not leave a validated encoded video."
}

$exeHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ExePath).Hash
$encodedHash = (
    Get-FileHash -Algorithm SHA256 -LiteralPath $report.artifacts.encoded
).Hash

[pscustomobject]@{
    Success = $true
    Executable = $ExePath
    ExecutableSha256 = $exeHash
    Report = $reportPath
    EncodedVideo = $report.artifacts.encoded
    EncodedSha256 = $encodedHash
    FFmpeg = $report.tools.ffmpeg.version
    FFprobe = $report.tools.ffprobe.version
    CPUEncoder = $report.transcode.encoder
    DefaultOutputDirectory = $report.runtime.default_output_directory
    ElapsedSeconds = $report.elapsed_seconds
} | Format-List
