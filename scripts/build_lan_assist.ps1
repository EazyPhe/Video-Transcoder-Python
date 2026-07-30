[CmdletBinding()]
param(
    [string]$PythonCommand = "python",
    [string]$FfmpegPath = "",
    [string]$FfprobePath = "",
    [switch]$SkipTests,
    [switch]$SkipSmoke
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Resolve-MediaTool {
    param(
        [string]$RequestedPath,
        [string]$Name
    )

    if ($RequestedPath) {
        return (Resolve-Path -LiteralPath $RequestedPath).Path
    }
    $command = Get-Command "$Name.exe" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    $searchRoot = "C:\ffmpeg"
    if (Test-Path -LiteralPath $searchRoot -PathType Container) {
        $match = Get-ChildItem `
            -LiteralPath $searchRoot `
            -Filter "$Name.exe" `
            -File `
            -Recurse `
            -ErrorAction SilentlyContinue |
            Sort-Object `
                @{ Expression = {
                    if ($_.FullName -match "(?i)full_build") { 0 } else { 1 }
                } }, `
                @{ Expression = "LastWriteTimeUtc"; Descending = $true } |
            Select-Object -First 1
        if ($match) {
            return $match.FullName
        }
    }
    throw "$Name.exe was not found. Supply -${Name}Path explicitly."
}

function Invoke-CheckedPython {
    param([string[]]$Arguments)
    & $PythonCommand @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE."
    }
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$specPath = Join-Path `
    $projectRoot `
    "packaging\portable\VideoTranscoderLanAssist.spec"
$distPath = Join-Path $projectRoot "dist\lan-assist"
$buildStamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss-fffffff")
$workPath = Join-Path $projectRoot "build\lan-assist\$buildStamp"
$privateRuntimeNames = @(
    "VideoTranscoderLanAssist.json",
    "lan-token.txt",
    ".video-transcoder-local-ledger.json",
    "lan-cache"
)
foreach ($name in $privateRuntimeNames) {
    if (Test-Path -LiteralPath (Join-Path $distPath $name)) {
        throw (
            "Refusing to build: the LAN release folder contains private " +
            "runtime state. Move it to a separate deployment folder first."
        )
    }
}

$pythonArchitecture = & $PythonCommand -c `
    "import platform,struct; print(platform.machine()); print(struct.calcsize('P')*8)"
if ($LASTEXITCODE -ne 0) {
    throw "Unable to run $PythonCommand."
}
if ($pythonArchitecture[-1] -ne "64") {
    throw "The portable Windows build requires 64-bit Python."
}

$resolvedFfmpeg = Resolve-MediaTool $FfmpegPath "ffmpeg"
$resolvedFfprobe = Resolve-MediaTool $FfprobePath "ffprobe"

Push-Location $projectRoot
try {
    Invoke-CheckedPython @("-m", "PyInstaller", "--version")
    if (-not $SkipTests) {
        Invoke-CheckedPython @("-m", "pytest", "-q")
    }
    $env:VIDEO_TRANSCODER_FFMPEG = $resolvedFfmpeg
    $env:VIDEO_TRANSCODER_FFPROBE = $resolvedFfprobe
    Invoke-CheckedPython @(
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--distpath",
        $distPath,
        "--workpath",
        $workPath,
        $specPath
    )
} finally {
    Pop-Location
}

$exePath = Join-Path $distPath "VideoTranscoderLanAssist.exe"
if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "PyInstaller did not produce the expected LAN executable."
}

foreach ($name in @(
    "THIRD_PARTY_NOTICES.txt",
    "VideoTranscoderLanAssist.coordinator.example.json",
    "VideoTranscoderLanAssist.helper.example.json"
)) {
    Copy-Item `
        -LiteralPath (Join-Path $projectRoot "packaging\portable\$name") `
        -Destination $distPath `
        -Force
}

if (-not $SkipSmoke) {
    $stamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
    $smokeRoot = Join-Path $projectRoot "build\lan-assist-smoke\$stamp"
    $mediaRoot = Join-Path $smokeRoot "media"
    $workRoot = Join-Path $smokeRoot "work"
    $configPath = Join-Path $smokeRoot "VideoTranscoderLanAssist.json"
    $stdoutPath = Join-Path $smokeRoot "stdout.jsonl"
    $stderrPath = Join-Path $smokeRoot "stderr.txt"
    New-Item -ItemType Directory -Force -Path $mediaRoot | Out-Null
    $config = [ordered]@{
        schema_version = 1
        mode = "coordinator"
        root = $mediaRoot
        work_root = $workRoot
        token_file = (Join-Path $smokeRoot "lan-token.txt")
        api_port = 41840
        dashboard_port = 41841
        consecutive_failure_limit = 3
    }
    $config | ConvertTo-Json -Depth 5 |
        Set-Content -LiteralPath $configPath -Encoding UTF8
    $process = Start-Process `
        -FilePath $exePath `
        -ArgumentList @(
            "--config",
            "`"$configPath`"",
            "--validate-config"
        ) `
        -WindowStyle Hidden `
        -Wait `
        -PassThru `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath
    if ($process.ExitCode -ne 0) {
        throw "LAN portable config smoke test failed."
    }
    $smoke = Get-Content -LiteralPath $stdoutPath -Raw |
        ConvertFrom-Json
    if (
        $smoke.Event -ne "ConfigValidated" -or
        $smoke.Status -ne "Ready"
    ) {
        throw "LAN portable config smoke evidence was invalid."
    }
}

$file = Get-Item -LiteralPath $exePath
$manifest = [ordered]@{
    schema_version = 1
    created_utc = [DateTime]::UtcNow.ToString("o")
    artifact = $file.Name
    size_bytes = $file.Length
    sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $exePath).Hash
    python_architecture = $pythonArchitecture
    ffmpeg_sha256 = (
        Get-FileHash -Algorithm SHA256 -LiteralPath $resolvedFfmpeg
    ).Hash
    ffprobe_sha256 = (
        Get-FileHash -Algorithm SHA256 -LiteralPath $resolvedFfprobe
    ).Hash
    signed = (
        (Get-AuthenticodeSignature -LiteralPath $exePath).Status -eq "Valid"
    )
}
$manifestPath = Join-Path $distPath "build-manifest.json"
$manifest | ConvertTo-Json -Depth 4 |
    Set-Content -LiteralPath $manifestPath -Encoding UTF8

[pscustomobject]@{
    Executable = $file.FullName
    SizeBytes = $file.Length
    SHA256 = $manifest.sha256
    Manifest = $manifestPath
    Signed = $manifest.signed
} | Format-List
