"""Durable application-state paths, atomic JSON I/O, and legacy migration."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


STATE_DIR_ENV = "VIDEO_TRANSCODER_STATE_DIR"
_STATE_LOCK = threading.RLock()


@dataclass(frozen=True)
class AppPaths:
    root: Path
    config: Path
    presets: Path
    queue: Path
    log: Path
    migration_marker: Path
    thumbnail_cache: Path


def resolve_app_paths(root: str | os.PathLike[str] | None = None) -> AppPaths:
    """Resolve state paths without creating or modifying the filesystem."""
    if root is not None:
        state_root = Path(root).expanduser()
    elif os.environ.get(STATE_DIR_ENV):
        state_root = Path(os.environ[STATE_DIR_ENV]).expanduser()
    elif os.environ.get("LOCALAPPDATA"):
        state_root = Path(os.environ["LOCALAPPDATA"]) / "VideoTranscoder"
    elif os.name == "nt":
        state_root = Path.home() / "AppData" / "Local" / "VideoTranscoder"
    else:
        base = Path(
            os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        state_root = base / "video-transcoder"

    return AppPaths(
        root=state_root,
        config=state_root / "transcode_config.json",
        presets=state_root / "custom_presets.json",
        queue=state_root / "transcode_queue.json",
        log=state_root / "transcode_log.txt",
        migration_marker=state_root / "state_migration.json",
        thumbnail_cache=state_root / "cache" / "thumbnails",
    )


def read_json(
    path: str | os.PathLike[str],
    expected_type: type | tuple[type, ...],
    default: Any,
) -> Any:
    """Read JSON only when its top-level value has the expected type."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, expected_type) else default
    except (OSError, json.JSONDecodeError, UnicodeError):
        return default


def atomic_write_json(
    path: str | os.PathLike[str],
    value: Any,
) -> None:
    """Durably replace a JSON file without exposing a partial write."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    with _STATE_LOCK:
        try:
            descriptor, temp_name = tempfile.mkstemp(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, destination)
            temp_name = ""
        finally:
            if temp_name:
                try:
                    os.remove(temp_name)
                except OSError:
                    pass


def atomic_update_mapping(
    path: str | os.PathLike[str],
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Atomically merge keys into a persisted JSON object."""
    with _STATE_LOCK:
        current = read_json(path, dict, {})
        current.update(updates)
        atomic_write_json(path, current)
        return current


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = os.path.normcase(str(path.resolve(strict=False)))
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _valid_json_candidates(
    directories: list[Path],
    filename: str,
    expected_type: type | tuple[type, ...],
) -> list[tuple[float, Path, Any]]:
    candidates: list[tuple[float, Path, Any]] = []
    for directory in directories:
        candidate = directory / filename
        if not candidate.is_file():
            continue
        sentinel = object()
        value = read_json(candidate, expected_type, sentinel)
        if value is sentinel:
            continue
        try:
            modified = candidate.stat().st_mtime
        except OSError:
            modified = 0.0
        candidates.append((modified, candidate, value))
    return candidates


def _migrate_logs(directories: list[Path], destination: Path) -> list[str]:
    """Combine distinct legacy log files while retaining every source."""
    if destination.exists():
        return []
    entries: list[tuple[float, Path, bytes]] = []
    hashes: set[str] = set()
    for directory in directories:
        candidate = directory / "transcode_log.txt"
        try:
            data = candidate.read_bytes()
            modified = candidate.stat().st_mtime
        except OSError:
            continue
        digest = hashlib.sha256(data).hexdigest()
        if digest not in hashes:
            hashes.add(digest)
            entries.append((modified, candidate, data))
    if not entries:
        return []

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        descriptor, temp_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        with os.fdopen(descriptor, "wb") as handle:
            for index, (_modified, source, data) in enumerate(
                    sorted(entries, key=lambda item: item[0])):
                if index:
                    handle.write(b"\n")
                header = (
                    f"--- Migrated from {source} ---\n"
                ).encode("utf-8", errors="replace")
                handle.write(header)
                handle.write(data)
                if data and not data.endswith(b"\n"):
                    handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
        temp_name = ""
    finally:
        if temp_name:
            try:
                os.remove(temp_name)
            except OSError:
                pass
    return [str(item[1]) for item in entries]


def migrate_legacy_state(
    paths: AppPaths,
    legacy_directories: Iterable[str | os.PathLike[str]] | None = None,
) -> bool:
    """Copy legacy CWD/repository state once; never alter legacy sources."""
    with _STATE_LOCK:
        if paths.migration_marker.is_file():
            return False

        if legacy_directories is None:
            source_dir = Path(__file__).resolve().parent
            directories = _dedupe_paths([
                Path.cwd(),
                source_dir.parent,
                source_dir,
            ])
        else:
            directories = _dedupe_paths(
                [Path(directory) for directory in legacy_directories])

        migrated: dict[str, Any] = {}

        if not paths.config.exists():
            candidates = _valid_json_candidates(
                directories, "transcode_config.json", dict)
            if candidates:
                _modified, source, value = max(
                    candidates, key=lambda item: item[0])
                atomic_write_json(paths.config, value)
                migrated["config"] = str(source)

        if not paths.queue.exists():
            candidates = _valid_json_candidates(
                directories, "transcode_queue.json", (list, dict))
            candidates = [
                candidate for candidate in candidates
                if (
                    isinstance(candidate[2], list)
                    and all(isinstance(item, dict) for item in candidate[2])
                )
                or (
                    isinstance(candidate[2], dict)
                    and candidate[2].get("version") == 2
                    and isinstance(candidate[2].get("items"), list)
                    and all(
                        isinstance(item, dict)
                        for item in candidate[2]["items"])
                    and isinstance(
                        candidate[2].get("global_settings", {}), dict)
                )
            ]
            if candidates:
                _modified, source, value = max(
                    candidates, key=lambda item: item[0])
                atomic_write_json(paths.queue, value)
                migrated["queue"] = str(source)

        if not paths.presets.exists():
            candidates = _valid_json_candidates(
                directories, "custom_presets.json", dict)
            if candidates:
                merged: dict[str, Any] = {}
                sources: list[str] = []
                for _modified, source, value in sorted(
                        candidates, key=lambda item: item[0]):
                    merged.update(value)
                    sources.append(str(source))
                atomic_write_json(paths.presets, merged)
                migrated["presets"] = sources

        log_sources = _migrate_logs(directories, paths.log)
        if log_sources:
            migrated["logs"] = log_sources

        atomic_write_json(
            paths.migration_marker,
            {
                "version": 1,
                "policy": "copy-only; destination-wins",
                "migrated": migrated,
            },
        )
        return True
