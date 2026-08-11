from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import threading
import time

import pytest

import lan_media


def source_info(*, hdr: bool = False) -> lan_media.MediaInfo:
    return lan_media.MediaInfo(
        video_index=0,
        video_codec="h264",
        width=3840,
        height=2160,
        duration=120.0,
        audio_streams=(
            lan_media.AudioInfo(index=1, duration=120.0, sample_rate=48000),
            lan_media.AudioInfo(index=2, duration=120.0, sample_rate=44100),
        ),
        average_frame_rate="30000/1001",
        transfer="smpte2084" if hdr else "bt709",
        primaries="bt2020" if hdr else "bt709",
        matrix="bt2020nc" if hdr else "bt709",
        color_range="tv",
        is_hdr=hdr,
    )


def candidate_probe() -> dict:
    return {
        "format": {
            "duration": "120.0",
            "format_name": "matroska,webm",
            "size": "123456",
        },
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "hevc",
                "width": 1920,
                "height": 1080,
                "duration": "120.0",
                "avg_frame_rate": "30000/1001",
                "color_transfer": "bt709",
                "color_primaries": "bt709",
                "color_space": "bt709",
                "color_range": "tv",
            },
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "duration": "120.0",
                "channels": 2,
                "channel_layout": "stereo",
                "bit_rate": "192000",
                "sample_rate": "48000",
            },
            {
                "index": 2,
                "codec_type": "audio",
                "codec_name": "aac",
                "duration": "120.0",
                "channels": 2,
                "channel_layout": "stereo",
                "bit_rate": "192000",
                "sample_rate": "44100",
            },
        ],
    }


def full_decode_evidence() -> lan_media.FullDecodeEvidence:
    return lan_media.FullDecodeEvidence(
        video=lan_media.StreamDecodeEvidence(
            stream_index=0,
            stream_type="video",
            success=True,
            frame_count=3600,
            out_time_seconds=120.0,
        ),
        audio=(
            lan_media.StreamDecodeEvidence(
                stream_index=1,
                stream_type="audio",
                success=True,
                frame_count=0,
                out_time_seconds=120.0,
            ),
            lan_media.StreamDecodeEvidence(
                stream_index=2,
                stream_type="audio",
                success=True,
                frame_count=0,
                out_time_seconds=119.98,
            ),
        ),
    )


def producer_evidence() -> lan_media.ProducerValidationEvidence:
    candidate = lan_media.CandidateEvidence(
        sha256="a" * 64,
        encoded_frame_count=3600,
        output_bytes=123456,
        encode_seconds=20.5,
        full_decode=full_decode_evidence(),
    )
    return lan_media.build_producer_validation_evidence(
        candidate,
        run_id="run-opaque",
        job_id="job-opaque",
        worker_id="xps-helper",
        worker_role="helper",
        attempt_id="attempt-opaque",
        fencing_epoch=7,
        contract_hash="b" * 64,
        producer_build_sha256="c" * 64,
        ffmpeg_sha256="d" * 64,
        ffprobe_sha256="e" * 64,
    )


def test_contract_hash_is_stable_and_behavior_bound():
    first = lan_media.MediaContract()
    second = lan_media.MediaContract()
    changed = lan_media.MediaContract(quality=23)

    assert first.digest() == second.digest()
    assert first.digest() != changed.digest()


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (3840, 2160, (1920, 1080)),
        (1280, 720, (1280, 720)),
        (720, 1280, (608, 1080)),
        (1919, 1079, (1918, 1078)),
    ],
)
def test_expected_dimensions_never_upscale(width, height, expected):
    result = lan_media.expected_dimensions(width, height)
    assert result == expected
    assert result[0] <= width
    assert result[1] <= height


def test_build_nvenc_command_matches_content_contract():
    info = source_info(hdr=True)
    command = lan_media.build_encode_command(
        "ffmpeg",
        "SOURCE",
        "CANDIDATE",
        info,
        "hevc_nvenc",
        240_000_000,
        source_size_bytes=120_000_000,
    )

    assert command[command.index("-c:v") + 1] == "hevc_nvenc"
    assert command[command.index("-preset") + 1] == "p7"
    assert command[command.index("-cq") + 1] == "22"
    assert command[command.index("-b:v") + 1] == "6260800"
    assert command[command.index("-maxrate:v") + 1] == "6660800"
    assert command[command.index("-bufsize:v") + 1] == "13321600"
    assert command[command.index("-fs") + 1] == "108000000"
    assert command[command.index("-b:v") + 1] != "0"
    assert command[command.index("-map") + 1] == "0:0"
    assert command.count("-map") == 2
    assert "0:a?" in command
    assert command[command.index("-ac:a") + 1] == "2"
    assert command[command.index("-b:a") + 1] == "192k"
    assert "-sn" in command
    assert command[-2:] == ["-n", "CANDIDATE"]
    video_filter = command[command.index("-vf") + 1]
    assert "tonemap=hable" in video_filter
    assert "min(1920,iw)" in video_filter
    assert "min(1080,ih)" in video_filter


def test_build_qsv_command_matches_content_contract():
    command = lan_media.build_encode_command(
        "ffmpeg",
        "SOURCE",
        "CANDIDATE",
        source_info(),
        "hevc_qsv",
        123456789,
    )

    assert command[command.index("-c:v") + 1] == "hevc_qsv"
    assert command[command.index("-preset") + 1] == "slower"
    assert command[command.index("-global_quality") + 1] == "22"
    assert "tonemap=hable" not in command[command.index("-vf") + 1]


def test_nvenc_budget_accounts_for_each_audio_stream():
    no_audio = source_info()
    no_audio = lan_media.MediaInfo(
        **{
            **no_audio.__dict__,
            "audio_streams": (),
        }
    )
    one_audio = lan_media.MediaInfo(
        **{
            **no_audio.__dict__,
            "audio_streams": (
                lan_media.AudioInfo(
                    index=1,
                    duration=120.0,
                    sample_rate=48_000,
                ),
            ),
        }
    )
    two_audio = source_info()

    budgets = [
        lan_media.build_nvenc_output_budget(
            120_000_000,
            240_000_000,
            info,
        )
        for info in (no_audio, one_audio, two_audio)
    ]

    assert budgets[0].video_average_bitrate == 6_664_000
    assert budgets[0].video_average_bitrate - budgets[1].video_average_bitrate == 201_600
    assert budgets[1].video_average_bitrate - budgets[2].video_average_bitrate == 201_600


def test_nvenc_budget_prorates_a_duration_limited_source():
    budget = lan_media.build_nvenc_output_budget(
        120_000_000,
        240_000_000,
        source_info(),
        duration_limit=30.0,
    )

    assert budget.effective_source_bytes == 30_000_000
    assert budget.target_output_bytes == 25_500_000
    assert budget.ffmpeg_limit_bytes == 27_000_000
    assert budget.hard_limit_bytes == 28_500_000


@pytest.mark.parametrize(
    ("source_bytes", "reservation", "duration"),
    [
        (0, 240_000_000, 120.0),
        (120_000_000, 0, 120.0),
        (120_000_000, 240_000_000, 0.0),
        (120_000_000, 240_000_000, float("nan")),
    ],
)
def test_nvenc_budget_rejects_invalid_or_insufficient_inputs(
    source_bytes,
    reservation,
    duration,
):
    info = source_info()
    info = lan_media.MediaInfo(
        **{
            **info.__dict__,
            "duration": duration,
        }
    )

    with pytest.raises(lan_media.MediaContractError) as raised:
        lan_media.build_nvenc_output_budget(
            source_bytes,
            reservation,
            info,
        )

    assert raised.value.category == "OutputBudgetInsufficient"


def test_encode_rejects_candidate_above_hard_cap_before_validation(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source.mp4"
    candidate = tmp_path / "candidate.mkv"
    source.write_bytes(b"s" * 1_000_000)
    info = source_info()
    info = lan_media.MediaInfo(
        **{
            **info.__dict__,
            "duration": 10.0,
            "audio_streams": (),
        }
    )
    monkeypatch.setattr(lan_media, "probe_media", lambda *_args: info)
    monkeypatch.setattr(
        lan_media,
        "decode_audio_durations",
        lambda *_args, **_kwargs: (),
    )

    def fake_run(_command, **_kwargs):
        candidate.write_bytes(b"c" * 950_001)
        return 0, lan_media.DecodeStats(True, 240, 10.0), 1.0

    monkeypatch.setattr(lan_media, "run_ffmpeg", fake_run)
    monkeypatch.setattr(
        lan_media,
        "validate_candidate_with_evidence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("oversized candidate reached validation")
        ),
    )

    with pytest.raises(lan_media.MediaContractError) as rejected:
        lan_media.encode_candidate(
            "ffmpeg",
            "ffprobe",
            str(source),
            str(candidate),
            "hevc_nvenc",
            2_000_000,
        )

    assert rejected.value.category == "CandidateSizeNotBeneficial"


def test_parse_progress_uses_final_values():
    stats = lan_media.parse_progress(
        [
            "frame=10\n",
            "out_time_us=1000000\n",
            "progress=continue\n",
            "frame=25\n",
            "out_time_us=2500000\n",
            "progress=end\n",
        ]
    )
    assert stats.success is True
    assert stats.frame_count == 25
    assert stats.out_time_seconds == 2.5


def test_run_ffmpeg_parses_progress_incrementally(monkeypatch):
    class Process:
        def __init__(self):
            self.stdout = iter(
                [
                    *(f"frame={index}\n" for index in range(1, 5001)),
                    "out_time_us=2500000\n",
                    "progress=end\n",
                ]
            )
            self.stderr = iter(())
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(
        lan_media.subprocess, "Popen", lambda *_args, **_kwargs: Process()
    )
    monkeypatch.setattr(
        lan_media,
        "parse_progress",
        lambda _lines: (_ for _ in ()).throw(
            AssertionError("batch reparsing is forbidden")
        ),
    )

    exit_code, stats, _elapsed = lan_media.run_ffmpeg(["ffmpeg"])

    assert exit_code == 0
    assert stats.success
    assert stats.frame_count == 5000
    assert stats.out_time_seconds == 2.5


def test_run_ffmpeg_cancels_silent_process_with_bounded_escalation(
    monkeypatch,
):
    released = threading.Event()

    class SilentStdout:
        def __iter__(self):
            return self

        def __next__(self):
            released.wait(5)
            raise StopIteration

    class Process:
        def __init__(self):
            self.stdout = SilentStdout()
            self.stderr = iter(())
            self.returncode = None
            self.terminated = False
            self.killed = False
            self.wait_timeouts = []

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            if self.returncode is None:
                raise subprocess.TimeoutExpired("ffmpeg", timeout)
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True
            self.returncode = -9
            released.set()

    process = Process()
    monkeypatch.setattr(
        lan_media.subprocess, "Popen", lambda *_args, **_kwargs: process
    )
    cancel = threading.Event()
    timer = threading.Timer(0.05, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        exit_code, stats, _elapsed = lan_media.run_ffmpeg(
            ["ffmpeg"], cancel_event=cancel
        )
    finally:
        timer.cancel()
        released.set()

    assert time.monotonic() - started < 1.0
    assert exit_code == -9
    assert not stats.success
    assert process.terminated
    assert process.killed
    assert process.wait_timeouts == [0.25, 0.25]


def test_media_info_rejects_incomplete_hdr_metadata():
    probe = {
        "format": {"duration": "10"},
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "hevc",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "24/1",
                "color_transfer": "smpte2084",
                "color_primaries": "bt709",
                "color_space": "bt2020nc",
                "color_range": "tv",
            }
        ],
    }
    with pytest.raises(lan_media.MediaContractError) as raised:
        lan_media.media_info_from_probe(probe)
    assert raised.value.category == "UnsafeHdrMetadata"


def test_media_info_rejects_unknown_extra_stream():
    probe = {
        "format": {"duration": "10"},
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "hevc",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "24/1",
            },
            {"index": 1, "codec_type": "data"},
        ],
    }
    with pytest.raises(lan_media.MediaContractError) as raised:
        lan_media.media_info_from_probe(probe)
    assert raised.value.category == "UnsupportedExtraStreams"


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        "..",
        "../movie.mkv",
        r"..\movie.mkv",
        r"folder\movie.mkv",
        "C:movie.mkv",
        "/movie.mkv",
    ],
)
def test_top_level_relative_name_rejects_traversal(value):
    with pytest.raises(lan_media.MediaContractError):
        lan_media.validate_top_level_relative_name(value)


def test_top_level_relative_name_accepts_basename():
    assert (
        lan_media.validate_top_level_relative_name("opaque-input.mkv")
        == "opaque-input.mkv"
    )


def test_probe_entry_expression_is_one_argument(monkeypatch):
    captured = {}

    class Result:
        returncode = 0
        stdout = json.dumps({"format": {}, "streams": []})

    def fake_run(command, **kwargs):
        captured["command"] = command
        return Result()

    monkeypatch.setattr(lan_media.subprocess, "run", fake_run)
    lan_media.probe_json("ffprobe", "source")
    command = captured["command"]
    index = command.index("-show_entries")
    assert isinstance(command[index + 1], str)
    assert "format=duration" in command[index + 1]
    assert command[index + 2] == "-of"


def test_candidate_evidence_constructor_remains_backward_compatible():
    evidence = lan_media.CandidateEvidence("A" * 64, 10, 20, 1.5)

    assert evidence.full_decode is None
    with pytest.raises(lan_media.MediaContractError) as raised:
        lan_media.build_producer_validation_evidence(
            evidence,
            run_id="run",
            job_id="job",
            worker_id="worker",
            worker_role="helper",
            attempt_id="attempt",
            fencing_epoch=1,
            contract_hash="B" * 64,
            producer_build_sha256="C" * 64,
            ffmpeg_sha256="D" * 64,
            ffprobe_sha256="E" * 64,
        )
    assert raised.value.category == "FullDecodeEvidenceMissing"


def test_producer_full_evidence_round_trips_with_verified_digest():
    evidence = producer_evidence()
    serialized = evidence.to_dict()
    wire_value = json.loads(json.dumps(serialized))

    restored = lan_media.ProducerValidationEvidence.from_dict(wire_value)

    assert restored == evidence
    assert restored.schema_version == lan_media.VALIDATION_EVIDENCE_SCHEMA
    assert restored.mode == lan_media.PRODUCER_FULL_VALIDATION_MODE
    assert restored.candidate_sha256 == "A" * 64
    assert restored.full_decode.video.frame_count == 3600
    assert [item.out_time_seconds for item in restored.full_decode.audio] == [
        120.0,
        119.98,
    ]
    assert serialized["evidence_digest"] == evidence.digest()
    assert evidence.digest() == producer_evidence().digest()
    assert {
        "source_path",
        "candidate_path",
        "candidate_name",
        "filename",
    }.isdisjoint(serialized)


def test_producer_full_digest_covers_every_field_and_nested_decode_value():
    serialized = producer_evidence().to_dict()
    original_digest = serialized["evidence_digest"]
    mutations = (
        (("schema_version",), 2),
        (("mode",), "other-mode"),
        (("run_id",), "run-other"),
        (("job_id",), "job-other"),
        (("worker_id",), "worker-other"),
        (("worker_role",), "remote"),
        (("attempt_id",), "attempt-other"),
        (("fencing_epoch",), 8),
        (("contract_hash",), "1" * 64),
        (("producer_build_sha256",), "2" * 64),
        (("ffmpeg_sha256",), "3" * 64),
        (("ffprobe_sha256",), "4" * 64),
        (("candidate_sha256",), "5" * 64),
        (("candidate_bytes",), 123457),
        (("encoded_frame_count",), 3601),
        (("full_decode", "video", "stream_index"), 3),
        (("full_decode", "video", "stream_type"), "audio"),
        (("full_decode", "video", "success"), False),
        (("full_decode", "video", "frame_count"), 3601),
        (("full_decode", "video", "out_time_seconds"), 120.25),
        (("full_decode", "audio", 0, "stream_index"), 4),
        (("full_decode", "audio", 0, "stream_type"), "video"),
        (("full_decode", "audio", 0, "success"), False),
        (("full_decode", "audio", 0, "frame_count"), 1),
        (("full_decode", "audio", 0, "out_time_seconds"), 119.5),
    )

    for path, replacement in mutations:
        tampered = copy.deepcopy(serialized)
        target = tampered
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = replacement
        payload = dict(tampered)
        payload.pop("evidence_digest")
        recalculated = hashlib.sha256(
            json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest().upper()
        assert recalculated != original_digest
        with pytest.raises(lan_media.MediaContractError):
            lan_media.ProducerValidationEvidence.from_dict(tampered)


def test_producer_full_evidence_requires_a_wire_digest():
    serialized = producer_evidence().canonical_dict()

    with pytest.raises(lan_media.MediaContractError) as raised:
        lan_media.ProducerValidationEvidence.from_dict(serialized)

    assert raised.value.category == "ValidationEvidenceDigestMissing"


def test_metadata_validator_does_not_decode_candidate(
    tmp_path, monkeypatch
):
    candidate = tmp_path / "candidate.mkv"
    candidate.write_bytes(b"candidate")
    monkeypatch.setattr(
        lan_media, "probe_json", lambda _ffprobe, _path: candidate_probe()
    )
    monkeypatch.setattr(
        lan_media,
        "decode_stream_stats",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("metadata validation must not decode")
        ),
    )

    result = lan_media.validate_candidate_metadata(
        "ffprobe",
        str(candidate),
        source_info(),
        (120.0, 120.0),
    )

    assert result is not None
    assert result.video_index == 0
    assert [item.index for item in result.audio_streams] == [1, 2]


def test_full_validator_returns_all_decode_evidence_and_legacy_api_stays_bool(
    tmp_path, monkeypatch
):
    candidate = tmp_path / "candidate.mkv"
    candidate.write_bytes(b"candidate")
    monkeypatch.setattr(
        lan_media, "probe_json", lambda _ffprobe, _path: candidate_probe()
    )
    calls = []

    def fake_decode(_ffmpeg, _path, stream_index, stream_type, **_kwargs):
        calls.append((stream_index, stream_type))
        if stream_type == "video":
            return lan_media.DecodeStats(True, 3600, 120.0)
        duration = 120.0 if stream_index == 1 else 119.98
        return lan_media.DecodeStats(True, 0, duration)

    monkeypatch.setattr(lan_media, "decode_stream_stats", fake_decode)

    evidence = lan_media.validate_candidate_with_evidence(
        "ffmpeg",
        "ffprobe",
        str(candidate),
        source_info(),
        (120.0, 120.0),
        3600,
    )

    assert evidence == full_decode_evidence()
    assert calls == [(1, "audio"), (2, "audio"), (0, "video")]
    calls.clear()
    assert lan_media.validate_candidate(
        "ffmpeg",
        "ffprobe",
        str(candidate),
        source_info(),
        (120.0, 120.0),
        3600,
        decode_to_end=False,
    )
    assert calls == [(1, "audio"), (2, "audio")]
    calls.clear()
    assert lan_media.validate_candidate(
        "ffmpeg",
        "ffprobe",
        str(candidate),
        source_info(),
        (120.0, 120.0),
        3600,
    )
    assert calls == [(1, "audio"), (2, "audio"), (0, "video")]


def test_full_validator_rejects_incomplete_audio_decode(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate.mkv"
    candidate.write_bytes(b"candidate")
    monkeypatch.setattr(
        lan_media, "probe_json", lambda _ffprobe, _path: candidate_probe()
    )

    def fake_decode(_ffmpeg, _path, stream_index, stream_type, **_kwargs):
        if stream_index == 2:
            return lan_media.DecodeStats(False, 0, 20.0)
        if stream_type == "video":
            return lan_media.DecodeStats(True, 3600, 120.0)
        return lan_media.DecodeStats(True, 0, 120.0)

    monkeypatch.setattr(lan_media, "decode_stream_stats", fake_decode)

    assert (
        lan_media.validate_candidate_with_evidence(
            "ffmpeg",
            "ffprobe",
            str(candidate),
            source_info(),
            (120.0, 120.0),
            3600,
        )
        is None
    )
