<#
.SYNOPSIS
Stops one verified legacy batch runner at a transaction-free boundary.

.DESCRIPTION
Reads ProcessId and ProcessStartUtc from the coordinator state, retains a
kernel handle to defeat PID reuse, pins and hashes the runner script, and
requires an exact PowerShell -File invocation with -Mode Batch.

The runner is suspended for every journal observation. A stop is attempted
only after this invocation observes active-transaction.json present and then
absent. The root stays suspended while its descendants are frozen to a stable
fixed point and the journal is checked again. A reappearing journal is
preserved and the process tree is resumed.

Only a StoppedAtBoundary result with SafeToStartReplacement=true authorizes a
replacement runner. Every other result fails closed. ResourceProofPath accepts
one or more regular lock or lease files. Every supplied file must be reported
as held by the retained target-tree identity before stopping, must remain
present, and must be exclusively openable after the exact process tree exits.

.PARAMETER TimeoutSeconds
Zero waits indefinitely. A nonzero timeout returns TimedOut without stopping
the target.

.OUTPUTS
One compact JSON status object. No process command line or configured path is
included in output.

.NOTES
Exit codes: 0 stopped safely; 2 recovery journal reappeared; 3 stopped but a
recovery journal remains; 4 timed out; 5 target exited before a proven
boundary or another unsafe nonexception outcome; 10 validation or operational
failure requiring manual review.
#>
[CmdletBinding()]
param(
    [string]$StatePath,
    [string]$JournalPath,
    [string]$ExpectedRunnerPath,
    [string]$ExpectedRunnerSha256,
    [string]$ExpectedHostPath,
    [string[]]$ResourceProofPath = @(),
    [ValidateRange(500, 1000)]
    [int]$PollMilliseconds = 750,
    [ValidateRange(0, 604800)]
    [int]$TimeoutSeconds = 0
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
Set-StrictMode -Version Latest

if (-not ("LegacyHandoffNative" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

public sealed class LegacyHandoffProcessRecord
{
    public int ProcessId { get; private set; }
    public int ParentProcessId { get; private set; }

    internal LegacyHandoffProcessRecord(int processId, int parentProcessId)
    {
        ProcessId = processId;
        ParentProcessId = parentProcessId;
    }
}

public sealed class LegacyHandoffProcessIdentity
{
    public int ProcessId { get; private set; }
    public long CreationFileTime { get; private set; }

    internal LegacyHandoffProcessIdentity(
        int processId,
        long creationFileTime)
    {
        ProcessId = processId;
        CreationFileTime = creationFileTime;
    }
}

public sealed class LegacyHandoffProcessHandle : IDisposable
{
    internal IntPtr Handle;
    public int ProcessId { get; private set; }
    public long CreationFileTime { get; private set; }
    public bool IsSuspended { get; internal set; }

    internal LegacyHandoffProcessHandle(
        IntPtr handle,
        int processId,
        long creationFileTime)
    {
        Handle = handle;
        ProcessId = processId;
        CreationFileTime = creationFileTime;
    }

    public void Dispose()
    {
        if (Handle != IntPtr.Zero)
        {
            LegacyHandoffNative.CloseHandle(Handle);
            Handle = IntPtr.Zero;
        }
    }
}

public static class LegacyHandoffNative
{
    private const uint PROCESS_TERMINATE = 0x0001;
    private const uint PROCESS_SUSPEND_RESUME = 0x0800;
    private const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
    private const uint SYNCHRONIZE = 0x00100000;
    private const uint TH32CS_SNAPPROCESS = 0x00000002;
    private const uint WAIT_OBJECT_0 = 0x00000000;
    private const uint WAIT_TIMEOUT = 0x00000102;
    private const uint INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF;
    private const int ERROR_FILE_NOT_FOUND = 2;
    private const int ERROR_PATH_NOT_FOUND = 3;
    private const int ERROR_NO_MORE_FILES = 18;
    private const int ERROR_MORE_DATA = 234;
    private const int CCH_RM_SESSION_KEY = 32;
    private static readonly IntPtr INVALID_HANDLE_VALUE = new IntPtr(-1);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct PROCESSENTRY32
    {
        public uint dwSize;
        public uint cntUsage;
        public uint th32ProcessID;
        public IntPtr th32DefaultHeapID;
        public uint th32ModuleID;
        public uint cntThreads;
        public uint th32ParentProcessID;
        public int pcPriClassBase;
        public uint dwFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
        public string szExeFile;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_BASIC_INFORMATION
    {
        public IntPtr Reserved1;
        public IntPtr PebBaseAddress;
        public IntPtr Reserved2_0;
        public IntPtr Reserved2_1;
        public IntPtr UniqueProcessId;
        public IntPtr InheritedFromUniqueProcessId;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct RM_FILETIME
    {
        public uint dwLowDateTime;
        public uint dwHighDateTime;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct RM_UNIQUE_PROCESS
    {
        public int dwProcessId;
        public RM_FILETIME ProcessStartTime;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct RM_PROCESS_INFO
    {
        public RM_UNIQUE_PROCESS Process;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)]
        public string strAppName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)]
        public string strServiceShortName;
        public int ApplicationType;
        public uint AppStatus;
        public uint TSSessionId;
        [MarshalAs(UnmanagedType.Bool)]
        public bool bRestartable;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(
        uint dwDesiredAccess,
        bool bInheritHandle,
        uint dwProcessId);

    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern bool CloseHandle(IntPtr hObject);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetProcessTimes(
        IntPtr hProcess,
        out long creationTime,
        out long exitTime,
        out long kernelTime,
        out long userTime);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(
        IntPtr hHandle,
        uint dwMilliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(
        IntPtr hProcess,
        uint uExitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr CreateToolhelp32Snapshot(
        uint dwFlags,
        uint th32ProcessID);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool Process32FirstW(
        IntPtr hSnapshot,
        ref PROCESSENTRY32 lppe);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool Process32NextW(
        IntPtr hSnapshot,
        ref PROCESSENTRY32 lppe);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint GetFileAttributesW(string lpFileName);

    [DllImport("shell32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CommandLineToArgvW(
        string commandLine,
        out int argc);

    [DllImport("kernel32.dll")]
    private static extern IntPtr LocalFree(IntPtr hMem);

    [DllImport("ntdll.dll")]
    private static extern int NtSuspendProcess(IntPtr processHandle);

    [DllImport("ntdll.dll")]
    private static extern int NtResumeProcess(IntPtr processHandle);

    [DllImport("ntdll.dll")]
    private static extern int NtQueryInformationProcess(
        IntPtr processHandle,
        int processInformationClass,
        ref PROCESS_BASIC_INFORMATION processInformation,
        int processInformationLength,
        out int returnLength);

    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    private static extern int RmStartSession(
        out uint sessionHandle,
        int sessionFlags,
        StringBuilder sessionKey);

    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    private static extern int RmRegisterResources(
        uint sessionHandle,
        uint fileCount,
        string[] fileNames,
        uint applicationCount,
        RM_UNIQUE_PROCESS[] applications,
        uint serviceCount,
        string[] serviceNames);

    [DllImport("rstrtmgr.dll")]
    private static extern int RmGetList(
        uint sessionHandle,
        out uint processInfoNeeded,
        ref uint processInfoCount,
        [In, Out] RM_PROCESS_INFO[] affectedApps,
        ref uint rebootReasons);

    [DllImport("rstrtmgr.dll")]
    private static extern int RmEndSession(uint sessionHandle);

    public static LegacyHandoffProcessHandle Open(int processId)
    {
        uint access = PROCESS_TERMINATE |
            PROCESS_SUSPEND_RESUME |
            PROCESS_QUERY_LIMITED_INFORMATION |
            SYNCHRONIZE;
        IntPtr handle = OpenProcess(access, false, unchecked((uint)processId));
        if (handle == IntPtr.Zero)
        {
            throw new Win32Exception(Marshal.GetLastWin32Error());
        }

        long creation;
        long exit;
        long kernel;
        long user;
        if (!GetProcessTimes(handle, out creation, out exit, out kernel, out user))
        {
            int error = Marshal.GetLastWin32Error();
            CloseHandle(handle);
            throw new Win32Exception(error);
        }
        return new LegacyHandoffProcessHandle(
            handle,
            processId,
            creation);
    }

    public static bool IsExited(LegacyHandoffProcessHandle process)
    {
        uint result = WaitForSingleObject(process.Handle, 0);
        if (result == WAIT_OBJECT_0)
        {
            return true;
        }
        if (result == WAIT_TIMEOUT)
        {
            return false;
        }
        throw new Win32Exception(Marshal.GetLastWin32Error());
    }

    public static void Suspend(LegacyHandoffProcessHandle process)
    {
        if (process.IsSuspended)
        {
            throw new InvalidOperationException("Process is already suspended.");
        }
        if (IsExited(process))
        {
            throw new InvalidOperationException(
                "Process exited before it could be suspended.");
        }
        int status = NtSuspendProcess(process.Handle);
        if (status != 0)
        {
            throw new InvalidOperationException(
                "NtSuspendProcess failed with NTSTATUS 0x" +
                status.ToString("X8") + ".");
        }
        process.IsSuspended = true;
    }

    public static void Resume(LegacyHandoffProcessHandle process)
    {
        if (!process.IsSuspended)
        {
            return;
        }
        if (IsExited(process))
        {
            process.IsSuspended = false;
            return;
        }
        int status = NtResumeProcess(process.Handle);
        if (status != 0)
        {
            throw new InvalidOperationException(
                "NtResumeProcess failed with NTSTATUS 0x" +
                status.ToString("X8") + ".");
        }
        process.IsSuspended = false;
    }

    public static void Terminate(
        LegacyHandoffProcessHandle process,
        uint exitCode)
    {
        if (!IsExited(process) &&
            !TerminateProcess(process.Handle, exitCode))
        {
            throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        uint result = WaitForSingleObject(process.Handle, 10000);
        if (result == WAIT_TIMEOUT)
        {
            throw new TimeoutException("Process did not terminate.");
        }
        if (result != WAIT_OBJECT_0)
        {
            throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        process.IsSuspended = false;
    }

    public static bool PathExistsStrict(string path)
    {
        uint attributes = GetFileAttributesW(path);
        if (attributes != INVALID_FILE_ATTRIBUTES)
        {
            return true;
        }
        int error = Marshal.GetLastWin32Error();
        if (error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND)
        {
            return false;
        }
        throw new Win32Exception(error);
    }

    public static string[] ParseCommandLine(string commandLine)
    {
        int count;
        IntPtr argv = CommandLineToArgvW(commandLine, out count);
        if (argv == IntPtr.Zero)
        {
            throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        try
        {
            string[] result = new string[count];
            for (int index = 0; index < count; index++)
            {
                IntPtr item = Marshal.ReadIntPtr(
                    argv,
                    index * IntPtr.Size);
                result[index] = Marshal.PtrToStringUni(item);
            }
            return result;
        }
        finally
        {
            LocalFree(argv);
        }
    }

    public static int GetParentProcessId(
        LegacyHandoffProcessHandle process)
    {
        PROCESS_BASIC_INFORMATION information =
            new PROCESS_BASIC_INFORMATION();
        int returned;
        int status = NtQueryInformationProcess(
            process.Handle,
            0,
            ref information,
            Marshal.SizeOf(typeof(PROCESS_BASIC_INFORMATION)),
            out returned);
        if (status != 0)
        {
            throw new InvalidOperationException(
                "NtQueryInformationProcess failed with NTSTATUS 0x" +
                status.ToString("X8") + ".");
        }
        return unchecked((int)information.InheritedFromUniqueProcessId.ToInt64());
    }

    public static bool IsNoMoreFilesError(int error)
    {
        return error == ERROR_NO_MORE_FILES;
    }

    public static LegacyHandoffProcessRecord[] GetProcessSnapshot()
    {
        List<LegacyHandoffProcessRecord> records =
            new List<LegacyHandoffProcessRecord>();
        IntPtr snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if (snapshot == INVALID_HANDLE_VALUE)
        {
            throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        try
        {
            PROCESSENTRY32 entry = new PROCESSENTRY32();
            entry.dwSize =
                unchecked((uint)Marshal.SizeOf(typeof(PROCESSENTRY32)));
            if (!Process32FirstW(snapshot, ref entry))
            {
                int firstError = Marshal.GetLastWin32Error();
                if (firstError == ERROR_NO_MORE_FILES)
                {
                    return records.ToArray();
                }
                throw new Win32Exception(firstError);
            }
            while (true)
            {
                records.Add(new LegacyHandoffProcessRecord(
                    unchecked((int)entry.th32ProcessID),
                    unchecked((int)entry.th32ParentProcessID)));
                entry.dwSize =
                    unchecked((uint)Marshal.SizeOf(
                        typeof(PROCESSENTRY32)));
                if (Process32NextW(snapshot, ref entry))
                {
                    continue;
                }
                int nextError = Marshal.GetLastWin32Error();
                if (nextError != ERROR_NO_MORE_FILES)
                {
                    throw new Win32Exception(nextError);
                }
                break;
            }
        }
        finally
        {
            CloseHandle(snapshot);
        }

        return records.ToArray();
    }

    public static int[] GetDescendantProcessIds(int rootProcessId)
    {
        Dictionary<int, int> parents = new Dictionary<int, int>();
        foreach (LegacyHandoffProcessRecord record in GetProcessSnapshot())
        {
            parents[record.ProcessId] = record.ParentProcessId;
        }
        HashSet<int> descendants = new HashSet<int>();
        bool changed;
        do
        {
            changed = false;
            foreach (KeyValuePair<int, int> pair in parents)
            {
                if (pair.Key == rootProcessId ||
                    descendants.Contains(pair.Key))
                {
                    continue;
                }
                if (pair.Value == rootProcessId ||
                    descendants.Contains(pair.Value))
                {
                    descendants.Add(pair.Key);
                    changed = true;
                }
            }
        }
        while (changed);

        int[] result = new int[descendants.Count];
        descendants.CopyTo(result);
        return result;
    }

    public static LegacyHandoffProcessIdentity[] GetLockingProcesses(
        string path)
    {
        uint session;
        StringBuilder key = new StringBuilder(CCH_RM_SESSION_KEY + 1);
        int startResult = RmStartSession(out session, 0, key);
        if (startResult != 0)
        {
            throw new Win32Exception(startResult);
        }
        try
        {
            int registerResult = RmRegisterResources(
                session,
                1,
                new string[] { path },
                0,
                null,
                0,
                null);
            if (registerResult != 0)
            {
                throw new Win32Exception(registerResult);
            }

            uint needed = 0;
            uint count = 0;
            uint reasons = 0;
            int listResult = RmGetList(
                session,
                out needed,
                ref count,
                null,
                ref reasons);
            if (listResult == 0 && needed == 0)
            {
                return new LegacyHandoffProcessIdentity[0];
            }
            if (listResult != ERROR_MORE_DATA)
            {
                throw new Win32Exception(listResult);
            }

            RM_PROCESS_INFO[] processInfo =
                new RM_PROCESS_INFO[needed];
            count = needed;
            listResult = RmGetList(
                session,
                out needed,
                ref count,
                processInfo,
                ref reasons);
            if (listResult != 0)
            {
                throw new Win32Exception(listResult);
            }
            List<LegacyHandoffProcessIdentity> result =
                new List<LegacyHandoffProcessIdentity>();
            for (int index = 0; index < count; index++)
            {
                RM_UNIQUE_PROCESS process = processInfo[index].Process;
                long creationFileTime =
                    (unchecked((long)process.ProcessStartTime.dwHighDateTime)
                        << 32) |
                    process.ProcessStartTime.dwLowDateTime;
                result.Add(new LegacyHandoffProcessIdentity(
                    process.dwProcessId,
                    creationFileTime));
            }
            return result.ToArray();
        }
        finally
        {
            int endResult = RmEndSession(session);
            if (endResult != 0)
            {
                throw new Win32Exception(endResult);
            }
        }
    }
}
"@
}

function Get-FullPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "A required path was empty."
    }
    return [IO.Path]::GetFullPath($Path)
}

function Test-PathEqual {
    param(
        [Parameter(Mandatory = $true)][string]$Left,
        [Parameter(Mandatory = $true)][string]$Right
    )

    return [string]::Equals(
        (Get-FullPath -Path $Left),
        (Get-FullPath -Path $Right),
        [StringComparison]::OrdinalIgnoreCase)
}

function Read-HandoffState {
    param([Parameter(Mandatory = $true)][string]$Path)

    $lastError = $null
    foreach ($attempt in 1..5) {
        try {
            $stream = [IO.FileStream]::new(
                $Path,
                [IO.FileMode]::Open,
                [IO.FileAccess]::Read,
                [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
            try {
                $reader = [IO.StreamReader]::new(
                    $stream,
                    [Text.Encoding]::UTF8,
                    $true,
                    4096,
                    $true)
                try {
                    $text = $reader.ReadToEnd()
                }
                finally {
                    $reader.Dispose()
                }
            }
            finally {
                $stream.Dispose()
            }

            $state = $text | ConvertFrom-Json -ErrorAction Stop
            if ($null -eq $state.PSObject.Properties["ProcessId"] -or
                $null -eq $state.PSObject.Properties["ProcessStartUtc"]) {
                throw "State lacks the required process identity fields."
            }
            [int]$processId = $state.ProcessId
            if ($processId -le 0) {
                throw "State contains an invalid process identity."
            }
            [DateTimeOffset]$processStart = [DateTimeOffset]::Parse(
                [string]$state.ProcessStartUtc,
                [Globalization.CultureInfo]::InvariantCulture,
                [Globalization.DateTimeStyles]::AssumeUniversal -bor
                    [Globalization.DateTimeStyles]::AdjustToUniversal)
            return [pscustomobject]@{
                ProcessId = $processId
                ProcessStartUtc = $processStart
            }
        }
        catch {
            $lastError = $_
            if ($attempt -lt 5) {
                Start-Sleep -Milliseconds 100
            }
        }
    }
    throw "The coordinator state could not be read safely: $($lastError.Exception.Message)"
}

function Assert-StateMatchesHandle {
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)]$Handle
    )

    if ([int]$State.ProcessId -ne [int]$Handle.ProcessId) {
        throw "The state process identity changed."
    }
    [long]$stateFileTime =
        $State.ProcessStartUtc.UtcDateTime.ToFileTimeUtc()
    [long]$driftTicks = [Math]::Abs(
        $stateFileTime - [long]$Handle.CreationFileTime)
    # ISO-8601 writers sometimes truncate sub-millisecond FILETIME digits.
    # One millisecond is the maximum accepted serialization loss.
    if ($driftTicks -gt [TimeSpan]::TicksPerMillisecond) {
        throw "The state process start time does not match the retained handle."
    }
}

function Assert-StateIdentityUnchanged {
    param(
        [Parameter(Mandatory = $true)]$Initial,
        [Parameter(Mandatory = $true)]$Confirmed
    )

    if ([int]$Initial.ProcessId -ne [int]$Confirmed.ProcessId -or
        $Initial.ProcessStartUtc.UtcDateTime.Ticks -ne
            $Confirmed.ProcessStartUtc.UtcDateTime.Ticks) {
        throw "The state process identity changed during validation."
    }
}

function Open-VerifiedRunnerPin {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )

    if ($ExpectedSha256 -notmatch "^[0-9A-Fa-f]{64}$") {
        throw "The expected runner SHA-256 is invalid."
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.PSIsContainer -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "The expected runner must be a regular file."
    }

    $stream = [IO.FileStream]::new(
        $item.FullName,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read)
    try {
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            $actual = [BitConverter]::ToString(
                $sha.ComputeHash($stream)).Replace("-", "")
        }
        finally {
            $sha.Dispose()
        }
        if (-not [string]::Equals(
                $actual,
                $ExpectedSha256,
                [StringComparison]::OrdinalIgnoreCase)) {
            throw "The runner SHA-256 does not match."
        }
        $stream.Position = 0
        return $stream
    }
    catch {
        $stream.Dispose()
        throw
    }
}

function Get-SingleOptionValue {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Option
    )

    $matches = New-Object Collections.Generic.List[int]
    for ($index = 0; $index -lt $Arguments.Count; $index++) {
        if ([string]::Equals(
                $Arguments[$index],
                $Option,
                [StringComparison]::OrdinalIgnoreCase)) {
            $matches.Add($index)
        }
    }
    if ($matches.Count -ne 1) {
        throw "The target command line does not contain one exact required option."
    }
    $optionIndex = $matches[0]
    if ($optionIndex + 1 -ge $Arguments.Count) {
        throw "The target command line has a missing option value."
    }
    return [string]$Arguments[$optionIndex + 1]
}

function Test-DangerousPowerShellExecutionToken {
    param([Parameter(Mandatory = $true)][string]$Token)

    return $Token -match (
        "^(?i:[-/](?:" +
        "c|co|com|comm|comma|comman|command|" +
        "e|ec|en|enc|enco|encod|encode|encoded|" +
        "encodedc|encodedco|encodedcom|encodedcomm|" +
        "encodedcomma|encodedcomman|encodedcommand|" +
        "f|fi|fil|file))$")
}

function Test-ExecutableTokenMatches {
    param(
        [Parameter(Mandatory = $true)][string]$Token,
        [Parameter(Mandatory = $true)][string]$ExpectedPath
    )

    if ([IO.Path]::IsPathRooted($Token)) {
        return Test-PathEqual -Left $Token -Right $ExpectedPath
    }
    if ($Token.Contains([string][IO.Path]::DirectorySeparatorChar) -or
        $Token.Contains([string][IO.Path]::AltDirectorySeparatorChar)) {
        return $false
    }
    return [string]::Equals(
        $Token,
        [IO.Path]::GetFileName($ExpectedPath),
        [StringComparison]::OrdinalIgnoreCase)
}

function Assert-PowerShellFileGrammar {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$RunnerPath,
        [Parameter(Mandatory = $true)][string]$HostPath
    )

    if ($Arguments.Count -lt 4 -or
        -not (Test-ExecutableTokenMatches `
            -Token $Arguments[0] `
            -ExpectedPath $HostPath)) {
        throw "The PowerShell command line has an invalid executable token."
    }

    $flagOptions = @(
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive"
    )
    $valuedOptions = @{
        "-ExecutionPolicy" = @(
            "AllSigned",
            "Bypass",
            "Default",
            "RemoteSigned",
            "Restricted",
            "Undefined",
            "Unrestricted"
        )
        "-InputFormat" = @("Text", "XML")
        "-OutputFormat" = @("Text", "XML")
        "-WindowStyle" = @(
            "Normal",
            "Minimized",
            "Maximized",
            "Hidden"
        )
    }
    $seenHostOptions =
        [Collections.Generic.HashSet[string]]::new(
            [StringComparer]::OrdinalIgnoreCase)
    [int]$fileIndex = -1
    [int]$index = 1
    while ($index -lt $Arguments.Count) {
        [string]$token = $Arguments[$index]
        if ([string]::Equals(
                $token,
                "-File",
                [StringComparison]::OrdinalIgnoreCase)) {
            $fileIndex = $index
            break
        }
        if ($flagOptions -icontains $token) {
            if (-not $seenHostOptions.Add($token)) {
                throw "A PowerShell host option is duplicated."
            }
            $index++
            continue
        }
        [string]$canonicalValuedOption = $null
        foreach ($knownOption in $valuedOptions.Keys) {
            if ([string]::Equals(
                    $token,
                    $knownOption,
                    [StringComparison]::OrdinalIgnoreCase)) {
                $canonicalValuedOption = $knownOption
                break
            }
        }
        if ($null -eq $canonicalValuedOption -or
            -not $seenHostOptions.Add($canonicalValuedOption) -or
            $index + 1 -ge $Arguments.Count) {
            throw "The PowerShell host invocation is outside the exact allowlist."
        }
        [string]$optionValue = $Arguments[$index + 1]
        if ($valuedOptions[$canonicalValuedOption] -inotcontains
                $optionValue) {
            throw "A PowerShell host option value is invalid."
        }
        $index += 2
    }

    if ($fileIndex -lt 0 -or $fileIndex + 1 -ge $Arguments.Count) {
        throw "The exact terminal -File host option is missing."
    }
    if (-not (Test-PathEqual `
            -Left $Arguments[$fileIndex + 1] `
            -Right $RunnerPath)) {
        throw "The target runner path does not match."
    }

    [int]$scriptArgumentStart = $fileIndex + 2
    if ($scriptArgumentStart -ge $Arguments.Count) {
        throw "The runner has no script arguments."
    }
    [string[]]$scriptArguments = @(
        $Arguments[$scriptArgumentStart..($Arguments.Count - 1)]
    )
    foreach ($scriptToken in $scriptArguments) {
        if (Test-DangerousPowerShellExecutionToken -Token $scriptToken) {
            throw "A command-execution or abbreviated host token is forbidden."
        }
    }
    $actualMode = Get-SingleOptionValue `
        -Arguments $scriptArguments `
        -Option "-Mode"
    if (-not [string]::Equals(
            $actualMode,
            "Batch",
            [StringComparison]::OrdinalIgnoreCase)) {
        throw "The target is not running in Batch mode."
    }
}

function Assert-ExactRunnerTarget {
    param(
        [Parameter(Mandatory = $true)]$Handle,
        [Parameter(Mandatory = $true)][string]$RunnerPath,
        [Parameter(Mandatory = $true)][string]$HostPath
    )

    if ([LegacyHandoffNative]::IsExited($Handle)) {
        throw "The target exited before exact validation."
    }
    $target = Get-CimInstance `
        -ClassName Win32_Process `
        -Filter "ProcessId = $([int]$Handle.ProcessId)" `
        -ErrorAction Stop
    if ($null -eq $target -or
        [string]::IsNullOrWhiteSpace([string]$target.ExecutablePath) -or
        [string]::IsNullOrWhiteSpace([string]$target.CommandLine)) {
        throw "The exact target process could not be inspected."
    }
    if (-not (Test-PathEqual `
            -Left ([string]$target.ExecutablePath) `
            -Right $HostPath)) {
        throw "The target host executable does not match."
    }

    [string[]]$arguments =
        [LegacyHandoffNative]::ParseCommandLine([string]$target.CommandLine)
    Assert-PowerShellFileGrammar `
        -Arguments $arguments `
        -RunnerPath $RunnerPath `
        -HostPath $HostPath
    if ([LegacyHandoffNative]::IsExited($Handle)) {
        throw "The target exited during exact validation."
    }
}

function Get-DescendantSnapshot {
    param([Parameter(Mandatory = $true)][int]$RootProcessId)

    $records =
        [Collections.Generic.Dictionary[int,LegacyHandoffProcessRecord]]::new()
    foreach ($record in [LegacyHandoffNative]::GetProcessSnapshot()) {
        if ($records.ContainsKey([int]$record.ProcessId)) {
            throw "A process snapshot contained a duplicate PID."
        }
        $records.Add([int]$record.ProcessId, $record)
    }

    $depth = [Collections.Generic.Dictionary[int,int]]::new()
    $depth.Add($RootProcessId, 0)
    $result = New-Object Collections.Generic.List[object]
    [bool]$changed = $true
    while ($changed) {
        $changed = $false
        foreach ($record in $records.Values) {
            [int]$processId = $record.ProcessId
            [int]$parentId = $record.ParentProcessId
            if ($processId -eq $RootProcessId -or
                $depth.ContainsKey($processId) -or
                -not $depth.ContainsKey($parentId)) {
                continue
            }
            [int]$recordDepth = $depth[$parentId] + 1
            $depth.Add($processId, $recordDepth)
            $result.Add([pscustomobject]@{
                    ProcessId = $processId
                    ParentProcessId = $parentId
                    Depth = $recordDepth
                })
            $changed = $true
        }
    }
    return @($result | Sort-Object Depth, ProcessId)
}

function Assert-RetainedTreeAncestry {
    param(
        [Parameter(Mandatory = $true)]$RootHandle,
        [object[]]$Descendants
    )

    $byId =
        [Collections.Generic.Dictionary[
            int,
            LegacyHandoffProcessHandle]]::new()
    foreach ($child in @($Descendants)) {
        if ($byId.ContainsKey([int]$child.ProcessId)) {
            throw "A retained process identity was duplicated."
        }
        $byId.Add([int]$child.ProcessId, $child)
    }
    foreach ($child in @($Descendants)) {
        if ([LegacyHandoffNative]::IsExited($child)) {
            throw "A retained descendant exited before handoff."
        }
        [int]$parentId =
            [LegacyHandoffNative]::GetParentProcessId($child)
        $parentHandle = if ($parentId -eq [int]$RootHandle.ProcessId) {
            $RootHandle
        }
        elseif ($byId.ContainsKey($parentId)) {
            $byId[$parentId]
        }
        else {
            throw "A retained descendant no longer has proven ancestry."
        }
        if ([LegacyHandoffNative]::IsExited($parentHandle) -or
            [long]$parentHandle.CreationFileTime -gt
                [long]$child.CreationFileTime) {
            throw "A retained descendant has an invalid parent identity."
        }
    }
}

function Freeze-StableDescendantTree {
    param([Parameter(Mandatory = $true)]$RootHandle)

    $frozen =
        [Collections.Generic.Dictionary[
            int,
            LegacyHandoffProcessHandle]]::new()
    [int]$stablePasses = 0
    try {
        while ($stablePasses -lt 2) {
            [int]$added = 0
            $snapshot = @(
                Get-DescendantSnapshot `
                    -RootProcessId ([int]$RootHandle.ProcessId)
            )
            foreach ($record in $snapshot) {
                [int]$childId = $record.ProcessId
                [int]$parentId = $record.ParentProcessId
                if ($frozen.ContainsKey($childId)) {
                    continue
                }
                $parentHandle = if (
                    $parentId -eq [int]$RootHandle.ProcessId) {
                    $RootHandle
                }
                elseif ($frozen.ContainsKey($parentId)) {
                    $frozen[$parentId]
                }
                else {
                    throw "Snapshot ancestry could not be retained safely."
                }

                $child = $null
                try {
                    $child = [LegacyHandoffNative]::Open($childId)
                    if ([LegacyHandoffNative]::IsExited($child)) {
                        $child.Dispose()
                        $child = $null
                        continue
                    }
                    if ([long]$child.CreationFileTime -lt
                            [long]$parentHandle.CreationFileTime -or
                        [int][LegacyHandoffNative]::
                            GetParentProcessId($child) -ne $parentId) {
                        throw "Snapshot-to-handle process ancestry changed."
                    }
                    [LegacyHandoffNative]::Suspend($child)
                    if ([LegacyHandoffNative]::IsExited($child) -or
                        [int][LegacyHandoffNative]::
                            GetParentProcessId($child) -ne $parentId) {
                        throw "Process ancestry changed while being retained."
                    }
                    $frozen.Add($childId, $child)
                    $child = $null
                    $added++
                }
                catch {
                    if ($null -ne $child) {
                        try {
                            [LegacyHandoffNative]::Resume($child)
                        }
                        catch {
                        }
                        $child.Dispose()
                    }
                    throw
                }
            }
            Assert-RetainedTreeAncestry `
                -RootHandle $RootHandle `
                -Descendants @($frozen.Values)
            if ($added -eq 0) {
                $stablePasses++
            }
            else {
                $stablePasses = 0
            }
            Start-Sleep -Milliseconds 25
        }
        return @($frozen.Values)
    }
    catch {
        foreach ($child in @($frozen.Values)) {
            try {
                [LegacyHandoffNative]::Resume($child)
            }
            catch {
            }
            $child.Dispose()
        }
        throw
    }
}

function Resume-FrozenProcessTree {
    param(
        [object[]]$Descendants,
        [Parameter(Mandatory = $true)]$RootHandle
    )

    $errors = New-Object Collections.Generic.List[string]
    foreach ($child in @($Descendants)) {
        if ($null -eq $child) {
            continue
        }
        try {
            [LegacyHandoffNative]::Resume($child)
        }
        catch {
            $errors.Add("descendant")
        }
    }
    try {
        [LegacyHandoffNative]::Resume($RootHandle)
    }
    catch {
        $errors.Add("root")
    }
    if ($errors.Count -gt 0) {
        throw "One or more frozen processes could not be resumed."
    }
}

function Invoke-BestEffortTreeTermination {
    param(
        [Parameter(Mandatory = $true)]$RootHandle,
        [object[]]$Descendants,
        [Parameter(Mandatory = $true)][uint32]$ExitCode,
        [int]$SimulatedFailureProcessId = 0
    )

    $handles = @($Descendants) + @($RootHandle)
    $failures =
        [Collections.Generic.HashSet[int]]::new()
    foreach ($attempt in 1..2) {
        foreach ($handle in $handles) {
            [bool]$alreadyExited = $false
            try {
                $alreadyExited =
                    [LegacyHandoffNative]::IsExited($handle)
            }
            catch {
            }
            if ($alreadyExited) {
                continue
            }
            try {
                if ([int]$handle.ProcessId -eq
                        $SimulatedFailureProcessId) {
                    throw "Synthetic termination failure."
                }
                [LegacyHandoffNative]::Terminate($handle, $ExitCode)
            }
            catch {
                [void]$failures.Add([int]$handle.ProcessId)
            }
        }
    }

    $survivors = New-Object Collections.Generic.List[object]
    [bool]$resumeFailure = $false
    foreach ($handle in $handles) {
        try {
            if (-not [LegacyHandoffNative]::IsExited($handle)) {
                $survivors.Add($handle)
            }
        }
        catch {
            $survivors.Add($handle)
        }
    }
    foreach ($survivor in $survivors) {
        try {
            [LegacyHandoffNative]::Resume($survivor)
        }
        catch {
            $resumeFailure = $true
        }
    }

    [bool]$rootExited = $false
    try {
        $rootExited = [LegacyHandoffNative]::IsExited($RootHandle)
    }
    catch {
    }
    [int]$descendantsExited = 0
    foreach ($child in @($Descendants)) {
        try {
            if ([LegacyHandoffNative]::IsExited($child)) {
                $descendantsExited++
            }
        }
        catch {
        }
    }
    return [pscustomobject]@{
        AllExited = ($survivors.Count -eq 0)
        RootExited = $rootExited
        DescendantsExited = $descendantsExited
        SurvivorCount = $survivors.Count
        FailureCount = $failures.Count
        ResumeFailure = $resumeFailure
    }
}

function Resolve-ResourceProofPaths {
    param([string[]]$Path)

    if (@($Path).Count -lt 1) {
        throw "At least one resource proof path is required."
    }
    $seen =
        [Collections.Generic.HashSet[string]]::new(
            [StringComparer]::OrdinalIgnoreCase)
    $resolved = New-Object Collections.Generic.List[string]
    foreach ($resource in @($Path)) {
        $fullPath = Get-FullPath -Path $resource
        if (-not $seen.Add($fullPath)) {
            throw "Resource proof paths must be unique."
        }
        if (-not [LegacyHandoffNative]::PathExistsStrict($fullPath)) {
            throw "A resource proof target is missing."
        }
        $item = Get-Item -LiteralPath $fullPath -Force -ErrorAction Stop
        if ($item.PSIsContainer -or
            ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "A resource proof target is not a regular file."
        }
        $resolved.Add($fullPath)
    }
    return @($resolved | ForEach-Object { $_ })
}

function Test-ExclusiveOpenIsSharingViolation {
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        $stream = [IO.FileStream]::new(
            $Path,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::None)
        $stream.Dispose()
        return $false
    }
    catch [IO.IOException] {
        [int]$nativeError = $_.Exception.HResult -band 0xFFFF
        if ($nativeError -in @(32, 33)) {
            return $true
        }
        throw
    }
}

function Assert-MeaningfulResourceLocks {
    param(
        [Parameter(Mandatory = $true)][string[]]$Path,
        [Parameter(Mandatory = $true)]$RootHandle,
        [object[]]$Descendants
    )

    $treeIdentities =
        [Collections.Generic.HashSet[string]]::new(
            [StringComparer]::Ordinal)
    foreach ($handle in @($RootHandle) + @($Descendants)) {
        $key = "{0}:{1}" -f
            ([int]$handle.ProcessId),
            ([long]$handle.CreationFileTime)
        [void]$treeIdentities.Add($key)
    }

    [int]$meaningfulCount = 0
    foreach ($resource in $Path) {
        if (-not (Test-ExclusiveOpenIsSharingViolation -Path $resource)) {
            throw "A resource proof target is not locked before handoff."
        }
        [bool]$heldByExactTree = $false
        foreach ($owner in [LegacyHandoffNative]::
                GetLockingProcesses($resource)) {
            $ownerKey = "{0}:{1}" -f
                ([int]$owner.ProcessId),
                ([long]$owner.CreationFileTime)
            if ($treeIdentities.Contains($ownerKey)) {
                $heldByExactTree = $true
                break
            }
        }
        if (-not $heldByExactTree) {
            throw "A resource proof lock is not held by the exact target tree."
        }
        $meaningfulCount++
    }
    if ($meaningfulCount -lt 1) {
        throw "No meaningful resource lock was proven."
    }
    return $meaningfulCount
}

function Assert-ExclusiveResourceRelease {
    param([string[]]$Path)

    [int]$probed = 0
    foreach ($resource in @($Path)) {
        if (-not [LegacyHandoffNative]::PathExistsStrict($resource)) {
            throw "A resource proof target is missing."
        }
        $item = Get-Item -LiteralPath $resource -Force -ErrorAction Stop
        if ($item.PSIsContainer -or
            ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "A resource proof target is not a regular file."
        }
        $stream = [IO.FileStream]::new(
            $resource,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::None)
        $stream.Dispose()
        $probed++
    }
    return $probed
}

function New-HandoffResult {
    param(
        [Parameter(Mandatory = $true)][string]$Outcome,
        [Parameter(Mandatory = $true)][bool]$TransitionObserved,
        [int]$DescendantCount = 0,
        [bool]$RootExited = $false,
        [bool]$JournalPresent = $false,
        [bool]$ResourceReleaseProven = $false,
        [int]$PreStopResourceProofCount = 0,
        [int]$ResourceProbeCount = 0
    )

    return [pscustomobject]@{
        Outcome = $Outcome
        SafeToStartReplacement =
            ($Outcome -eq "StoppedAtBoundary" -and
                $ResourceReleaseProven -and
                $PreStopResourceProofCount -ge 1 -and
                $ResourceProbeCount -eq $PreStopResourceProofCount -and
                -not $JournalPresent)
        TransitionObserved = $TransitionObserved
        DescendantCount = $DescendantCount
        RootExited = $RootExited
        JournalPresent = $JournalPresent
        ResourceReleaseProven = $ResourceReleaseProven
        PreStopResourceProofCount = $PreStopResourceProofCount
        ResourceProbeCount = $ResourceProbeCount
        CheckedUtc = [DateTime]::UtcNow.ToString("o")
    }
}

function Invoke-LegacyHandoffCore {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StatePath,
        [Parameter(Mandatory = $true)][string]$JournalPath,
        [Parameter(Mandatory = $true)][string]$ExpectedRunnerPath,
        [Parameter(Mandatory = $true)][string]$ExpectedRunnerSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedHostPath,
        [string[]]$ResourceProofPath = @(),
        [ValidateRange(500, 1000)]
        [int]$PollMilliseconds = 750,
        [ValidateRange(0, 604800)]
        [int]$TimeoutSeconds = 0,
        [scriptblock]$CandidateHook,
        [int]$SimulatedTerminationFailureProcessId = 0
    )

    $stateFullPath = Get-FullPath -Path $StatePath
    $journalFullPath = Get-FullPath -Path $JournalPath
    $runnerFullPath = Get-FullPath -Path $ExpectedRunnerPath
    $hostFullPath = Get-FullPath -Path $ExpectedHostPath
    if (Test-PathEqual -Left $stateFullPath -Right $journalFullPath) {
        throw "The state and journal paths must be distinct."
    }
    [string[]]$resolvedResourceProofPath = @(
        Resolve-ResourceProofPaths -Path $ResourceProofPath
    )

    $initialState = Read-HandoffState -Path $stateFullPath
    $rootHandle = $null
    $runnerPin = $null
    $descendants = @()
    [bool]$sawJournalPresent = $false
    [bool]$transitionObserved = $false
    $deadline = if ($TimeoutSeconds -eq 0) {
        [DateTime]::MaxValue
    }
    else {
        [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    }

    try {
        $rootHandle = [LegacyHandoffNative]::Open(
            [int]$initialState.ProcessId)
        Assert-StateMatchesHandle `
            -State $initialState `
            -Handle $rootHandle
        $runnerPin = Open-VerifiedRunnerPin `
            -Path $runnerFullPath `
            -ExpectedSha256 $ExpectedRunnerSha256
        Assert-ExactRunnerTarget `
            -Handle $rootHandle `
            -RunnerPath $runnerFullPath `
            -HostPath $hostFullPath
        $confirmedState = Read-HandoffState -Path $stateFullPath
        Assert-StateIdentityUnchanged `
            -Initial $initialState `
            -Confirmed $confirmedState
        Assert-StateMatchesHandle `
            -State $confirmedState `
            -Handle $rootHandle

        while ([DateTime]::UtcNow -lt $deadline) {
            if ([LegacyHandoffNative]::IsExited($rootHandle)) {
                return New-HandoffResult `
                    -Outcome "TargetExitedBeforeBoundary" `
                    -TransitionObserved $transitionObserved `
                    -RootExited $true `
                    -JournalPresent (
                        [LegacyHandoffNative]::PathExistsStrict(
                            $journalFullPath))
            }

            [LegacyHandoffNative]::Suspend($rootHandle)
            [bool]$journalExists =
                [LegacyHandoffNative]::PathExistsStrict($journalFullPath)
            if ($journalExists) {
                $sawJournalPresent = $true
                [LegacyHandoffNative]::Resume($rootHandle)
                Start-Sleep -Milliseconds $PollMilliseconds
                continue
            }
            if (-not $sawJournalPresent) {
                [LegacyHandoffNative]::Resume($rootHandle)
                Start-Sleep -Milliseconds $PollMilliseconds
                continue
            }
            $transitionObserved = $true

            $descendants = @(
                Freeze-StableDescendantTree -RootHandle $rootHandle
            )
            Assert-RetainedTreeAncestry `
                -RootHandle $rootHandle `
                -Descendants $descendants
            [int]$preStopResourceProofCount =
                Assert-MeaningfulResourceLocks `
                    -Path $resolvedResourceProofPath `
                    -RootHandle $rootHandle `
                    -Descendants $descendants
            if ($null -ne $CandidateHook) {
                & $CandidateHook
            }
            Start-Sleep -Milliseconds 100
            if ([LegacyHandoffNative]::PathExistsStrict($journalFullPath)) {
                Resume-FrozenProcessTree `
                    -Descendants $descendants `
                    -RootHandle $rootHandle
                foreach ($child in @($descendants)) {
                    $child.Dispose()
                }
                $descendants = @()
                return New-HandoffResult `
                    -Outcome "RecoveryRequired" `
                    -TransitionObserved $true `
                    -JournalPresent $true `
                    -PreStopResourceProofCount `
                        $preStopResourceProofCount
            }

            [uint32]$stopExitCode = [uint32]::Parse(
                "C0DE0001",
                [Globalization.NumberStyles]::HexNumber)
            $termination = Invoke-BestEffortTreeTermination `
                -RootHandle $rootHandle `
                -Descendants $descendants `
                -ExitCode $stopExitCode `
                -SimulatedFailureProcessId `
                    $SimulatedTerminationFailureProcessId
            [bool]$journalAfterStop =
                [LegacyHandoffNative]::PathExistsStrict($journalFullPath)
            if (-not $termination.AllExited) {
                return New-HandoffResult `
                    -Outcome "TerminationIncomplete" `
                    -TransitionObserved $true `
                    -DescendantCount `
                        ([int]$termination.DescendantsExited) `
                    -RootExited ([bool]$termination.RootExited) `
                    -JournalPresent $journalAfterStop `
                    -PreStopResourceProofCount `
                        $preStopResourceProofCount
            }
            if ($journalAfterStop) {
                return New-HandoffResult `
                    -Outcome "StoppedButRecoveryRequired" `
                    -TransitionObserved $true `
                    -DescendantCount @($descendants).Count `
                    -RootExited $true `
                    -JournalPresent $true `
                    -PreStopResourceProofCount `
                        $preStopResourceProofCount
            }
            [int]$probeCount = 0
            try {
                $probeCount = Assert-ExclusiveResourceRelease `
                    -Path $resolvedResourceProofPath
            }
            catch {
                return New-HandoffResult `
                    -Outcome "ResourceReleaseIncomplete" `
                    -TransitionObserved $true `
                    -DescendantCount @($descendants).Count `
                    -RootExited $true `
                    -JournalPresent $false `
                    -PreStopResourceProofCount `
                        $preStopResourceProofCount
            }
            return New-HandoffResult `
                -Outcome "StoppedAtBoundary" `
                -TransitionObserved $true `
                -DescendantCount @($descendants).Count `
                -RootExited $true `
                -JournalPresent $false `
                -ResourceReleaseProven $true `
                -PreStopResourceProofCount `
                    $preStopResourceProofCount `
                -ResourceProbeCount $probeCount
        }

        return New-HandoffResult `
            -Outcome "TimedOut" `
            -TransitionObserved $transitionObserved `
            -RootExited $false `
            -JournalPresent (
                [LegacyHandoffNative]::PathExistsStrict($journalFullPath))
    }
    finally {
        foreach ($child in @($descendants)) {
            if ($null -ne $child) {
                try {
                    if (-not [LegacyHandoffNative]::IsExited($child)) {
                        [LegacyHandoffNative]::Resume($child)
                    }
                }
                catch {
                }
            }
        }
        if ($null -ne $rootHandle) {
            try {
                if (-not [LegacyHandoffNative]::IsExited($rootHandle)) {
                    [LegacyHandoffNative]::Resume($rootHandle)
                }
            }
            catch {
            }
        }
        foreach ($child in @($descendants)) {
            if ($null -ne $child) {
                $child.Dispose()
            }
        }
        if ($null -ne $runnerPin) {
            $runnerPin.Dispose()
        }
        if ($null -ne $rootHandle) {
            $rootHandle.Dispose()
        }
    }
}

function Invoke-LegacyHandoffMain {
    foreach ($required in @(
            $StatePath,
            $JournalPath,
            $ExpectedRunnerPath,
            $ExpectedRunnerSha256,
            $ExpectedHostPath)) {
        if ([string]::IsNullOrWhiteSpace([string]$required)) {
            throw "All identity and safety parameters are required."
        }
    }
    return Invoke-LegacyHandoffCore `
        -StatePath $StatePath `
        -JournalPath $JournalPath `
        -ExpectedRunnerPath $ExpectedRunnerPath `
        -ExpectedRunnerSha256 $ExpectedRunnerSha256 `
        -ExpectedHostPath $ExpectedHostPath `
        -ResourceProofPath $ResourceProofPath `
        -PollMilliseconds $PollMilliseconds `
        -TimeoutSeconds $TimeoutSeconds
}

if ($MyInvocation.InvocationName -ne ".") {
    try {
        $result = Invoke-LegacyHandoffMain
        $result | ConvertTo-Json -Compress
        switch ($result.Outcome) {
            "StoppedAtBoundary" {
                exit 0
            }
            "RecoveryRequired" {
                exit 2
            }
            "StoppedButRecoveryRequired" {
                exit 3
            }
            "TimedOut" {
                exit 4
            }
            default {
                exit 5
            }
        }
    }
    catch {
        [pscustomobject]@{
            Outcome = "ManualRecoveryRequired"
            SafeToStartReplacement = $false
            ErrorType = $_.Exception.GetType().Name
            CheckedUtc = [DateTime]::UtcNow.ToString("o")
        } | ConvertTo-Json -Compress
        exit 10
    }
}
