"""Regression coverage for safe publishing, state, and queue overrides."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from app_state import (  # noqa: E402
    atomic_update_mapping,
    atomic_write_json,
    migrate_legacy_state,
    read_json,
    resolve_app_paths,
)
from transcode import (  # noqa: E402
    CODECS_AMD,
    CODECS_CPU,
    EncodeResult,
    Settings,
    TranscodeEngine,
    TranscodeProcessControl,
    apply_settings_override,
    build_audio_extract_command,
    build_ffmpeg_command,
    build_output_filename,
    capture_file_identity,
    copy_validated_output,
    delete_source_if_unchanged,
    expected_output_duration,
    make_temporary_output_path,
    merge_portable_overrides,
    paths_refer_to_same_file,
    run_batch,
    sanitize_portable_override,
    run_vmaf_score,
    save_queue,
    load_queue,
    settings_override_diff,
    settings_to_dict,
    validate_output_file,
)


def _cpu_settings() -> Settings:
    settings = Settings()
    settings.codec = next(
        codec for codec in CODECS_CPU if codec.encoder == "libx264")
    return settings


def test_temporary_output_retains_media_suffix(tmp_path):
    final = tmp_path / "movie.mp4"
    temporary = Path(make_temporary_output_path(str(final)))
    assert temporary.parent == final.parent
    assert temporary.suffix == ".mp4"
    assert ".part" in temporary.name
    assert temporary != final


def test_same_path_normalizes_relative_and_absolute(tmp_path, monkeypatch):
    source = tmp_path / "movie.mp4"
    source.write_bytes(b"input")
    monkeypatch.chdir(tmp_path)
    assert paths_refer_to_same_file("movie.mp4", str(source.resolve()))


def test_expected_duration_applies_trim_and_preview():
    settings = _cpu_settings()
    settings.trim_start = 10
    settings.trim_end = 100
    assert expected_output_duration(200, settings) == 90
    assert expected_output_duration(200, settings, preview=True) == 60


def test_trimmed_preview_emits_one_effective_duration_limit():
    settings = _cpu_settings()
    settings.trim_start = 10
    settings.trim_end = 30
    command = build_ffmpeg_command(
        "source.mp4", "output.mp4", settings, preview=True)
    assert command.count("-t") == 1
    assert command[command.index("-t") + 1] == "20"


def test_audio_extract_applies_trim_and_preview():
    settings = _cpu_settings()
    settings.audio_extract = True
    settings.trim_start = 10
    settings.trim_end = 100
    command = build_audio_extract_command(
        "source.mp4",
        "output.mp3",
        settings=settings,
        preview=True,
    )
    assert command[command.index("-ss") + 1] == "10"
    assert command.count("-t") == 1
    assert command[command.index("-t") + 1] == "60"
    assert build_output_filename(
        "source.mp4", settings) == "source.mp3"
    assert build_output_filename(
        "source.mp4", settings, preview=True) == "source_preview.mp3"


def test_unsafe_output_template_is_rejected():
    settings = _cpu_settings()
    settings.filename_template = r"..\outside"
    with pytest.raises(ValueError, match="safe filename"):
        build_output_filename("source.mp4", settings)


def test_invalid_bitrate_never_enables_two_pass():
    settings = _cpu_settings()
    settings.bitrate_mode = "vbr"
    settings.target_bitrate = ""
    command = build_ffmpeg_command(
        "source.mp4", "output.mp4", settings, pass_number=1)
    assert "-pass" not in command
    assert "-crf" in command


def test_cbr_uses_rate_constraints():
    settings = _cpu_settings()
    settings.bitrate_mode = "cbr"
    settings.target_bitrate = "3000k"
    command = build_ffmpeg_command("source.mp4", "output.mp4", settings)
    assert command[command.index("-minrate") + 1] == "3000k"
    assert command[command.index("-maxrate") + 1] == "3000k"
    assert command[command.index("-bufsize") + 1] == "6000k"
    amd = next(
        codec for codec in CODECS_AMD if codec.encoder == "h264_amf")
    settings.codec = amd
    amd_cbr = build_ffmpeg_command("source.mp4", "output.mp4", settings)
    rc_positions = [
        index for index, token in enumerate(amd_cbr) if token == "-rc"]
    assert amd_cbr[rc_positions[-1] + 1] == "cbr"
    settings.bitrate_mode = "vbr"
    amd_vbr = build_ffmpeg_command("source.mp4", "output.mp4", settings)
    rc_positions = [
        index for index, token in enumerate(amd_vbr) if token == "-rc"]
    assert amd_vbr[rc_positions[-1] + 1] == "vbr_peak"


def test_output_validation_rejects_duration_mismatch(tmp_path):
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    source.write_bytes(b"source")
    output.write_bytes(b"output")
    settings = _cpu_settings()
    with (
        patch("transcode.probe_video",
              return_value={"video_codec": "h264", "audio_codec": "aac"}),
        patch("transcode.get_duration", side_effect=[100.0, 20.0]),
    ):
        valid, message, _duration = validate_output_file(
            str(source), str(output), settings)
    assert not valid
    assert "Duration mismatch" in message


def test_engine_publishes_only_after_validation(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    source.write_bytes(b"input")
    output.write_bytes(b"old-output")
    temporary = tmp_path / ".output.test.part.mp4"
    settings = _cpu_settings()
    settings.skip_existing = False

    monkeypatch.setattr(
        "transcode.make_temporary_output_path",
        lambda _path: str(temporary),
    )
    monkeypatch.setattr(
        "transcode.get_duration",
        lambda path: 10.0,
    )
    monkeypatch.setattr(
        "transcode.probe_video",
        lambda path: {"video_codec": "h264", "hdr": False},
    )

    engine = TranscodeEngine()

    def _run(*_args, **_kwargs):
        temporary.write_bytes(b"new-output")
        return True, "", 0.1

    monkeypatch.setattr(engine, "_run_command", _run)
    result = engine.encode_to(str(source), str(output), settings)

    assert result.success
    assert result.validated
    assert output.read_bytes() == b"new-output"
    assert not temporary.exists()


def test_engine_preserves_existing_output_when_validation_fails(
        tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    temporary = tmp_path / ".output.test.part.mp4"
    source.write_bytes(b"input")
    output.write_bytes(b"known-good-old-output")
    settings = _cpu_settings()
    settings.skip_existing = False

    monkeypatch.setattr(
        "transcode.make_temporary_output_path",
        lambda _path: str(temporary),
    )
    monkeypatch.setattr("transcode.get_duration", lambda _path: 10.0)
    engine = TranscodeEngine()

    def _run(*_args, **_kwargs):
        temporary.write_bytes(b"bad-new-output")
        return True, "", 0.1

    monkeypatch.setattr(engine, "_run_command", _run)
    monkeypatch.setattr(
        "transcode.validate_output_file",
        lambda *_args, **_kwargs: (False, "bad stream", 0.0),
    )
    result = engine.encode_to(str(source), str(output), settings)

    assert not result.success
    assert "validation failed" in result.error.lower()
    assert output.read_bytes() == b"known-good-old-output"
    assert not temporary.exists()


def test_cancellation_during_validation_prevents_publish(
        tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    temporary = tmp_path / ".output.test.part.mp4"
    source.write_bytes(b"input")
    output.write_bytes(b"known-good-old-output")
    settings = _cpu_settings()
    settings.skip_existing = False
    control = TranscodeProcessControl()
    engine = TranscodeEngine(process_control=control)
    monkeypatch.setattr(
        "transcode.make_temporary_output_path",
        lambda _path: str(temporary),
    )
    monkeypatch.setattr("transcode.get_duration", lambda _path: 10.0)

    def _run(*_args, **_kwargs):
        temporary.write_bytes(b"cancelled-output")
        return True, "", 0.1

    def _validate(*_args, **_kwargs):
        control.cancel()
        return True, "valid", 10.0

    monkeypatch.setattr(engine, "_run_command", _run)
    monkeypatch.setattr("transcode.validate_output_file", _validate)
    result = engine.encode_to(str(source), str(output), settings)
    assert not result.success
    assert result.error == "Cancelled by user"
    assert output.read_bytes() == b"known-good-old-output"
    assert not temporary.exists()


def test_engine_rejects_output_equal_to_input(tmp_path):
    source = tmp_path / "movie.mp4"
    source.write_bytes(b"input")
    result = TranscodeEngine().encode_to(
        str(source), str(source), _cpu_settings())
    assert not result.success
    assert "same as the input" in result.error


def test_source_replacement_is_retained_instead_of_deleted(tmp_path):
    source = tmp_path / "source.mp4"
    replacement = tmp_path / "replacement.mp4"
    source.write_bytes(b"encoded-original")
    identity = capture_file_identity(str(source))
    replacement.write_bytes(b"new-arrival")
    os.replace(replacement, source)
    deleted, error = delete_source_if_unchanged(str(source), identity)
    assert not deleted
    assert "changed" in error
    assert source.read_bytes() == b"new-arrival"


def test_copy_validated_output_is_atomic_and_size_checked(tmp_path):
    source = tmp_path / "source.mp4"
    destination = tmp_path / "secondary"
    source.write_bytes(b"verified-output")
    copied, copied_path, error = copy_validated_output(
        str(source), str(destination))
    assert copied
    assert not error
    assert Path(copied_path).read_bytes() == b"verified-output"
    assert not list(destination.glob("*.part*"))


def test_engine_secondary_copy_never_overwrites_input(tmp_path, monkeypatch):
    source_dir = tmp_path / "source"
    output_dir = tmp_path / "output"
    source_dir.mkdir()
    output_dir.mkdir()
    source = source_dir / "movie.mp4"
    output = output_dir / "movie.mp4"
    temporary = output_dir / ".movie.test.part.mp4"
    source.write_bytes(b"original-source")
    settings = _cpu_settings()
    settings.skip_existing = False
    settings.post_copy_dir = str(source_dir)
    monkeypatch.setattr(
        "transcode.make_temporary_output_path",
        lambda _path: str(temporary),
    )
    monkeypatch.setattr("transcode.get_duration", lambda _path: 10.0)
    engine = TranscodeEngine()

    def _run(*_args, **_kwargs):
        temporary.write_bytes(b"validated-output")
        return True, "", 0.1

    monkeypatch.setattr(engine, "_run_command", _run)
    monkeypatch.setattr(
        "transcode.validate_output_file",
        lambda *_args, **_kwargs: (True, "valid", 10.0),
    )
    result = engine.encode_to(str(source), str(output), settings)
    assert result.success
    assert "overwrite the input" in result.post_copy_error
    assert source.read_bytes() == b"original-source"


def test_settings_override_is_sparse_and_does_not_mutate_base():
    base = _cpu_settings()
    candidate = apply_settings_override(
        base,
        {"quality": "high", "resolution": "720"},
        CODECS_CPU,
    )
    diff = settings_override_diff(base, candidate)
    assert diff == {"quality": "high", "resolution": "720"}
    assert base.quality == "medium"
    assert base.resolution is None


def test_settings_override_rejects_unavailable_encoder():
    with pytest.raises(ValueError, match="not available"):
        apply_settings_override(
            _cpu_settings(),
            {"codec_encoder": "hevc_nvenc"},
            CODECS_CPU,
        )


def test_required_override_integer_rejects_null_cleanly():
    with pytest.raises(ValueError, match="concurrent must be an integer"):
        apply_settings_override(
            _cpu_settings(),
            {"concurrent": None},
            CODECS_CPU,
        )
    with pytest.raises(ValueError, match="item settings must be an object"):
        merge_portable_overrides(
            _cpu_settings(), {}, None, CODECS_CPU)


def test_exported_global_settings_are_materialized_per_item():
    base = _cpu_settings()
    override = merge_portable_overrides(
        base,
        {
            "quality": "high",
            "codec_encoder": "libx265",
            "filename_template": "{name}_{date}",
            "concurrent": 3,
        },
        {"resolution": "720"},
        CODECS_CPU,
    )
    effective = apply_settings_override(base, override, CODECS_CPU)
    assert effective.codec.encoder == "libx265"
    assert effective.quality == "high"
    assert effective.resolution == "720"
    assert effective.filename_template == "{name}_{date}"
    assert effective.concurrent == 3


def test_portable_override_drops_destructive_or_executable_fields():
    sanitized = sanitize_portable_override({
        "quality": "high",
        "delete_originals": "yes",
        "post_command": "dangerous.exe",
        "advanced_args": ["-f", "rawvideo"],
        "post_copy_dir": r"C:\Sensitive",
        "filename_template": r"..\escape",
    })
    assert sanitized == {"quality": "high"}


def test_settings_serialization_copies_mutable_lists():
    settings = _cpu_settings()
    settings.video_filters = ["hflip"]
    data = settings_to_dict(settings)
    data["video_filters"].append("vflip")
    assert settings.video_filters == ["hflip"]
    assert data["codec_encoder"] == "libx264"


def test_atomic_update_preserves_existing_keys(tmp_path):
    config = tmp_path / "config.json"
    atomic_write_json(config, {"quality": "high", "theme": "dark"})
    atomic_update_mapping(config, {"theme": "light"})
    assert read_json(config, dict, {}) == {
        "quality": "high",
        "theme": "light",
    }


def test_migration_uses_newest_valid_empty_queue_without_touching_source(
        tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    state = tmp_path / "state"
    old.mkdir()
    new.mkdir()
    old_queue = old / "transcode_queue.json"
    new_queue = new / "transcode_queue.json"
    old_queue.write_text('[{"path": "old.mp4"}]', encoding="utf-8")
    new_queue.write_text("[]", encoding="utf-8")
    os.utime(old_queue, (100, 100))
    os.utime(new_queue, (200, 200))
    original_bytes = new_queue.read_bytes()

    paths = resolve_app_paths(state)
    assert migrate_legacy_state(paths, [old, new])
    assert read_json(paths.queue, list, None) == []
    assert new_queue.read_bytes() == original_bytes
    assert not migrate_legacy_state(paths, [old, new])


def test_migration_destination_wins(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "transcode_config.json").write_text(
        '{"quality": "low"}', encoding="utf-8")
    paths = resolve_app_paths(tmp_path / "state")
    atomic_write_json(paths.config, {"quality": "high"})
    migrate_legacy_state(paths, [legacy])
    assert read_json(paths.config, dict, {})["quality"] == "high"


def test_migration_accepts_versioned_queue_documents(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    document = {
        "version": 2,
        "items": [{"path": "movie.mp4", "status": "queued"}],
    }
    (legacy / "transcode_queue.json").write_text(
        json.dumps(document), encoding="utf-8")
    paths = resolve_app_paths(tmp_path / "state")
    migrate_legacy_state(paths, [legacy])
    assert read_json(paths.queue, dict, {}) == document


def test_importing_core_does_not_create_or_migrate_state(tmp_path):
    state = tmp_path / "isolated-state"
    source_dir = Path(__file__).resolve().parents[1] / "src"
    environment = os.environ.copy()
    environment["VIDEO_TRANSCODER_STATE_DIR"] = str(state)
    environment["PYTHONPATH"] = str(source_dir)
    completed = subprocess.run(
        [sys.executable, "-c", "import transcode"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert not state.exists()


def test_versioned_queue_round_trip(tmp_path, monkeypatch):
    queue_path = tmp_path / "queue.json"
    monkeypatch.setattr("transcode.QUEUE_FILE", str(queue_path))
    document = {
        "version": 2,
        "items": [{
            "path": "movie.mp4",
            "settings_override": {"quality": "high"},
        }],
    }
    save_queue(document)
    assert load_queue() == document


def test_cli_batch_rejects_same_stem_output_collisions(tmp_path):
    first = tmp_path / "movie.mp4"
    second = tmp_path / "movie.mkv"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    settings = _cpu_settings()
    with patch("transcode.process_file") as process:
        run_batch(settings, [first, second])
    process.assert_not_called()


def test_process_control_suspends_and_resumes_owned_process(monkeypatch):
    process = MagicMock(pid=1234)
    process.poll.return_value = None
    managed = MagicMock()
    monkeypatch.setattr("transcode.psutil.Process", lambda _pid: managed)
    control = TranscodeProcessControl()
    control.register(process)
    control.pause()
    assert process.pid in control._suspended
    managed.suspend.assert_called_once()
    control.resume()
    managed.resume.assert_called_once()
    assert process.pid not in control._suspended


def test_process_registration_cannot_strand_a_resumed_process(monkeypatch):
    process = MagicMock(pid=5678)
    process.poll.return_value = None
    managed = MagicMock()
    suspend_started = threading.Event()
    allow_suspend_to_finish = threading.Event()

    def _suspend():
        suspend_started.set()
        assert allow_suspend_to_finish.wait(timeout=2)

    managed.suspend.side_effect = _suspend
    monkeypatch.setattr("transcode.psutil.Process", lambda _pid: managed)
    control = TranscodeProcessControl()
    control.pause()
    registering = threading.Thread(target=control.register, args=(process,))
    registering.start()
    assert suspend_started.wait(timeout=2)
    resuming = threading.Thread(target=control.resume)
    resuming.start()
    allow_suspend_to_finish.set()
    registering.join(timeout=2)
    resuming.join(timeout=2)
    assert not registering.is_alive()
    assert not resuming.is_alive()
    managed.resume.assert_called_once()
    assert process.pid not in control._suspended


def test_vmaf_parses_json_report(tmp_path, monkeypatch):
    reference = tmp_path / "reference.mp4"
    distorted = tmp_path / "distorted.mp4"
    report = tmp_path / "report.json"
    reference.write_bytes(b"reference")
    distorted.write_bytes(b"distorted")

    def _mkstemp(**_kwargs):
        descriptor = os.open(
            report, os.O_CREAT | os.O_RDWR | os.O_TRUNC)
        return descriptor, str(report)

    process = MagicMock(returncode=0)

    def _communicate(**_kwargs):
        report.write_text(json.dumps({
            "pooled_metrics": {
                "vmaf": {"harmonic_mean": 93.25}
            }
        }), encoding="utf-8")
        return None, ""

    process.communicate.side_effect = _communicate
    monkeypatch.setattr("transcode.tempfile.mkstemp", _mkstemp)
    commands: list[list[str]] = []

    def _popen(command, **_kwargs):
        commands.append(command)
        return process

    monkeypatch.setattr("transcode.subprocess.Popen", _popen)
    score = run_vmaf_score(
        str(reference),
        str(distorted),
        sample_seconds=5,
        trim_start=12,
    )
    assert score == 93.25
    assert not report.exists()
    command = commands[0]
    first_input = command.index("-i")
    second_input = command.index("-i", first_input + 1)
    seek = command.index("-ss")
    assert command[first_input + 1] == str(distorted)
    assert first_input < seek < second_input
    assert command[seek + 1] == "12.0"
    assert command[second_input + 1] == str(reference)
    filter_graph = command[command.index("-lavfi") + 1]
    assert filter_graph.count("pad=1280:720") == 2
