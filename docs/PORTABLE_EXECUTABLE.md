# Portable Windows Executable

`VideoTranscoderPortable.exe` is a one-file Windows x64 build containing:

- the Python 3.10 runtime;
- CustomTkinter, Tcl/Tk, Pillow, TkinterDnD2, pystray, Rich, and psutil;
- FFmpeg and FFprobe 8.0.1 full builds;
- the shared transactional `TranscodeEngine`.

Python and FFmpeg do not need to be installed on the destination PC.

## Use It

1. Copy `VideoTranscoderPortable.exe` and `THIRD_PARTY_NOTICES.txt` to the
   destination PC.
2. Double-click the EXE.
3. Add one or more videos using **Browse Files**, **Browse Folder**, window
   drag-and-drop, or by passing file paths to the executable.
4. Choose a preset or settings and click **Start Encoding**.
5. Review the Queue, Log, and History tabs. Output defaults to the folder
   containing the EXE. Click **Change** beside the output path to select
   another folder for the current session.

If an input video is already in the EXE folder and the filename template would
produce the same name, the safety preflight refuses to overwrite it. Choose
another output folder or a filename template such as `{name}_compressed`.

Saved settings and queues remain under
`%LOCALAPPDATA%\VideoTranscoder`. Set `VIDEO_TRANSCODER_STATE_DIR` before
launch to keep state in another directory.
Restored per-file queue overrides may still specify their own destinations;
those intentionally take precedence for those queue items.

## Compatibility

| Item | Status |
|---|---|
| Windows 10/11 x64 | Supported; remotely validated on Windows 10 build 19045 |
| Python installed | Not required |
| FFmpeg installed | Not required |
| NVIDIA/AMD/Intel hardware encoding | Requires a compatible GPU and current driver |
| CPU encoding | Supported; validated with `libx264` on an Intel-only PC |
| Windows on ARM | Not native and not validated |
| 32-bit Windows | Not supported |
| Linux or macOS | Not supported by this EXE |
| Temporary disk space | Allow at least 500 MB while the app is open |

The EXE is currently unsigned, so SmartScreen or endpoint protection may hold
the first launch for inspection. Verify its SHA-256 against
`build-manifest.json`. Do not disable antivirus protection.

## Run the Built-In Self-Test

The self-test does not open the GUI. It verifies the frozen runtime and bundled
tools, generates a two-second source video, transcodes it through the real
engine with CPU H.264/AAC, validates the result, and writes JSON evidence.

```powershell
$exe = "C:\Tools\VideoTranscoderPortable.exe"
$report = "C:\Tools\VideoTranscoderSelfTest\report.json"
$env:VIDEO_TRANSCODER_STATE_DIR = "C:\Tools\VideoTranscoderSelfTest\state"
$process = Start-Process `
    -FilePath $exe `
    -ArgumentList @("--portable-self-test", "`"$report`"") `
    -WindowStyle Hidden `
    -Wait `
    -PassThru
$process.ExitCode
Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
```

Exit code `0` and `"success": true` mean that runtime discovery, video
generation, transcoding, and validation all passed.

## Build It

Install the GUI and portable build dependencies, then run the builder:

```powershell
python -m pip install -e ".[gui,portable,dev]"
& .\scripts\build_portable.ps1
```

The builder:

1. verifies 64-bit Python and finds a matching FFmpeg/FFprobe pair, preferring
   a full build;
2. runs the test suite;
3. builds `dist\portable\VideoTranscoderPortable.exe`;
4. writes its size and SHA-256 to `dist\portable\build-manifest.json`;
5. runs the frozen-runtime self-test.

Explicit tool paths can be supplied when more than one FFmpeg installation is
present:

```powershell
& .\scripts\build_portable.ps1 `
    -FfmpegPath "C:\ffmpeg\ffmpeg-8.0.1-full_build\bin\ffmpeg.exe" `
    -FfprobePath "C:\ffmpeg\ffmpeg-8.0.1-full_build\bin\ffprobe.exe"
```

PyInstaller performs the one-file packaging. On launch, it extracts bundled
files to a temporary `_MEI...` directory; the application deliberately
resolves FFmpeg and FFprobe from that directory before saved or system paths.

## Validation

The portable workflow was validated on 2026-07-28:

- local frozen-runtime generation, CPU encode, and output validation passed;
- the frozen default output resolved to the directory containing the EXE;
- a separate Windows 10 x64 PC with no usable Python or FFmpeg installation
  used the bundled FFmpeg and FFprobe successfully;
- remote CPU `libx264` produced validated H.264 video and AAC audio without
  hardware acceleration;
- remote GUI startup remained healthy in the non-interactive SSH session.

Each build writes its artifact-specific size and SHA-256 to
`dist\portable\build-manifest.json`. Generated executables, manifests, and
test evidence are deliberately excluded from source control.

## Third-Party Licensing

The bundled Gyan.dev FFmpeg full build reports `--enable-gpl` and
`--enable-version3`. Keep `THIRD_PARTY_NOTICES.txt` with any distributed copy
and review the FFmpeg licensing/source obligations before publishing a release.
