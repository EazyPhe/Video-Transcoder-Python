"""Content-blind media contract used by LAN transcoding workers.

This module deliberately separates candidate production from publication.
Workers may create and validate an attempt-specific candidate, but only the
storage-host coordinator is allowed to publish it or delete a source.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Callable, Iterable, Sequence


SUPPORTED_EXTENSIONS = frozenset(
    {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"}
)
CONTRACT_SCHEMA = 1
VALIDATION_EVIDENCE_SCHEMA = 1
PRODUCER_FULL_VALIDATION_MODE = "producer-full"
MAX_WIDTH = 1920
MAX_HEIGHT = 1080
AAC_TARGET_BITRATE = 192_000
NVENC_TARGET_SOURCE_RATIO = 0.85
NVENC_FFMPEG_LIMIT_SOURCE_RATIO = 0.90
MAXIMUM_CANDIDATE_SOURCE_RATIO = 0.95
NVENC_MUX_OVERHEAD_RATIO = 0.02
MINIMUM_MUX_OVERHEAD_BYTES = 64 * 1024
AAC_BUDGET_HEADROOM = 1.05
MINIMUM_NVENC_VIDEO_BITRATE = 100_000
_SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


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
class NvencOutputBudget:
    """Deterministic byte and bitrate limits for one NVENC candidate."""

    effective_source_bytes: int
    target_output_bytes: int
    ffmpeg_limit_bytes: int
    hard_limit_bytes: int
    video_average_bitrate: int
    video_maximum_bitrate: int
    video_buffer_size: int


@dataclass(frozen=True)
class DecodeStats:
    success: bool
    frame_count: int
    out_time_seconds: float


@dataclass(frozen=True)
class StreamDecodeEvidence:
    """Path-free proof that one exact output stream decoded to EOS."""

    stream_index: int
    stream_type: str
    success: bool
    frame_count: int
    out_time_seconds: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.stream_index, bool)
            or not isinstance(self.stream_index, int)
            or self.stream_index < 0
        ):
            raise ValueError("stream_index must be a non-negative integer")
        if self.stream_type not in {"video", "audio"}:
            raise ValueError("stream_type must be video or audio")
        if not isinstance(self.success, bool):
            raise ValueError("success must be bool")
        if (
            isinstance(self.frame_count, bool)
            or not isinstance(self.frame_count, int)
            or self.frame_count < 0
        ):
            raise ValueError("frame_count must be a non-negative integer")
        if (
            isinstance(self.out_time_seconds, bool)
            or not isinstance(self.out_time_seconds, (int, float))
            or not math.isfinite(self.out_time_seconds)
            or self.out_time_seconds < 0
        ):
            raise ValueError("out_time_seconds must be finite and non-negative")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> "StreamDecodeEvidence":
        if not isinstance(value, dict):
            raise MediaContractError("ValidationEvidenceInvalid")
        expected = {item.name for item in fields(cls)}
        if set(value) != expected:
            raise MediaContractError("ValidationEvidenceInvalid")
        try:
            return cls(**value)
        except (TypeError, ValueError) as exc:
            raise MediaContractError("ValidationEvidenceInvalid") from exc


@dataclass(frozen=True)
class FullDecodeEvidence:
    """Complete video and ordered audio-stream decode results."""

    video: StreamDecodeEvidence
    audio: tuple[StreamDecodeEvidence, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.video, StreamDecodeEvidence):
            raise ValueError("video must be StreamDecodeEvidence")
        if self.video.stream_type != "video":
            raise ValueError("video evidence must describe a video stream")
        try:
            normalized_audio = tuple(self.audio)
        except TypeError as exc:
            raise ValueError("audio must be an iterable of evidence") from exc
        if any(
            not isinstance(item, StreamDecodeEvidence)
            or item.stream_type != "audio"
            for item in normalized_audio
        ):
            raise ValueError("audio evidence must describe audio streams")
        if len({item.stream_index for item in normalized_audio}) != len(
            normalized_audio
        ):
            raise ValueError("audio stream indexes must be unique")
        object.__setattr__(self, "audio", normalized_audio)

    def to_dict(self) -> dict[str, object]:
        return {
            "video": self.video.to_dict(),
            "audio": [item.to_dict() for item in self.audio],
        }

    @classmethod
    def from_dict(cls, value: object) -> "FullDecodeEvidence":
        if not isinstance(value, dict) or set(value) != {"video", "audio"}:
            raise MediaContractError("ValidationEvidenceInvalid")
        raw_audio = value.get("audio")
        if not isinstance(raw_audio, list):
            raise MediaContractError("ValidationEvidenceInvalid")
        try:
            return cls(
                video=StreamDecodeEvidence.from_dict(value.get("video")),
                audio=tuple(
                    StreamDecodeEvidence.from_dict(item) for item in raw_audio
                ),
            )
        except (TypeError, ValueError) as exc:
            raise MediaContractError("ValidationEvidenceInvalid") from exc


@dataclass(frozen=True)
class CandidateEvidence:
    sha256: str
    encoded_frame_count: int
    output_bytes: int
    encode_seconds: float
    full_decode: FullDecodeEvidence | None = None


@dataclass(frozen=True)
class ProducerValidationEvidence:
    """Fenced, content-blind producer-full validation attestation."""

    schema_version: int
    mode: str
    run_id: str
    job_id: str
    worker_id: str
    worker_role: str
    attempt_id: str
    fencing_epoch: int
    contract_hash: str
    producer_build_sha256: str
    ffmpeg_sha256: str
    ffprobe_sha256: str
    candidate_sha256: str
    candidate_bytes: int
    encoded_frame_count: int
    full_decode: FullDecodeEvidence

    def __post_init__(self) -> None:
        if self.schema_version != VALIDATION_EVIDENCE_SCHEMA:
            raise ValueError("validation evidence schema is unsupported")
        if self.mode != PRODUCER_FULL_VALIDATION_MODE:
            raise ValueError("validation evidence mode is unsupported")
        for name in ("run_id", "job_id", "worker_id", "attempt_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or any(
                character in value for character in "\r\n\0"
            ):
                raise ValueError(f"{name} must be a non-empty opaque token")
        if self.worker_role not in {"helper", "remote"}:
            raise ValueError("worker_role is unsupported")
        if (
            isinstance(self.fencing_epoch, bool)
            or not isinstance(self.fencing_epoch, int)
            or self.fencing_epoch <= 0
        ):
            raise ValueError("fencing_epoch must be a positive integer")
        for name in (
            "contract_hash",
            "producer_build_sha256",
            "ffmpeg_sha256",
            "ffprobe_sha256",
            "candidate_sha256",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ValueError(f"{name} must be a SHA-256 digest")
            object.__setattr__(self, name, value.upper())
        for name in ("candidate_bytes", "encoded_frame_count"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.full_decode, FullDecodeEvidence):
            raise ValueError("full_decode must be FullDecodeEvidence")
        if (
            not self.full_decode.video.success
            or self.full_decode.video.out_time_seconds <= 0
            or self.full_decode.video.frame_count != self.encoded_frame_count
            or any(
                not item.success or item.out_time_seconds <= 0
                for item in self.full_decode.audio
            )
        ):
            raise ValueError("full decode evidence is incomplete")

    def canonical_dict(self) -> dict[str, object]:
        """Return the exact, digest-covered JSON payload."""

        return json.loads(_canonical_json_bytes(asdict(self)).decode("utf-8"))

    def digest(self) -> str:
        return hashlib.sha256(
            _canonical_json_bytes(asdict(self))
        ).hexdigest().upper()

    def to_dict(self) -> dict[str, object]:
        value = self.canonical_dict()
        value["evidence_digest"] = self.digest()
        return value

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        require_digest: bool = True,
    ) -> "ProducerValidationEvidence":
        if not isinstance(value, dict):
            raise MediaContractError("ValidationEvidenceInvalid")
        raw = dict(value)
        supplied_digest = raw.pop("evidence_digest", None)
        expected = {item.name for item in fields(cls)}
        if set(raw) != expected:
            raise MediaContractError("ValidationEvidenceInvalid")
        if require_digest and supplied_digest is None:
            raise MediaContractError("ValidationEvidenceDigestMissing")
        try:
            raw["full_decode"] = FullDecodeEvidence.from_dict(
                raw["full_decode"]
            )
            evidence = cls(**raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise MediaContractError("ValidationEvidenceInvalid") from exc
        if supplied_digest is not None and (
            not isinstance(supplied_digest, str)
            or _SHA256.fullmatch(supplied_digest) is None
            or not hmac.compare_digest(
                supplied_digest.upper(), evidence.digest()
            )
        ):
            raise MediaContractError("ValidationEvidenceDigestMismatch")
        return evidence


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


def maximum_beneficial_output_bytes(
    source_size_bytes: int,
    maximum_output_bytes: int,
) -> int:
    """Return the fail-closed publication cap for a full source."""

    for value in (source_size_bytes, maximum_output_bytes):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise MediaContractError("OutputBudgetInsufficient")
    return min(
        maximum_output_bytes,
        math.floor(source_size_bytes * MAXIMUM_CANDIDATE_SOURCE_RATIO),
    )


def build_nvenc_output_budget(
    source_size_bytes: int,
    maximum_output_bytes: int,
    media_info: MediaInfo,
    *,
    duration_limit: float | None = None,
    contract: MediaContract = DEFAULT_CONTRACT,
) -> NvencOutputBudget:
    """Budget NVENC output below the source while retaining CQ as a ceiling.

    The coordinator reservation protects free disk space and can be twice the
    source size.  It is therefore not a useful bitrate target.  This budget
    instead derives a bounded VBR rate from the exact source length and media
    duration, reserving space for every AAC stream and Matroska overhead.
    """

    if not isinstance(media_info, MediaInfo):
        raise MediaContractError("OutputBudgetInsufficient")
    for value in (source_size_bytes, maximum_output_bytes):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise MediaContractError("OutputBudgetInsufficient")
    source_duration = float(media_info.duration)
    if not math.isfinite(source_duration) or source_duration <= 0:
        raise MediaContractError("OutputBudgetInsufficient")
    effective_duration = source_duration
    if duration_limit is not None:
        try:
            requested_duration = float(duration_limit)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MediaContractError("OutputBudgetInsufficient") from exc
        if not math.isfinite(requested_duration) or requested_duration <= 0:
            raise MediaContractError("OutputBudgetInsufficient")
        effective_duration = min(source_duration, requested_duration)

    effective_source_bytes = source_size_bytes
    if effective_duration < source_duration:
        effective_source_bytes = math.floor(
            source_size_bytes * effective_duration / source_duration
        )
    if effective_source_bytes <= 0:
        raise MediaContractError("OutputBudgetInsufficient")

    target_output_bytes = min(
        maximum_output_bytes,
        math.floor(effective_source_bytes * NVENC_TARGET_SOURCE_RATIO),
    )
    ffmpeg_limit_bytes = min(
        maximum_output_bytes,
        math.floor(
            effective_source_bytes * NVENC_FFMPEG_LIMIT_SOURCE_RATIO
        ),
    )
    hard_limit_bytes = maximum_beneficial_output_bytes(
        effective_source_bytes,
        maximum_output_bytes,
    )
    if not (
        0 < target_output_bytes < ffmpeg_limit_bytes < hard_limit_bytes
    ):
        raise MediaContractError("OutputBudgetInsufficient")

    mux_overhead_bytes = max(
        MINIMUM_MUX_OVERHEAD_BYTES,
        math.ceil(target_output_bytes * NVENC_MUX_OVERHEAD_RATIO),
    )
    audio_bytes = math.ceil(
        effective_duration
        * media_info.audio_count
        * contract.audio_bitrate
        * AAC_BUDGET_HEADROOM
        / 8
    )
    target_video_bytes = (
        target_output_bytes - mux_overhead_bytes - audio_bytes
    )
    maximum_video_bytes = (
        ffmpeg_limit_bytes - mux_overhead_bytes - audio_bytes
    )
    video_average_bitrate = math.floor(
        target_video_bytes * 8 / effective_duration
    )
    video_maximum_bitrate = math.floor(
        maximum_video_bytes * 8 / effective_duration
    )
    if (
        target_video_bytes <= 0
        or maximum_video_bytes <= 0
        or video_average_bitrate < MINIMUM_NVENC_VIDEO_BITRATE
        or video_maximum_bitrate < video_average_bitrate
    ):
        raise MediaContractError("OutputBudgetInsufficient")
    return NvencOutputBudget(
        effective_source_bytes=effective_source_bytes,
        target_output_bytes=target_output_bytes,
        ffmpeg_limit_bytes=ffmpeg_limit_bytes,
        hard_limit_bytes=hard_limit_bytes,
        video_average_bitrate=video_average_bitrate,
        video_maximum_bitrate=video_maximum_bitrate,
        video_buffer_size=video_maximum_bitrate * 2,
    )


def build_producer_validation_evidence(
    candidate: CandidateEvidence,
    *,
    run_id: str,
    job_id: str,
    worker_id: str,
    worker_role: str,
    attempt_id: str,
    fencing_epoch: int,
    contract_hash: str,
    producer_build_sha256: str,
    ffmpeg_sha256: str,
    ffprobe_sha256: str,
) -> ProducerValidationEvidence:
    """Bind a successful full decode to one exact fenced producer attempt."""

    if not isinstance(candidate, CandidateEvidence):
        raise MediaContractError("ValidationEvidenceInvalid")
    if candidate.full_decode is None:
        raise MediaContractError("FullDecodeEvidenceMissing")
    try:
        return ProducerValidationEvidence(
            schema_version=VALIDATION_EVIDENCE_SCHEMA,
            mode=PRODUCER_FULL_VALIDATION_MODE,
            run_id=run_id,
            job_id=job_id,
            worker_id=worker_id,
            worker_role=worker_role,
            attempt_id=attempt_id,
            fencing_epoch=fencing_epoch,
            contract_hash=contract_hash,
            producer_build_sha256=producer_build_sha256,
            ffmpeg_sha256=ffmpeg_sha256,
            ffprobe_sha256=ffprobe_sha256,
            candidate_sha256=candidate.sha256,
            candidate_bytes=candidate.output_bytes,
            encoded_frame_count=candidate.encoded_frame_count,
            full_decode=candidate.full_decode,
        )
    except (TypeError, ValueError) as exc:
        raise MediaContractError("ValidationEvidenceInvalid") from exc


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
    source_size_bytes: int | None = None,
    duration_limit: float | None = None,
    contract: MediaContract = DEFAULT_CONTRACT,
) -> list[str]:
    if encoder not in {"hevc_qsv", "hevc_nvenc"}:
        raise MediaContractError("EncoderUnsupported")
    if maximum_output_bytes <= 0:
        raise MediaContractError("OutputLimitInvalid")
    if duration_limit is not None and duration_limit <= 0:
        raise MediaContractError("DurationLimitInvalid")
    nvenc_budget = None
    if encoder == "hevc_nvenc":
        if source_size_bytes is None:
            raise MediaContractError("OutputBudgetInsufficient")
        nvenc_budget = build_nvenc_output_budget(
            source_size_bytes,
            maximum_output_bytes,
            media_info,
            duration_limit=duration_limit,
            contract=contract,
        )

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
        assert nvenc_budget is not None
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
                str(nvenc_budget.video_average_bitrate),
                "-maxrate:v",
                str(nvenc_budget.video_maximum_bitrate),
                "-bufsize:v",
                str(nvenc_budget.video_buffer_size),
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
            str(
                nvenc_budget.ffmpeg_limit_bytes
                if nvenc_budget is not None
                else maximum_output_bytes
            ),
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


def validate_candidate_metadata(
    ffprobe: str,
    candidate: str,
    source_info: MediaInfo,
    expected_audio_durations: Sequence[float],
    *,
    expected_duration: float | None = None,
    contract: MediaContract = DEFAULT_CONTRACT,
) -> MediaInfo | None:
    """Validate the candidate contract using only file metadata and FFprobe.

    The returned ``MediaInfo`` identifies the exact output stream indexes that
    a caller may decode or compare with producer-full validation evidence.
    ``None`` preserves the fail-closed boolean style of ``validate_candidate``.
    """

    try:
        if not os.path.isfile(candidate) or os.path.getsize(candidate) <= 0:
            return None
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
            return None
        video = videos[0]
        if str(_value(video, "codec_name", "")) != contract.video_codec:
            return None
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
            return None
        source_aspect = source_info.width / float(source_info.height)
        output_aspect = width / float(height)
        if abs(source_aspect - output_aspect) / source_aspect > 0.01:
            return None

        audio = [
            stream
            for stream in streams
            if isinstance(stream, dict)
            and str(_value(stream, "codec_type", "")) == "audio"
        ]
        if len(audio) != source_info.audio_count:
            return None
        if len(expected_audio_durations) != source_info.audio_count:
            return None
        for index, stream in enumerate(audio):
            if str(_value(stream, "codec_name", "")) != contract.audio_codec:
                return None
            if _as_int(_value(stream, "channels", 0)) != contract.audio_channels:
                return None
            layout = str(_value(stream, "channel_layout", ""))
            if layout and layout != "stereo":
                return None
            bit_rate = _as_int(_value(stream, "bit_rate", 0))
            if bit_rate and not 150_000 <= bit_rate <= 235_000:
                return None
            if _as_int(_value(stream, "sample_rate", 0)) != (
                source_info.audio_streams[index].sample_rate
            ):
                return None

        if any(
            isinstance(stream, dict)
            and str(_value(stream, "codec_type", "")) == "subtitle"
            for stream in streams
        ):
            return None
        raw_format = _value(probe, "format", {})
        format_name = str(_value(raw_format, "format_name", ""))
        if contract.container not in format_name.split(","):
            return None
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
            return None

        if source_info.is_hdr:
            if (
                str(_value(video, "color_transfer", "")) != "bt709"
                or str(_value(video, "color_primaries", "")) != "bt709"
                or str(_value(video, "color_space", "")) != "bt709"
                or str(_value(video, "color_range", "")) != "tv"
            ):
                return None

        source_fps = rational_to_float(source_info.average_frame_rate)
        actual_fps = rational_to_float(
            str(_value(video, "avg_frame_rate", ""))
        )
        if source_fps > 0 and actual_fps > 0:
            if abs(source_fps - actual_fps) > max(0.01, source_fps * 0.002):
                return None

        return media_info_from_probe(probe)
    except (OSError, MediaContractError, ValueError, ZeroDivisionError):
        return None


def _decode_audio_evidence(
    ffmpeg: str,
    candidate: str,
    output_info: MediaInfo,
    *,
    cancel_event: threading.Event | None = None,
) -> tuple[StreamDecodeEvidence, ...]:
    evidence: list[StreamDecodeEvidence] = []
    for audio in output_info.audio_streams:
        stats = decode_stream_stats(
            ffmpeg,
            candidate,
            audio.index,
            "audio",
            cancel_event=cancel_event,
        )
        evidence.append(
            StreamDecodeEvidence(
                stream_index=audio.index,
                stream_type="audio",
                success=stats.success,
                frame_count=stats.frame_count,
                out_time_seconds=stats.out_time_seconds,
            )
        )
    return tuple(evidence)


def collect_full_decode_evidence(
    ffmpeg: str,
    candidate: str,
    output_info: MediaInfo,
    *,
    cancel_event: threading.Event | None = None,
) -> FullDecodeEvidence:
    """Decode every output audio stream and the video stream through EOS."""

    audio = _decode_audio_evidence(
        ffmpeg,
        candidate,
        output_info,
        cancel_event=cancel_event,
    )
    stats = decode_stream_stats(
        ffmpeg,
        candidate,
        output_info.video_index,
        "video",
        cancel_event=cancel_event,
    )
    return FullDecodeEvidence(
        video=StreamDecodeEvidence(
            stream_index=output_info.video_index,
            stream_type="video",
            success=stats.success,
            frame_count=stats.frame_count,
            out_time_seconds=stats.out_time_seconds,
        ),
        audio=audio,
    )


def _full_decode_matches(
    evidence: FullDecodeEvidence,
    expected_audio_durations: Sequence[float],
    expected_frame_count: int,
) -> bool:
    if len(evidence.audio) != len(expected_audio_durations):
        return False
    for expected, actual in zip(expected_audio_durations, evidence.audio):
        if (
            not actual.success
            or actual.out_time_seconds <= 0
            or abs(expected - actual.out_time_seconds)
            > _duration_tolerance(expected)
        ):
            return False
    return (
        evidence.video.success
        and (
            expected_frame_count <= 0
            or evidence.video.frame_count == expected_frame_count
        )
    )


def validate_candidate_with_evidence(
    ffmpeg: str,
    ffprobe: str,
    candidate: str,
    source_info: MediaInfo,
    expected_audio_durations: Sequence[float],
    expected_frame_count: int,
    *,
    expected_duration: float | None = None,
    contract: MediaContract = DEFAULT_CONTRACT,
    cancel_event: threading.Event | None = None,
) -> FullDecodeEvidence | None:
    """Validate metadata and every decode, returning producer-full evidence."""

    output_info = validate_candidate_metadata(
        ffprobe,
        candidate,
        source_info,
        expected_audio_durations,
        expected_duration=expected_duration,
        contract=contract,
    )
    if output_info is None:
        return None
    try:
        evidence = collect_full_decode_evidence(
            ffmpeg,
            candidate,
            output_info,
            cancel_event=cancel_event,
        )
        if not _full_decode_matches(
            evidence,
            expected_audio_durations,
            expected_frame_count,
        ):
            return None
        return evidence
    except (OSError, MediaContractError, ValueError, ZeroDivisionError):
        return None


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
    """Preserve the legacy boolean validation API and decode behavior."""

    output_info = validate_candidate_metadata(
        ffprobe,
        candidate,
        source_info,
        expected_audio_durations,
        expected_duration=expected_duration,
        contract=contract,
    )
    if output_info is None:
        return False
    try:
        audio = _decode_audio_evidence(
            ffmpeg,
            candidate,
            output_info,
            cancel_event=cancel_event,
        )
        for expected, actual in zip(expected_audio_durations, audio):
            if (
                not actual.success
                or actual.out_time_seconds <= 0
                or abs(expected - actual.out_time_seconds)
                > _duration_tolerance(expected)
            ):
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
    source_size_bytes = os.path.getsize(source)
    nvenc_budget = (
        build_nvenc_output_budget(
            source_size_bytes,
            maximum_output_bytes,
            source_info,
            duration_limit=duration_limit,
            contract=contract,
        )
        if encoder == "hevc_nvenc"
        else None
    )
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
        source_size_bytes=source_size_bytes,
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
    candidate_bytes = os.path.getsize(candidate)
    if candidate_bytes <= 0:
        raise MediaContractError("CandidateSizeInvalid")
    if (
        nvenc_budget is not None
        and candidate_bytes > nvenc_budget.hard_limit_bytes
    ):
        raise MediaContractError("CandidateSizeNotBeneficial")
    expected_duration = min(
        source_info.duration, duration_limit
    ) if duration_limit else source_info.duration
    if phase_callback is not None:
        phase_callback("LocalValidation")
    full_decode = validate_candidate_with_evidence(
        ffmpeg,
        ffprobe,
        candidate,
        source_info,
        audio_durations,
        stats.frame_count,
        expected_duration=expected_duration,
        contract=contract,
        cancel_event=cancel_event,
    )
    if full_decode is None:
        raise MediaContractError("CandidateValidationFailed")
    candidate_sha256 = sha256_file(candidate)
    return (
        source_info,
        audio_durations,
        CandidateEvidence(
            sha256=candidate_sha256,
            encoded_frame_count=stats.frame_count,
            output_bytes=candidate_bytes,
            encode_seconds=elapsed,
            full_decode=full_decode,
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
