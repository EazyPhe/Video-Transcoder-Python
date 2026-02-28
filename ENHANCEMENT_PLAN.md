# Video Transcoder — Enhancement Implementation Plan

> Generated: 2026-02-28
> Baseline: 68/68 tests passing | transcode.py 1,860 lines | gui.py 2,694 lines
>
> **Status: ALL PHASES COMPLETE** (2026-02-28)
> Final: 116/116 tests passing | transcode.py ~2,400 lines | gui.py ~3,000 lines

---

## Phase 1 — Bug Fixes (Priority: Critical)

These affect correctness and should be fixed before any feature work.

### 1.1 Fix `EncodeResult` missing `output_file` field
- **File:** `src/transcode.py` (line ~146)
- **Problem:** `gui.py` line 2186 creates `EncodeResult(... output_file=out_path ...)` in `_handle_audio_extract()`, but the `EncodeResult` dataclass has no `output_file` attribute. This causes a `TypeError` crash when extracting audio.
- **Fix:** Add `output_file: str = ""` field to the `EncodeResult` dataclass.
- **Test:** Add a test constructing `EncodeResult(output_file="test.mp3")` and verifying the field.
- **Effort:** 5 minutes

### 1.2 Fix `_load_saved_settings()` AMD/Intel codec restore
- **File:** `src/gui.py` (line ~1378)
- **Problem:** `find_codec_by_encoder(encoder, self.has_gpu)` is called without `has_amd`/`has_intel` kwargs. If the user's last saved codec was an AMD or Intel encoder, it won't be found on restore.
- **Fix:** Change to `find_codec_by_encoder(encoder, self.has_gpu, self.has_amd, self.has_intel)`.
- **Test:** Manual verification — save an AMD codec, restart, confirm it restores.
- **Effort:** 5 minutes

### 1.3 Fix `_on_audio_extract_toggle()` no-op
- **File:** `src/gui.py` (lines ~2135-2139)
- **Problem:** Both branches set `state="normal"`, so the toggle does nothing visually. Video settings should be disabled when audio-only mode is active.
- **Fix:** When audio extract is checked, disable codec/quality/resolution/fps/subtitle/10-bit/2-pass/auto-crop controls (set `state="disabled"`). Re-enable them when unchecked.
- **Test:** Manual GUI verification.
- **Effort:** 15 minutes

### 1.4 Replace deprecated `wmic` with PowerShell `Get-CimInstance`
- **File:** `src/transcode.py` (lines ~397-427)
- **Problem:** `detect_amd_gpu()` and `detect_intel_gpu()` use `wmic path win32_VideoController get name`, which is deprecated and removed in newer Windows 11 builds.
- **Fix:** Replace with:
  ```python
  subprocess.run(
      ["powershell", "-NoProfile", "-Command",
       "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"],
      capture_output=True, text=True, timeout=10,
  )
  ```
- **Test:** Add mocked tests for both detection functions with PowerShell output format.
- **Effort:** 15 minutes

### 1.5 Fix race condition in `_poll_watch_folder()`
- **File:** `src/gui.py` (lines ~2420-2445)
- **Problem:** Files detected mid-write (e.g., still being copied) are immediately added to the queue. Encoding may start on an incomplete file.
- **Fix:** Track file sizes across two poll cycles. Only add files whose size hasn't changed between consecutive polls (stable for ~6 seconds).
- **Test:** Mock scenario with a growing file, verify it's not added until stable.
- **Effort:** 20 minutes

### 1.6 Fix CLI `process_file()` ignoring `filename_template`
- **File:** `src/transcode.py` (line ~1691)
- **Problem:** The CLI path always uses `{name}.{ext}` for output, ignoring `settings.filename_template`. Only the GUI respects templates.
- **Fix:** Apply `render_filename_template()` in `process_file()` when the template is not the default `"{name}"`.
- **Test:** Add test verifying output filename uses template tokens.
- **Effort:** 10 minutes

### 1.7 Add try/except around trim value parsing in GUI
- **File:** `src/gui.py` in `_build_settings()`
- **Problem:** Non-numeric trim input causes an unhandled `ValueError`.
- **Fix:** Wrap `float(ts)` / `float(te)` in try/except, show an error message on bad input.
- **Test:** Enter "abc" in trim field, verify graceful error.
- **Effort:** 5 minutes

---

## Phase 2 — Code Quality & Cleanup (Priority: High)

Reduces tech debt and makes subsequent feature work easier.

### 2.1 Remove dead code: `check_encoder_available()`
- **File:** `src/transcode.py` (line ~431)
- **Action:** Delete the function. All runtime filtering already uses `_get_available_encoders()` + `get_all_codecs()`.
- **Effort:** 2 minutes

### 2.2 Extract `resolve_preset_codec()` helper (DRY)
- **Files:** `src/transcode.py`, `src/gui.py`
- **Problem:** The NVENC → AMF/QSV fallback mapping is duplicated in `main()`, `_apply_preset()`, and `_load_saved_settings()`.
- **Fix:** Create a shared function in `transcode.py`:
  ```python
  def resolve_preset_codec(
      preset: dict, has_gpu: bool, has_amd: bool, has_intel: bool,
  ) -> Optional[CodecOption]:
  ```
- **Test:** Unit test with each GPU vendor combination.
- **Effort:** 20 minutes

### 2.3 Combine `probe_video()` into a single ffprobe call
- **File:** `src/transcode.py` (lines ~477-555)
- **Problem:** Two separate subprocess calls (one for video stream, one for audio stream).
- **Fix:** Use `-show_streams` without `-select_streams` to get both in one call, then filter by `codec_type` in Python.
- **Test:** Update/add mocked test for `probe_video()` verifying both video and audio metadata.
- **Effort:** 15 minutes

### 2.4 Unify config/preset serialization
- **Files:** `src/transcode.py`
- **Problem:** `save_custom_preset()` saves 14 fields while `save_config()` saves 20 fields. Custom presets miss `auto_crop`, `audio_extract`, `notification_*`, `concurrent`, `post_action`.
- **Fix:** Extract a `_settings_to_dict(settings)` helper used by both functions. Add missing fields to custom preset format with backward-compatible defaults on load.
- **Test:** Round-trip test: save preset → load → verify all fields match.
- **Effort:** 20 minutes

### 2.5 Replace magic strings with Enums
- **File:** `src/transcode.py`
- **Action:** Create enums for commonly used string values:
  ```python
  class Quality(str, Enum):
      HIGH = "high"
      MEDIUM = "medium"
      LOW = "low"

  class SubtitleMode(str, Enum):
      KEEP = "keep"
      BURN = "burn"
      STRIP = "strip"

  class PostAction(str, Enum):
      NONE = "none"
      SHUTDOWN = "shutdown"
      SLEEP = "sleep"
      COMMAND = "command"

  class GpuVendor(str, Enum):
      NVIDIA = "nvidia"
      AMD = "amd"
      INTEL = "intel"
      NONE = ""
  ```
- **Migration:** Update `Settings`, `CodecOption`, and all references. `str` base class ensures backward-compatible JSON serialization.
- **Test:** Update existing tests to use enums; verify JSON round-trip.
- **Effort:** 45 minutes

### 2.6 Reduce global mutable state
- **File:** `src/transcode.py`
- **Action:** Create a `RuntimeConfig` dataclass to hold `ffmpeg_path`, `ffprobe_path`, `_ffmpeg_encoders`, and pass it where needed instead of mutating module-level globals.
- **Note:** This is a larger refactor. Keep backward-compatible module-level accessors initially.
- **Effort:** 1-2 hours

### 2.7 Structured logging
- **File:** `src/transcode.py`
- **Action:** Replace plain-text `log_message()` with JSON-structured logs:
  ```json
  {
    "timestamp": "2026-02-28T14:30:00",
    "event": "encode_complete",
    "file": "video.mp4",
    "codec": "hevc_nvenc",
    "input_size_mb": 1500,
    "output_size_mb": 450,
    "savings_pct": 70,
    "encode_time_s": 120,
    "valid": true
  }
  ```
- **Keep** the human-readable log as well (or make it configurable).
- **Effort:** 30 minutes

---

## Phase 3 — Performance Improvements (Priority: High)

### 3.1 Thread pool for batch metadata probing
- **File:** `src/gui.py` in `_probe_queue_metadata()`
- **Fix:** Use `ThreadPoolExecutor(max_workers=4)` to probe multiple files in parallel.
- **Effort:** 10 minutes

### 3.2 Cache encoder list at startup
- **File:** `src/transcode.py`
- **Fix:** Call `_get_available_encoders()` eagerly at import time (or in `check_ffmpeg()`) instead of lazily on first `get_all_codecs()` call.
- **Effort:** 5 minutes

### 3.3 Persistent background thread for status bar polling
- **File:** `src/gui.py`
- **Problem:** `_update_status_bar()` spawns a new thread every 3 seconds during encoding.
- **Fix:** Use a single persistent daemon thread with a `threading.Event` for start/stop. The thread loops, calls `get_system_stats()`, and posts results via `self.after()`.
- **Effort:** 20 minutes

### 3.4 Incremental queue UI updates
- **File:** `src/gui.py` in `_refresh_queue()` and `_update_queue_item()`
- **Problem:** `_refresh_queue()` destroys and recreates all widgets on every status change — O(n) for each item update.
- **Fix:** In `_update_queue_item()`, update only the affected row's status label, progress bar, and color. Only call `_refresh_queue()` for structural changes (add/remove/reorder).
- **Effort:** 45 minutes

---

## Phase 4 — Testing Enhancements (Priority: High)

### 4.1 `encode_file()` integration test
- Mock `subprocess.Popen` to simulate FFmpeg progress output lines (`out_time_us=`, `speed=`, `fps=`).
- Verify `EncodeResult` fields: `success`, `encode_time`, `output_size`.
- **Effort:** 30 minutes

### 4.2 `encode_file_gui()` pause/cancel test
- Mock subprocess, fire `cancel_event.set()` mid-stream, verify result has `error="Cancelled by user"`.
- Fire `pause_event.clear()` then `set()`, verify encoding resumes.
- **Effort:** 30 minutes

### 4.3 `probe_video()` test
- Mock ffprobe JSON output for a typical video file.
- Verify all metadata dict keys are populated correctly.
- **Effort:** 15 minutes

### 4.4 `notify_complete()` test
- Mock `subprocess.run` and `subprocess.Popen`.
- Verify correct PowerShell command construction for sound and toast.
- **Effort:** 15 minutes

### 4.5 `execute_post_action()` test
- Mock subprocess for each action type (shutdown, sleep, command).
- Verify correct system commands are dispatched.
- **Effort:** 15 minutes

### 4.6 Boundary / edge case tests
- `format_duration(-5)` → should return `"0:00"` (already clamps via `max(0, ...)`).
- `format_size(0)` → verify output.
- `estimate_output_mb(0, "libx264", "medium", None, "128k")` → should return 0.1 (min).
- `render_filename_template()` with special characters (`{name}` where name contains `[brackets]`, spaces, unicode).
- **Effort:** 20 minutes

### 4.7 Custom preset round-trip test
- `save_custom_preset()` → `load_custom_presets()` → verify all fields match.
- Test with all possible field values.
- **Effort:** 15 minutes

### 4.8 GUI helper function tests (no tkinter required)
- Test `_build_output_path()` logic extracted to a pure function.
- Test `estimate_output_mb()` with various encoder/quality combos.
- Test preset application logic (codec resolution with fallback).
- **Effort:** 30 minutes

---

## Phase 5 — Feature: HDR / Dolby Vision Support

### 5.1 HDR metadata detection
- **File:** `src/transcode.py` in `probe_video()`
- Add ffprobe fields: `color_primaries`, `color_transfer`, `color_space`, `side_data_list` (for HDR10+ / Dolby Vision).
- Return new keys in metadata dict: `hdr_type` (`"SDR"`, `"HDR10"`, `"HDR10+"`, `"HLG"`, `"DolbyVision"`).
- **Effort:** 30 minutes

### 5.2 HDR passthrough in `build_ffmpeg_command()`
- When input is HDR and output codec supports it, add:
  ```
  -color_primaries bt2020 -color_trc smpte2084 -colorspace bt2020nc
  ```
- Add `hdr_mode` to `Settings`: `"auto"` (passthrough if supported), `"tonemap"` (convert to SDR), `"force_sdr"`.
- **Effort:** 45 minutes

### 5.3 Tone-mapping option
- Add FFmpeg filter for SDR conversion:
  ```
  -vf zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709:t=bt709:m=bt709,tonemap=hable,format=yuv420p
  ```
- Integrate into `vf_parts` builder in `build_ffmpeg_command()`.
- **Effort:** 30 minutes

### 5.4 GUI controls
- Add `hdr_mode` dropdown in settings: Auto / Tone-map to SDR / Force SDR.
- Show HDR badge in queue metadata column when detected.
- **Effort:** 20 minutes

### 5.5 Tests
- Mock ffprobe output with HDR metadata, verify detection.
- Verify `build_ffmpeg_command()` includes correct color flags.
- Verify tone-map filter construction.
- **Effort:** 30 minutes

---

## Phase 6 — Feature: Per-File Settings Override

### 6.1 Data model
- Add `settings_override: Optional[dict]` field to `QueueItem`.
- When encoding, merge override dict onto global `Settings` before building the FFmpeg command.
- **Effort:** 20 minutes

### 6.2 Override dialog
- **File:** `src/gui.py`
- Create `_SettingsOverrideDialog(ctk.CTkToplevel)` — a mini version of the settings panel.
- Pre-populate with global settings, allow user to change any field.
- Store changes as a diff dict (only overridden fields).
- **Effort:** 1.5 hours

### 6.3 Queue display integration
- Show a small icon/indicator on queue items that have overrides.
- Right-click menu: "Override Settings..." / "Clear Override".
- **Effort:** 30 minutes

### 6.4 Encoding integration
- In `_encode_single_item()`, apply override before building command.
- **Effort:** 20 minutes

### 6.5 Persistence
- Include overrides in `_save_queue_to_disk()` / `_load_queue_from_disk()`.
- **Effort:** 15 minutes

### 6.6 Tests
- Test settings merge logic (override replaces only specified fields).
- Test queue serialization with overrides.
- **Effort:** 20 minutes

---

## Phase 7 — Feature: FFmpeg Filter Chain Builder

### 7.1 Filter model
- **File:** `src/transcode.py`
- Add `video_filters: list[str]` field to `Settings`.
- Integrate into `build_ffmpeg_command()` by prepending to `vf_parts`.
- **Effort:** 15 minutes

### 7.2 Filter builder GUI panel
- **File:** `src/gui.py`
- New tab or collapsible section: "Video Filters"
- Filter types with sliders/inputs:
  - **Brightness/Contrast/Saturation:** `eq=brightness={b}:contrast={c}:saturation={s}` — sliders from -1.0 to 1.0.
  - **Denoise:** dropdown (Off / Light / Medium / Heavy) mapping to `hqdn3d` parameters.
  - **Sharpen:** `unsharp=5:5:0.8:3:3:0.4` with strength slider.
  - **Speed change:** `setpts={factor}*PTS` + matching `atempo` for audio — dropdown (0.5x, 0.75x, 1x, 1.25x, 1.5x, 2x).
  - **Deinterlace:** `yadif` toggle.
  - **Custom filter:** free-text entry for advanced users.
- **Effort:** 3-4 hours

### 7.3 Filter preview
- Generate a single filtered frame (thumbnail) so users can preview the effect before full encode.
- Reuse `generate_thumbnail()` with a `-vf` argument.
- **Effort:** 30 minutes

### 7.4 Tests
- Verify filter strings are correctly appended to FFmpeg command.
- Test filter chain ordering (crop → filters → scale).
- **Effort:** 20 minutes

---

## Phase 8 — Feature: Subtitle Extraction & Download

### 8.1 Subtitle stream detection
- **File:** `src/transcode.py`
- Extend `probe_video()` to list all subtitle streams (index, language, codec, type).
- Return new key `subtitle_tracks: list[dict]`.
- **Effort:** 20 minutes

### 8.2 Subtitle extraction command builder
- New function `build_subtitle_extract_command(input_file, output_file, stream_index, format)`.
- Supported formats: SRT, ASS/SSA, WebVTT.
- **Effort:** 20 minutes

### 8.3 GUI integration
- Add "Extract Subtitles" button in queue item right-click menu.
- Dialog showing detected subtitle tracks with checkboxes.
- **Effort:** 1 hour

### 8.4 OpenSubtitles integration (optional)
- Use OpenSubtitles REST API to search/download subtitles by file hash.
- Requires API key (user provides in settings).
- Add to optional dependencies.
- **Effort:** 2-3 hours

### 8.5 Tests
- Mock ffprobe output with subtitle streams, verify detection.
- Verify subtitle extract command construction.
- **Effort:** 15 minutes

---

## Phase 9 — Feature: Encoding Queue Import/Export

### 9.1 Queue serialization format
- Define a JSON schema for portable queue files:
  ```json
  {
    "version": 1,
    "created": "2026-02-28T14:30:00",
    "global_settings": { ... },
    "items": [
      {
        "path": "C:\\Videos\\file.mp4",
        "settings_override": { "quality": "high" },
        "status": "queued"
      }
    ]
  }
  ```
- **Effort:** 15 minutes

### 9.2 Export function
- **File:** `src/gui.py`
- "Export Queue" button in queue header → save-as dialog → writes JSON.
- Include global settings + per-file overrides.
- **Effort:** 20 minutes

### 9.3 Import function
- "Import Queue" button → open dialog → reads JSON → adds items to queue.
- Validate file paths exist (warn about missing files).
- Apply global settings from the imported file (with confirmation).
- **Effort:** 30 minutes

### 9.4 CLI support
- Add `--import-queue <file>` argument to `transcode.py` for headless batch processing from an exported queue file.
- **Effort:** 30 minutes

### 9.5 Tests
- Round-trip test: build queue → export → import → verify identical.
- Test import with missing files.
- **Effort:** 20 minutes

---

## Phase 10 — Feature: VMAF Quality Scoring

### 10.1 VMAF command builder
- **File:** `src/transcode.py`
- New function:
  ```python
  def run_vmaf_score(
      original: str, encoded: str, sample_duration: float = 30,
  ) -> Optional[float]:
  ```
- Uses `ffmpeg -lavfi libvmaf` with model path auto-detection.
- Samples a section from the middle of the video for speed.
- **Effort:** 30 minutes

### 10.2 Integration into encode results
- Add `vmaf_score: Optional[float]` to `EncodeResult`.
- Optionally run VMAF after each encode (controlled by a `Settings` toggle).
- **Effort:** 15 minutes

### 10.3 GUI display
- Show VMAF score in queue status column and results summary.
- Color-coded: green (>90), yellow (70-90), red (<70).
- **Effort:** 20 minutes

### 10.4 Profile comparison enhancement
- Include VMAF score in `_run_comparison()` output table.
- **Effort:** 15 minutes

### 10.5 Tests
- Mock VMAF ffmpeg output, verify score parsing.
- Test with missing libvmaf (graceful fallback).
- **Effort:** 15 minutes

---

## Phase 11 — Feature: Bitrate Mode Choice

### 11.1 Bitrate mode enum and Settings field
- **File:** `src/transcode.py`
- Add bitrate mode to Settings:
  ```python
  class BitrateMode(str, Enum):
      CRF = "crf"          # Current behavior (constant quality)
      CBR = "cbr"          # Constant bitrate
      VBV = "vbv"          # CRF with max bitrate cap
      TARGET_SIZE = "target_size"  # Auto-calculate from desired file size
  
  # Settings fields:
  bitrate_mode: str = "crf"
  target_bitrate: Optional[int] = None    # kbps, for CBR/VBV
  target_size_mb: Optional[float] = None  # for TARGET_SIZE mode
  max_bitrate: Optional[int] = None       # kbps, for VBV
  ```
- **Effort:** 15 minutes

### 11.2 Command builder updates
- **File:** `src/transcode.py` in `build_ffmpeg_command()`
- **CRF mode:** Current behavior (no change).
- **CBR mode:** Replace CRF flag with `-b:v {target_bitrate}k -maxrate {target_bitrate}k -bufsize {target_bitrate*2}k`.
- **VBV mode:** Keep CRF flag, add `-maxrate {max_bitrate}k -bufsize {max_bitrate*2}k`.
- **Target size:** Calculate bitrate from `(target_size_mb * 8192) / duration_seconds - audio_bitrate`.
- **Effort:** 45 minutes

### 11.3 GUI controls
- Add "Bitrate Mode" dropdown replacing or alongside Quality.
- Conditional fields: show bitrate input for CBR, max bitrate for VBV, target size for TARGET_SIZE.
- Update size estimate to use actual target when in CBR/TARGET_SIZE modes.
- **Effort:** 1 hour

### 11.4 CLI menus
- Add bitrate mode selection in custom settings flow.
- **Effort:** 30 minutes

### 11.5 Tests
- Verify command output for each bitrate mode.
- Test target size calculation accuracy.
- **Effort:** 30 minutes

---

## Phase 12 — Feature: Encoding Profiles with Advanced Codec Options

### 12.1 Advanced args field
- **File:** `src/transcode.py`
- Add `advanced_args: list[str] = field(default_factory=list)` to `Settings`.
- Append to FFmpeg command after codec args in `build_ffmpeg_command()`.
- **Effort:** 10 minutes

### 12.2 Per-codec option definitions
- Define known options per encoder:
  ```python
  ADVANCED_OPTIONS = {
      "hevc_nvenc": [
          {"name": "B-Frames", "flag": "-bf", "type": "int", "range": [0, 5], "default": 3},
          {"name": "Lookahead", "flag": "-rc-lookahead", "type": "int", "range": [0, 53], "default": 32},
          {"name": "AQ Strength", "flag": "-aq-strength", "type": "int", "range": [1, 15], "default": 8},
          {"name": "Weighted Prediction", "flag": "-weighted_pred", "type": "bool", "default": False},
      ],
      "libx265": [
          {"name": "Preset", "flag": "-preset", "type": "choice",
           "choices": ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow", "placebo"],
           "default": "slow"},
          {"name": "Tune", "flag": "-tune", "type": "choice",
           "choices": ["none", "psnr", "ssim", "grain", "zero-latency", "fast-decode", "animation"],
           "default": "none"},
      ],
      "libsvtav1": [
          {"name": "Preset", "flag": "-preset", "type": "int", "range": [0, 13], "default": 6},
          {"name": "Film Grain", "flag": "-svtav1-params", "type": "int_param",
           "param_name": "film-grain", "range": [0, 50], "default": 0},
          {"name": "Keyframe Interval", "flag": "-g", "type": "int", "range": [0, 600], "default": 240},
      ],
  }
  ```
- **Effort:** 30 minutes

### 12.3 Advanced options GUI dialog
- **File:** `src/gui.py`
- "Advanced..." button next to codec dropdown → opens `_AdvancedCodecDialog`.
- Dynamically generates sliders/dropdowns/toggles based on `ADVANCED_OPTIONS` for the selected encoder.
- Returns a `list[str]` of extra FFmpeg args.
- **Effort:** 2 hours

### 12.4 Persistence
- Save advanced args in config and custom presets.
- **Effort:** 15 minutes

### 12.5 Tests
- Verify advanced args are appended to FFmpeg command.
- Test with various encoder/option combinations.
- **Effort:** 20 minutes

---

## Phase 13 — Feature: Drag-and-Drop Queue Reordering

### 13.1 Mouse event-based drag handler
- **File:** `src/gui.py`
- Add `<B1-Motion>`, `<ButtonPress-1>`, `<ButtonRelease-1>` bindings to queue item rows.
- Visual feedback: highlight drop position with a line indicator.
- On release: reorder `self.queue` and refresh.
- **Effort:** 2-3 hours (tricky with CTkScrollableFrame)

### 13.2 Fallback
- Keep Move Up/Down buttons for keyboard accessibility.
- **Effort:** 0 (already exists)

### 13.3 Tests
- Manual testing — simulate drag across multiple positions.
- **Effort:** Manual only

---

## Phase 14 — Feature: Scene-Based Smart Encoding

### 14.1 Scene detection
- **File:** `src/transcode.py`
- New function:
  ```python
  def detect_scenes(input_file: str, threshold: float = 0.3) -> list[float]:
  ```
- Uses FFmpeg's `select='gt(scene,{threshold})'` filter to find scene-change timestamps.
- **Effort:** 30 minutes

### 14.2 Segment-based parallel encoding
- Split video at scene boundaries using `-ss`/`-to` seeks.
- Encode segments in parallel using `ThreadPoolExecutor`.
- Concatenate using FFmpeg concat demuxer.
- **Effort:** 2-3 hours

### 14.3 GUI option
- Add "Smart parallel encoding" checkbox (CPU codecs only).
- Show segment count and parallelism level in status.
- **Effort:** 30 minutes

### 14.4 Tests
- Mock scene detection output.
- Verify segment boundary calculation.
- Verify concat command construction.
- **Effort:** 30 minutes

---

## Phase 15 — Feature: Network/Cloud Output Paths

### 15.1 UNC path support
- **File:** `src/gui.py` in `_change_output_dir()`
- Allow typing UNC paths (`\\server\share\output`) in addition to the folder picker.
- Validate path accessibility before accepting.
- **Effort:** 20 minutes

### 15.2 Post-encode upload hook
- Add `post_upload` field to Settings: `"none"`, `"copy_to"`, `"custom_script"`.
- `copy_to`: additional output path — copy completed files to a second location.
- `custom_script`: run a user script with `{output_file}` as argument.
- **Effort:** 45 minutes

### 15.3 GUI controls
- Add "Copy output to:" field with browse button.
- **Effort:** 20 minutes

### 15.4 Tests
- Test copy-to logic with mock filesystem.
- **Effort:** 15 minutes

---

## Phase 16 — Architecture: Extract `TranscodeEngine` Class

### 16.1 Engine class
- **New file or section in:** `src/transcode.py`
- Extract encoding orchestration logic into `TranscodeEngine`:
  ```python
  class TranscodeEngine:
      def __init__(self, config: RuntimeConfig):
          self.config = config
          self.cancel_event = threading.Event()
          self.pause_event = threading.Event()
          self.pause_event.set()
      
      def encode_single(self, input_file, output_file, settings, **kwargs) -> EncodeResult: ...
      def encode_batch(self, items, settings, on_progress, on_complete) -> list[EncodeResult]: ...
      def cancel(self): ...
      def pause(self): ...
      def resume(self): ...
  ```
- **Effort:** 2-3 hours

### 16.2 Event bus
- Implement a simple event emitter:
  ```python
  class TranscodeEventBus:
      def on(self, event: str, callback): ...
      def emit(self, event: str, **data): ...
  ```
- Events: `progress`, `item_start`, `item_complete`, `item_failed`, `batch_complete`, `log`.
- GUI subscribes to events instead of receiving direct callbacks.
- **Effort:** 1-2 hours

### 16.3 Migrate GUI to use engine
- Replace direct `encode_file_gui()` calls with `TranscodeEngine` methods.
- Replace `self.after(0, ...)` callbacks with event bus subscriptions.
- **Effort:** 2-3 hours

### 16.4 Tests
- Full unit test coverage of `TranscodeEngine` with mocked subprocess.
- **Effort:** 2 hours

---

## Implementation Order (Recommended)

| Sprint | Phases | Duration Est. | Description |
|--------|--------|---------------|-------------|
| **1** | Phase 1 (Bugs) | 1.5 hours | Fix all bugs — immediate correctness wins |
| **2** | Phase 2 (Code Quality) | 3-4 hours | Clean up tech debt before feature work |
| **3** | Phase 3 (Performance) | 1.5 hours | Faster UI, less overhead |
| **4** | Phase 4 (Tests) | 3 hours | Expand to ~95+ tests, catch regressions |
| **5** | Phase 11 (Bitrate Modes) | 2.5 hours | High user impact, relatively contained |
| **6** | Phase 5 (HDR Support) | 2.5 hours | Important for modern content |
| **7** | Phase 12 (Advanced Codec Options) | 3 hours | Power-user feature |
| **8** | Phase 7 (Filter Chain Builder) | 4-5 hours | Major feature, high complexity |
| **9** | Phase 6 (Per-File Overrides) | 3 hours | Unlocks granular queue control |
| **10** | Phase 9 (Queue Import/Export) | 2 hours | Foundation for batch workflows |
| **11** | Phase 10 (VMAF Scoring) | 1.5 hours | Quality assurance feature |
| **12** | Phase 8 (Subtitle Extraction) | 3-4 hours | Niche but valuable |
| **13** | Phase 16 (Architecture Refactor) | 6-8 hours | Foundation for web UI / extensibility |
| **14** | Phase 13 (DnD Queue Reorder) | 2-3 hours | UX polish |
| **15** | Phase 14 (Scene-Based Encoding) | 3-4 hours | Advanced performance feature |
| **16** | Phase 15 (Network/Cloud Paths) | 1.5 hours | Enterprise/NAS use case |

**Total estimated effort:** ~45-55 hours across all phases.

---

## Success Criteria

- All existing 68 tests continue to pass after each phase.
- New test count target: **120+** tests after Phase 4.
- Zero `wmic` usage after Phase 1.4.
- GUI syntax check passes: `python -c "exec(open('src/gui.py').read().split('if __name__')[0])"`.
- No unhandled exceptions in normal user workflows.
- Each phase is independently deployable (no half-finished features in `master`).
