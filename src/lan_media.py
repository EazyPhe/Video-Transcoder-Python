"""Content-blind media contract used by LAN transcoding workers.

This module deliberately separates candidate production from publication.
Workers may create and validate an attempt-specific candidate, but only the
storage-host coordinator is allowed to publish it or delete a source.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence


SUPPORTED_EXTENSIONS = frozenset(
    {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"}
)
CONTRACT_SCHEMA = 1
MAX_WIDTH = 1920
MAX_HEIGHT = 1080
AAC_TARGET_BITRATE = 192_000


class MediaContractError(RuntimeError):
    """A path-redacted media failure with a stable machine category."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class AudioInfo:
    index: int
    duration: float
    sample_rate: int


@dataclass(frozen=True)
class MediaInfo:
    video_index: int
    video_codec: str
    width: int
    height: int
    duration: float
    audio_streams: tuple[AudioInfo, ...]
    average_frame_rate: str
    transfer: str
    primaries: str
    matrix: str
    color_range: str
    is_hdr: bool

    @property
    def audio_count(self) -> int:
        return len(self.audio_streams)


@dataclass(frozen=True)
class DecodeStats:
    success: bool
    frame_count: int
    out_time_seconds: float


@dataclass(frozen=True)
class CandidateEvidence:
    sha256: str
    encoded_frame_count: int
    output_bytes: int
    encode_seconds: float


@dataclass(frozen=True)
class MediaContract:
    max_width: int = MAX_WIDTH
    max_height: int = MAX_HEIGHT
    video_codec: str = "hevc"
    quality: int = 22
    audio_codec: str = "aac"
    audio_channels: int = 2
    audio_bitrate: int = AAC_TARGET_BITRATE
    container: str = "matroska"
    tone_map: str = "hable"
    preserve_frame_timing: bool = True
    remove_subtitles: bool = True

    def digest(self) -> str:
        payload = {
            "schema": CONTRACT_SCHEMA,
            **asdict(self),
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest().upper()


DEFAULT_CONTRACT = MediaContract()
_RATIONAL = re.compile(r"^(-?\d+)/([1-9]\d*)$")


def _value(mapping: object, key: str, default: object = "") -> object:
    if isinstance(mapping, dict):
        return mapping.get(key, default)
    return default


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def rational_to_float(value: str) -> float:
    match = _RATIONAL.fullmatch(value or "")
    if not match:
        return 0.0
    return int(match.group(1)) / int(match.group(2))


def expected_dimensions(
    source_width: int,
    source_height: int,
    max_width: int = MAX_WIDTH,
    max_height: int = MAX_HEIGHT,
) -> tuple[int, int]:
    if source_width < 2 or source_height < 2:
        raise MediaContractError("SourceDimensionsInvalid")
    factor = min(
        1.0,
        max_width / float(source_width),
        max_height / float(source_height),
    )
    width = max(2, int(math.floor(source_width * factor / 2.0 + 0.5) * 2))
    height = max(2, int(math.floor(source_height * factor / 2.0 + 0.5) * 2))
    # Hardware HEVC encoders need even dimensions. Nearest-even rounding can
    # otherwise turn an odd-sized source into a one-pixel upscale.
    width = min(width, source_width - (source_width % 2))
    height = min(height, source_height - (source_height % 2))
    return width, height


def _probe_entries() -> str:
    return (
        "format=duration,format_name,size:"
        "stream=index,codec_type,codec_name,width,height,duration,start_time,"
        "avg_frame_rate,r_frame_rate,bit_rate,"
        "color_transfer,color_primaries,color_space,color_range,pix_fmt,"
        "channels,channel_layout,sample_rate:"
        "stream_disposition=attached_pic:"
        "stream_side_data=side_data_type,dv_profile"
    )


def probe_json(ffprobe: str, path: str, timeout: float = 60.0) -> dict:
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                _probe_entries(),
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if os.name == "nt"
                else 0
            ),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaContractError("ProbeUnavailable") from exc
    if completed.returncode != 0:
        raise MediaContractError("ProbeFailed")
    try:
        value = json.loads(completed.stdout)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise MediaContractError("ProbeInvalidJson") from exc
    if not isinstance(value, dict):
        raise MediaContractError("ProbeInvalidJson")
    return value


def media_info_from_probe(probe: dict) -> MediaInfo:
    raw_streams = _value(probe, "streams", [])
    streams = raw_streams if isinstance(raw_streams, list) else []
    video_streams: list[dict] = []
    audio_streams: list[dict] = []
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        stream_type = str(_value(stream, "codec_type", ""))
        disposition = _value(stream, "disposition", {})
        attached = _as_int(_value(disposition, "attached_pic", 0)) == 1
        if stream_type == "video" and not attached:
            video_streams.append(stream)
        elif stream_type == "video" and attached:
            raise MediaContractError("AttachedPicturesUnsupported")
        elif stream_type == "audio":
            audio_streams.append(stream)
        elif stream_type not in {"subtitle"}:
            raise MediaContractError("UnsupportedExtraStreams")

    if not video_streams:
        raise MediaContractError("NoVideoStream")
    if len(video_streams) != 1:
        raise MediaContractError("MultipleVideoStreamsUnsupported")

    video = video_streams[0]
    for side_data in _value(video, "side_data_list", []) or []:
        side_type = str(_value(side_data, "side_data_type", ""))
        if "DOVI" in side_type.upper() or "DOLBY VISION" in side_type.upper():
            raise MediaContractError("UnsupportedHdrVariant")

    raw_format = _value(probe, "format", {})
    duration = _as_float(_value(raw_format, "duration", 0.0))
    if duration <= 0.0:
        duration = _as_float(_value(video, "duration", 0.0))
    if duration <= 0.0:
        raise MediaContractError("DurationUnavailable")

    width = _as_int(_value(video, "width", 0))
    height = _as_int(_value(video, "height", 0))
    expected_dimensions(width, height)

    transfer = str(_value(video, "color_transfer", ""))
    primaries = str(_value(video, "color_primaries", ""))
    matrix = str(_value(video, "color_space", ""))
    color_range = str(_value(video, "color_range", ""))
    hdr_transfer = transfer in {"smpte2084", "arib-std-b67"}
    bt2020_primaries = primaries == "bt2020"
    bt2020_matrix = matrix in {"bt2020nc", "bt2020c"}
    is_hdr = hdr_transfer and bt2020_primaries and bt2020_matrix
    if hdr_transfer and (
        not bt2020_primaries
        or not bt2020_matrix
        or color_range != "tv"
    ):
        raise MediaContractError("UnsafeHdrMetadata")
    if not hdr_transfer and (bt2020_primaries or bt2020_matrix):
        raise MediaContractError("AmbiguousBt2020")

    audio = tuple(
        AudioInfo(
            index=_as_int(_value(stream, "index", -1), -1),
            duration=_as_float(_value(stream, "duration", 0.0)),
            sample_rate=_as_int(_value(stream, "sample_rate", 0)),
        )
        for stream in audio_streams
    )
    if any(item.index < 0 or item.sample_rate <= 0 for item in audio):
        raise MediaContractError("AudioMetadataInvalid")

    return MediaInfo(
        video_index=_as_int(_value(video, "index", 0)),
        video_codec=str(_value(video, "codec_name", "")),
        width=width,
        height=height,
        duration=duration,
        audio_streams=audio,
        average_frame_rate=str(_value(video, "avg_frame_rate", "")),
        transfer=transfer,
        primaries=primaries,
        matrix=matrix,
        color_range=color_range,
        is_hdr=is_hdr,
    )


def probe_media(ffprobe: str, path: str, timeout: float = 60.0) -> MediaInfo:
    return media_info_from_probe(probe_json(ffprobe, path, timeout))


def _scale_filter(contract: MediaContract) -> str:
    return (
        f"scale=w='min({contract.max_width},iw)':"
        f"h='min({contract.max_height},ih)':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2:"
        "flags=lanczos"
    )


def video_filter(info: MediaInfo, contract: MediaContract) -> str:
    scale = _scale_filter(contract)
    if not info.is_hdr:
        return f"{scale},format=nv12"
    return (
        "zscale=t=linear:npl=100,format=gbrpf32le,"
        "zscale=p=bt709,"
        f"tonemap={contract.tone_map}:desat=0,"
        "zscale=t=bt709:m=bt709:r=tv,"
        f"{scale},format=nv12"
    )


def build_encode_command(
    ffmpeg: str,
    source: str,
    candidate: str,
    media_info: MediaInfo,
    encoder: str,
    maximum_output_bytes: int,
    *,
    duration_limit: float | None = None,
    contract: MediaContract = DEFAULT_CONTRACT,
) -> list[str]:
    if encoder not in {"hevc_qsv", "hevc_nvenc"}:
        raise MediaContractError("EncoderUnsupported")
    if maximum_output_bytes <= 0:
        raise MediaContractError("OutputLimitInvalid")

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-nostdin",
        "-xerror",
        "-progress",
        "pipe:1",
        "-i",
        source,
    ]
    if duration_limit is not None:
        if duration_limit <= 0:
            raise MediaContractError("DurationLimitInvalid")
        command.extend(["-t", f"{duration_limit:.6f}"])
    command.extend(
        [
            "-map",
            f"0:{media_info.video_index}",
            "-map",
            "0:a?",
            "-sn",
            "-dn",
            "-map_metadata",
            "0",
            "-map_chapters",
            "0",
            "-vf",
            video_filter(media_info, contract),
            "-c:v",
            encoder,
        ]
    )
    if encoder == "hevc_qsv":
        command.extend(
            [
                "-preset",
                "slower",
                "-global_quality",
                str(contract.quality),
                "-profile:v",
                "main",
            ]
        )
    else:
        command.extend(
            [
                "-preset",
                "p7",
                "-tune",
                "hq",
                "-rc",
                "vbr",
                "-cq",
                str(contract.quality),
                "-b:v",
                "0",
                "-profile:v",
                "main",
            ]
        )
    command.extend(
        [
            "-c:a",
            contract.audio_codec,
            "-ac:a",
            str(contract.audio_channels),
            "-b:a",
            f"{contract.audio_bitrate // 1000}k",
            "-fps_mode:v",
            "passthrough",
            "-max_muxing_queue_size",
            "4096",
        ]
    )
    if media_info.is_hdr:
        command.extend(
            [
                "-color_primaries",
                "bt709",
                "-color_trc",
                "bt709",
                "-colorspace",
                "bt709",
                "-color_range",
                "tv",
            ]
        )
    command.extend(
        [
            "-fs",
            str(maximum_output_bytes),
            "-f",
            "matroska",
            "-n",
            candidate,
        ]
    )
    return command


def parse_progress(lines: Iterable[str]) -> DecodeStats:
    frame_count = 0
    out_time_us = 0
    saw_end = False
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("frame="):
            frame_count = max(frame_count, _as_int(line[6:].strip()))
        elif line.startswith("out_time_us="):
            out_time_us = max(out_time_us, _as_int(line[12:].strip()))
        elif line == "progress=end":
            saw_end = True
    return DecodeStats(
        success=saw_end,
        frame_count=frame_count,
        out_time_seconds=out_time_us / 1_000_000.0,
    )


def run_ffmpeg(
    command: Sequence[str],
    *,
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[[DecodeStats], None] | None = None,
) -> tuple[int, DecodeStats, float]:
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if os.name == "nt"
                else 0
            ),
        )
    except OSError as exc:
        raise MediaContractError("FfmpegUnavailable") from exc

    def drain_stderr() -> None:
        if process.stderr is None:
            return
        # Drain without retaining diagnostics. FFmpeg messages can contain
        # private paths and a long-running error stream must stay bounded.
        for _line in process.stderr:
            pass

    stdout_lines: queue.Queue[object] = queue.Queue(maxsize=256)
    stdout_closed = object()
    reader_stop = threading.Event()

    def enqueue_stdout(value: object) -> bool:
        while not reader_stop.is_set():
            try:
                stdout_lines.put(value, timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def drain_stdout() -> None:
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    if not enqueue_stdout(line):
                        return
        finally:
            enqueue_stdout(stdout_closed)

    def wait_bounded(timeout: float) -> bool:
        try:
            process.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def stop_bounded() -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError:
            pass
        if wait_bounded(0.25):
            return
        try:
            process.kill()
        except OSError:
            pass
        if not wait_bounded(0.25):
            raise MediaContractError("FfmpegTerminationFailed")

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stdout_thread = threading.Thread(target=drain_stdout, daemon=True)
    stderr_thread.start()
    stdout_thread.start()
    frame_count = 0
    out_time_us = 0
    saw_end = False
    stdout_finished = False
    exit_seen_at: float | None = None
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                break
            try:
                queued = stdout_lines.get(timeout=0.05)
            except queue.Empty:
                queued = None
            if queued is stdout_closed:
                stdout_finished = True
            elif isinstance(queued, str):
                line = queued
                stripped = line.strip()
                changed = False
                if stripped.startswith("frame="):
                    frame_count = max(
                        frame_count, _as_int(stripped[6:].strip())
                    )
                    changed = True
                elif stripped.startswith("out_time_us="):
                    out_time_us = max(
                        out_time_us, _as_int(stripped[12:].strip())
                    )
                    changed = True
                elif stripped == "progress=end":
                    saw_end = True
                    changed = True
                elif stripped.startswith("progress="):
                    changed = True
                if changed:
                    current = DecodeStats(
                        saw_end,
                        frame_count,
                        out_time_us / 1_000_000.0,
                    )
                    if progress_callback is not None:
                        progress_callback(current)
            if process.poll() is not None:
                if stdout_finished:
                    break
                if exit_seen_at is None:
                    exit_seen_at = time.monotonic()
                elif time.monotonic() - exit_seen_at >= 5.0:
                    # A dead child cannot be allowed to strand shutdown on a
                    # pipe reader. Any already queued progress was consumed.
                    break
    finally:
        reader_stop.set()
        if process.poll() is None:
            stop_bounded()
        stdout_thread.join(timeout=0.5)
        stderr_thread.join(timeout=0.5)
    elapsed = time.monotonic() - started
    stats = DecodeStats(
        saw_end,
        frame_count,
        out_time_us / 1_000_000.0,
    )
    if process.returncode == 0 and not stats.success:
        stats = DecodeStats(True, stats.frame_count, stats.out_time_seconds)
    return int(process.returncode or 0), stats, elapsed


def decode_stream_stats(
    ffmpeg: str,
    path: str,
    stream_index: int,
    stream_type: str,
    *,
    cancel_event: threading.Event | None = None,
) -> DecodeStats:
    if stream_type not in {"video", "audio"}:
        raise MediaContractError("DecodeStreamTypeInvalid")
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-nostdin",
        "-xerror",
        "-progress",
        "pipe:1",
        "-i",
        path,
        "-map",
        f"0:{stream_index}",
    ]
    command.extend(
        ["-an", "-sn", "-dn"]
        if stream_type == "video"
        else ["-vn", "-sn", "-dn"]
    )
    command.extend(["-f", "null", "NUL" if os.name == "nt" else "/dev/null"])
    exit_code, stats, _elapsed = run_ffmpeg(
        command, cancel_event=cancel_event
    )
    return DecodeStats(
        success=exit_code == 0 and stats.success,
        frame_count=stats.frame_count,
        out_time_seconds=stats.out_time_seconds,
    )


def decode_audio_durations(
    ffmpeg: str,
    path: str,
    media_info: MediaInfo,
    maximum_duration: float = 0.0,
    *,
    cancel_event: threading.Event | None = None,
) -> tuple[float, ...]:
    durations: list[float] = []
    for audio in media_info.audio_streams:
        stats = decode_stream_stats(
            ffmpeg,
            path,
            audio.index,
            "audio",
            cancel_event=cancel_event,
        )
        if not stats.success or stats.out_time_seconds <= 0:
            raise MediaContractError("AudioDecodeValidationFailed")
        duration = stats.out_time_seconds
        if maximum_duration > 0:
            duration = min(duration, maximum_duration)
        durations.append(duration)
    return tuple(durations)


def _duration_tolerance(duration: float) -> float:
    return max(0.5, min(2.0, duration * 0.005))


def validate_candidate(
    ffmpeg: str,
    ffprobe: str,
    candidate: str,
    source_info: MediaInfo,
    expected_audio_durations: Sequence[float],
    expected_frame_count: int,
    *,
    decode_to_end: bool = True,
    expected_duration: float | None = None,
    contract: MediaContract = DEFAULT_CONTRACT,
    cancel_event: threading.Event | None = None,
) -> bool:
    try:
        if not os.path.isfile(candidate) or os.path.getsize(candidate) <= 0:
            return False
        probe = probe_json(ffprobe, candidate)
        raw_streams = _value(probe, "streams", [])
        streams = raw_streams if isinstance(raw_streams, list) else []
        videos = [
            stream
            for stream in streams
            if isinstance(stream, dict)
            and str(_value(stream, "codec_type", "")) == "video"
        ]
        if len(videos) != 1:
            return False
        video = videos[0]
        if str(_value(video, "codec_name", "")) != contract.video_codec:
            return False
        width = _as_int(_value(video, "width", 0))
        height = _as_int(_value(video, "height", 0))
        expected_width, expected_height = expected_dimensions(
            source_info.width,
            source_info.height,
            contract.max_width,
            contract.max_height,
        )
        if (
            width <= 0
            or height <= 0
            or width > contract.max_width
            or height > contract.max_height
            or abs(width - expected_width) > 2
            or abs(height - expected_height) > 2
            or width > source_info.width
            or height > source_info.height
        ):
            return False
        source_aspect = source_info.width / float(source_info.height)
        output_aspect = width / float(height)
        if abs(source_aspect - output_aspect) / source_aspect > 0.01:
            return False

        audio = [
            stream
            for stream in streams
            if isinstance(stream, dict)
            and str(_value(stream, "codec_type", "")) == "audio"
        ]
        if len(audio) != source_info.audio_count:
            return False
        if len(expected_audio_durations) != source_info.audio_count:
            return False
        for index, stream in enumerate(audio):
            if str(_value(stream, "codec_name", "")) != contract.audio_codec:
                return False
            if _as_int(_value(stream, "channels", 0)) != contract.audio_channels:
                return False
            layout = str(_value(stream, "channel_layout", ""))
            if layout and layout != "stereo":
                return False
            bit_rate = _as_int(_value(stream, "bit_rate", 0))
            if bit_rate and not 150_000 <= bit_rate <= 235_000:
                return False
            if _as_int(_value(stream, "sample_rate", 0)) != (
                source_info.audio_streams[index].sample_rate
            ):
                return False

        if any(
            isinstance(stream, dict)
            and str(_value(stream, "codec_type", "")) == "subtitle"
            for stream in streams
        ):
            return False
        raw_format = _value(probe, "format", {})
        format_name = str(_value(raw_format, "format_name", ""))
        if contract.container not in format_name.split(","):
            return False
        actual_duration = _as_float(_value(raw_format, "duration", 0.0))
        target_duration = (
            source_info.duration
            if expected_duration is None
            else expected_duration
        )
        if (
            actual_duration <= 0
            or abs(actual_duration - target_duration)
            > _duration_tolerance(target_duration)
        ):
            return False

        if source_info.is_hdr:
            if (
                str(_value(video, "color_transfer", "")) != "bt709"
                or str(_value(video, "color_primaries", "")) != "bt709"
                or str(_value(video, "color_space", "")) != "bt709"
                or str(_value(video, "color_range", "")) != "tv"
            ):
                return False

        source_fps = rational_to_float(source_info.average_frame_rate)
        actual_fps = rational_to_float(
            str(_value(video, "avg_frame_rate", ""))
        )
        if source_fps > 0 and actual_fps > 0:
            if abs(source_fps - actual_fps) > max(0.01, source_fps * 0.002):
                return False

        output_info = media_info_from_probe(probe)
        actual_audio_durations = decode_audio_durations(
            ffmpeg,
            candidate,
            output_info,
            cancel_event=cancel_event,
        )
        for expected, actual in zip(
            expected_audio_durations, actual_audio_durations
        ):
            if abs(expected - actual) > _duration_tolerance(expected):
                return False

        if decode_to_end:
            decoded = decode_stream_stats(
                ffmpeg,
                candidate,
                output_info.video_index,
                "video",
                cancel_event=cancel_event,
            )
            if not decoded.success:
                return False
            if (
                expected_frame_count > 0
                and decoded.frame_count != expected_frame_count
            ):
                return False
        return True
    except (OSError, MediaContractError, ValueError, ZeroDivisionError):
        return False


def sha256_file(path: str, block_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(block_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest().upper()


def encode_candidate(
    ffmpeg: str,
    ffprobe: str,
    source: str,
    candidate: str,
    encoder: str,
    maximum_output_bytes: int,
    *,
    duration_limit: float | None = None,
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[[DecodeStats], None] | None = None,
    metadata_callback: Callable[[MediaInfo], None] | None = None,
    phase_callback: Callable[[str], None] | None = None,
    contract: MediaContract = DEFAULT_CONTRACT,
) -> tuple[MediaInfo, tuple[float, ...], CandidateEvidence]:
    if phase_callback is not None:
        phase_callback("SourceRead")
    source_info = probe_media(ffprobe, source)
    if metadata_callback is not None:
        metadata_callback(source_info)
    audio_durations = decode_audio_durations(
        ffmpeg,
        source,
        source_info,
        maximum_duration=duration_limit or 0.0,
        cancel_event=cancel_event,
    )
    command = build_encode_command(
        ffmpeg,
        source,
        candidate,
        source_info,
        encoder,
        maximum_output_bytes,
        duration_limit=duration_limit,
        contract=contract,
    )
    if phase_callback is not None:
        phase_callback("Converting")
    exit_code, stats, elapsed = run_ffmpeg(
        command,
        cancel_event=cancel_event,
        progress_callback=progress_callback,
    )
    if exit_code != 0:
        raise MediaContractError("EncodeFailed")
    if stats.frame_count <= 0:
        raise MediaContractError("EncodedFrameCountUnavailable")
    expected_duration = min(
        source_info.duration, duration_limit
    ) if duration_limit else source_info.duration
    if phase_callback is not None:
        phase_callback("LocalValidation")
    if not validate_candidate(
        ffmpeg,
        ffprobe,
        candidate,
        source_info,
        audio_durations,
        stats.frame_count,
        expected_duration=expected_duration,
        contract=contract,
        cancel_event=cancel_event,
    ):
        raise MediaContractError("CandidateValidationFailed")
    return (
        source_info,
        audio_durations,
        CandidateEvidence(
            sha256=sha256_file(candidate),
            encoded_frame_count=stats.frame_count,
            output_bytes=os.path.getsize(candidate),
            encode_seconds=elapsed,
        ),
    )


def redact_path(path: str) -> str:
    """Return a stable opaque identifier suitable for aggregate diagnostics."""
    normalized = os.path.normcase(os.path.abspath(path))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def validate_top_level_relative_name(value: str) -> str:
    """Accept exactly one top-level filename and reject path traversal."""
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or Path(value).name != value
        or "/" in value
        or "\\" in value
        or ":" in value
    ):
        raise MediaContractError("RelativeNameInvalid")
    return value
