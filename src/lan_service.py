"""Coordinator service, HTTP API adapter, and reconnecting LAN supervisor."""

from __future__ import annotations

import dataclasses
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from lan_coordinator import (
    ClaimPayload,
    CommitOutcome,
    CoordinatorError,
    DistributedCoordinator,
    SubmitPayload,
    WORK_PHASES,
    submit_payload_from_dict,
)
from lan_helper_control import (
    HelperControlCommand,
    HelperControlError,
    HelperControlStore,
)
from lan_protocol import WorkerRole
from lan_transport import (
    DispatchRejected,
    LoopbackJsonClient,
    LoopbackJsonServer,
    PublicError,
    SshLocalForward,
    TransportError,
)
from lan_worker import (
    ComputeWorker,
    DirectCoordinatorClient,
    LocalFallbackWorker,
    WorkerResult,
)


class HttpCoordinatorClient:
    """Typed coordinator client over the authenticated loopback transport."""

    def __init__(
        self,
        transport: LoopbackJsonClient,
        *,
        submit_poll_seconds: float = 1.0,
    ):
        self.transport = transport
        self.submit_poll_seconds = float(submit_poll_seconds)
        if self.submit_poll_seconds <= 0:
            raise ValueError("submit_poll_seconds must be positive")

    def helper_seen(self) -> None:
        self.transport.call("helper_seen", {})

    def helper_state(
        self,
        *,
        worker_id: str,
        control_id: str,
        revision: int,
        pc_in_use: bool,
    ) -> dict[str, str | int | bool]:
        result = self.transport.call(
            "helper_state",
            {
                "worker_id": worker_id,
                "control_id": control_id,
                "revision": revision,
                "pc_in_use": pc_in_use,
            },
        )
        if (
            not isinstance(result.get("run_id"), str)
            or len(str(result["run_id"])) != 32
            or isinstance(result.get("revision"), bool)
            or not isinstance(result.get("revision"), int)
            or type(result.get("pc_in_use")) is not bool
        ):
            raise TransportError(PublicError.TRANSPORT_PROTOCOL_ERROR)
        return {
            "run_id": str(result["run_id"]),
            "revision": int(result["revision"]),
            "pc_in_use": bool(result["pc_in_use"]),
        }

    def claim(self, worker_id, worker_role):
        result = self.transport.call(
            "claim",
            {
                "worker_id": str(worker_id),
                "worker_role": WorkerRole(worker_role).value,
            },
        )
        if result.get("idle") is True:
            return None
        try:
            return ClaimPayload(**result)
        except (TypeError, ValueError) as exc:
            raise TransportError(
                PublicError.TRANSPORT_PROTOCOL_ERROR
            ) from exc

    def heartbeat(self, **kwargs):
        result = self.transport.call("heartbeat", dict(kwargs))
        try:
            return ClaimPayload(**result)
        except (TypeError, ValueError) as exc:
            raise TransportError(
                PublicError.TRANSPORT_PROTOCOL_ERROR
            ) from exc

    def abandon(self, **kwargs):
        result = self.transport.call("abandon", dict(kwargs))
        if not isinstance(result.get("released"), bool):
            raise TransportError(PublicError.TRANSPORT_PROTOCOL_ERROR)
        return bool(result["released"])

    def report_failure(self, **kwargs):
        result = self.transport.call("report_failure", dict(kwargs))
        if not isinstance(result.get("released"), bool):
            raise TransportError(PublicError.TRANSPORT_PROTOCOL_ERROR)
        return bool(result["released"])

    def submit(self, payload: SubmitPayload):
        request = {
            "run_id": payload.run_id,
            "contract_hash": payload.contract_hash,
            "worker_id": payload.worker_id,
            "attempt_id": payload.attempt_id,
            "fencing_epoch": payload.fencing_epoch,
            "candidate_sha256": payload.candidate_sha256,
            "encoded_frame_count": payload.encoded_frame_count,
            "encode_seconds": payload.encode_seconds,
        }
        if payload.validation_evidence is not None:
            # Producer-full evidence has a digest-covered wire form.  A generic
            # dataclass conversion omits that required evidence_digest field.
            request["validation_evidence"] = (
                payload.validation_evidence.to_dict()
            )
        result = self.transport.call("submit", request)
        if (
            result.get("kind") != "Accepted"
            or result.get("category") != "CommitAccepted"
        ):
            raise TransportError(PublicError.TRANSPORT_PROTOCOL_ERROR)
        while True:
            result = self.transport.call(
                "poll_submission",
                {
                    "worker_id": payload.worker_id,
                    "attempt_id": payload.attempt_id,
                    "fencing_epoch": payload.fencing_epoch,
                },
            )
            if result.get("kind") != "Pending":
                break
            time.sleep(self.submit_poll_seconds)
        try:
            return CommitOutcome(
                kind=str(result["kind"]),
                category=str(result["category"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TransportError(
                PublicError.TRANSPORT_PROTOCOL_ERROR
            ) from exc

    def status(self) -> dict[str, Any]:
        return self.transport.call("status", {})


class CoordinatorDispatcher:
    """Strict API surface: remote HTTP clients can only act as helpers."""

    def __init__(self, coordinator: DistributedCoordinator):
        self.coordinator = coordinator
        self._admission_condition = threading.Condition()
        self._accepting = True
        self._active_calls = 0

    def __call__(
        self, endpoint: str, request: dict[str, Any]
    ) -> Mapping[str, Any]:
        with self._admission_condition:
            if not self._accepting:
                raise DispatchRejected(PublicError.SERVER_UNAVAILABLE)
            self._active_calls += 1
        try:
            return self._dispatch(endpoint, request)
        finally:
            with self._admission_condition:
                self._active_calls -= 1
                self._admission_condition.notify_all()

    def close_admission(self) -> None:
        """Atomically reject every coordinator call that has not yet begun."""

        with self._admission_condition:
            self._accepting = False

    def wait_for_idle(self, timeout: float) -> bool:
        """Wait a bounded time for calls admitted before shutdown to finish."""

        timeout = float(timeout)
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        deadline = time.monotonic() + timeout
        with self._admission_condition:
            while self._active_calls:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._admission_condition.wait(remaining)
            return True

    def _dispatch(
        self, endpoint: str, request: dict[str, Any]
    ) -> Mapping[str, Any]:
        try:
            if endpoint == "helper_state":
                self._keys(
                    request,
                    {
                        "worker_id",
                        "control_id",
                        "revision",
                        "pc_in_use",
                    },
                )
                if type(request["pc_in_use"]) is not bool:
                    self._invalid()
                return self.coordinator.helper_state(
                    worker_id=self._worker_id(request["worker_id"]),
                    control_id=self._opaque(request["control_id"]),
                    revision=self._positive_int(request["revision"]),
                    pc_in_use=request["pc_in_use"],
                )
            if endpoint == "helper_seen":
                if request:
                    self._invalid()
                self.coordinator.helper_seen()
                return {"accepted": True}
            if endpoint == "status":
                if request:
                    self._invalid()
                return self.coordinator.snapshot()
            if endpoint == "claim":
                self._keys(request, {"worker_id", "worker_role"})
                if request["worker_role"] != WorkerRole.HELPER.value:
                    self._invalid()
                claim = self.coordinator.claim(
                    self._worker_id(request["worker_id"]),
                    WorkerRole.HELPER,
                )
                if claim is None:
                    return {"idle": True}
                return dataclasses.asdict(claim)
            if endpoint == "heartbeat":
                self._keys(
                    request,
                    {
                        "worker_id",
                        "attempt_id",
                        "fencing_epoch",
                        "progress_seconds",
                        "frame_count",
                        "phase",
                        "media_duration_seconds",
                        "encode_elapsed_seconds",
                        "transfer_bytes",
                        "transfer_total_bytes",
                        "transfer_elapsed_seconds",
                    },
                )
                claim = self.coordinator.heartbeat(
                    worker_id=self._worker_id(request["worker_id"]),
                    attempt_id=self._opaque(request["attempt_id"]),
                    fencing_epoch=self._positive_int(
                        request["fencing_epoch"]
                    ),
                    progress_seconds=self._nonnegative_float(
                        request["progress_seconds"]
                    ),
                    frame_count=self._nonnegative_int(
                        request["frame_count"]
                    ),
                    phase=self._phase(request["phase"]),
                    media_duration_seconds=self._nonnegative_float(
                        request["media_duration_seconds"]
                    ),
                    encode_elapsed_seconds=self._nonnegative_float(
                        request["encode_elapsed_seconds"]
                    ),
                    transfer_bytes=self._nonnegative_int(
                        request["transfer_bytes"]
                    ),
                    transfer_total_bytes=self._nonnegative_int(
                        request["transfer_total_bytes"]
                    ),
                    transfer_elapsed_seconds=self._nonnegative_float(
                        request["transfer_elapsed_seconds"]
                    ),
                )
                return dataclasses.asdict(claim)
            if endpoint == "abandon":
                self._keys(
                    request,
                    {"worker_id", "attempt_id", "fencing_epoch"},
                )
                released = self.coordinator.abandon(
                    worker_id=self._worker_id(request["worker_id"]),
                    attempt_id=self._opaque(request["attempt_id"]),
                    fencing_epoch=self._positive_int(
                        request["fencing_epoch"]
                    ),
                )
                return {"released": released}
            if endpoint == "report_failure":
                self._keys(
                    request,
                    {
                        "worker_id",
                        "attempt_id",
                        "fencing_epoch",
                        "failure_category",
                    },
                )
                released = self.coordinator.report_failure(
                    worker_id=self._worker_id(request["worker_id"]),
                    attempt_id=self._opaque(request["attempt_id"]),
                    fencing_epoch=self._positive_int(
                        request["fencing_epoch"]
                    ),
                    failure_category=self._opaque(
                        request["failure_category"]
                    ),
                )
                return {"released": released}
            if endpoint == "submit":
                self._keys_optional(
                    request,
                    {
                        "run_id",
                        "contract_hash",
                        "worker_id",
                        "attempt_id",
                        "fencing_epoch",
                        "candidate_sha256",
                        "encoded_frame_count",
                        "encode_seconds",
                    },
                    {"validation_evidence"},
                )
                outcome = self.coordinator.submit_async(
                    submit_payload_from_dict(request)
                )
                return dataclasses.asdict(outcome)
            if endpoint == "poll_submission":
                self._keys(
                    request,
                    {"worker_id", "attempt_id", "fencing_epoch"},
                )
                outcome = self.coordinator.poll_submission(
                    worker_id=self._worker_id(request["worker_id"]),
                    attempt_id=self._opaque(request["attempt_id"]),
                    fencing_epoch=self._positive_int(
                        request["fencing_epoch"]
                    ),
                )
                return dataclasses.asdict(outcome)
            raise DispatchRejected(PublicError.ENDPOINT_NOT_FOUND)
        except DispatchRejected:
            raise
        except CoordinatorError as exc:
            if exc.systemic:
                raise DispatchRejected(
                    PublicError.SERVER_UNAVAILABLE
                ) from exc
            if exc.category in {
                "StaleAttempt",
                "BindingMismatch",
                "CandidateNotReady",
                "DestinationAppeared",
                "StaleHelperControl",
                "HelperControlConflict",
            }:
                raise DispatchRejected(
                    PublicError.REQUEST_CONFLICT
                ) from exc
            raise DispatchRejected(PublicError.INVALID_REQUEST) from exc
        except (KeyError, TypeError, ValueError, OverflowError):
            self._invalid()

    @staticmethod
    def _keys(request: dict[str, Any], expected: set[str]) -> None:
        if set(request) != expected:
            CoordinatorDispatcher._invalid()

    @staticmethod
    def _keys_optional(
        request: dict[str, Any],
        required: set[str],
        optional: set[str],
    ) -> None:
        keys = set(request)
        if not required.issubset(keys) or not keys.issubset(
            required | optional
        ):
            CoordinatorDispatcher._invalid()

    @staticmethod
    def _worker_id(value: object) -> str:
        result = str(value) if isinstance(value, str) else ""
        if not result or len(result) > 128:
            CoordinatorDispatcher._invalid()
        return result

    @staticmethod
    def _opaque(value: object) -> str:
        result = str(value) if isinstance(value, str) else ""
        if not result or len(result) > 128:
            CoordinatorDispatcher._invalid()
        return result

    @staticmethod
    def _phase(value: object) -> str:
        result = CoordinatorDispatcher._opaque(value)
        if result not in WORK_PHASES:
            CoordinatorDispatcher._invalid()
        return result

    @staticmethod
    def _positive_int(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            CoordinatorDispatcher._invalid()
        return value

    @staticmethod
    def _nonnegative_int(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            CoordinatorDispatcher._invalid()
        return value

    @staticmethod
    def _nonnegative_float(value: object) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= float(value) < float("inf")
        ):
            CoordinatorDispatcher._invalid()
        return float(value)

    @staticmethod
    def _invalid() -> None:
        raise DispatchRejected(PublicError.INVALID_REQUEST)


class CoordinatorService:
    """Run the remote QSV lane, lease reaper, and loopback API together."""

    def __init__(
        self,
        *,
        coordinator: DistributedCoordinator,
        token_file: str,
        api_port: int,
        remote_worker_id: str = "remote-qsv",
        keep_alive_when_complete: bool = False,
        reaper_interval_seconds: float = 2.0,
        shutdown_timeout_seconds: float = 15.0,
    ) -> None:
        self.coordinator = coordinator
        if type(keep_alive_when_complete) is not bool:
            raise ValueError("keep_alive_when_complete must be boolean")
        self.keep_alive_when_complete = keep_alive_when_complete
        self.stop_event = threading.Event()
        self.dispatcher = CoordinatorDispatcher(coordinator)
        self.server = LoopbackJsonServer(
            token_file=token_file,
            dispatcher=self.dispatcher,
            host="127.0.0.1",
            port=api_port,
        )
        self.remote_worker = ComputeWorker(
            client=DirectCoordinatorClient(coordinator),
            worker_id=remote_worker_id,
            worker_role=WorkerRole.REMOTE,
            encoder="hevc_qsv",
            source_root=coordinator.staging_root,
            staging_root=coordinator.staging_root,
            local_cache_root=coordinator.staging_root,
            ffmpeg=coordinator.ffmpeg,
            ffprobe=coordinator.ffprobe,
            contract=coordinator.contract,
            direct_staging=True,
            external_cancel_event=self.stop_event,
            producer_build_sha256=coordinator.runtime_build_hash,
            ffmpeg_sha256=coordinator.ffmpeg_hash,
            ffprobe_sha256=coordinator.ffprobe_hash,
        )
        self.reaper_interval_seconds = reaper_interval_seconds
        self.shutdown_timeout_seconds = float(shutdown_timeout_seconds)
        if self.shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        self._threads: list[threading.Thread] = []
        self.last_remote_result = WorkerResult("Idle", "NotStarted")
        self._close_lock = threading.Lock()
        self._closed = False

    def start(self) -> "CoordinatorService":
        self.server.start()
        self._threads = [
            threading.Thread(
                target=self._remote_loop,
                name="lan-remote-qsv-worker",
                daemon=True,
            ),
            threading.Thread(
                target=self._reaper_loop,
                name="lan-lease-reaper",
                daemon=True,
            ),
        ]
        for thread in self._threads:
            thread.start()
        return self

    def _remote_loop(self) -> None:
        while not self.stop_event.is_set():
            if self.coordinator.systemic_failure:
                self.stop_event.set()
                return
            try:
                result = self.remote_worker.run_one()
                self.last_remote_result = result
            except Exception:
                self.stop_event.set()
                return
            if result.kind == "Idle":
                snapshot = self.coordinator.snapshot()
                if (
                    snapshot["pending"] == 0
                    and snapshot["leased"] == 0
                    and snapshot["suspect"] == 0
                    and snapshot["committing"] == 0
                ):
                    if not getattr(self, "keep_alive_when_complete", False):
                        self.stop_event.set()
                        return
                self.stop_event.wait(1.0)

    def _reaper_loop(self) -> None:
        while not self.stop_event.wait(self.reaper_interval_seconds):
            try:
                self.coordinator.reap()
            except Exception:
                self.stop_event.set()
                return

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        for thread in self._threads:
            remaining = (
                None
                if deadline is None
                else max(0.0, deadline - time.monotonic())
            )
            thread.join(remaining)
        return all(not thread.is_alive() for thread in self._threads)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return

            # Stop API admission before touching either worker. Calls that
            # crossed the gate are drained by the HTTP server and dispatcher;
            # calls arriving later receive only a category-level 503.
            self.dispatcher.close_admission()
            server_error: Exception | None = None
            try:
                self.server.close(timeout=self.shutdown_timeout_seconds)
            except Exception as exc:
                server_error = exc

            dispatch_drained = self.dispatcher.wait_for_idle(
                self.shutdown_timeout_seconds
            )
            self.stop_event.set()

            if server_error is not None or not dispatch_drained:
                # Admission is already closed, so coordinator teardown must
                # fail closed rather than race an admitted request.
                raise TransportError(PublicError.SERVER_UNAVAILABLE) from (
                    server_error
                )

            for thread in self._threads:
                thread.join()
            self.coordinator.wait_for_async_commits()
            self._closed = True

    def __enter__(self) -> "CoordinatorService":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.close()


class LanAssistSupervisor:
    """Reconnect helper mode and finish one local fallback file while offline."""

    def __init__(
        self,
        *,
        tunnel: SshLocalForward,
        client: HttpCoordinatorClient,
        helper_worker: ComputeWorker,
        fallback_worker: LocalFallbackWorker,
        reconnect_seconds: float = 30.0,
        reconnect_initial_seconds: float | None = None,
        reconnect_max_seconds: float | None = None,
        reconnect_multiplier: float = 4.0,
        status_callback=None,
        control_store: HelperControlStore | None = None,
        helper_worker_id: str | None = None,
        control_poll_seconds: float = 0.25,
        control_heartbeat_seconds: float = 5.0,
        control_clock=None,
    ) -> None:
        initial_seconds = (
            reconnect_seconds
            if reconnect_initial_seconds is None
            else reconnect_initial_seconds
        )
        if (
            isinstance(initial_seconds, bool)
            or not isinstance(initial_seconds, (int, float))
            or not math.isfinite(float(initial_seconds))
        ):
            raise ValueError("reconnect backoff values must be finite numbers")
        maximum_seconds = (
            max(600.0, float(initial_seconds))
            if reconnect_max_seconds is None
            else reconnect_max_seconds
        )
        numeric_values = (
            maximum_seconds,
            reconnect_multiplier,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in numeric_values
        ):
            raise ValueError("reconnect backoff values must be finite numbers")
        if (
            float(initial_seconds) <= 0
            or float(maximum_seconds) < float(initial_seconds)
            or float(reconnect_multiplier) <= 1.0
        ):
            raise ValueError("invalid reconnect backoff configuration")
        self.tunnel = tunnel
        self.client = client
        self.helper_worker = helper_worker
        self.fallback_worker = fallback_worker
        # Keep the original attribute and constructor argument as a parsing
        # alias. Its value is now the initial delay for capped backoff.
        self.reconnect_seconds = float(initial_seconds)
        self.reconnect_initial_seconds = float(initial_seconds)
        self.reconnect_max_seconds = float(maximum_seconds)
        self.reconnect_multiplier = float(reconnect_multiplier)
        self.status_callback = status_callback or (lambda _result: None)
        self.control_store = control_store
        self.helper_worker_id = (
            str(helper_worker_id) if helper_worker_id is not None else ""
        )
        self.control_poll_seconds = float(control_poll_seconds)
        self.control_heartbeat_seconds = float(control_heartbeat_seconds)
        self._control_clock = control_clock or time.monotonic
        if self.control_store is not None and (
            not self.helper_worker_id
            or not math.isfinite(self.control_poll_seconds)
            or self.control_poll_seconds <= 0
            or not math.isfinite(self.control_heartbeat_seconds)
            or self.control_heartbeat_seconds <= 0
        ):
            raise ValueError("invalid helper control configuration")
        self.stop_event = threading.Event()
        self.terminal_result: WorkerResult | None = None
        self._last_control_status_signature: tuple[object, ...] | None = None
        self._last_control_status_written_at: float | None = None
        self._acknowledged_control: tuple[str, int, bool, str] | None = None

    def _write_control_status(
        self,
        command: HelperControlCommand,
        *,
        effective_state: str,
        category: str,
        run_id: str = "",
        refresh_identical: bool = False,
    ) -> None:
        store = self.control_store
        if store is None:
            return
        signature = (
            command.control_id,
            command.revision,
            command.pc_in_use,
            effective_state,
            category,
            run_id,
        )
        try:
            now = float(self._control_clock())
        except (TypeError, ValueError, OverflowError) as exc:
            raise HelperControlError("ControlClockInvalid") from exc
        if not math.isfinite(now):
            raise HelperControlError("ControlClockInvalid")
        if signature == self._last_control_status_signature:
            last_written = self._last_control_status_written_at
            if (
                not refresh_identical
                or last_written is None
                or now - last_written < self.control_heartbeat_seconds
            ):
                return
        store.write_status(
            command,
            effective_state=effective_state,
            category=category,
            run_id=run_id,
        )
        self._last_control_status_signature = signature
        self._last_control_status_written_at = now

    def _control_blocked(
        self,
        category: str,
        command: HelperControlCommand | None = None,
    ) -> None:
        if command is not None:
            try:
                self._write_control_status(
                    command,
                    effective_state="blocked",
                    category=category,
                )
            except HelperControlError:
                pass
        blocked = WorkerResult(
            "Blocked",
            "HelperControlBlocked",
            phase=category,
        )
        self.status_callback(blocked)
        self.terminal_result = blocked
        self.stop_event.set()
        self.tunnel.stop()

    def _announce_control(
        self, command: HelperControlCommand
    ) -> dict[str, str | int | bool]:
        store = self.control_store
        if store is None:
            raise RuntimeError("helper control store unavailable")
        prior = self._acknowledged_control
        command_key = (
            command.control_id,
            command.revision,
            command.pc_in_use,
        )
        if prior is None or prior[:3] != command_key:
            self._write_control_status(
                command,
                effective_state="pending",
                category="",
            )
        try:
            acknowledged = self.client.helper_state(
                worker_id=self.helper_worker_id,
                control_id=command.control_id,
                revision=command.revision,
                pc_in_use=command.pc_in_use,
            )
        except TransportError as exc:
            if exc.category in {
                PublicError.REQUEST_CONFLICT,
                PublicError.INVALID_REQUEST,
            }:
                raise HelperControlError("ControlSyncRejected") from exc
            raise
        if (
            acknowledged["revision"] != command.revision
            or acknowledged["pc_in_use"] is not command.pc_in_use
        ):
            raise TransportError(PublicError.TRANSPORT_PROTOCOL_ERROR)
        run_id = str(acknowledged["run_id"])
        self._write_control_status(
            command,
            effective_state=("paused" if command.pc_in_use else "available"),
            category="",
            run_id=run_id,
            refresh_identical=True,
        )
        self._acknowledged_control = (*command_key, run_id)
        return acknowledged

    def _run_one_with_control(
        self,
        worker: ComputeWorker | LocalFallbackWorker,
        command: HelperControlCommand,
        run_id: str,
    ) -> WorkerResult:
        """Observe local commands during work without touching the transaction."""

        store = self.control_store
        if store is None:
            return worker.run_one()
        finished = threading.Event()
        watcher_error: list[HelperControlError] = []
        work_started = getattr(worker, "work_started_event", None)

        def watch_local_control() -> None:
            while not finished.wait(self.control_poll_seconds):
                if (
                    isinstance(work_started, threading.Event)
                    and not work_started.is_set()
                ):
                    continue
                try:
                    latest = store.read_command()
                    self._write_control_status(
                        latest,
                        effective_state=(
                            "draining" if latest.pc_in_use else "working"
                        ),
                        category=(
                            "PcInUsePending" if latest.pc_in_use else ""
                        ),
                        run_id=run_id,
                        refresh_identical=True,
                    )
                except HelperControlError as exc:
                    watcher_error.append(exc)
                    return

        watcher = threading.Thread(
            target=watch_local_control,
            name="lan-helper-control-watch",
            daemon=True,
        )
        watcher.start()
        try:
            result = worker.run_one()
        finally:
            finished.set()
            watcher.join()
        if watcher_error:
            raise watcher_error[0]
        return result

    def _finish_terminal_result(
        self,
        result: WorkerResult,
        command: HelperControlCommand | None,
        *,
        attempt_sync: bool = True,
    ) -> None:
        """Sync the latest command when possible, then stop as blocked."""

        store = self.control_store
        if store is not None:
            try:
                with store.claim_gate():
                    latest = store.read_command()
                    run_id = ""
                    if attempt_sync:
                        try:
                            acknowledged = self._announce_control(latest)
                            run_id = str(acknowledged["run_id"])
                        except TransportError:
                            # A blocked helper must still publish its exact
                            # local terminal state when unreachable.
                            pass
                    self._write_control_status(
                        latest,
                        effective_state="blocked",
                        category=result.category,
                        run_id=run_id,
                    )
            except HelperControlError as exc:
                self._control_blocked(exc.category, command)
                return
        self.terminal_result = result
        self.stop_event.set()
        self.tunnel.stop()

    def _wait_for_reconnect_or_control(
        self,
        command: HelperControlCommand | None,
        delay_seconds: float,
    ) -> bool:
        """Wait for backoff unless a monotonic control revision wakes it."""

        store = self.control_store
        if store is None or command is None:
            self.stop_event.wait(delay_seconds)
            return False
        deadline = time.monotonic() + delay_seconds
        baseline_revision = command.revision
        while not self.stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self.stop_event.wait(
                min(self.control_poll_seconds, remaining)
            ):
                return False
            latest = store.read_command()
            self._write_control_status(
                latest,
                effective_state="pending",
                category="CoordinatorSyncPending",
                run_id="",
                refresh_identical=True,
            )
            if latest.revision != baseline_revision:
                return True

    def run(self) -> None:
        self.terminal_result = None
        reconnect_delay = self.reconnect_initial_seconds
        while not self.stop_event.is_set():
            disconnected_reported = False
            current_pc_in_use = False
            current_run_id = ""
            try:
                command: HelperControlCommand | None = None
                if self.control_store is not None:
                    command = self.control_store.read_command()
                    current_pc_in_use = command.pc_in_use
                if not self.tunnel.status().running:
                    self.tunnel.start()
                self.tunnel.ensure_running()
                if command is not None:
                    acknowledged = self._announce_control(command)
                    current_run_id = str(acknowledged["run_id"])
                    if command.pc_in_use:
                        self.status_callback(
                            WorkerResult("Waiting", "PcInUsePaused")
                        )
                        reconnect_delay = self.reconnect_initial_seconds
                        self.stop_event.wait(1.0)
                        continue
                    # Narrow the idle pause race: never begin a fresh claim
                    # from an availability snapshot that changed after its
                    # coordinator acknowledgement.
                    latest_command = self.control_store.read_command()
                    if latest_command != command:
                        continue
                    result = self._run_one_with_control(
                        self.helper_worker,
                        command,
                        current_run_id,
                    )
                else:
                    self.client.helper_seen()
                    result = self.helper_worker.run_one()
                self.status_callback(result)
                if (
                    result.kind == "Blocked"
                    or result.category == "AuthBlocked"
                ):
                    self._finish_terminal_result(result, command)
                    return
                if result.kind == "Disconnected":
                    disconnected_reported = True
                    raise TransportError(PublicError.CONNECTION_FAILED)
                reconnect_delay = self.reconnect_initial_seconds
                if result.kind == "Idle":
                    self.stop_event.wait(1.0)
            except HelperControlError as exc:
                self._control_blocked(exc.category, command)
                return
            except (TransportError, OSError) as exc:
                self.tunnel.stop()
                if (
                    isinstance(exc, TransportError)
                    and exc.category is PublicError.AUTHENTICATION_FAILED
                ):
                    blocked = WorkerResult(
                        "Blocked",
                        "AuthBlocked",
                        transport_category=exc.category.value,
                    )
                    self.status_callback(blocked)
                    self._finish_terminal_result(
                        blocked,
                        command,
                        attempt_sync=False,
                    )
                    return
                if not disconnected_reported:
                    transport_category = (
                        exc.category.value
                        if isinstance(exc, TransportError)
                        else ""
                    )
                    self.status_callback(
                        WorkerResult(
                            "Disconnected",
                            "CoordinatorDisconnected",
                            transport_category=transport_category,
                        )
                    )
                if self.control_store is not None:
                    try:
                        command = self.control_store.read_command()
                        current_pc_in_use = command.pc_in_use
                        self._write_control_status(
                            command,
                            effective_state="pending",
                            category="CoordinatorSyncPending",
                            run_id="",
                            refresh_identical=True,
                        )
                    except HelperControlError as control_exc:
                        self._control_blocked(control_exc.category, command)
                        return
                # A local encode is allowed to finish. Connectivity is checked
                # again only after this one safe local transaction returns.
                if not current_pc_in_use:
                    try:
                        if self.control_store is not None:
                            # Keep this read adjacent to the fallback call so
                            # an idle pause cannot knowingly start new work.
                            command = self.control_store.read_command()
                            if command.pc_in_use:
                                current_pc_in_use = True
                                result = None
                            else:
                                result = self._run_one_with_control(
                                    self.fallback_worker,
                                    command,
                                    "",
                                )
                        else:
                            result = self.fallback_worker.run_one()
                    except HelperControlError as control_exc:
                        self._control_blocked(control_exc.category, command)
                        return
                    if result is not None:
                        self.status_callback(result)
                        if self.control_store is not None:
                            try:
                                command = self.control_store.read_command()
                                self._write_control_status(
                                    command,
                                    effective_state="pending",
                                    category="CoordinatorSyncPending",
                                    run_id="",
                                    refresh_identical=True,
                                )
                            except HelperControlError as control_exc:
                                self._control_blocked(
                                    control_exc.category,
                                    command,
                                )
                                return
                if self.stop_event.is_set():
                    return
                self.status_callback(
                    WorkerResult("Waiting", "ReconnectBackoff")
                )
                try:
                    control_woke = self._wait_for_reconnect_or_control(
                        command,
                        reconnect_delay,
                    )
                except HelperControlError as control_exc:
                    self._control_blocked(control_exc.category, command)
                    return
                reconnect_delay = (
                    self.reconnect_initial_seconds
                    if control_woke
                    else min(
                        self.reconnect_max_seconds,
                        reconnect_delay * self.reconnect_multiplier,
                    )
                )

    def stop(self) -> None:
        self.stop_event.set()
        self.tunnel.stop()


__all__ = [
    "CoordinatorDispatcher",
    "CoordinatorService",
    "HttpCoordinatorClient",
    "LanAssistSupervisor",
]
