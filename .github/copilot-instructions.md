# Copilot Instructions — Video Transcoder (Python)

## Project Overview

A two-file video transcoding tool built on FFmpeg with both a GUI (CustomTkinter) and CLI (Rich) interface.
Users compress/convert video files via interactive menus, preset profiles, drag-and-drop, or a full graphical queue.
Target audience: Windows desktop users with NVIDIA, AMD, or Intel GPUs.
Includes a pytest test suite with 68 tests covering core encoding logic.

## Architecture

```
Video-Transcoder-Python/
├── src/
│   ├── transcode.py        # Core encoding engine + CLI (~1,860 lines)
│   └── gui.py              # GUI application (CustomTkinter) (~2,700 lines)
├── tests/
│   └── test_transcode.py   # pytest test suite (68 tests)
├── docs/
│   └── screenshots/        # Screenshots for README
├── run.bat                 # Windows launcher for CLI
├── run_gui.bat             # Windows launcher for GUI
├── requirements.txt        # Dependencies: rich>=13.0, customtkinter>=5.2
└── LICENSE                 # MIT License
```

Source code lives in `src/`. The `.bat` launchers in the project root call `src/transcode.py` and `src/gui.py` respectively. Tests live in `tests/`.

The codebase is intentionally **two files** (engine + GUI). Do not split further unless a file exceeds ~3,000 lines or the user explicitly requests it.

### Code Sections — transcode.py (in order)

1. **Imports & Rich fallback** — graceful exit if `rich` is missing
2. **Configuration constants** — `FFMPEG_PATH`, `FFPROBE_PATH`, extensions, output dir, log/config filenames, `CUSTOM_PRESETS_FILE`, `QUEUE_FILE`, `AUDIO_EXTRACT_FORMATS`
3. **Data classes** — `CodecOption` (with `gpu_vendor` field: `"nvidia"`, `"amd"`, `"intel"`, or `""`), `Settings` (with fields for 10-bit, 2-pass, trim, filename template, post-action, concurrent, auto_crop, audio_extract, audio_extract_format, notification_sound, notification_toast), `EncodeResult`
4. **Codec definitions** — `CODECS_GPU` (NVENC: hevc_nvenc, h264_nvenc), `CODECS_AMD` (AMF: hevc_amf, h264_amf), `CODECS_INTEL` (QSV: hevc_qsv, h264_qsv), `CODECS_CPU` (libx264, libx265, libaom-av1, libsvtav1), `_10BIT_PIX_FMT` mapping
5. **Presets** — 5 named profiles in `PRESETS` dict; `FILENAME_TEMPLATES` list
6. **Custom preset management** — `save_custom_preset()`, `load_custom_presets()`, `delete_custom_preset()`
7. **Utility functions** — Multi-vendor GPU detection (`detect_gpu()`, `detect_amd_gpu()`, `detect_intel_gpu()`), FFmpeg checks, `probe_video()`, duration/size helpers, `render_filename_template()`, logging, config save/load, notifications, `get_all_codecs()` (with encoder availability filtering), `find_codec_by_encoder()`, `get_system_stats()`, `check_encoder_available()`, `_get_available_encoders()`
8. **Validation** — `validate_settings()` checks for incompatible combinations (e.g., 2-pass with GPU codec, Opus with MP4)
9. **Crop detection** — `detect_crop()` runs FFmpeg cropdetect on a sample and returns a crop filter string
10. **Audio extraction** — `build_audio_extract_command()` builds FFmpeg commands for audio-only extraction (MP3/AAC/FLAC/Opus)
11. **Queue persistence** — `save_queue()` and `load_queue()` serialize/deserialize queue items to `transcode_queue.json`
12. **Encoding** — `build_ffmpeg_command()` (supports `pass_number`, 10-bit, trim, `crop_filter`, vendor-specific `-hwaccel` routing) and `encode_file()` with real-time progress parsing
13. **Post-encode** — `execute_post_action()` (shutdown / sleep / custom command)
14. **Menus** — Rich-formatted interactive menus (`menu_*` functions)
15. **Main logic** — `process_file()`, `run_batch()`, `run_single()`, `main()`

### Code Sections — gui.py (in order)

1. **Imports** — stdlib + imports from `transcode.py` (all shared functions/classes including AMD/Intel detection, validation, crop, audio extract, queue persistence, system stats)
2. **GUI dependency check** — graceful exit if `customtkinter` missing; optional `tkinterdnd2`, `pystray`, `Pillow`
3. **Constants** — `POLL_MS`, `_BITRATE_EST` size estimation parameters
4. **`_ToolTip`** — hover tooltip helper class for attaching descriptive tooltips to any widget
5. **`encode_file_gui()`** — GUI-specific encoding wrapper with progress/log/pause/cancel callbacks, `pass_number` and `crop_filter` support
6. **`QueueItem`** dataclass — path, status, result, metadata
7. **Helper dialogs** — `_PresetPicker` (custom preset selection dialog)
8. **`TranscoderApp`** — main application class:
   - `__init__` — AMD/Intel GPU auto-detection, state vars for per-item progress and status polling
   - `_build_ui()` — all UI elements: settings rows (including auto-crop, audio extract, notification toggles), queue table with per-file progress bars, Move Up/Down/Compare buttons, Log tab with Export/Clear, status bar with CPU/GPU/temp and time estimate
   - `_build_settings()` / `_load_saved_settings()` — Settings construction and restoration (includes new fields: auto_crop, audio_extract, audio_extract_format, notification_sound, notification_toast)
   - Encoding coordination: `_start_encoding()` (with `validate_settings()` warnings), `_worker()`, `_worker_sequential()`, `_worker_concurrent()`, `_encode_single_item()` (with auto-crop and audio extract support)
   - Queue management: `_move_queue_up()`, `_move_queue_down()`, `_save_queue_to_disk()`, `_load_queue_from_disk()`
   - Profile comparison: `_compare_profiles()`, `_run_comparison()`
   - Watch folder: `_toggle_watch_folder()`, `_poll_watch_folder()`
   - Custom presets: `_save_custom_preset()`, `_load_custom_preset()`, `_delete_custom_preset()`
   - Metadata: `_probe_queue_metadata()`, `_show_metadata_popup()`
   - Log management: `_export_log()`, `_clear_log()`
   - Status bar: `_start_status_polling()`, `_stop_status_polling()`, `_update_status_bar()`, `_estimate_batch_time()`
   - Geometry persistence: `_save_geometry()`, `_restore_geometry()`
   - Tooltips: `_apply_tooltips()`
   - Audio extract: `_on_audio_extract_toggle()`, `_handle_audio_extract()`
   - Keyboard shortcuts: `_bind_shortcuts()`
   - Post-encode: `_encoding_done()` triggers `execute_post_action()` (respects notification_sound/notification_toast settings)
   - Close handler: `_on_close()` saves queue and geometry before exit

## Conventions & Style

- **Python 3.10+** — uses `list[str]`, `dict[str, int]`, `X | None` style type hints (via `Optional` currently; either form is acceptable).
- **Dataclasses over dicts** — use `@dataclass` for structured configuration; avoid raw dicts for internal state.
- **Rich library for CLI UI** — never use bare `print()` for user-facing CLI output. Use `console.print()` with Rich markup (`[bold]`, `[green]`, `[dim]`, etc.).
- **CustomTkinter for GUI** — all GUI widgets use `customtkinter` (`ctk`). Use `ctk.CTkLabel`, `ctk.CTkButton`, `ctk.CTkOptionMenu`, etc. Never use raw `tkinter` widgets in the GUI.
- **Rich Prompt for CLI input** — use `Prompt.ask()` / `IntPrompt.ask()` with explicit `choices` and `default` values. Never use bare `input()` except for "Press Enter to exit".
- **No external dependencies beyond Rich + CustomTkinter** — keep the dependency footprint minimal. Use stdlib (`subprocess`, `json`, `pathlib`, `threading`, `dataclasses`, `concurrent.futures`) for everything else. Optional extras (`tkinterdnd2`, `Pillow`, `pystray`) are feature-gated.
- **FFmpeg via subprocess** — always use `subprocess.Popen` with `-progress pipe:1 -nostats` for progress parsing. Drain stderr in a background `threading.Thread` to avoid pipe deadlocks.
- **Windows-first** — paths use raw strings (`r"..."`), notifications use PowerShell, window title set via `os.system("title ...")`. Avoid Unix-only features.
- **ASCII safe** — do not use emoji in console output; Windows terminals may not render them. Use Rich markup for emphasis instead.
- **Constants at module level** — configurable values (`FFMPEG_PATH`, `VIDEO_EXTENSIONS`, `OUTPUT_DIR`, etc.) are module-level constants, not buried in functions.
- **Thread safety in GUI** — never update GUI widgets directly from background threads. Always use `self.after(0, ...)` to schedule updates on the main thread.
- **Encoding helpers use callbacks** — `encode_file_gui()` accepts `on_progress`, `on_log`, `cancel_event`, `pause_event` for non-blocking GUI integration.

## FFmpeg Details

- **FFmpeg path**: Auto-detected. Recommended: FFmpeg **full** build from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) (8.0.1+). The "essentials" build works but lacks `libsvtav1` and some other encoders.
- **FFprobe path**: Same directory as ffmpeg, `ffprobe.exe`
- **Encoder availability**: `_get_available_encoders()` runs `ffmpeg -encoders` once and caches the result in `_ffmpeg_encoders: set[str] | None`. `get_all_codecs()` filters out codecs whose encoder is not present in the user’s FFmpeg build.
- **GPU encoding (NVIDIA)**: NVENC via `hevc_nvenc` / `h264_nvenc` with `-preset p7 -tune hq -rc vbr`; quality via `-cq`
- **GPU encoding (AMD)**: AMF via `hevc_amf` / `h264_amf` with `-quality quality`; quality via `-rc cqp -qp_p` (plus `-qp_i` injected by `build_ffmpeg_command`)
- **GPU encoding (Intel)**: QSV via `hevc_qsv` / `h264_qsv` with `-preset veryslow`; quality via `-global_quality` (ICQ mode)
- **Quality control**: NVENC uses `-cq`, AMF uses `-rc cqp -qp_p`/`-qp_i`, QSV uses `-global_quality`, CPU codecs use `-crf`
- **Hardware decode routing**: `gpu_vendor` field determines `-hwaccel`: nvidia→`cuda`, amd→`d3d11va`, intel→`qsv`
- **10-bit encoding**: `_10BIT_PIX_FMT` maps encoder → pixel format (`p010le` for GPU, `yuv420p10le` for CPU)
- **2-pass encoding**: CPU codecs only; pass 1 outputs to NUL with `-an -sn -f null`, pass 2 writes real output; passlog path is unique per file (`ffmpeg2pass_{stem}`) for concurrent safety; 2-pass log files are cleaned up after
- **Trim support**: `-ss` before input (fast seek), `-t` or `-to` after input
- **Progress parsing**: Parse `out_time_us=`, `speed=`, `fps=` lines from FFmpeg's `-progress pipe:1` output
- **Validation**: Compare input/output duration via ffprobe; warn if mismatch exceeds 2 seconds

## Adding a New Codec

1. Create a `CodecOption` instance with `name`, `encoder`, `args`, `crf_flag`, `crf_values` dict, `requires_gpu` flag, and `gpu_vendor` (`"nvidia"`, `"amd"`, `"intel"`, or `""` for CPU).
2. Append to the appropriate list: `CODECS_GPU` (NVIDIA), `CODECS_AMD` (AMD), `CODECS_INTEL` (Intel), or `CODECS_CPU`.
3. Add a row to `speed_map` in `menu_codec()` for the CLI display table.
4. Add a pixel format entry to `_10BIT_PIX_FMT` if the codec supports 10-bit.
5. If the codec should appear in presets, update the relevant `PRESETS` entry.
6. If the codec needs special quality flag handling (like AMF's `-qp_i`), add a branch in `build_ffmpeg_command()`.
7. The codec will only appear in the UI if its encoder exists in the user’s FFmpeg build (checked by `_get_available_encoders()`).

## Adding a New Preset

Add an entry to the `PRESETS` dict with keys: `name`, `desc`, `codec_gpu`, `codec_cpu`, `quality`, `resolution`, `fps`, `audio`. The preset menu auto-generates from this dict.

## Adding a Custom Preset (User-Facing)

Users can save/load/delete custom presets via the GUI. These are stored in `custom_presets.json` using `save_custom_preset()`, `load_custom_presets()`, `delete_custom_preset()` from `transcode.py`.

## Adding a New Filename Template

Append to the `FILENAME_TEMPLATES` list in `transcode.py`. Tokens expanded by `render_filename_template()`: `{name}`, `{codec}`, `{quality}`, `{res}`, `{fps}`, `{date}`.

## Adding a New GUI Setting

1. Add the field to the `Settings` dataclass with a default value.
2. Add a `ctk.StringVar` / `ctk.BooleanVar` in `_build_ui()` and place the widget in the settings frame.
3. Map the variable to the `Settings` field in `_build_settings()`.
4. Restore it from config in `_load_saved_settings()`.
5. Include it in `save_config()` in `transcode.py`.

## Adding a New Menu Option

For the **CLI**, follow the existing `menu_*()` pattern:
1. Print options with `console.print("  [cyan][N][/] Label")`.
2. Use `Prompt.ask("  Choice", choices=[...], default="...")`.
3. Return the mapped value via a dict lookup.

For the **GUI**, follow the existing settings row pattern:
1. Create a `ctk.StringVar` / `ctk.BooleanVar` in `_build_ui()`.
2. Add a `ctk.CTkLabel` + `ctk.CTkOptionMenu` / `ctk.CTkCheckBox` to the settings frame using `grid()`.
3. Wire it in `_build_settings()` and `_load_saved_settings()`.

## Error Handling

- FFmpeg failures: capture stderr, show last 200 chars to user, log full error.
- File not found: check with `os.path.isfile()` before processing.
- GPU detection: wrap `nvidia-smi` (NVIDIA), WMI queries (AMD/Intel) in try/except; gracefully fall back to CPU codecs.
- Input validation: `validate_settings()` returns a list of warning strings for incompatible combinations; GUI shows a confirmation dialog.
- Never crash on a single file failure in batch mode — log the error and continue.

## Testing Guidance

- **Run tests**: `python -m pytest tests/ -v` — runs 68 tests covering core encoding logic
- **Test coverage**: Codec definitions, command building, audio extraction, validation, queue persistence, presets, crop detection (mocked), filename templates, config persistence, 2-pass passlog uniqueness, encoder availability filtering
- **Syntax check**: `python -c "import py_compile; py_compile.compile('src/transcode.py', doraise=True)"` and same for `src/gui.py`
- **Import check**: `cd src && python -c "from transcode import detect_gpu, detect_amd_gpu, detect_intel_gpu, check_ffmpeg, probe_video, validate_settings, detect_crop, build_audio_extract_command, save_queue, load_queue, get_system_stats; print('OK')"`
- **GUI import check**: `cd src && python -c "exec(open('gui.py').read().split('if __name__')[0]); print('OK')"`
- **Dry run**: Use Preview mode (first 60 seconds) on a small test file before full batch encoding.
- When adding new tests, use `pytest` and mock `subprocess.Popen` / `subprocess.run` calls. Place tests in `tests/test_transcode.py`.
- Tests use an autouse `_bypass_encoder_filter` fixture that sets `_ffmpeg_encoders` to all defined codec encoder names, so tests validate codec logic regardless of the local FFmpeg build.
- Test 2-pass encoding with a CPU codec (libx264/libx265) on a short file; verify pass log files are cleaned up.

## Available MCP Servers

The following MCP (Model Context Protocol) servers are available in Copilot Chat and can be used during development of this project. Reference them by their tool prefix.

### Context7 — Library Documentation Lookup

| Tool | Purpose |
|------|---------|
| `mcp_context7_resolve-library-id` | Find the Context7-compatible library ID for a package (e.g., `rich`, `ffmpeg-python`) |
| `mcp_context7_query-docs` | Fetch up-to-date documentation and code examples for a resolved library |

**When to use**: Look up Rich library APIs, FFmpeg filter syntax, Python stdlib docs, or any third-party library before writing or modifying code. Always resolve the library ID first, then query docs.

**Example workflow**:
1. `resolve-library-id` with query `"rich"` → gets the library ID
2. `query-docs` with that ID + topic `"Progress bar custom columns"` → returns current API docs and examples

### GitHub MCP — Repository & Issue Management

| Tool | Purpose |
|------|---------|
| `mcp_io_github_git_get_me` | Get current authenticated GitHub user info |
| `mcp_io_github_git_create_repository` | Create a new GitHub repo for this project |
| `mcp_io_github_git_list_issues` / `search_issues` | List or search issues in a repo |
| `mcp_io_github_git_issue_write` | Create or update issues |
| `mcp_io_github_git_create_pull_request` | Open a pull request |
| `mcp_io_github_git_push_files` | Push file changes to a repo |
| `mcp_io_github_git_create_branch` | Create a feature branch |
| `mcp_io_github_git_search_code` | Search code across GitHub repos |
| `mcp_io_github_git_get_file_contents` | Read a file from a GitHub repo |
| `mcp_io_github_git_create_or_update_file` | Write/update a file in a repo |

**When to use**: When publishing this project to GitHub, managing issues/feature requests, creating PRs, or searching other repos for FFmpeg encoding patterns and examples.

**Tips**:
- Always call `get_me` first to confirm auth context
- Use `search_code` to find real-world FFmpeg command patterns in open-source projects
- Use `search_issues` before creating duplicates
- Look for PR templates in `.github/PULL_REQUEST_TEMPLATE` before opening PRs

### GitKraken — Local Git & GitLens Operations

| Tool | Purpose |
|------|---------|
| `mcp_gitkraken_git_status` | Check working tree status |
| `mcp_gitkraken_git_add_or_commit` | Stage and commit changes |
| `mcp_gitkraken_git_branch` | Create/list/delete branches |
| `mcp_gitkraken_git_checkout` | Switch branches |
| `mcp_gitkraken_git_log_or_diff` | View commit history or diffs |
| `mcp_gitkraken_git_push` | Push commits to remote |
| `mcp_gitkraken_git_stash` | Stash/pop work in progress |
| `mcp_gitkraken_git_blame` | View line-by-line blame |
| `mcp_gitkraken_gitlens_start_work` | Start working on an issue (creates branch + tracks) |
| `mcp_gitkraken_pull_request_create` | Create a PR from GitKraken |
| `mcp_gitkraken_issues_assigned_to_me` | List issues assigned to you |

**When to use**: For all local git operations — committing changes, branching for features, reviewing diffs before pushing. Prefer these over raw `git` terminal commands for better integration.

### Pylance — Python Language Intelligence

| Tool | Purpose |
|------|---------|
| `mcp_pylance_mcp_s_pylanceDocString` | Generate or retrieve docstrings for functions/classes |
| `mcp_pylance_mcp_s_pylanceDocuments` | Get document symbols and structure |
| `mcp_pylance_mcp_s_pylanceImports` | Manage and optimize imports |
| `mcp_pylance_mcp_s_pylanceSyntaxErrors` | Check for syntax errors |
| `mcp_pylance_mcp_s_pylanceInvokeRefactoring` | Invoke automated refactoring (rename, extract, etc.) |
| `mcp_pylance_mcp_s_pylanceInstalledTopLevelModules` | List installed Python packages |
| `mcp_pylance_mcp_s_pylanceRunCodeSnippet` | Run a code snippet and get output |

**When to use**: For Python-specific tasks — checking for type errors, generating docstrings, verifying imports, running quick code snippets to test logic, or performing refactoring operations.

### Filesystem MCP — Direct File Operations

| Tool | Purpose |
|------|---------|
| `mcp_filesystem_read_file` / `read_multiple_files` | Read file contents |
| `mcp_filesystem_write_file` | Write/overwrite a file |
| `mcp_filesystem_create_directory` | Create directories |
| `mcp_filesystem_list_directory` | List folder contents |
| `mcp_filesystem_search_files` | Search for files by pattern |
| `mcp_filesystem_move_file` | Move or rename files |
| `mcp_filesystem_get_file_info` | Get file metadata (size, dates) |

**When to use**: For file management tasks outside the code editor — creating test fixtures, reading sample FFmpeg output for testing, managing output directories, or bulk file operations.

**Note**: `list_allowed_directories` shows which paths the server can access.

### Desktop Commander — Process & System Management

| Tool | Purpose |
|------|---------|
| `mcp_io_github_won_start_process` | Start a process (e.g., run FFmpeg directly) |
| `mcp_io_github_won_read_process_output` | Read stdout/stderr from a running process |
| `mcp_io_github_won_kill_process` | Kill a running process |
| `mcp_io_github_won_list_processes` | List running processes |
| `mcp_io_github_won_interact_with_process` | Send input to a running process |
| `mcp_io_github_won_start_search` / `get_more_search_results` | Full-text search across files |
| `mcp_io_github_won_read_file` / `write_file` | File I/O |
| `mcp_io_github_won_edit_block` | Edit a specific block in a file |

**When to use**: For running and monitoring long-lived processes (like FFmpeg encodes during testing), searching across project files, or interacting with running processes. Useful for debugging encoding issues by capturing raw FFmpeg output.

### Microsoft Markdown Converter

| Tool | Purpose |
|------|---------|
| `mcp_microsoft_mar_convert_to_markdown` | Convert documents (PDF, DOCX, HTML, images) to Markdown |

**When to use**: If documentation or reference material needs to be converted to Markdown format.

### MCP Server Usage Tips

1. **Documentation first**: Before implementing new FFmpeg features or Rich UI patterns, use Context7 to fetch current docs rather than relying on training data.
2. **Git workflow**: Use GitKraken tools for branching and commits — keeps everything tracked in VS Code.
3. **Code quality**: Use Pylance tools to check for type errors and generate docstrings before committing.
4. **Search before writing**: Use GitHub `search_code` to find proven FFmpeg encoding patterns from popular open-source projects.
5. **Test with Desktop Commander**: Use `start_process` to run FFmpeg commands directly and `read_process_output` to inspect results, without needing to run the full Python app.

## Future Enhancement Ideas

- Web UI (Flask/FastAPI) alternative frontend
- Per-file settings override in batch mode
- Encoding profiles with per-codec advanced options (B-frames, GOP size, lookahead)
- Subtitle extraction / download integration
- Network / SMB / cloud output paths
- Progress notification via webhook or email
- Video preview with seek scrubbing in GUI
- Drag-and-drop reordering of queue items (currently uses Move Up/Down buttons)
- GUI test coverage (mock tkinter / CustomTkinter widgets)
- Expand pytest suite to cover gui.py helper functions
- Encoding queue import/export (share queues between machines)
- FFmpeg filter chain builder (brightness, contrast, denoise, stabilize)
