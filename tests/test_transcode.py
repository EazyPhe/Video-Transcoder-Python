"""Tests for transcode.py core functions.

Run with:  pytest tests/ -v
"""
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

# Ensure src/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from transcode import (
    CodecOption,
    Settings,
    EncodeResult,
    CODECS_GPU,
    CODECS_CPU,
    CODECS_AMD,
    CODECS_INTEL,
    PRESETS,
    FILENAME_TEMPLATES,
    AUDIO_EXTRACT_FORMATS,
    _10BIT_PIX_FMT,
    get_all_codecs,
    find_codec_by_encoder,
    render_filename_template,
    build_ffmpeg_command,
    build_audio_extract_command,
    validate_settings,
    detect_crop,
    save_queue,
    load_queue,
    get_system_stats,
    format_duration,
    format_size,
    save_config,
    load_config,
    QUEUE_FILE,
    resolve_preset_codec,
    build_subtitle_extract_command,
    detect_scenes,
    export_queue,
    import_queue,
    ADVANCED_OPTIONS,
    TranscodeEventBus,
    TranscodeEngine,
    _probe_duration,
    _apply_hdr_flags,
)
import transcode as _transcode_module


# ============================================================
#  CODEC DEFINITIONS
# ============================================================

@pytest.fixture(autouse=True)
def _bypass_encoder_filter():
    """Disable FFmpeg encoder filtering so tests see all defined codecs."""
    old = _transcode_module._ffmpeg_encoders
    # Collect every encoder from all codec lists so nothing gets filtered
    _transcode_module._ffmpeg_encoders = {
        c.encoder for c in CODECS_GPU + CODECS_CPU + CODECS_AMD + CODECS_INTEL
    }
    yield
    _transcode_module._ffmpeg_encoders = old


class TestCodecDefinitions:
    """Test that codec lists are well-formed."""

    def test_codecs_gpu_not_empty(self):
        assert len(CODECS_GPU) >= 2

    def test_codecs_cpu_not_empty(self):
        assert len(CODECS_CPU) >= 4  # libx265, libx264, libaom-av1, libsvtav1

    def test_codecs_amd_not_empty(self):
        assert len(CODECS_AMD) >= 2

    def test_codecs_intel_not_empty(self):
        assert len(CODECS_INTEL) >= 2

    def test_all_codecs_have_required_fields(self):
        for codec in CODECS_GPU + CODECS_CPU + CODECS_AMD + CODECS_INTEL:
            assert codec.name, f"Missing name for {codec.encoder}"
            assert codec.encoder, f"Missing encoder for {codec.name}"
            assert isinstance(codec.args, list)
            assert codec.crf_flag
            assert "high" in codec.crf_values
            assert "medium" in codec.crf_values
            assert "low" in codec.crf_values

    def test_gpu_codecs_have_gpu_vendor(self):
        for codec in CODECS_GPU:
            assert codec.gpu_vendor == "nvidia"
            assert codec.requires_gpu is True
        for codec in CODECS_AMD:
            assert codec.gpu_vendor == "amd"
            assert codec.requires_gpu is True
        for codec in CODECS_INTEL:
            assert codec.gpu_vendor == "intel"
            assert codec.requires_gpu is True

    def test_cpu_codecs_no_gpu_vendor(self):
        for codec in CODECS_CPU:
            assert codec.gpu_vendor == ""
            assert codec.requires_gpu is False

    def test_svtav1_in_cpu_codecs(self):
        names = [c.encoder for c in CODECS_CPU]
        assert "libsvtav1" in names

    def test_10bit_pix_fmt_covers_all_codecs(self):
        for codec in CODECS_GPU + CODECS_CPU + CODECS_AMD + CODECS_INTEL:
            if codec.encoder in ("libaom-av1", "libsvtav1", "libx265",
                                  "libx264", "hevc_nvenc", "h264_nvenc",
                                  "hevc_amf", "h264_amf", "hevc_qsv",
                                  "h264_qsv"):
                assert codec.encoder in _10BIT_PIX_FMT, (
                    f"{codec.encoder} missing from _10BIT_PIX_FMT")


# ============================================================
#  get_all_codecs / find_codec_by_encoder
# ============================================================

class TestCodecDiscovery:
    def test_get_all_codecs_gpu_only(self):
        codecs = get_all_codecs(True)
        encoders = {c.encoder for c in codecs}
        assert "hevc_nvenc" in encoders
        assert "libx264" in encoders

    def test_get_all_codecs_cpu_only(self):
        codecs = get_all_codecs(False)
        encoders = {c.encoder for c in codecs}
        assert "hevc_nvenc" not in encoders
        assert "libx264" in encoders

    def test_get_all_codecs_amd(self):
        codecs = get_all_codecs(False, has_amd=True)
        encoders = {c.encoder for c in codecs}
        assert "hevc_amf" in encoders
        assert "hevc_nvenc" not in encoders

    def test_get_all_codecs_intel(self):
        codecs = get_all_codecs(False, has_intel=True)
        encoders = {c.encoder for c in codecs}
        assert "hevc_qsv" in encoders

    def test_get_all_codecs_all_vendors(self):
        codecs = get_all_codecs(True, has_amd=True, has_intel=True)
        encoders = {c.encoder for c in codecs}
        assert "hevc_nvenc" in encoders
        assert "hevc_amf" in encoders
        assert "hevc_qsv" in encoders
        assert "libsvtav1" in encoders

    def test_find_codec_by_encoder_found(self):
        codec = find_codec_by_encoder("libx264", False)
        assert codec is not None
        assert codec.encoder == "libx264"

    def test_find_codec_by_encoder_not_found(self):
        codec = find_codec_by_encoder("nonexistent", False)
        assert codec is None

    def test_find_codec_by_encoder_gpu(self):
        codec = find_codec_by_encoder("hevc_nvenc", True)
        assert codec is not None
        assert codec.gpu_vendor == "nvidia"

    def test_find_amd_codec(self):
        codec = find_codec_by_encoder("hevc_amf", False, has_amd=True)
        assert codec is not None
        assert codec.gpu_vendor == "amd"


# ============================================================
#  SETTINGS DATACLASS
# ============================================================

class TestSettings:
    def test_default_settings(self):
        s = Settings()
        assert s.codec is None
        assert s.quality == "medium"
        assert s.auto_crop is False
        assert s.audio_extract is False
        assert s.audio_extract_format == "mp3"
        assert s.notification_sound is True
        assert s.notification_toast is True
        assert s.concurrent == 1

    def test_settings_with_values(self):
        codec = CODECS_CPU[0]
        s = Settings(codec=codec, quality="high", auto_crop=True,
                     audio_extract=True, audio_extract_format="flac")
        assert s.codec == codec
        assert s.auto_crop is True
        assert s.audio_extract_format == "flac"


# ============================================================
#  build_ffmpeg_command
# ============================================================

class TestBuildFFmpegCommand:
    @pytest.fixture
    def cpu_settings(self):
        s = Settings()
        s.codec = find_codec_by_encoder("libx264", False)
        s.quality = "medium"
        s.output_format = "mp4"
        s.audio_bitrate = "192k"
        s.audio_codec = "aac"
        return s

    @pytest.fixture
    def gpu_settings(self):
        s = Settings()
        s.codec = find_codec_by_encoder("hevc_nvenc", True)
        s.quality = "medium"
        s.output_format = "mp4"
        s.audio_bitrate = "192k"
        s.audio_codec = "aac"
        return s

    def test_cpu_command_has_encoder(self, cpu_settings):
        cmd = build_ffmpeg_command("in.mp4", "out.mp4", cpu_settings)
        assert "-c:v" in cmd
        assert "libx264" in cmd

    def test_cpu_command_has_crf(self, cpu_settings):
        cmd = build_ffmpeg_command("in.mp4", "out.mp4", cpu_settings)
        idx = cmd.index("-crf")
        crf_val = cmd[idx + 1]
        assert crf_val == str(cpu_settings.codec.crf_values["medium"])

    def test_gpu_command_has_hwaccel_when_enabled(self, gpu_settings):
        gpu_settings.hwaccel = True
        cmd = build_ffmpeg_command("in.mp4", "out.mp4", gpu_settings)
        assert "-hwaccel" in cmd
        assert "cuda" in cmd

    def test_gpu_command_no_hwaccel_when_disabled(self, gpu_settings):
        gpu_settings.hwaccel = False
        cmd = build_ffmpeg_command("in.mp4", "out.mp4", gpu_settings)
        assert "-hwaccel" not in cmd

    def test_crop_filter_applied(self, cpu_settings):
        cmd = build_ffmpeg_command(
            "in.mp4", "out.mp4", cpu_settings,
            crop_filter="crop=1920:800:0:140")
        cmd_str = " ".join(cmd)
        assert "crop=1920:800:0:140" in cmd_str

    def test_crop_filter_empty(self, cpu_settings):
        cmd = build_ffmpeg_command(
            "in.mp4", "out.mp4", cpu_settings, crop_filter="")
        cmd_str = " ".join(cmd)
        assert "crop=" not in cmd_str

    def test_preview_adds_time_limit(self, cpu_settings):
        cmd = build_ffmpeg_command(
            "in.mp4", "out.mp4", cpu_settings, preview=True)
        assert "-t" in cmd
        assert "60" in cmd

    def test_10bit_sets_pixel_format(self, cpu_settings):
        cpu_settings.ten_bit = True
        cmd = build_ffmpeg_command("in.mp4", "out.mp4", cpu_settings)
        assert "-pix_fmt" in cmd
        idx = cmd.index("-pix_fmt")
        assert cmd[idx + 1] == "yuv420p10le"

    def test_resolution_scaling(self, cpu_settings):
        cpu_settings.resolution = "720"
        cmd = build_ffmpeg_command("in.mp4", "out.mp4", cpu_settings)
        cmd_str = " ".join(cmd)
        assert "720" in cmd_str

    def test_output_path_in_command(self, cpu_settings):
        cmd = build_ffmpeg_command("in.mp4", "out.mp4", cpu_settings)
        assert "out.mp4" in cmd

    def test_pass_number_1(self, cpu_settings):
        cmd = build_ffmpeg_command(
            "in.mp4", "out.mp4", cpu_settings, pass_number=1)
        assert "-pass" in cmd
        assert "1" in cmd

    def test_two_pass_unique_passlog_per_file(self, cpu_settings):
        """Concurrent 2-pass encodes must get unique passlog paths."""
        cmd_a = build_ffmpeg_command(
            "video_a.mp4", "out/video_a.mp4", cpu_settings, pass_number=1)
        cmd_b = build_ffmpeg_command(
            "video_b.mp4", "out/video_b.mp4", cpu_settings, pass_number=1)
        idx_a = cmd_a.index("-passlogfile")
        idx_b = cmd_b.index("-passlogfile")
        passlog_a = cmd_a[idx_a + 1]
        passlog_b = cmd_b[idx_b + 1]
        assert passlog_a != passlog_b
        assert "video_a" in passlog_a
        assert "video_b" in passlog_b

    def test_amf_qp_handling(self):
        s = Settings()
        s.codec = find_codec_by_encoder(
            "hevc_amf", False, has_amd=True)
        if s.codec is None:
            pytest.skip("AMF codec not available")
        s.quality = "medium"
        s.output_format = "mp4"
        s.audio_bitrate = "192k"
        s.audio_codec = "aac"
        cmd = build_ffmpeg_command("in.mp4", "out.mp4", s)
        assert "-qp_p" in cmd
        assert "-qp_i" in cmd

    def test_qsv_hwaccel(self):
        s = Settings()
        s.codec = find_codec_by_encoder(
            "hevc_qsv", False, has_intel=True)
        if s.codec is None:
            pytest.skip("QSV codec not available")
        s.quality = "medium"
        s.output_format = "mp4"
        s.audio_bitrate = "192k"
        s.audio_codec = "aac"
        s.hwaccel = True
        cmd = build_ffmpeg_command("in.mp4", "out.mp4", s)
        assert "-hwaccel" in cmd
        assert "qsv" in cmd


# ============================================================
#  build_audio_extract_command
# ============================================================

class TestAudioExtract:
    def test_mp3_extraction(self):
        cmd = build_audio_extract_command("in.mp4", "out.mp3", "mp3")
        assert "-vn" in cmd
        assert "-sn" in cmd
        assert "libmp3lame" in cmd

    def test_flac_extraction(self):
        cmd = build_audio_extract_command("in.mp4", "out.flac", "flac")
        assert "flac" in cmd
        assert "-b:a" not in cmd  # FLAC doesn't use bitrate

    def test_aac_extraction(self):
        cmd = build_audio_extract_command("in.mp4", "out.m4a", "aac")
        assert "aac" in cmd

    def test_opus_extraction(self):
        cmd = build_audio_extract_command("in.mp4", "out.ogg", "opus")
        assert "libopus" in cmd

    def test_custom_bitrate(self):
        cmd = build_audio_extract_command(
            "in.mp4", "out.mp3", "mp3", bitrate="320k")
        assert "320k" in cmd

    def test_audio_formats_dict(self):
        for key, val in AUDIO_EXTRACT_FORMATS.items():
            assert "codec" in val
            assert "ext" in val


# ============================================================
#  validate_settings
# ============================================================

class TestValidateSettings:
    def test_no_codec_is_error(self):
        s = Settings()
        warnings = validate_settings(s)
        assert any("No codec" in w for w in warnings)

    def test_opus_mp4_warning(self):
        s = Settings()
        s.codec = find_codec_by_encoder("libx264", False)
        s.audio_codec = "opus"
        s.output_format = "mp4"
        warnings = validate_settings(s)
        assert any("Opus" in w for w in warnings)

    def test_two_pass_gpu_warning(self):
        s = Settings()
        s.codec = find_codec_by_encoder("hevc_nvenc", True)
        if s.codec is None:
            pytest.skip("NVENC codec not available")
        s.two_pass = True
        warnings = validate_settings(s)
        assert any("2-pass" in w for w in warnings)

    def test_concurrent_gpu_warning(self):
        s = Settings()
        s.codec = find_codec_by_encoder("hevc_nvenc", True)
        if s.codec is None:
            pytest.skip("NVENC codec not available")
        s.concurrent = 3
        warnings = validate_settings(s)
        assert any("Concurrent" in w for w in warnings)

    def test_auto_crop_audio_extract_warning(self):
        s = Settings()
        s.codec = find_codec_by_encoder("libx264", False)
        s.auto_crop = True
        s.audio_extract = True
        warnings = validate_settings(s)
        assert any("Auto-crop" in w for w in warnings)

    def test_valid_settings_no_warnings(self):
        s = Settings()
        s.codec = find_codec_by_encoder("libx264", False)
        s.quality = "medium"
        warnings = validate_settings(s)
        assert len(warnings) == 0

    def test_trim_start_gte_end_warning(self):
        s = Settings()
        s.codec = find_codec_by_encoder("libx264", False)
        s.trim_start = 60
        s.trim_end = 30
        warnings = validate_settings(s)
        assert any("Trim" in w for w in warnings)


# ============================================================
#  render_filename_template
# ============================================================

class TestFilenameTemplate:
    def test_default_template(self):
        s = Settings()
        s.codec = find_codec_by_encoder("libx264", False)
        s.quality = "medium"
        result = render_filename_template("{name}", "test_video.mp4", s)
        assert result == "test_video"

    def test_codec_quality_template(self):
        s = Settings()
        s.codec = find_codec_by_encoder("libx264", False)
        s.quality = "high"
        result = render_filename_template(
            "{name}_{codec}_{quality}", "my_video.mp4", s)
        assert "my_video" in result
        assert "libx264" in result or "H.264" in result

    def test_date_template(self):
        s = Settings()
        s.codec = find_codec_by_encoder("libx264", False)
        result = render_filename_template(
            "{name}_{date}", "test.mp4", s)
        assert "test" in result
        # Date is in YYYYMMDD_HHMMSS format
        import re
        assert re.search(r"\d{8}_\d{6}", result)


# ============================================================
#  format_duration / format_size
# ============================================================

class TestFormatHelpers:
    def test_format_duration_seconds(self):
        r = format_duration(45)
        assert "45" in r

    def test_format_duration_minutes(self):
        r = format_duration(125)
        assert "2" in r  # 2 minutes
        assert "05" in r or "5" in r  # 5 seconds

    def test_format_duration_hours(self):
        r = format_duration(3661)
        assert "1" in r  # 1 hour

    def test_format_duration_zero(self):
        r = format_duration(0)
        assert r is not None

    def test_format_size_small(self):
        r = format_size(0.5)
        assert "MB" in r or "0" in r

    def test_format_size_large(self):
        r = format_size(1500)
        assert "GB" in r or "1500" in r or "1.5" in r


# ============================================================
#  QUEUE PERSISTENCE
# ============================================================

class TestQueuePersistence:
    def test_save_and_load_queue(self, tmp_path, monkeypatch):
        queue_file = str(tmp_path / "test_queue.json")
        monkeypatch.setattr("transcode.QUEUE_FILE", queue_file)

        items = [
            {"path": "C:\\video1.mp4", "status": "queued"},
            {"path": "C:\\video2.mkv", "status": "failed"},
        ]
        save_queue(items)
        loaded = load_queue()
        assert len(loaded) == 2
        assert loaded[0]["path"] == "C:\\video1.mp4"
        assert loaded[1]["status"] == "failed"

    def test_load_empty_queue(self, tmp_path, monkeypatch):
        queue_file = str(tmp_path / "nonexistent.json")
        monkeypatch.setattr("transcode.QUEUE_FILE", queue_file)
        loaded = load_queue()
        assert loaded == []

    def test_load_corrupt_queue(self, tmp_path, monkeypatch):
        queue_file = str(tmp_path / "corrupt.json")
        monkeypatch.setattr("transcode.QUEUE_FILE", queue_file)
        with open(queue_file, "w") as f:
            f.write("not valid json{{{")
        loaded = load_queue()
        assert loaded == []


# ============================================================
#  PRESETS
# ============================================================

class TestPresets:
    def test_all_presets_have_required_keys(self):
        required = {"name", "desc", "codec_gpu", "codec_cpu",
                     "quality", "resolution", "fps", "audio"}
        for key, preset in PRESETS.items():
            for rk in required:
                assert rk in preset, (
                    f"Preset '{key}' missing key '{rk}'")

    def test_preset_gpu_codecs_exist(self):
        for key, preset in PRESETS.items():
            enc = preset["codec_gpu"]
            codec = find_codec_by_encoder(enc, True)
            assert codec is not None, (
                f"Preset '{key}' GPU codec '{enc}' not found")

    def test_preset_cpu_codecs_exist(self):
        for key, preset in PRESETS.items():
            enc = preset["codec_cpu"]
            codec = find_codec_by_encoder(enc, False)
            assert codec is not None, (
                f"Preset '{key}' CPU codec '{enc}' not found")


# ============================================================
#  detect_crop (mocked)
# ============================================================

class TestDetectCrop:
    @patch("transcode.os.path.isfile", return_value=True)
    @patch("transcode.subprocess.run")
    def test_detect_crop_success(self, mock_run, mock_isfile):
        mock_run.return_value = MagicMock(
            returncode=0,
            stderr="[Parsed_cropdetect] crop=1920:800:0:140\n"
                   "[Parsed_cropdetect] crop=1920:800:0:140\n")
        result = detect_crop("test.mp4", 120)
        assert result == "crop=1920:800:0:140"

    @patch("transcode.subprocess.run")
    def test_detect_crop_no_result(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stderr="nothing useful here\n")
        result = detect_crop("test.mp4", 120)
        assert result == ""

    def test_detect_crop_nonexistent_file(self):
        result = detect_crop("nonexistent_file.mp4", 0)
        assert result == ""

    @patch("transcode.subprocess.run",
           side_effect=FileNotFoundError())
    def test_detect_crop_ffmpeg_missing(self, mock_run):
        result = detect_crop("test.mp4", 120)
        assert result == ""


# ============================================================
#  get_system_stats (mocked)
# ============================================================

class TestSystemStats:
    @patch("transcode.subprocess.run")
    def test_get_system_stats_returns_dict(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="50\n")
        stats = get_system_stats()
        assert isinstance(stats, dict)
        assert "cpu" in stats
        assert "gpu_util" in stats


# ============================================================
#  CONFIG PERSISTENCE
# ============================================================

class TestConfigPersistence:
    def test_save_and_load_config(self, tmp_path, monkeypatch):
        config_file = str(tmp_path / "test_config.json")
        monkeypatch.setattr("transcode.CONFIG_FILE", config_file)

        s = Settings()
        s.codec = find_codec_by_encoder("libx264", False)
        s.quality = "high"
        s.auto_crop = True
        s.notification_sound = False
        save_config(s)

        cfg = load_config()
        assert cfg is not None
        assert cfg["quality"] == "high"
        assert cfg["auto_crop"] is True
        assert cfg["notification_sound"] is False


# ============================================================
#  ENCODE RESULT output_file FIELD (Phase 1 – Bug 1.1)
# ============================================================

class TestEncodeResultOutputFile:
    def test_default_output_file(self):
        r = EncodeResult(file="in.mp4", success=True)
        assert r.output_file == ""

    def test_explicit_output_file(self):
        r = EncodeResult(file="in.mp4", success=True, output_file="/out/out.mp4")
        assert r.output_file == "/out/out.mp4"


# ============================================================
#  RESOLVE PRESET CODEC (Phase 2)
# ============================================================

class TestResolvePresetCodec:
    def test_resolve_gpu_codec(self):
        preset = {"codec_gpu": "hevc_nvenc", "codec_cpu": "libx265"}
        c = resolve_preset_codec(preset, has_gpu=True)
        assert c is not None
        assert c.encoder == "hevc_nvenc"

    def test_resolve_falls_to_cpu(self):
        preset = {"codec_gpu": "hevc_nvenc", "codec_cpu": "libx265"}
        c = resolve_preset_codec(preset, has_gpu=False)
        assert c is not None
        assert c.encoder == "libx265"

    def test_resolve_amf_fallback(self):
        preset = {"codec_gpu": "hevc_nvenc", "codec_cpu": "libx265"}
        c = resolve_preset_codec(preset, has_gpu=False, has_amd=True)
        assert c is not None
        assert c.encoder == "hevc_amf"

    def test_resolve_qsv_fallback(self):
        preset = {"codec_gpu": "hevc_nvenc", "codec_cpu": "libx265"}
        c = resolve_preset_codec(preset, has_gpu=False, has_intel=True)
        assert c is not None
        assert c.encoder == "hevc_qsv"

    def test_resolve_unknown_returns_none(self):
        preset = {"codec_gpu": "nonexistent", "codec_cpu": "nonexistent"}
        c = resolve_preset_codec(preset, has_gpu=False)
        assert c is None


# ============================================================
#  PROBE VIDEO — SINGLE CALL + HDR + SUBTITLE (Phase 2.3 / 5)
# ============================================================

class TestProbeVideoExtended:
    @patch("transcode.os.path.isfile", return_value=True)
    @patch("transcode.subprocess.run")
    def test_probe_returns_hdr_fields(self, mock_run, _mock_isfile):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"streams": [
                {"codec_type": "video", "codec_name": "hevc",
                 "width": 3840, "height": 2160,
                 "color_transfer": "smpte2084",
                 "color_primaries": "bt2020",
                 "color_space": "bt2020nc",
                 "pix_fmt": "yuv420p10le",
                 "r_frame_rate": "24/1"},
                {"codec_type": "audio", "codec_name": "aac",
                 "channels": 2},
                {"codec_type": "subtitle", "codec_name": "srt",
                 "tags": {"language": "eng"}},
            ]}),
        )
        from transcode import probe_video
        info = probe_video("test.mkv")
        assert info["hdr"] is True
        assert info["color_transfer"] == "smpte2084"
        assert info["color_primaries"] == "bt2020"
        assert len(info["subtitle_streams"]) == 1
        assert info["subtitle_streams"][0]["language"] == "eng"

    @patch("transcode.os.path.isfile", return_value=True)
    @patch("transcode.subprocess.run")
    def test_probe_sdr_not_hdr(self, mock_run, _mock_isfile):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"streams": [
                {"codec_type": "video", "codec_name": "h264",
                 "width": 1920, "height": 1080,
                 "color_transfer": "bt709",
                 "color_primaries": "bt709",
                 "pix_fmt": "yuv420p",
                 "r_frame_rate": "30/1"},
            ]}),
        )
        from transcode import probe_video
        info = probe_video("test.mp4")
        assert info["hdr"] is False
        assert info["subtitle_streams"] == []


# ============================================================
#  VALIDATE SETTINGS — NEW FIELDS (Phase 11)
# ============================================================

class TestValidateSettingsExtended:
    def test_filesize_mode_no_target(self):
        s = Settings()
        s.codec = find_codec_by_encoder("libx264", False)
        s.bitrate_mode = "filesize"
        s.target_size_mb = 0
        w = validate_settings(s)
        assert any("target size" in x.lower() for x in w)

    def test_cbr_mode_no_bitrate(self):
        s = Settings()
        s.codec = find_codec_by_encoder("libx264", False)
        s.bitrate_mode = "cbr"
        s.target_bitrate = ""
        w = validate_settings(s)
        assert any("target bitrate" in x.lower() for x in w)

    def test_vbr_mode_no_bitrate(self):
        s = Settings()
        s.codec = find_codec_by_encoder("libx264", False)
        s.bitrate_mode = "vbr"
        s.target_bitrate = ""
        w = validate_settings(s)
        assert any("target bitrate" in x.lower() for x in w)

    def test_crf_mode_no_warnings(self):
        s = Settings()
        s.codec = find_codec_by_encoder("libx264", False)
        s.bitrate_mode = "crf"
        w = validate_settings(s)
        # Should have no bitrate-related warnings
        assert not any("bitrate" in x.lower() for x in w)


# ============================================================
#  BUILD FFMPEG COMMAND — BITRATE MODES (Phase 11)
# ============================================================

class TestBitrateModesCommand:
    def test_cbr_mode_adds_bv(self):
        s = Settings()
        s.codec = find_codec_by_encoder("libx264", False)
        s.bitrate_mode = "cbr"
        s.target_bitrate = "6000k"
        cmd = build_ffmpeg_command("in.mp4", "out.mp4", s)
        assert "-b:v" in cmd
        assert "6000k" in cmd
        # Should NOT have -crf
        assert "-crf" not in cmd

    def test_vbr_mode_adds_maxrate(self):
        s = Settings()
        s.codec = find_codec_by_encoder("libx264", False)
        s.bitrate_mode = "vbr"
        s.target_bitrate = "6000k"
        s.max_bitrate = "8000k"
        cmd = build_ffmpeg_command("in.mp4", "out.mp4", s)
        assert "-b:v" in cmd
        assert "-maxrate" in cmd
        assert "8000k" in cmd

    def test_crf_mode_default(self):
        s = Settings()
        s.codec = find_codec_by_encoder("libx264", False)
        s.bitrate_mode = "crf"
        cmd = build_ffmpeg_command("in.mp4", "out.mp4", s)
        assert "-crf" in cmd


# ============================================================
#  BUILD FFMPEG COMMAND — HDR (Phase 5)
# ============================================================

class TestHDRCommand:
    def test_hdr_passthrough_adds_color_flags(self):
        s = Settings()
        s.codec = find_codec_by_encoder("libx265", False)
        s.hdr_mode = "passthrough"
        hdr_info = {"hdr": True, "color_transfer": "smpte2084",
                    "color_primaries": "bt2020", "color_space": "bt2020nc"}
        cmd = build_ffmpeg_command("in.mkv", "out.mkv", s, hdr_info=hdr_info)
        assert "-color_trc" in cmd
        assert "smpte2084" in cmd

    def test_hdr_tonemap_adds_vf(self):
        s = Settings()
        s.codec = find_codec_by_encoder("libx264", False)
        s.hdr_mode = "tonemap"
        hdr_info = {"hdr": True, "color_transfer": "smpte2084",
                    "color_primaries": "bt2020", "color_space": "bt2020nc"}
        cmd = build_ffmpeg_command("in.mkv", "out.mp4", s, hdr_info=hdr_info)
        vf_idx = cmd.index("-vf") if "-vf" in cmd else -1
        assert vf_idx >= 0
        assert "tonemap" in cmd[vf_idx + 1]

    def test_hdr_off_no_flags(self):
        s = Settings()
        s.codec = find_codec_by_encoder("libx264", False)
        s.hdr_mode = "off"
        hdr_info = {"hdr": True, "color_transfer": "smpte2084",
                    "color_primaries": "bt2020", "color_space": "bt2020nc"}
        cmd = build_ffmpeg_command("in.mkv", "out.mp4", s, hdr_info=hdr_info)
        assert "-color_trc" not in cmd


# ============================================================
#  BUILD FFMPEG COMMAND — CUSTOM FILTERS + ADVANCED (Phase 7, 12)
# ============================================================

class TestFiltersAndAdvanced:
    def test_custom_video_filters(self):
        s = Settings()
        s.codec = find_codec_by_encoder("libx264", False)
        s.video_filters = ["eq=brightness=0.06", "unsharp=5:5:0.8"]
        cmd = build_ffmpeg_command("in.mp4", "out.mp4", s)
        assert "-vf" in cmd
        vf_val = cmd[cmd.index("-vf") + 1]
        assert "eq=brightness=0.06" in vf_val
        assert "unsharp=5:5:0.8" in vf_val

    def test_advanced_args(self):
        s = Settings()
        s.codec = find_codec_by_encoder("libx264", False)
        s.advanced_args = ["-x264-params", "rc-lookahead=60"]
        cmd = build_ffmpeg_command("in.mp4", "out.mp4", s)
        assert "-x264-params" in cmd
        assert "rc-lookahead=60" in cmd


# ============================================================
#  SUBTITLE EXTRACT COMMAND (Phase 8)
# ============================================================

class TestSubtitleExtract:
    def test_basic_srt_extract(self):
        cmd = build_subtitle_extract_command("in.mkv", "out.srt")
        assert cmd[0] != ""  # FFMPEG_PATH placeholder
        assert "-map" in cmd
        assert "0:s:0" in cmd

    def test_stream_index(self):
        cmd = build_subtitle_extract_command("in.mkv", "out.srt", stream_index=2)
        assert "0:s:2" in cmd


# ============================================================
#  SCENE DETECTION (Phase 14)
# ============================================================

class TestSceneDetection:
    @patch("transcode.os.path.isfile", return_value=True)
    @patch("transcode.subprocess.run")
    def test_detect_scenes_parses_timestamps(self, mock_run, _mock_isfile):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="",
            stderr="[Parsed_showinfo] n:0 pts:0 pts_time:1.5 pos:123\n"
                   "[Parsed_showinfo] n:1 pts:0 pts_time:5.2 pos:456\n")
        result = detect_scenes("test.mp4")
        assert len(result) == 2
        assert abs(result[0] - 1.5) < 0.01
        assert abs(result[1] - 5.2) < 0.01

    @patch("transcode.subprocess.run",
           side_effect=FileNotFoundError())
    def test_detect_scenes_missing_ffmpeg(self, mock_run):
        result = detect_scenes("test.mp4")
        assert result == []


# ============================================================
#  QUEUE IMPORT / EXPORT (Phase 9)
# ============================================================

class TestQueueImportExport:
    def test_export_and_import(self, tmp_path):
        items = [{"path": "video1.mp4", "status": "pending"},
                 {"path": "video2.mkv", "status": "done"}]
        filepath = str(tmp_path / "queue_export.json")
        assert export_queue(items, filepath)
        loaded = import_queue(filepath)
        assert len(loaded) == 2
        assert loaded[0]["path"] == "video1.mp4"

    def test_import_invalid_file(self, tmp_path):
        filepath = str(tmp_path / "bad.json")
        with open(filepath, "w") as f:
            f.write("{bad json}")
        loaded = import_queue(filepath)
        assert loaded == []

    def test_import_nonexistent(self):
        loaded = import_queue("/nonexistent/path.json")
        assert loaded == []


# ============================================================
#  ADVANCED OPTIONS DICT (Phase 12)
# ============================================================

class TestAdvancedOptions:
    def test_known_encoders_have_options(self):
        assert "hevc_nvenc" in ADVANCED_OPTIONS
        assert "libx265" in ADVANCED_OPTIONS
        assert "libx264" in ADVANCED_OPTIONS
        assert "libsvtav1" in ADVANCED_OPTIONS

    def test_options_have_required_keys(self):
        for encoder, opts in ADVANCED_OPTIONS.items():
            for opt in opts:
                assert "flag" in opt, f"Missing 'flag' in {encoder}"
                assert "desc" in opt, f"Missing 'desc' in {encoder}"


# ============================================================
#  TRANSCODE EVENT BUS (Phase 16)
# ============================================================

class TestTranscodeEventBus:
    def test_on_and_emit(self):
        bus = TranscodeEventBus()
        results = []
        bus.on("test", lambda x: results.append(x))
        bus.emit("test", 42)
        assert results == [42]

    def test_off_removes_callback(self):
        bus = TranscodeEventBus()
        results = []
        cb = lambda x: results.append(x)
        bus.on("test", cb)
        bus.off("test", cb)
        bus.emit("test", 42)
        assert results == []

    def test_off_all(self):
        bus = TranscodeEventBus()
        results = []
        bus.on("test", lambda x: results.append(x))
        bus.off("test")
        bus.emit("test", 42)
        assert results == []

    def test_emit_no_listeners(self):
        bus = TranscodeEventBus()
        # Should not raise
        bus.emit("nonexistent", 1, 2, 3)

    def test_multiple_listeners(self):
        bus = TranscodeEventBus()
        r1, r2 = [], []
        bus.on("test", lambda x: r1.append(x))
        bus.on("test", lambda x: r2.append(x))
        bus.emit("test", "hello")
        assert r1 == ["hello"]
        assert r2 == ["hello"]

    def test_error_in_callback_doesnt_crash(self):
        bus = TranscodeEventBus()
        results = []
        bus.on("test", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        bus.on("test", lambda: results.append("ok"))
        bus.emit("test")
        assert results == ["ok"]


# ============================================================
#  TRANSCODE ENGINE (Phase 16)
# ============================================================

class TestTranscodeEngine:
    def test_engine_creates_with_bus(self):
        bus = TranscodeEventBus()
        engine = TranscodeEngine(bus)
        assert engine.bus is bus

    def test_engine_creates_default_bus(self):
        engine = TranscodeEngine()
        assert engine.bus is not None

    def test_cancel_sets_event(self):
        engine = TranscodeEngine()
        assert not engine._cancel.is_set()
        engine.cancel()
        assert engine._cancel.is_set()

    def test_pause_toggle(self):
        engine = TranscodeEngine()
        assert not engine._pause.is_set()
        engine.pause()
        assert engine._pause.is_set()
        engine.pause()
        assert not engine._pause.is_set()


# ============================================================
#  SETTINGS NEW FIELDS (Phase 5, 7, 11, 12, 15)
# ============================================================

class TestSettingsNewFields:
    def test_default_hdr_mode(self):
        s = Settings()
        assert s.hdr_mode == "auto"

    def test_default_bitrate_mode(self):
        s = Settings()
        assert s.bitrate_mode == "crf"

    def test_default_video_filters(self):
        s = Settings()
        assert s.video_filters is None

    def test_default_advanced_args(self):
        s = Settings()
        assert s.advanced_args is None

    def test_default_post_upload(self):
        s = Settings()
        assert s.post_upload == ""

    def test_target_size_mb(self):
        s = Settings()
        s.target_size_mb = 100.5
        assert s.target_size_mb == 100.5


# ============================================================
#  CONFIG PERSISTENCE — NEW FIELDS
# ============================================================

class TestConfigNewFields:
    def test_save_and_load_new_fields(self, tmp_path, monkeypatch):
        config_file = str(tmp_path / "test_config2.json")
        monkeypatch.setattr("transcode.CONFIG_FILE", config_file)

        s = Settings()
        s.codec = find_codec_by_encoder("libx264", False)
        s.hdr_mode = "tonemap"
        s.bitrate_mode = "vbr"
        s.target_bitrate = "6000k"
        s.max_bitrate = "8000k"
        s.target_size_mb = 50.0
        save_config(s)

        cfg = load_config()
        assert cfg["hdr_mode"] == "tonemap"
        assert cfg["bitrate_mode"] == "vbr"
        assert cfg["target_bitrate"] == "6000k"
        assert cfg["max_bitrate"] == "8000k"
        assert cfg["target_size_mb"] == 50.0


# ============================================================
#  CUSTOM PRESETS — ALL FIELDS SAVED (Phase 2.4)
# ============================================================

class TestCustomPresetsAllFields:
    def test_custom_preset_saves_all_fields(self, tmp_path, monkeypatch):
        preset_file = str(tmp_path / "test_presets.json")
        monkeypatch.setattr("transcode.CUSTOM_PRESETS_FILE", preset_file)

        from transcode import save_custom_preset, load_custom_presets
        s = Settings()
        s.codec = find_codec_by_encoder("libx264", False)
        s.auto_crop = True
        s.audio_extract = True
        s.notification_sound = False
        save_custom_preset("test_preset", s)

        presets = load_custom_presets()
        p = presets["test_preset"]
        assert p["auto_crop"] is True
        assert p["audio_extract"] is True
        assert p["notification_sound"] is False
