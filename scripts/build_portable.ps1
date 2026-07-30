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
    "packaging\portable\VideoTranscoderPortable.spec"
$distPath = Join-Path $projectRoot "dist\portable"
$workPath = Join-Path $projectRoot "build\portable"

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
foreach ($tool in @($resolvedFfmpeg, $resolvedFfprobe)) {
    if (-not (Test-Path -LiteralPath $tool -PathType Leaf)) {
        throw "Required media tool was not found: $tool"
    }
}

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

$exePath = Join-Path $distPath "VideoTranscoderPortable.exe"
if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "PyInstaller did not produce the expected executable: $exePath"
}

Copy-Item `
    -LiteralPath (Join-Path `
        $projectRoot `
        "packaging\portable\THIRD_PARTY_NOTICES.txt") `
    -Destination $distPath `
    -Force

$file = Get-Item -LiteralPath $exePath
$manifest = [ordered]@{
    schema_version = 1
    created_utc = [DateTime]::UtcNow.ToString("o")
    artifact = $file.FullName
    size_bytes = $file.Length
    sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $exePath).Hash
    python_architecture = $pythonArchitecture
    ffmpeg = $resolvedFfmpeg
    ffprobe = $resolvedFfprobe
    signed = (
        (Get-AuthenticodeSignature -LiteralPath $exePath).Status -eq "Valid"
    )
}
$manifestPath = Join-Path $distPath "build-manifest.json"
$manifest | ConvertTo-Json -Depth 4 |
    Set-Content -LiteralPath $manifestPath -Encoding UTF8

if (-not $SkipSmoke) {
    & (Join-Path $PSScriptRoot "test_portable.ps1") -ExePath $exePath
    if ($LASTEXITCODE -ne 0) {
        throw "Portable smoke test failed with exit code $LASTEXITCODE."
    }
}

[pscustomobject]@{
    Executable = $file.FullName
    SizeBytes = $file.Length
    SHA256 = $manifest.sha256
    Manifest = $manifestPath
    Signed = $manifest.signed
} | Format-List
