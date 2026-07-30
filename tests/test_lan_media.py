from __future__ import annotations

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
        123456789,
    )

    assert command[command.index("-c:v") + 1] == "hevc_nvenc"
    assert command[command.index("-preset") + 1] == "p7"
    assert command[command.index("-cq") + 1] == "22"
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
