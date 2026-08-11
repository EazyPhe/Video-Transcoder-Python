[CmdletBinding()]
param(
    [ValidateRange(5, 300)]
    [int]$RetrySeconds = 30
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$expectedHost = "HOT-BOX"
$deploymentRoot = "C:\VideoTranscoderHotBox"
$helperLauncher = Join-Path $deploymentRoot "Start-LanAssist.ps1"
$helperConfig = Join-Path $deploymentRoot "VideoTranscoderLanAssist.system.json"
$helperExecutable = Join-Path $deploymentRoot "VideoTranscoderLanAssist.exe"
$shareRoot = "\\INSPIRON\Inspiron"
$stagingRoot = (
    "\\INSPIRON\Inspiron\Development\VideoTranscoderToolchain\" +
    "jobs\stuff-distributed\staging"
)
$shareUser = "INSPIRON\TranscodeWorker"
$supervisorStatus = Join-Path $deploymentRoot "main-worker-supervisor.json"
$tunnelOwnerRecord = Join-Path $deploymentRoot "lan-tunnel-owner.json"
$tunnelLocalAddress = "127.0.0.1"
$tunnelLocalPort = 41801
$tunnelRemoteAddress = "127.0.0.1"
$tunnelRemotePort = 41800
$tunnelSignature = (
    "-L 127.0.0.1:41801:127.0.0.1:41800 codex-remote"
)
$tunnelCommand = [string[]]@(
    "ssh",
    "-n",
    "-N",
    "-T",
    "-o", "BatchMode=yes",
    "-o", "PreferredAuthentications=publickey",
    "-o", "PasswordAuthentication=no",
    "-o", "KbdInteractiveAuthentication=no",
    "-o", "NumberOfPasswordPrompts=0",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "ConnectionAttempts=1",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
    "-o", "ConnectTimeout=10",
    "-L", "127.0.0.1:41801:127.0.0.1:41800",
    "codex-remote"
)
$tunnelCommandBytes = [Text.UTF8Encoding]::new($false).GetBytes(
    [string]::Join(([string][char]0), $tunnelCommand)
)
$tunnelCommandHasher = [Security.Cryptography.SHA256]::Create()
try {
    $expectedTunnelCommandSha256 = [BitConverter]::ToString(
        $tunnelCommandHasher.ComputeHash($tunnelCommandBytes)
    ).Replace("-", "")
} finally {
    $tunnelCommandHasher.Dispose()
}

if ($env:COMPUTERNAME -ine $expectedHost) {
    throw "This supervisor is restricted to HOT-BOX."
}
if (-not (Test-Path -LiteralPath $helperLauncher -PathType Leaf)) {
    throw "The deployed LAN helper launcher is missing."
}
if (-not (Test-Path -LiteralPath $helperConfig -PathType Leaf)) {
    throw "The SYSTEM LAN helper configuration is missing."
}

function Write-SupervisorStatus {
    param(
        [string]$Status,
        [string]$Category,
        [int]$HelperExitCode = 0
    )

    $payload = [ordered]@{
        SchemaVersion = 1
        Event = "HotBoxMainWorkerSupervisor"
        Status = $Status
        Category = $Category
        HelperExitCode = $HelperExitCode
        UpdatedUtc = [DateTime]::UtcNow.ToString("o")
    }
    $temporary = "$supervisorStatus.tmp"
    [IO.File]::WriteAllText(
        $temporary,
        ($payload | ConvertTo-Json -Compress),
        [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporary -Destination $supervisorStatus -Force
}

function Get-TunnelPortListeners {
    return @(
        Get-NetTCPConnection `
            -State Listen `
            -ErrorAction Stop |
            Where-Object {
                [int]$_.LocalPort -eq $tunnelLocalPort
            }
    )
}

function Get-LiveCurrentHelpers {
    return @(
        Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object {
                [string]$_.Name -ieq "VideoTranscoderLanAssist.exe" -and
                [string]::Equals(
                    [string]$_.ExecutablePath,
                    $helperExecutable,
                    [StringComparison]::OrdinalIgnoreCase
                )
            }
    )
}

function Get-HeldTunnelProcess {
    param([int]$ProcessId)

    try {
        return [Diagnostics.Process]::GetProcessById($ProcessId)
    } catch [ArgumentException] {
        return
    } catch [Management.Automation.MethodInvocationException] {
        if ($_.Exception.InnerException -is [ArgumentException]) {
            return
        }
        throw
    }
}

function Get-TunnelOwnerRecordPresence {
    try {
        $entry = Get-Item `
            -LiteralPath $tunnelOwnerRecord `
            -Force `
            -ErrorAction Stop
        if ($null -eq $entry) {
            return "Unknown"
        }
        return "Present"
    } catch [Management.Automation.ItemNotFoundException] {
        return "Absent"
    } catch {
        return "Unknown"
    }
}

function Test-JsonInteger {
    param(
        [object]$Value,
        [long]$Minimum,
        [long]$Maximum
    )

    if ($null -eq $Value -or $Value -is [bool]) {
        return $false
    }
    $valueType = $Value.GetType()
    if ($valueType -notin @([int], [long])) {
        return $false
    }
    $number = [long]$Value
    return $number -ge $Minimum -and $number -le $Maximum
}

function Get-ByteSha256 {
    param([byte[]]$Bytes)

    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString(
            $hasher.ComputeHash($Bytes)
        ).Replace("-", "")
    } finally {
        $hasher.Dispose()
    }
}

function Read-ValidatedTunnelOwnerRecord {
    try {
        if (-not (Test-Path -LiteralPath $tunnelOwnerRecord -PathType Leaf)) {
            return $null
        }
        $item = Get-Item -LiteralPath $tunnelOwnerRecord -Force
        if (
            $item.PSIsContainer -or
            ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            $item.Length -le 0 -or
            $item.Length -gt 4KB
        ) {
            return $null
        }
        $bytes = [IO.File]::ReadAllBytes($tunnelOwnerRecord)
        if ($bytes.Length -ne $item.Length -or $bytes.Length -gt 4KB) {
            return $null
        }
        $text = [Text.UTF8Encoding]::new($false, $true).GetString($bytes)
        $record = $text | ConvertFrom-Json -ErrorAction Stop
    } catch {
        return $null
    }

    if ($null -eq $record -or $record -isnot [pscustomobject]) {
        return $null
    }

    $expectedKeys = [string[]]@(
        "schema_version",
        "event",
        "ssh_pid",
        "helper_pid",
        "ssh_creation_filetime",
        "local_address",
        "local_port",
        "remote_address",
        "remote_port",
        "command_sha256"
    )
    $actualKeys = @($record.PSObject.Properties.Name)
    if (
        $actualKeys.Count -ne $expectedKeys.Count -or
        @($actualKeys | Where-Object { $_ -cnotin $expectedKeys }).Count -ne 0
    ) {
        return $null
    }
    foreach ($key in $expectedKeys) {
        $keyPattern = '"{0}"\s*:' -f [regex]::Escape($key)
        if ([regex]::Matches($text, $keyPattern).Count -ne 1) {
            return $null
        }
    }

    if (
        -not (Test-JsonInteger $record.schema_version 1 1) -or
        $record.event -isnot [string] -or
        [string]$record.event -cne "LanTunnelOwner" -or
        -not (Test-JsonInteger $record.ssh_pid 1 ([int]::MaxValue)) -or
        -not (Test-JsonInteger $record.helper_pid 1 ([int]::MaxValue)) -or
        [int]$record.ssh_pid -eq [int]$record.helper_pid -or
        -not (Test-JsonInteger `
            $record.ssh_creation_filetime `
            1 `
            ([long]::MaxValue)) -or
        $record.local_address -isnot [string] -or
        [string]$record.local_address -cne $tunnelLocalAddress -or
        -not (Test-JsonInteger `
            $record.local_port `
            $tunnelLocalPort `
            $tunnelLocalPort) -or
        $record.remote_address -isnot [string] -or
        [string]$record.remote_address -cne $tunnelRemoteAddress -or
        -not (Test-JsonInteger `
            $record.remote_port `
            $tunnelRemotePort `
            $tunnelRemotePort) -or
        $record.command_sha256 -isnot [string] -or
        [string]$record.command_sha256 -cnotmatch "^[0-9A-F]{64}$" -or
        [string]$record.command_sha256 -cne $expectedTunnelCommandSha256
    ) {
        return $null
    }

    return [pscustomobject]@{
        Value = $record
        ContentSha256 = Get-ByteSha256 $bytes
        SshProcessId = [int]$record.ssh_pid
        HelperProcessId = [int]$record.helper_pid
        SshCreationFiletime = [long]$record.ssh_creation_filetime
    }
}

function Test-SameTunnelOwnerRecord {
    param(
        [object]$First,
        [object]$Second
    )

    return (
        $null -ne $First -and
        $null -ne $Second -and
        [string]$First.ContentSha256 -ceq [string]$Second.ContentSha256 -and
        [int]$First.SshProcessId -eq [int]$Second.SshProcessId -and
        [int]$First.HelperProcessId -eq [int]$Second.HelperProcessId -and
        [long]$First.SshCreationFiletime -eq
            [long]$Second.SshCreationFiletime
    )
}

function Test-HeldTunnelOwner {
    param(
        [object]$HeldProcess,
        [object]$OwnerRecord
    )

    if ($null -eq $HeldProcess -or $null -eq $OwnerRecord) {
        return $false
    }
    try {
        [void]$HeldProcess.Refresh()
        if ($HeldProcess.HasExited) {
            return $false
        }
        $heldId = [int]$HeldProcess.Id
        $heldPath = [string]$HeldProcess.Path
        $heldStartFiletime = $HeldProcess.StartTime.
            ToUniversalTime().ToFileTimeUtc()
    } catch {
        return $false
    }

    return (
        $heldId -eq [int]$OwnerRecord.SshProcessId -and
        $heldPath.EndsWith(
            "\ssh.exe",
            [StringComparison]::OrdinalIgnoreCase
        ) -and
        [IO.Path]::GetFileName($heldPath) -ieq "ssh.exe" -and
        $heldStartFiletime -eq [long]$OwnerRecord.SshCreationFiletime
    )
}

function Test-RecordedTunnelProcessGone {
    param([object]$OwnerRecord)

    if ($null -eq $OwnerRecord) {
        return $false
    }
    $candidate = $null
    try {
        $candidate = Get-HeldTunnelProcess $OwnerRecord.SshProcessId
        if ($null -eq $candidate) {
            return $true
        }
        [void]$candidate.Refresh()
        if ([int]$candidate.Id -ne [int]$OwnerRecord.SshProcessId) {
            return $false
        }
        if ($candidate.HasExited) {
            return $true
        }
        $candidateStartFiletime = $candidate.StartTime.
            ToUniversalTime().ToFileTimeUtc()
        return (
            $candidateStartFiletime -ne
                [long]$OwnerRecord.SshCreationFiletime
        )
    } catch {
        return $false
    } finally {
        if ($null -ne $candidate) {
            $candidate.Dispose()
        }
    }
}

function Test-CimTunnelOwner {
    param(
        [object]$CimProcess,
        [object]$OwnerRecord
    )

    if (
        $null -eq $CimProcess -or
        $null -eq $OwnerRecord -or
        [int]$CimProcess.ProcessId -ne [int]$OwnerRecord.SshProcessId -or
        [string]$CimProcess.Name -ine "ssh.exe" -or
        [int]$CimProcess.ParentProcessId -ne
            [int]$OwnerRecord.HelperProcessId -or
        ([string]$CimProcess.CommandLine).IndexOf(
            $tunnelSignature,
            [StringComparison]::Ordinal
        ) -lt 0
    ) {
        return $false
    }
    try {
        $cimFiletime = ([DateTime]$CimProcess.CreationDate).
            ToUniversalTime().ToFileTimeUtc()
    } catch {
        return $false
    }
    return [Math]::Abs(
        $cimFiletime - [long]$OwnerRecord.SshCreationFiletime
    ) -le 10
}

function Remove-VerifiedTunnelOwnerRecord {
    param([object]$OwnerRecord)

    try {
        $listenersBeforeRemoval = @(Get-TunnelPortListeners)
        $helpersBeforeRemoval = @(Get-LiveCurrentHelpers)
    } catch {
        Write-SupervisorStatus "Waiting" "TunnelOwnershipUnproven"
        return $false
    }
    if (
        $listenersBeforeRemoval.Count -ne 0 -or
        $helpersBeforeRemoval.Count -ne 0 -or
        -not (Test-RecordedTunnelProcessGone $OwnerRecord)
    ) {
        Write-SupervisorStatus "Waiting" "TunnelOwnershipUnproven"
        return $false
    }

    $recordBeforeRemoval = Read-ValidatedTunnelOwnerRecord
    if (-not (Test-SameTunnelOwnerRecord $OwnerRecord $recordBeforeRemoval)) {
        Write-SupervisorStatus "Waiting" "TunnelOwnerRecordChanged"
        return $false
    }
    try {
        Remove-Item `
            -LiteralPath $tunnelOwnerRecord `
            -Force `
            -ErrorAction Stop
    } catch {
        Write-SupervisorStatus "Waiting" "StaleTunnelCleanupFailed"
        return $false
    }
    if ((Get-TunnelOwnerRecordPresence) -ne "Absent") {
        Write-SupervisorStatus "Waiting" "StaleTunnelCleanupFailed"
        return $false
    }
    return $true
}

function Clear-StaleHelperTunnel {
    try {
        $listeners = @(Get-TunnelPortListeners)
    } catch {
        Write-SupervisorStatus "Waiting" "TunnelOwnershipUnproven"
        return $false
    }
    if ($listeners.Count -eq 0) {
        $recordPresence = Get-TunnelOwnerRecordPresence
        if ($recordPresence -eq "Absent") {
            return $true
        }
        if ($recordPresence -ne "Present") {
            Write-SupervisorStatus "Waiting" "TunnelOwnershipUnproven"
            return $false
        }

        $ownerRecord = Read-ValidatedTunnelOwnerRecord
        if ($null -eq $ownerRecord) {
            Write-SupervisorStatus "Waiting" "TunnelOwnershipUnproven"
            return $false
        }
        try {
            $liveHelpers = @(Get-LiveCurrentHelpers)
        } catch {
            Write-SupervisorStatus "Waiting" "TunnelOwnershipUnproven"
            return $false
        }
        if ($liveHelpers.Count -ne 0) {
            Write-SupervisorStatus "Waiting" "HelperTunnelActive"
            return $false
        }
        if (-not (Test-RecordedTunnelProcessGone $ownerRecord)) {
            Write-SupervisorStatus "Waiting" "TunnelOwnershipUnproven"
            return $false
        }

        try {
            $confirmedListeners = @(Get-TunnelPortListeners)
            $confirmedHelpers = @(Get-LiveCurrentHelpers)
        } catch {
            Write-SupervisorStatus "Waiting" "TunnelOwnershipUnproven"
            return $false
        }
        $confirmedRecord = Read-ValidatedTunnelOwnerRecord
        if (
            $confirmedListeners.Count -ne 0 -or
            $confirmedHelpers.Count -ne 0 -or
            -not (Test-SameTunnelOwnerRecord `
                $ownerRecord `
                $confirmedRecord) -or
            -not (Test-RecordedTunnelProcessGone $confirmedRecord)
        ) {
            Write-SupervisorStatus "Waiting" "TunnelOwnershipUnproven"
            return $false
        }
        return Remove-VerifiedTunnelOwnerRecord $ownerRecord
    }
    if (
        $listeners.Count -ne 1 -or
        [string]$listeners[0].LocalAddress -cne $tunnelLocalAddress
    ) {
        Write-SupervisorStatus "Waiting" "TunnelOwnershipUnproven"
        return $false
    }

    $ownerRecord = Read-ValidatedTunnelOwnerRecord
    if (
        $null -eq $ownerRecord -or
        [int]$listeners[0].OwningProcess -ne $ownerRecord.SshProcessId
    ) {
        Write-SupervisorStatus "Waiting" "TunnelOwnershipUnproven"
        return $false
    }

    try {
        $liveHelpers = @(Get-LiveCurrentHelpers)
    } catch {
        Write-SupervisorStatus "Waiting" "TunnelOwnershipUnproven"
        return $false
    }
    if ($liveHelpers.Count -ne 0) {
        Write-SupervisorStatus "Waiting" "HelperTunnelActive"
        return $false
    }

    try {
        $ownerCim = @(
            Get-CimInstance `
                Win32_Process `
                -Filter ("ProcessId = {0}" -f $ownerRecord.SshProcessId) `
                -ErrorAction Stop
        )
    } catch {
        Write-SupervisorStatus "Waiting" "TunnelOwnershipUnproven"
        return $false
    }
    if (
        $ownerCim.Count -ne 1 -or
        -not (Test-CimTunnelOwner $ownerCim[0] $ownerRecord)
    ) {
        Write-SupervisorStatus "Waiting" "TunnelOwnershipUnproven"
        return $false
    }

    $heldProcess = $null
    try {
        $heldProcess = Get-HeldTunnelProcess $ownerRecord.SshProcessId
    } catch {
        if ($null -ne $heldProcess) {
            $heldProcess.Dispose()
        }
        Write-SupervisorStatus "Waiting" "TunnelOwnershipUnproven"
        return $false
    }
    try {
        if (-not (Test-HeldTunnelOwner $heldProcess $ownerRecord)) {
            Write-SupervisorStatus "Waiting" "TunnelOwnershipUnproven"
            return $false
        }

        try {
            $confirmedRecord = Read-ValidatedTunnelOwnerRecord
            $confirmedListeners = @(Get-TunnelPortListeners)
            $confirmedHelpers = @(Get-LiveCurrentHelpers)
            $confirmedCim = @(
                Get-CimInstance `
                    Win32_Process `
                    -Filter (
                        "ProcessId = {0}" -f $ownerRecord.SshProcessId
                    ) `
                    -ErrorAction Stop
            )
        } catch {
            Write-SupervisorStatus "Waiting" "TunnelOwnershipUnproven"
            return $false
        }
        if (
            -not (Test-SameTunnelOwnerRecord `
                $ownerRecord `
                $confirmedRecord) -or
            $confirmedListeners.Count -ne 1 -or
            [string]$confirmedListeners[0].LocalAddress -cne
                $tunnelLocalAddress -or
            [int]$confirmedListeners[0].OwningProcess -ne
                $ownerRecord.SshProcessId -or
            $confirmedHelpers.Count -ne 0 -or
            $confirmedCim.Count -ne 1 -or
            -not (Test-CimTunnelOwner `
                $confirmedCim[0] `
                $confirmedRecord) -or
            -not (Test-HeldTunnelOwner $heldProcess $confirmedRecord)
        ) {
            Write-SupervisorStatus "Waiting" "TunnelOwnershipUnproven"
            return $false
        }

        try {
            $heldProcess.Kill()
            if (-not $heldProcess.WaitForExit(5000)) {
                Write-SupervisorStatus "Waiting" "StaleTunnelCleanupFailed"
                return $false
            }
        } catch {
            Write-SupervisorStatus "Waiting" "StaleTunnelCleanupFailed"
            return $false
        }

        try {
            for ($attempt = 1; $attempt -le 20; $attempt++) {
                if (@(Get-TunnelPortListeners).Count -eq 0) {
                    break
                }
                Start-Sleep -Milliseconds 250
            }
            $remainingListeners = @(Get-TunnelPortListeners)
        } catch {
            Write-SupervisorStatus "Waiting" "StaleTunnelCleanupFailed"
            return $false
        }
        if ($remainingListeners.Count -ne 0) {
            Write-SupervisorStatus "Waiting" "StaleTunnelCleanupFailed"
            return $false
        }
        return Remove-VerifiedTunnelOwnerRecord $ownerRecord
    } finally {
        $heldProcess.Dispose()
    }
}

function Test-ShareSession {
    $lines = @(& net.exe use 2>$null)
    if ($LASTEXITCODE -ne 0) {
        return $false
    }
    $pattern = "(?i)^\s*OK\s+(?:[A-Za-z]:\s+)?" +
        [regex]::Escape($shareRoot) + "(?=\s|$)"
    return $null -ne ($lines | Where-Object { [string]$_ -match $pattern })
}

function Connect-WorkerShare {
    if (Test-ShareSession) {
        try {
            [void][IO.File]::GetAttributes($stagingRoot)
            return $true
        } catch {}
    }

    & net.exe use $shareRoot "/delete" "/y" 1>$null 2>$null
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = (Get-Command cmd.exe -ErrorAction Stop).Source
    $startInfo.Arguments = (
        '/d /c net use "{0}" "" /user:"{1}" /persistent:no' -f
            $shareRoot,
            $shareUser
    )
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $connectionProcess = [Diagnostics.Process]::new()
    $connectionProcess.StartInfo = $startInfo
    try {
        if (-not $connectionProcess.Start()) {
            return $false
        }
        [void]$connectionProcess.StandardOutput.ReadToEnd()
        [void]$connectionProcess.StandardError.ReadToEnd()
        $connectionProcess.WaitForExit()
        $connectionExit = $connectionProcess.ExitCode
    } finally {
        $connectionProcess.Dispose()
    }
    if ($connectionExit -ne 0) {
        return $false
    }
    try {
        [void][IO.File]::GetAttributes($stagingRoot)
        return $true
    } catch {
        return $false
    }
}

while ($true) {
    try {
        if (-not (Connect-WorkerShare)) {
            Write-SupervisorStatus "Waiting" "SmbConnectionUnavailable"
            Start-Sleep -Seconds $RetrySeconds
            continue
        }
        if (-not (Clear-StaleHelperTunnel)) {
            Start-Sleep -Seconds $RetrySeconds
            continue
        }

        Write-SupervisorStatus "Starting" ""
        & $helperLauncher -ConfigPath $helperConfig
        $helperExit = if ($null -eq $LASTEXITCODE) { 1 } else { $LASTEXITCODE }
        Write-SupervisorStatus "Waiting" "HelperExited" $helperExit
    } catch {
        Write-SupervisorStatus "Waiting" $_.Exception.GetType().Name 1
    }
    Start-Sleep -Seconds $RetrySeconds
}
