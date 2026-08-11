[CmdletBinding()]
param(
    [string]$ConfigPath = "",

    [string]$UserName = "",

    [ValidateRange(1, 30)]
    [int]$SshTimeoutSeconds = 8,

    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-ConnectionConfiguration {
    param([string]$Path)

    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    $file = Get-Item -LiteralPath $resolved -ErrorAction Stop
    if ($file.Length -gt 1MB) {
        throw "LAN Assist configuration is unexpectedly large."
    }
    $config = Get-Content -LiteralPath $resolved -Raw |
        ConvertFrom-Json -ErrorAction Stop
    if (
        $config.schema_version -ne 1 -or
        $config.mode -ne "helper" -or
        [string]::IsNullOrWhiteSpace([string]$config.ssh_destination) -or
        [string]::IsNullOrWhiteSpace([string]$config.staging_root)
    ) {
        throw "LAN Assist helper configuration is invalid."
    }
    if (
        [string]$config.ssh_destination -match '^-' -or
        [string]$config.ssh_destination -match '[\s\x00-\x1f]'
    ) {
        throw "LAN Assist SSH destination is invalid."
    }
    $staging = [string]$config.staging_root
    if ($staging -notmatch '^(\\\\([^\\]+)\\[^\\]+)(?:\\|$)') {
        throw "LAN Assist staging root must be a UNC path."
    }
    $shareRoot = $Matches[1]
    $serverName = $Matches[2]
    $sshExecutable = if (
        $config.PSObject.Properties.Name -notcontains "ssh_executable" -or
        [string]::IsNullOrWhiteSpace([string]$config.ssh_executable)
    ) {
        "ssh"
    } else {
        [string]$config.ssh_executable
    }
    [pscustomobject]@{
        ConfigPath = $resolved
        StagingRoot = $staging
        ShareRoot = $shareRoot
        ServerName = $serverName
        SshDestination = [string]$config.ssh_destination
        SshExecutable = $sshExecutable
    }
}

function Get-ShareConnectionRecord {
    param(
        [string]$ShareRoot,
        [AllowNull()]
        [string[]]$Lines = $null
    )

    if ($null -eq $Lines) {
        $Lines = @(& net.exe use 2>$null)
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to inspect existing SMB sessions safely."
        }
    }
    $sharePattern = [regex]::Escape($ShareRoot.TrimEnd("\"))
    $pattern = (
        '^\s*(?<status>\S+)\s+' +
        '(?:(?<local>[A-Za-z]:)\s+)?' +
        '(?<remote>' + $sharePattern + ')(?=\s|$)'
    )
    foreach ($line in $Lines) {
        $match = [regex]::Match(
            [string]$line,
            $pattern,
            [Text.RegularExpressions.RegexOptions]::IgnoreCase
        )
        if ($match.Success) {
            return [pscustomobject]@{
                Status = $match.Groups["status"].Value
                RemotePath = $match.Groups["remote"].Value
            }
        }
    }
    return $null
}

function Get-ScopedCredentialTargets {
    param(
        [string]$ServerName,
        [AllowNull()]
        [string[]]$Lines = $null
    )

    if ($null -eq $Lines) {
        $Lines = @(& cmdkey.exe /list 2>$null)
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to inspect scoped Windows credentials safely."
        }
    }
    $targets = @()
    foreach ($line in $Lines) {
        $match = [regex]::Match(
            [string]$line,
            '^\s*Target:\s*(?<target>.+?)\s*$',
            [Text.RegularExpressions.RegexOptions]::IgnoreCase
        )
        if (-not $match.Success) {
            continue
        }
        $target = $match.Groups["target"].Value
        $identity = $target
        $marker = $target.IndexOf(
            ":target=",
            [StringComparison]::OrdinalIgnoreCase
        )
        if ($marker -ge 0) {
            $identity = $target.Substring($marker + 8)
        }
        if (
            $identity -ieq $ServerName -or
            $identity -ieq ("cifs/{0}" -f $ServerName)
        ) {
            $targets += $target
        }
    }
    return @($targets)
}

function Test-ExistingShareConnection {
    param([string]$ShareRoot)

    $record = Get-ShareConnectionRecord $ShareRoot
    return (
        $null -ne $record -and
        [string]$record.Status -eq "OK" -and
        [string]$record.RemotePath -ieq $ShareRoot.TrimEnd("\")
    )
}

function Remove-ScopedConnection {
    param(
        [string]$ShareRoot,
        [string]$ServerName
    )

    $record = Get-ShareConnectionRecord $ShareRoot
    if ($null -ne $record) {
        & net.exe use $ShareRoot "/delete" "/y" 1>$null 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to remove the scoped SMB session safely."
        }
    }
    if ($null -ne (Get-ShareConnectionRecord $ShareRoot)) {
        throw "The scoped SMB session is still present after cleanup."
    }
    foreach ($target in @(Get-ScopedCredentialTargets $ServerName)) {
        & cmdkey.exe "/delete:$target" 1>$null 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to remove a scoped Windows credential safely."
        }
    }
    if (@(Get-ScopedCredentialTargets $ServerName).Count -ne 0) {
        throw "A scoped Windows credential is still present after cleanup."
    }
}

function Test-SshIdentity {
    param(
        [string]$Executable,
        [string]$Destination,
        [int]$TimeoutSeconds
    )

    $command = Get-Command $Executable -ErrorAction SilentlyContinue
    if (-not $command) {
        return $false
    }
    $arguments = @(
        "-n", "-T",
        "-o", "BatchMode=yes",
        "-o", "PreferredAuthentications=publickey",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "NumberOfPasswordPrompts=0",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectionAttempts=1",
        "-o", "ConnectTimeout=$TimeoutSeconds",
        $Destination,
        "cmd.exe", "/d", "/c", "exit", "0"
    )
    & $command.Source @arguments 1>$null 2>$null
    return $LASTEXITCODE -eq 0
}

function New-NonPersistentConnection {
    param(
        [string]$ShareRoot,
        [Management.Automation.PSCredential]$Credential
    )

    if (
        $ShareRoot -match '["\x00-\x1f]' -or
        $Credential.UserName -match '["\x00-\x1f]'
    ) {
        throw "SMB connection identity is invalid."
    }
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = (Get-Command net.exe -ErrorAction Stop).Source
    $startInfo.Arguments = (
        'use "{0}" * /user:"{1}" /persistent:no' -f
            $ShareRoot,
            $Credential.UserName
    )
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    $bstr = [IntPtr]::Zero
    try {
        if (-not $process.Start()) {
            throw "Unable to start the scoped SMB connection process."
        }
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
            $Credential.Password
        )
        for ($index = 0; $index -lt $Credential.Password.Length; $index++) {
            $character = [Runtime.InteropServices.Marshal]::ReadInt16(
                $bstr,
                $index * 2
            )
            $process.StandardInput.Write([char]$character)
        }
        $process.StandardInput.WriteLine()
        $process.StandardInput.Close()
        [void]$process.StandardOutput.ReadToEnd()
        [void]$process.StandardError.ReadToEnd()
        $process.WaitForExit()
        return $process.ExitCode -eq 0
    } finally {
        if ($bstr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
        $process.Dispose()
    }
}

$defaultConfigPath = Join-Path $PSScriptRoot "VideoTranscoderLanAssist.json"
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = $defaultConfigPath
}
$connection = Get-ConnectionConfiguration $ConfigPath

if ($ValidateOnly) {
    [ordered]@{
        Event = "TravelSafeConnectorValidated"
        Status = "Ready"
        Mode = "helper"
    } | ConvertTo-Json -Compress
    exit 0
}

if (-not (Test-SshIdentity `
    $connection.SshExecutable `
    $connection.SshDestination `
    $SshTimeoutSeconds
)) {
    [ordered]@{
        Event = "ShareConnection"
        Status = "NotStarted"
        Reason = "HomeNetworkUnavailable"
    } | ConvertTo-Json -Compress
    exit 2
}

$existingRecord = Get-ShareConnectionRecord $connection.ShareRoot
if (
    $null -ne $existingRecord -and
    [string]$existingRecord.Status -eq "OK"
) {
    try {
        [void][IO.File]::GetAttributes($connection.StagingRoot)
        [ordered]@{
            Event = "ShareConnection"
            Status = "Ready"
            Reason = "ExistingSessionVerified"
        } | ConvertTo-Json -Compress
        exit 0
    } catch {}
}

# Never layer an explicit attempt over a stale session or saved credential.
Remove-ScopedConnection $connection.ShareRoot $connection.ServerName

$credentialParameters = @{
    Message = "Enter the account password, not the Windows Hello PIN"
}
if (-not [string]::IsNullOrWhiteSpace($UserName)) {
    $credentialParameters.UserName = $UserName
}
$credential = Get-Credential @credentialParameters
if ($null -eq $credential) {
    [ordered]@{
        Event = "ShareConnection"
        Status = "NotStarted"
        Reason = "CredentialPromptCancelled"
    } | ConvertTo-Json -Compress
    exit 2
}

$connected = New-NonPersistentConnection `
    $connection.ShareRoot `
    $credential
if (-not $connected) {
    Remove-ScopedConnection $connection.ShareRoot $connection.ServerName
    [ordered]@{
        Event = "ShareConnection"
        Status = "Blocked"
        Reason = "AuthenticationRejected"
    } | ConvertTo-Json -Compress
    exit 3
}

$connectedRecord = Get-ShareConnectionRecord $connection.ShareRoot
if (
    $null -eq $connectedRecord -or
    [string]$connectedRecord.Status -ne "OK"
) {
    Remove-ScopedConnection $connection.ShareRoot $connection.ServerName
    [ordered]@{
        Event = "ShareConnection"
        Status = "Blocked"
        Reason = "SessionNotReady"
    } | ConvertTo-Json -Compress
    exit 3
}

try {
    [void][IO.File]::GetAttributes($connection.StagingRoot)
} catch {
    Remove-ScopedConnection $connection.ShareRoot $connection.ServerName
    [ordered]@{
        Event = "ShareConnection"
        Status = "Blocked"
        Reason = "StagingAccessRejected"
    } | ConvertTo-Json -Compress
    exit 3
}

[ordered]@{
    Event = "ShareConnection"
    Status = "Ready"
    Reason = "AuthenticatedSessionVerified"
} | ConvertTo-Json -Compress
exit 0
