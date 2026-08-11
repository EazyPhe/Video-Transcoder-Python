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

function Invoke-CheckedPowerShell {
    param([string]$ScriptPath)
    & powershell.exe `
        -NoLogo `
        -NoProfile `
        -NonInteractive `
        -ExecutionPolicy Bypass `
        -File $ScriptPath
    if ($LASTEXITCODE -ne 0) {
        throw "PowerShell safety test failed with exit code $LASTEXITCODE."
    }
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$specPath = Join-Path `
    $projectRoot `
    "packaging\portable\VideoTranscoderLanAssist.spec"
$traySpecPath = Join-Path `
    $projectRoot `
    "packaging\portable\VideoTranscoderLanTray.spec"
$distPath = Join-Path $projectRoot "dist\lan-assist"
$buildStamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss-fffffff")
$workPath = Join-Path $projectRoot "build\lan-assist\$buildStamp"
$privateRuntimeNames = @(
    "VideoTranscoderLanAssist.json",
    "lan-token.txt",
    ".video-transcoder-local-ledger.json",
    "lan-cache",
    "aggregate-status.json",
    "helper-events.jsonl",
    "helper-control.json",
    "helper-control-status.json"
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
        Invoke-CheckedPowerShell (
            Join-Path $projectRoot `
                "scripts\tests\Test-LanHelperSafetyScripts.ps1"
        )
        Invoke-CheckedPowerShell (
            Join-Path $projectRoot `
                "scripts\tests\Test-LanCoordinatorSafetyScript.ps1"
        )
        Invoke-CheckedPowerShell (
            Join-Path $projectRoot `
                "scripts\tests\Test-LanCoordinatorDeploymentScript.ps1"
        )
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
    Invoke-CheckedPython @(
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--distpath",
        $distPath,
        "--workpath",
        (Join-Path $workPath "tray"),
        $traySpecPath
    )
} finally {
    Pop-Location
}

$exePath = Join-Path $distPath "VideoTranscoderLanAssist.exe"
$trayExePath = Join-Path $distPath "VideoTranscoderLanTray.exe"
if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "PyInstaller did not produce the expected LAN executable."
}
if (-not (Test-Path -LiteralPath $trayExePath -PathType Leaf)) {
    throw "PyInstaller did not produce the expected LAN tray executable."
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
$releaseScripts = @(
    @{
        Source = "scripts\Start-LanHelperSafely.ps1"
        Destination = "Start-LanAssist.ps1"
    },
    @{
        Source = "scripts\Connect-LanHelperShare.ps1"
        Destination = "Connect-LanHelperShare.ps1"
    },
    @{
        Source = "scripts\Start-LanCoordinatorSafely.ps1"
        Destination = "Start-Coordinator.ps1"
    }
)
foreach ($script in $releaseScripts) {
    Copy-Item `
        -LiteralPath (Join-Path $projectRoot $script.Source) `
        -Destination (Join-Path $distPath $script.Destination) `
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
        helper_worker_id = "helper-nvenc"
        helper_control_id = "b37609d869b84f8b8e05744ee428adb2"
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

    $trayConfigPath = Join-Path $smokeRoot "TrayConfig.json"
    $trayControlPath = Join-Path $smokeRoot "helper-control.json"
    $trayReportPath = Join-Path $smokeRoot "tray-self-test.json"
    $trayConfig = [ordered]@{
        schema_version = 1
        mode = "helper"
        control_file = ".\helper-control.json"
        control_status_file = ".\helper-control-status.json"
        control_id = "b37609d869b84f8b8e05744ee428adb2"
    }
    $trayControl = [ordered]@{
        schema_version = 1
        control_id = "b37609d869b84f8b8e05744ee428adb2"
        revision = 1
        pc_in_use = $false
    }
    [IO.File]::WriteAllText(
        $trayConfigPath,
        ($trayConfig | ConvertTo-Json -Compress),
        [Text.UTF8Encoding]::new($false)
    )
    [IO.File]::WriteAllText(
        $trayControlPath,
        ($trayControl | ConvertTo-Json -Compress),
        [Text.UTF8Encoding]::new($false)
    )
    $trayProcess = Start-Process `
        -FilePath $trayExePath `
        -ArgumentList @(
            "--config",
            "`"$trayConfigPath`"",
            "--self-test-report",
            "`"$trayReportPath`""
        ) `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    if ($trayProcess.ExitCode -ne 0) {
        throw "LAN tray packaged self-test failed."
    }
    $traySmoke = Get-Content -LiteralPath $trayReportPath -Raw |
        ConvertFrom-Json
    if (
        $traySmoke.event -ne "LanTraySelfTest" -or
        $traySmoke.status -ne "Ready" -or
        -not $traySmoke.tray_dependencies -or
        $traySmoke.control_id -ne $trayControl.control_id -or
        [int64]$traySmoke.revision -ne 1
    ) {
        throw "LAN tray packaged self-test evidence was invalid."
    }
}

$file = Get-Item -LiteralPath $exePath
$trayFile = Get-Item -LiteralPath $trayExePath
$manifest = [ordered]@{
    schema_version = 1
    created_utc = [DateTime]::UtcNow.ToString("o")
    artifact = $file.Name
    size_bytes = $file.Length
    sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $exePath).Hash
    tray_artifact = $trayFile.Name
    tray_size_bytes = $trayFile.Length
    tray_sha256 = (
        Get-FileHash -Algorithm SHA256 -LiteralPath $trayExePath
    ).Hash
    launcher_sha256 = (
        Get-FileHash `
            -Algorithm SHA256 `
            -LiteralPath (Join-Path $distPath "Start-LanAssist.ps1")
    ).Hash
    connector_sha256 = (
        Get-FileHash `
            -Algorithm SHA256 `
            -LiteralPath (Join-Path $distPath "Connect-LanHelperShare.ps1")
    ).Hash
    coordinator_launcher_sha256 = (
        Get-FileHash `
            -Algorithm SHA256 `
            -LiteralPath (Join-Path $distPath "Start-Coordinator.ps1")
    ).Hash
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
    tray_signed = (
        (Get-AuthenticodeSignature -LiteralPath $trayExePath).Status -eq "Valid"
    )
}
$manifestPath = Join-Path $distPath "build-manifest.json"
$manifest | ConvertTo-Json -Depth 4 |
    Set-Content -LiteralPath $manifestPath -Encoding UTF8

[pscustomobject]@{
    Executable = $file.FullName
    SizeBytes = $file.Length
    SHA256 = $manifest.sha256
    TrayExecutable = $trayFile.FullName
    TraySizeBytes = $trayFile.Length
    TraySHA256 = $manifest.tray_sha256
    Manifest = $manifestPath
    Signed = $manifest.signed
} | Format-List
