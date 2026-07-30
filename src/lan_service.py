"""Coordinator service, HTTP API adapter, and reconnecting LAN supervisor."""

from __future__ import annotations

import dataclasses
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
    submit_payload_from_dict,
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
        result = self.transport.call("submit", dataclasses.asdict(payload))
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
                self._keys(
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
        if result not in {
            "Claimed",
            "SourceRead",
            "Converting",
            "LocalValidation",
            "Uploading",
            "RemoteValidation",
            "Publishing",
        }:
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
        reaper_interval_seconds: float = 2.0,
        shutdown_timeout_seconds: float = 15.0,
    ) -> None:
        self.coordinator = coordinator
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
        reconnect_seconds: float = 5.0,
        status_callback=None,
    ) -> None:
        self.tunnel = tunnel
        self.client = client
        self.helper_worker = helper_worker
        self.fallback_worker = fallback_worker
        self.reconnect_seconds = reconnect_seconds
        self.status_callback = status_callback or (lambda _result: None)
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                if not self.tunnel.status().running:
                    self.tunnel.start()
                self.tunnel.ensure_running()
                self.client.helper_seen()
                result = self.helper_worker.run_one()
                self.status_callback(result)
                if result.kind == "Disconnected":
                    raise TransportError(PublicError.CONNECTION_FAILED)
                if result.kind == "Idle":
                    self.stop_event.wait(1.0)
            except (TransportError, OSError):
                self.tunnel.stop()
                # A local encode is allowed to finish. Connectivity is checked
                # again only after this one safe local transaction returns.
                result = self.fallback_worker.run_one()
                self.status_callback(result)
                self.stop_event.wait(self.reconnect_seconds)

    def stop(self) -> None:
        self.stop_event.set()
        self.tunnel.stop()


__all__ = [
    "CoordinatorDispatcher",
    "CoordinatorService",
    "HttpCoordinatorClient",
    "LanAssistSupervisor",
]
