"""Detailed dashboard for the personal LAN transcoder.

Unlike the coordinator's aggregate helper API, this surface intentionally
contains filenames and per-job details. It binds to IPv4 loopback by default;
an explicit private IPv4 address enables read-only LAN access. Host and Origin
headers are constrained, caching is disabled, and provider responses are
normalized to a fixed allow-list before returning JSON.
"""

from __future__ import annotations

from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import threading
import time
from typing import Any, Protocol
from urllib.parse import urlsplit


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_DASHBOARD_PORT = 41802
MAX_WORKERS = 4
MAX_QUEUE_ITEMS = 512
MAX_RECENT_ITEMS = 100
MAX_TEXT_LENGTH = 512


class DashboardSnapshotProvider(Protocol):
    """Provide detailed state that must stay on the coordinator PC."""

    def dashboard_snapshot(self) -> Mapping[str, Any]:
        """Return the detailed, local-only dashboard snapshot."""


class DashboardError(RuntimeError):
    """Stable dashboard startup/runtime error."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object, default: str = "") -> str:
    if not isinstance(value, str):
        return default
    cleaned = "".join(
        character
        for character in value
        if character == "\t" or ord(character) >= 0x20
    )
    return cleaned[:MAX_TEXT_LENGTH]


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        return default
    return converted


def _integer(value: object, default: int = 0) -> int:
    converted = _number(value, float(default))
    return int(converted)


def _boolean(value: object, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _normalize_worker(
    value: object,
    *,
    default_eligible: bool = True,
    default_standby_reason: str = "",
) -> dict[str, Any]:
    worker = _mapping(value)
    eligible = _boolean(
        worker.get("eligible_for_new_work"),
        default_eligible,
    )
    standby_reason = _text(worker.get("standby_reason"))
    if not eligible and not standby_reason:
        standby_reason = default_standby_reason
    return {
        "worker_id": _text(worker.get("worker_id")),
        "role": _text(worker.get("role")),
        "label": _text(worker.get("label")),
        "online": _boolean(worker.get("online")),
        "eligible_for_new_work": eligible,
        "standby_reason": standby_reason,
        "phase": _text(worker.get("phase"), "idle"),
        "current_filename": _text(worker.get("current_filename")),
        "source_size_bytes": _integer(worker.get("source_size_bytes")),
        "media_seconds": _number(worker.get("media_seconds")),
        "duration_seconds": _number(worker.get("duration_seconds")),
        "encode_elapsed_seconds": _number(
            worker.get("encode_elapsed_seconds")
        ),
        "encode_speed_ratio": _number(worker.get("encode_speed_ratio")),
        "encode_eta_seconds": _number(worker.get("encode_eta_seconds")),
        "transfer_bytes": _integer(worker.get("transfer_bytes")),
        "transfer_total_bytes": _integer(
            worker.get("transfer_total_bytes")
        ),
        "transfer_elapsed_seconds": _number(
            worker.get("transfer_elapsed_seconds")
        ),
        "transfer_bytes_per_second": _number(
            worker.get("transfer_bytes_per_second")
        ),
        "transfer_eta_seconds": _number(
            worker.get("transfer_eta_seconds")
        ),
        "updated_utc": _number(worker.get("updated_utc")),
    }


def _normalize_queue_item(value: object) -> dict[str, Any]:
    item = _mapping(value)
    return {
        "filename": _text(item.get("filename")),
        "size_bytes": _integer(item.get("size_bytes")),
        "state": _text(item.get("state"), "pending"),
        "worker_role": _text(item.get("worker_role")),
        "progress_percent": min(
            100.0,
            _number(item.get("progress_percent")),
        ),
    }


def _normalize_recent_item(value: object) -> dict[str, Any]:
    item = _mapping(value)
    return {
        "timestamp_utc": _number(item.get("timestamp_utc")),
        "worker_role": _text(item.get("worker_role")),
        "event": _text(item.get("event")),
        "filename": _text(item.get("filename")),
    }


def normalize_dashboard_snapshot(value: object) -> dict[str, Any]:
    """Allow-list and bound a local dashboard snapshot.

    In particular, unknown provider keys such as source paths, staging paths,
    authentication material, and process command lines are never serialized.
    """

    snapshot = _mapping(value)
    run = _mapping(snapshot.get("run"))
    totals = _mapping(snapshot.get("totals"))
    workers_value = snapshot.get("workers")
    queue_value = snapshot.get("queue")
    recent_value = snapshot.get("recent")
    workers = workers_value if isinstance(workers_value, list) else []
    queue = queue_value if isinstance(queue_value, list) else []
    recent = recent_value if isinstance(recent_value, list) else []
    scheduling_mode = _text(run.get("scheduling_mode"))
    scheduling_state = _text(run.get("scheduling_state"))
    validation_policy = _text(run.get("validation_policy"))
    preferred_worker_role = _text(run.get("preferred_worker_role"))
    helper_eligible = _boolean(run.get("helper_eligible"), True)
    remote_eligible = _boolean(run.get("remote_eligible"), True)
    fallback_countdown_seconds = _number(
        run.get("fallback_countdown_seconds")
    )
    helper_control_known = _boolean(run.get("helper_control_known"))
    helper_control_revision = _integer(
        run.get("helper_control_revision")
    )
    helper_paused = _boolean(run.get("helper_paused"))

    def worker_defaults(worker_value: object) -> tuple[bool, str]:
        worker = _mapping(worker_value)
        role = _text(worker.get("role"))
        eligible = (
            helper_eligible if role == "helper" else remote_eligible
        )
        reason = ""
        if not eligible:
            if role == "helper" and helper_paused:
                reason = "PC in use"
            elif role == "remote" and preferred_worker_role == "helper":
                reason = "HOT-BOX preferred for new transcodes"
            elif scheduling_state:
                reason = scheduling_state
            else:
                reason = "Not eligible for new work"
        return eligible, reason

    normalized_workers = []
    for worker in workers[:MAX_WORKERS]:
        eligible, standby_reason = worker_defaults(worker)
        normalized_worker = _normalize_worker(
            worker,
            default_eligible=eligible,
            default_standby_reason=standby_reason,
        )
        phase_key = "".join(
            character
            for character in normalized_worker["phase"].lower()
            if character.isalnum()
        )
        idle = (
            phase_key in {"idle", "pcinusepaused", "standby"}
            and not normalized_worker["current_filename"]
        )
        normalized_worker["pc_in_use_paused"] = bool(
            helper_paused
            and idle
            and normalized_worker["role"] == "helper"
        )
        normalized_workers.append(normalized_worker)
    return {
        "schema_version": _integer(snapshot.get("schema_version"), 1),
        "generated_utc": _number(
            snapshot.get("generated_utc"),
            time.time(),
        ),
        "run": {
            "run_id": _text(run.get("run_id")),
            "status": _text(run.get("status"), "unknown"),
            "failure_category": _text(run.get("failure_category")),
            "contract_hash": _text(run.get("contract_hash")),
            "started_utc": _number(run.get("started_utc")),
            "elapsed_seconds": _number(run.get("elapsed_seconds")),
            "eta_seconds": _number(run.get("eta_seconds")),
            "scheduling_mode": scheduling_mode,
            "scheduling_state": scheduling_state,
            "validation_policy": validation_policy,
            "preferred_worker_role": preferred_worker_role,
            "helper_eligible": helper_eligible,
            "remote_eligible": remote_eligible,
            "fallback_countdown_seconds": fallback_countdown_seconds,
            "helper_control_known": helper_control_known,
            "helper_control_revision": helper_control_revision,
            "helper_paused": helper_paused,
        },
        "totals": {
            "total_jobs": _integer(totals.get("total_jobs")),
            "pending": _integer(totals.get("pending")),
            "active": _integer(totals.get("active")),
            "completed": _integer(totals.get("completed")),
            "failed": _integer(totals.get("failed")),
            "skipped": _integer(totals.get("skipped")),
            "source_bytes_total": _integer(
                totals.get("source_bytes_total")
            ),
            "source_bytes_completed": _integer(
                totals.get("source_bytes_completed")
            ),
        },
        "workers": normalized_workers,
        "queue": [
            _normalize_queue_item(item)
            for item in queue[:MAX_QUEUE_ITEMS]
        ],
        "recent": [
            _normalize_recent_item(item)
            for item in recent[:MAX_RECENT_ITEMS]
        ],
    }


_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>LAN Transcode Monitor</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0b1117;
      --surface: #111a23;
      --surface-2: #17222d;
      --border: #2b3947;
      --text: #e6edf4;
      --muted: #9eacba;
      --quiet: #69798a;
      --cyan: #30bced;
      --green: #35c568;
      --red: #ff5d62;
      --amber: #f0b64d;
      --track: #293644;
      --radius: 7px;
      --ui: Inter, "Segoe UI Variable", "Segoe UI", system-ui, sans-serif;
      --mono: "Cascadia Code", "SFMono-Regular", Consolas, monospace;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-width: 320px;
      background: var(--bg);
      color: var(--text);
      font-family: var(--ui);
      font-size: 14px;
      line-height: 1.4;
    }
    button { font: inherit; }
    .shell {
      width: min(1500px, calc(100% - 40px));
      margin: 0 auto;
      padding: 22px 0 28px;
    }
    header {
      display: flex;
      align-items: center;
      gap: 22px;
      min-height: 36px;
      margin-bottom: 18px;
    }
    h1 {
      margin: 0;
      font-size: clamp(22px, 2.1vw, 30px);
      line-height: 1.1;
      letter-spacing: -0.025em;
    }
    .live {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--green);
      font-size: 13px;
      font-weight: 700;
      letter-spacing: .04em;
    }
    .live::before, .status-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: currentColor;
      content: "";
      flex: none;
    }
    .local-note, .updated { color: var(--muted); }
    .updated { margin-left: auto; font-variant-numeric: tabular-nums; }
    .refresh {
      min-width: 126px;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 8px 12px;
      background: transparent;
      color: var(--text);
      cursor: pointer;
    }
    .refresh:hover, .refresh:focus-visible {
      border-color: var(--cyan);
      outline: none;
    }
    .summary, .worker, .queue-panel, .activity-panel {
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: var(--surface);
    }
    .summary { padding: 18px 24px 16px; }
    .metrics {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      margin-bottom: 15px;
    }
    .metric { padding: 0 30px; border-left: 1px solid var(--border); }
    .metric:first-child { padding-left: 0; border-left: 0; }
    .metric:last-child { padding-right: 0; }
    .metric-label { color: var(--muted); margin-bottom: 3px; }
    .metric-value {
      font-family: var(--mono);
      font-size: clamp(20px, 2.2vw, 29px);
      font-weight: 650;
      font-variant-numeric: tabular-nums;
    }
    #overall-percent { color: var(--green); font-size: clamp(30px, 3vw, 42px); }
    .progress {
      width: 100%;
      height: 11px;
      overflow: hidden;
      border-radius: 999px;
      background: var(--track);
    }
    .progress > span {
      display: block;
      width: 0;
      height: 100%;
      border-radius: inherit;
      background: var(--green);
      transition: width 280ms ease;
    }
    .summary-foot {
      display: flex;
      gap: 10px;
      margin-top: 12px;
      color: var(--muted);
    }
    .policy-strip {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px 20px;
      margin-top: 12px;
      padding-top: 12px;
      border-top: 1px solid var(--border);
    }
    .policy-item { min-width: 0; }
    .policy-label {
      display: block;
      color: var(--muted);
      font-size: 12px;
    }
    .policy-value {
      display: block;
      overflow: hidden;
      color: var(--text);
      font-weight: 650;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .worker-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 12px;
    }
    .worker { overflow: hidden; border-top: 3px solid var(--lane); }
    .worker.remote { --lane: var(--cyan); }
    .worker.helper { --lane: var(--green); }
    .worker-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      padding: 11px 20px 10px;
      border-bottom: 1px solid var(--border);
    }
    .worker-title { margin: 0; color: var(--lane); font-size: 17px; }
    .worker-status {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--lane);
      text-transform: capitalize;
    }
    .worker.offline .worker-status { color: var(--red); }
    .worker.standby .worker-status { color: var(--amber); }
    .worker-body { padding: 12px 20px 15px; }
    .data-grid {
      display: grid;
      grid-template-columns: minmax(120px, 31%) minmax(0, 1fr);
      gap: 5px 14px;
      margin: 0;
    }
    dt { color: var(--muted); }
    dd {
      min-width: 0;
      margin: 0;
      overflow: hidden;
      color: var(--text);
      font-variant-numeric: tabular-nums;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .phase-section {
      margin-top: 11px;
      padding-top: 10px;
      border-top: 1px solid var(--border);
    }
    .phase-heading {
      margin: 0 0 6px;
      color: var(--lane);
      font-size: 13px;
      font-weight: 700;
    }
    .worker-progress {
      display: grid;
      grid-template-columns: 70px 1fr;
      gap: 12px;
      align-items: center;
      margin-top: 12px;
    }
    .worker-progress strong {
      color: var(--lane);
      font-family: var(--mono);
      font-size: 15px;
    }
    .worker .progress > span { background: var(--lane); }
    .queue-panel, .activity-panel { margin-top: 10px; overflow: hidden; }
    .section-title {
      margin: 0;
      padding: 8px 20px;
      border-bottom: 1px solid var(--border);
      color: var(--cyan);
      font-size: 16px;
    }
    .table-wrap { max-height: 255px; overflow: auto; }
    table { width: 100%; border-collapse: collapse; table-layout: fixed; }
    th, td {
      padding: 7px 12px;
      border-bottom: 1px solid #22303d;
      text-align: left;
      white-space: nowrap;
    }
    th {
      position: sticky;
      top: 0;
      z-index: 1;
      background: var(--surface-2);
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
    }
    td.filename { overflow: hidden; text-overflow: ellipsis; }
    td.numeric { font-family: var(--mono); font-variant-numeric: tabular-nums; }
    .state {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      text-transform: capitalize;
    }
    .state .status-dot { color: var(--quiet); }
    .state.encoding .status-dot { color: var(--cyan); }
    .state.transferring .status-dot,
    .state.validating .status-dot,
    .state.complete .status-dot { color: var(--green); }
    .state.failed .status-dot, .state.offline .status-dot { color: var(--red); }
    .state.committing .status-dot { color: var(--amber); }
    .row-progress { display: grid; grid-template-columns: 48px 1fr; gap: 8px; align-items: center; }
    .row-progress .progress { height: 7px; }
    .activity-list {
      display: grid;
      gap: 7px;
      max-height: 180px;
      margin: 0;
      padding: 11px 20px 14px;
      overflow: auto;
      list-style: none;
    }
    .activity {
      display: grid;
      grid-template-columns: 92px 115px minmax(0, 1fr);
      gap: 12px;
      color: var(--muted);
    }
    .activity time { font-family: var(--mono); }
    .activity .role { color: var(--text); font-weight: 650; }
    .activity .detail { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .empty { padding: 18px 20px; color: var(--quiet); }
    .error-banner {
      display: none;
      margin-bottom: 12px;
      border: 1px solid #7f3337;
      border-radius: var(--radius);
      padding: 10px 14px;
      background: #28161a;
      color: #ffb8bb;
    }
    .error-banner.visible { display: block; }
    .helper-control-banner {
      margin-bottom: 12px;
      border: 1px solid var(--border);
      border-left: 4px solid var(--amber);
      border-radius: var(--radius);
      padding: 10px 14px;
      background: #251f14;
      color: var(--text);
    }
    .helper-control-banner[hidden] { display: none; }
    @media (max-width: 900px) {
      .shell { width: min(100% - 24px, 760px); }
      header { flex-wrap: wrap; }
      .updated { order: 3; width: 100%; margin-left: 0; }
      .metrics { grid-template-columns: repeat(2, 1fr); gap: 16px 0; }
      .policy-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .metric:nth-child(3) { padding-left: 0; border-left: 0; }
      .worker-grid { grid-template-columns: 1fr; }
      table { min-width: 760px; }
    }
    @media (max-width: 540px) {
      .shell { width: calc(100% - 16px); padding-top: 14px; }
      header { gap: 12px; }
      .local-note { width: 100%; order: 2; }
      .refresh { margin-left: auto; }
      .summary { padding: 15px; }
      .metrics { grid-template-columns: 1fr 1fr; }
      .policy-strip { grid-template-columns: 1fr 1fr; }
      .metric { padding: 0 12px; }
      .metric-value { font-size: 18px; }
      #overall-percent { font-size: 30px; }
      .worker-head, .worker-body { padding-left: 14px; padding-right: 14px; }
      .activity { grid-template-columns: 78px minmax(0, 1fr); }
      .activity .detail { grid-column: 1 / -1; }
    }
    @media (prefers-reduced-motion: reduce) {
      .progress > span { transition: none; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <h1>LAN Transcode Monitor</h1>
      <span id="connection" class="live">LIVE</span>
      <span class="local-note">Local to this PC</span>
      <span id="updated" class="updated">Waiting for coordinator…</span>
      <button id="refresh" class="refresh" type="button"
              aria-pressed="false">Pause auto-refresh</button>
    </header>
    <div id="error" class="error-banner" role="alert"></div>
    <div id="helper-control-banner" class="helper-control-banner"
         role="status" hidden></div>
    <section class="summary" aria-labelledby="overall-heading">
      <div class="metrics">
        <div class="metric">
          <div id="overall-heading" class="metric-label">Overall progress</div>
          <div id="overall-percent" class="metric-value">0.0%</div>
        </div>
        <div class="metric">
          <div class="metric-label">Completed / Total</div>
          <div id="completed-total" class="metric-value">0 / 0</div>
        </div>
        <div class="metric">
          <div class="metric-label">Elapsed time</div>
          <div id="elapsed" class="metric-value">00:00:00</div>
        </div>
        <div class="metric">
          <div class="metric-label">Estimated time remaining</div>
          <div id="run-eta" class="metric-value">—</div>
        </div>
      </div>
      <div class="progress" aria-label="Overall progress">
        <span id="overall-bar"></span>
      </div>
      <div class="summary-foot">
        <span>Two-PC transcode run</span><span>·</span>
        <span id="run-status">Waiting</span><span>·</span>
        <span>Intel QSV + NVIDIA NVENC</span>
      </div>
      <div class="policy-strip" aria-label="Scheduling and validation policy">
        <div class="policy-item">
          <span class="policy-label">Scheduling</span>
          <span id="scheduling-mode" class="policy-value">Legacy dual-PC</span>
        </div>
        <div class="policy-item">
          <span class="policy-label">Scheduling state</span>
          <span id="scheduling-state" class="policy-value">Both lanes eligible</span>
        </div>
        <div class="policy-item">
          <span class="policy-label">Validation</span>
          <span id="validation-policy" class="policy-value">Legacy validation</span>
        </div>
        <div class="policy-item">
          <span class="policy-label">INSPIRON lane</span>
          <span id="remote-eligibility" class="policy-value">Eligible</span>
        </div>
      </div>
    </section>
    <section id="workers" class="worker-grid" aria-label="Worker status"></section>
    <section class="queue-panel" aria-labelledby="queue-title">
      <h2 id="queue-title" class="section-title">Queue</h2>
      <div class="table-wrap">
        <table>
          <colgroup>
            <col style="width:44%">
            <col style="width:18%">
            <col style="width:12%">
            <col style="width:12%">
            <col style="width:14%">
          </colgroup>
          <thead><tr>
            <th>Filename</th><th>Assigned lane</th><th>Size</th>
            <th>State</th><th>Progress</th>
          </tr></thead>
          <tbody id="queue-body"></tbody>
        </table>
        <div id="queue-empty" class="empty">Waiting for queue data…</div>
      </div>
    </section>
    <section class="activity-panel" aria-labelledby="activity-title">
      <h2 id="activity-title" class="section-title">Recent activity</h2>
      <ol id="activity" class="activity-list">
        <li class="empty">Waiting for activity…</li>
      </ol>
    </section>
  </main>
  <script>
  "use strict";
  const byId = (id) => document.getElementById(id);
  const state = { paused: false, timer: null };
  const clamp = (value) => Math.max(0, Math.min(100, Number(value) || 0));
  const text = (value, fallback = "—") =>
    typeof value === "string" && value.length ? value : fallback;
  const seconds = (value) => {
    const total = Math.max(0, Math.round(Number(value) || 0));
    if (!total) return "—";
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return [h, m, s].map((part) => String(part).padStart(2, "0")).join(":");
  };
  const bytes = (value) => {
    let number = Math.max(0, Number(value) || 0);
    if (!number) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let unit = 0;
    while (number >= 1024 && unit < units.length - 1) {
      number /= 1024;
      unit += 1;
    }
    return `${number.toFixed(unit > 1 ? 2 : 0)} ${units[unit]}`;
  };
  const rate = (value) => {
    const formatted = bytes(value);
    return formatted === "—" ? formatted : `${formatted}/s`;
  };
  const percent = (value) => `${clamp(value).toFixed(1)}%`;
  const setProgress = (element, value) => {
    const bounded = clamp(value);
    element.style.width = `${bounded}%`;
    element.parentElement.setAttribute("aria-valuenow", bounded.toFixed(1));
  };
  const make = (tag, className, value) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined) element.textContent = value;
    return element;
  };
  const laneLabel = (role) =>
    role === "helper" ? "HOT-BOX · NVIDIA NVENC" : "INSPIRON · Intel QSV";
  const humanize = (value, fallback = "—") => {
    const raw = text(value, "");
    if (!raw) return fallback;
    return raw
      .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
      .replace(/[_-]+/g, " ")
      .replace(/\\b\\w/g, (character) => character.toUpperCase());
  };
  function renderHelperControl(run) {
    const banner = byId("helper-control-banner");
    if (!run.helper_control_known || !run.helper_paused) {
      banner.hidden = true;
      banner.textContent = "";
      banner.removeAttribute("title");
      return;
    }
    banner.hidden = false;
    banner.title = `Read-only HOT-BOX helper status · revision ${
      Number(run.helper_control_revision) || 0
    }`;
    banner.textContent = run.remote_eligible === true
      ? "HOT-BOX PC in use · New HOT-BOX transcodes paused · " +
        "INSPIRON eligible for new work"
      : "HOT-BOX PC in use · New HOT-BOX transcodes paused · " +
        "Active file finishing before INSPIRON becomes eligible";
  }
  const phaseLabel = (value) => {
    const raw = text(value, "idle");
    const compact = raw.toLowerCase().replace(/[^a-z0-9]/g, "");
    const exact = {
      producerfullvalidation: "ProducerFullValidation",
      coordinatorintegrity: "CoordinatorIntegrity",
      postpublishintegrity: "PostPublishIntegrity"
    };
    return exact[compact] || humanize(raw, "Idle");
  };
  const phaseKey = (value) => {
    const phase = text(value, "idle").toLowerCase();
    const compact = phase.replace(/[^a-z0-9]/g, "");
    if ([
      "producerfullvalidation",
      "coordinatorintegrity",
      "postpublishintegrity"
    ].includes(compact)) return "validating";
    if (phase.includes("upload") || phase.includes("transfer")) {
      return "transferring";
    }
    if (phase.includes("validat")) return "validating";
    if (phase.includes("publish") || phase.includes("commit")) {
      return "committing";
    }
    if (phase.includes("encod")) return "encoding";
    if (phase.includes("complete")) return "complete";
    return phase;
  };
  const workerProgress = (worker) => {
    const phase = phaseKey(worker.phase);
    if (phase === "transferring" && worker.transfer_total_bytes) {
      return worker.transfer_bytes / worker.transfer_total_bytes * 100;
    }
    if (worker.duration_seconds) {
      return worker.media_seconds / worker.duration_seconds * 100;
    }
    return ["validating", "committing", "complete"].includes(phase)
      ? 100 : 0;
  };
  function dataRow(list, label, value) {
    list.append(make("dt", "", label), make("dd", "", value));
  }
  function workerCard(worker) {
    const role = worker.role === "helper" ? "helper" : "remote";
    const idle = ["idle", "pcinusepaused", "standby"].includes(
      phaseKey(worker.phase)
    ) && !worker.current_filename;
    const pcInUsePaused = Boolean(
      role === "helper" && worker.pc_in_use_paused === true && idle
    );
    const standby = Boolean(
      worker.online &&
      worker.eligible_for_new_work === false &&
      !worker.current_filename
    );
    const stateClass = pcInUsePaused
      ? " standby"
      : worker.online
      ? (standby ? " standby" : "")
      : " offline";
    const card = make("article", `worker ${role}${stateClass}`);
    const head = make("div", "worker-head");
    const legacyLabel = role === "helper"
      ? "Helper PC · NVIDIA NVENC"
      : "This PC · Intel QSV";
    const suppliedLabel = text(worker.label, "");
    head.append(
      make(
        "h2",
        "worker-title",
        !suppliedLabel || suppliedLabel === legacyLabel
          ? laneLabel(role)
          : suppliedLabel
      )
    );
    const statusText = pcInUsePaused
      ? "PC in use · Paused"
      : !worker.online
      ? (role === "remote" ? "INSPIRON Offline" : "HOT-BOX Offline")
      : standby
        ? (role === "remote" ? "INSPIRON Standby" : "HOT-BOX Standby")
        : phaseLabel(worker.phase);
    const status = make(
      "span",
      "worker-status",
      statusText
    );
    status.prepend(make("span", "status-dot"));
    head.append(status);
    const body = make("div", "worker-body");
    const details = make("dl", "data-grid");
    dataRow(details, "Current file", text(worker.current_filename, "No active file"));
    dataRow(details, "Source size", bytes(worker.source_size_bytes));
    dataRow(
      details,
      "Time (media)",
      `${seconds(worker.media_seconds)} / ${seconds(worker.duration_seconds)}`
    );
    dataRow(
      details,
      "Conversion",
      worker.encode_speed_ratio
        ? `${seconds(worker.encode_elapsed_seconds)} · ${worker.encode_speed_ratio.toFixed(2)}x`
        : seconds(worker.encode_elapsed_seconds)
    );
    dataRow(details, "Conversion ETA", seconds(worker.encode_eta_seconds));
    if (pcInUsePaused) {
      dataRow(details, "New work", "Paused while HOT-BOX PC is in use");
    } else if (worker.eligible_for_new_work === false) {
      dataRow(
        details,
        "New work",
        text(worker.standby_reason, "Standby")
      );
    }
    body.append(details);
    if (
      phaseKey(worker.phase) === "transferring" ||
      worker.transfer_total_bytes ||
      worker.transfer_bytes
    ) {
      const transfer = make("section", "phase-section");
      transfer.append(make("h3", "phase-heading", "Network transfer"));
      const transferDetails = make("dl", "data-grid");
      dataRow(
        transferDetails,
        "Transferred",
        `${bytes(worker.transfer_bytes)} / ${bytes(worker.transfer_total_bytes)}`
      );
      dataRow(
        transferDetails,
        "Transfer speed",
        rate(worker.transfer_bytes_per_second)
      );
      dataRow(
        transferDetails,
        "Transfer ETA",
        seconds(worker.transfer_eta_seconds)
      );
      transfer.append(transferDetails);
      body.append(transfer);
    }
    const progressValue = workerProgress(worker);
    const progress = make("div", "worker-progress");
    progress.append(make("strong", "", percent(progressValue)));
    const track = make("div", "progress");
    track.setAttribute("role", "progressbar");
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", "100");
    const fill = make("span");
    track.append(fill);
    progress.append(track);
    body.append(progress);
    setProgress(fill, progressValue);
    card.append(head, body);
    return card;
  }
  function renderWorkers(workers, run) {
    const values = Array.isArray(workers) ? workers.slice() : [];
    for (const role of ["remote", "helper"]) {
      if (!values.some((worker) => worker.role === role)) {
        values.push({
          role,
          online: false,
          phase: "idle",
          eligible_for_new_work: role === "helper"
            ? run.helper_eligible !== false
            : run.remote_eligible !== false
        });
      }
    }
    values.sort((a, b) =>
      (a.role === "remote" ? 0 : 1) - (b.role === "remote" ? 0 : 1)
    );
    byId("workers").replaceChildren(
      ...values.slice(0, 2).map(workerCard)
    );
  }
  function renderQueue(queue) {
    const body = byId("queue-body");
    const empty = byId("queue-empty");
    const rows = (Array.isArray(queue) ? queue : []).map((item) => {
      const row = document.createElement("tr");
      const filename = make("td", "filename", text(item.filename));
      filename.title = text(item.filename, "");
      const role = make(
        "td",
        "",
        item.worker_role ? laneLabel(item.worker_role) : "Unassigned"
      );
      const size = make("td", "numeric", bytes(item.size_bytes));
      const stateCell = document.createElement("td");
      const stateName = phaseKey(item.state);
      const status = make("span", `state ${stateName}`, phaseLabel(item.state));
      status.prepend(make("span", "status-dot"));
      stateCell.append(status);
      const progressCell = document.createElement("td");
      const progress = make("div", "row-progress");
      progress.append(make("span", "numeric", percent(item.progress_percent)));
      const track = make("div", "progress");
      track.setAttribute("role", "progressbar");
      track.setAttribute("aria-valuemin", "0");
      track.setAttribute("aria-valuemax", "100");
      const fill = make("span");
      track.append(fill);
      setProgress(fill, item.progress_percent);
      progress.append(track);
      progressCell.append(progress);
      row.append(filename, role, size, stateCell, progressCell);
      return row;
    });
    body.replaceChildren(...rows);
    empty.hidden = rows.length > 0;
  }
  function renderActivity(recent) {
    const entries = (Array.isArray(recent) ? recent : []).map((item) => {
      const entry = make("li", "activity");
      const date = item.timestamp_utc
        ? new Date(item.timestamp_utc * 1000)
        : null;
      const timestamp = make(
        "time",
        "",
        date ? date.toLocaleTimeString() : "—"
      );
      const role = make(
        "span",
        "role",
        item.worker_role ? laneLabel(item.worker_role).split(" · ")[0] : "Queue"
      );
      const detailText = [text(item.event, "Updated"), text(item.filename, "")]
        .filter(Boolean).join(" — ");
      const detail = make("span", "detail", detailText);
      detail.title = detailText;
      entry.append(timestamp, role, detail);
      return entry;
    });
    byId("activity").replaceChildren(
      ...(entries.length
        ? entries
        : [make("li", "empty", "No recent activity yet.")])
    );
  }
  function render(snapshot) {
    const run = snapshot.run || {};
    const totals = snapshot.totals || {};
    const total = Number(totals.total_jobs) || 0;
    const complete = Number(totals.completed) || 0;
    const overall = total ? complete / total * 100 : 0;
    byId("overall-percent").textContent = percent(overall);
    byId("completed-total").textContent = `${complete} / ${total}`;
    byId("elapsed").textContent = seconds(run.elapsed_seconds);
    byId("run-eta").textContent = seconds(run.eta_seconds);
    byId("run-status").textContent = text(run.status, "Unknown");
    const preferred = run.preferred_worker_role
      ? laneLabel(run.preferred_worker_role).split(" · ")[0]
      : "";
    const schedulingMode = humanize(
      run.scheduling_mode,
      "Legacy dual-PC"
    );
    byId("scheduling-mode").textContent = preferred
      ? `${schedulingMode} · ${preferred} preferred`
      : schedulingMode;
    byId("scheduling-state").textContent = humanize(
      run.scheduling_state,
      "Both lanes eligible"
    );
    byId("validation-policy").textContent = humanize(
      run.validation_policy,
      "Legacy validation"
    );
    const fallbackSeconds = Number(run.fallback_countdown_seconds) || 0;
    byId("remote-eligibility").textContent = run.remote_eligible === true
      ? "INSPIRON eligible"
      : fallbackSeconds > 0
        ? `${seconds(fallbackSeconds)} until INSPIRON eligible`
        : "INSPIRON not eligible";
    renderHelperControl(run);
    setProgress(byId("overall-bar"), overall);
    renderWorkers(snapshot.workers, run);
    renderQueue(snapshot.queue);
    renderActivity(snapshot.recent);
    const updated = snapshot.generated_utc
      ? new Date(snapshot.generated_utc * 1000)
      : new Date();
    byId("updated").textContent = `Last updated: ${updated.toLocaleTimeString()}`;
    byId("connection").textContent = "LIVE";
    byId("connection").style.color = "var(--green)";
    byId("error").classList.remove("visible");
  }
  async function refresh() {
    if (state.paused) return;
    try {
      const response = await fetch("/api/snapshot", {
        cache: "no-store",
        credentials: "omit",
        headers: { Accept: "application/json" }
      });
      if (!response.ok) throw new Error("snapshot unavailable");
      render(await response.json());
    } catch (_error) {
      byId("connection").textContent = "DISCONNECTED";
      byId("connection").style.color = "var(--red)";
      byId("error").textContent =
        "Detailed coordinator data is temporarily unavailable. Retrying locally…";
      byId("error").classList.add("visible");
    }
  }
  byId("refresh").addEventListener("click", () => {
    state.paused = !state.paused;
    byId("refresh").setAttribute("aria-pressed", String(state.paused));
    byId("refresh").textContent = state.paused
      ? "Resume auto-refresh" : "Pause auto-refresh";
    if (!state.paused) refresh();
  });
  refresh();
  state.timer = window.setInterval(refresh, 1500);
  </script>
</body>
</html>
"""


class _DashboardHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def _handler_factory(
    provider: DashboardSnapshotProvider,
    bind_host: str,
) -> type[BaseHTTPRequestHandler]:
    lan_mode = bind_host != LOOPBACK_HOST

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "LANTranscodeDashboard"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *args: object) -> None:
            del args

        def _request_is_allowed(self) -> bool:
            try:
                client = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                return False
            if lan_mode:
                if client.version != 4 or not (client.is_private or client.is_loopback):
                    return False
            elif str(client) != LOOPBACK_HOST:
                return False
            host_header = self.headers.get("Host", "")
            if not host_header or "," in host_header:
                return False
            try:
                parsed_host = urlsplit(f"//{host_header}")
                host = (parsed_host.hostname or "").lower()
                port = parsed_host.port
            except ValueError:
                return False
            allowed_hosts = {bind_host}
            if not lan_mode:
                allowed_hosts.add("localhost")
            if host not in allowed_hosts:
                return False
            if port is not None and port != self.server.server_port:
                return False
            origin = self.headers.get("Origin")
            if origin:
                try:
                    parsed_origin = urlsplit(origin)
                    origin_host = (parsed_origin.hostname or "").lower()
                    origin_port = parsed_origin.port
                except ValueError:
                    return False
                if (
                    parsed_origin.scheme != "http"
                    or origin_host not in allowed_hosts
                    or origin_port != self.server.server_port
                ):
                    return False
            return True

        def _headers(self, status: int, content_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; connect-src 'self'; "
                "img-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
                "form-action 'none'",
            )
            self.send_header(
                "Permissions-Policy",
                "camera=(), microphone=(), geolocation=(), payment=()",
            )
            self.send_header("Connection", "close")
            self.end_headers()

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self._headers(status, content_type, len(body))
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_error(self, status: int, category: str) -> None:
            body = json.dumps(
                {"error": category},
                separators=(",", ":"),
            ).encode("utf-8")
            self._send(status, "application/json; charset=utf-8", body)

        def _handle(self) -> None:
            if not self._request_is_allowed():
                self._send_error(403, "DashboardAccessDenied")
                return
            path = urlsplit(self.path).path
            if path in {"/", "/index.html"}:
                self._send(
                    200,
                    "text/html; charset=utf-8",
                    _DASHBOARD_HTML.encode("utf-8"),
                )
                return
            if path == "/api/snapshot":
                snapshot_method = getattr(
                    provider,
                    "dashboard_snapshot",
                    None,
                )
                if not callable(snapshot_method):
                    self._send_error(503, "SnapshotUnavailable")
                    return
                try:
                    snapshot = normalize_dashboard_snapshot(
                        snapshot_method()
                    )
                    body = json.dumps(
                        snapshot,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                except Exception:
                    self._send_error(503, "SnapshotUnavailable")
                    return
                self._send(
                    200,
                    "application/json; charset=utf-8",
                    body,
                )
                return
            if path == "/health":
                self._send(
                    200,
                    "application/json; charset=utf-8",
                    b'{"status":"ok"}',
                )
                return
            self._send_error(404, "NotFound")

        def do_GET(self) -> None:  # noqa: N802
            self._handle()

        def do_HEAD(self) -> None:  # noqa: N802
            self._handle()

        def do_POST(self) -> None:  # noqa: N802
            try:
                content_length = int(
                    self.headers.get("Content-Length", "0"),
                    10,
                )
            except ValueError:
                content_length = 0
            if 0 < content_length <= 4096:
                self.rfile.read(content_length)
            self._send_error(405, "MethodNotAllowed")

    return DashboardHandler


class DashboardServer:
    """Serve the detailed monitor on loopback or an explicit private IPv4."""

    def __init__(
        self,
        *,
        provider: DashboardSnapshotProvider,
        port: int = DEFAULT_DASHBOARD_PORT,
        host: str = LOOPBACK_HOST,
    ) -> None:
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 0 <= port <= 65535
        ):
            raise DashboardError("DashboardPortInvalid")
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise DashboardError("DashboardHostInvalid") from exc
        if (
            address.version != 4
            or address.is_unspecified
            or address.is_multicast
            or (not address.is_private and not address.is_loopback)
            or (address.is_loopback and host != LOOPBACK_HOST)
        ):
            raise DashboardError("DashboardHostInvalid")
        bind_host = str(address)
        try:
            self._server = _DashboardHttpServer(
                (bind_host, port),
                _handler_factory(provider, bind_host),
            )
        except OSError as exc:
            raise DashboardError("DashboardBindFailed") from exc
        host, bound_port = self._server.server_address[:2]
        if host != bind_host:
            self._server.server_close()
            raise DashboardError("DashboardBindingFailed")
        self._host = bind_host
        self._port = int(bound_port)
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self.port}/"

    def start(self) -> None:
        if self._closed:
            raise DashboardError("DashboardClosed")
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="lan-dashboard-http",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        if self._closed:
            return
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5.0)
        self._server.server_close()
        self._closed = True

    def __enter__(self) -> "DashboardServer":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = [
    "DEFAULT_DASHBOARD_PORT",
    "DashboardError",
    "DashboardServer",
    "DashboardSnapshotProvider",
    "LOOPBACK_HOST",
    "normalize_dashboard_snapshot",
]
