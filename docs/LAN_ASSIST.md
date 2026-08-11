# Personal LAN Assist edition

`VideoTranscoderLanAssist.exe` is a separate, opt-in companion to the generic
`VideoTranscoderPortable.exe`. The generic portable app keeps its normal GUI
and does not start a network service.

The LAN edition uses one external `VideoTranscoderLanAssist.json` beside the
EXE (or a file selected with `--config`). No hostname, media path, or bearer
token is embedded in the executable.

## Roles

- The storage PC runs `mode: "coordinator"`, owns the queue and transactional
  publisher, and is the only PC allowed to delete an original. With
  `scheduling_mode: "prefer-helper"`, its Intel QSV encoder remains on standby
  while the helper is available.
- The helper PC runs `mode: "helper"`, converts the largest remaining file
  with NVIDIA NVENC, uploads an attempt-specific candidate, and never publishes
  a final media name or deletes a remote original.
- If the helper loses the LAN connection, its remote attempt is cancelled and
  fenced. It then completes one file from its configured local fallback folder,
  retains that original, and reconnects with capped exponential backoff.
- SMB access or logon failures are terminal for that helper run. The helper
  abandons any active claim and exits without falling back or retrying the
  credential.
- After a complete SMB upload, only Windows sharing/byte-range lock errors 32
  and 33 are retried. Flush, verification-open, and publish each receive one
  independent 10-second window while the same lease heartbeat, attempt path,
  and captured identity remain in force. Authentication, network, disk,
  identity, hash, cancellation, and every unclassified error still fail
  immediately.

The current site layout uses INSPIRON as the coordinator and HOT-BOX as the
primary, headless NVENC helper. The tray-equipped XPS deployment described
below is an optional travel fallback that substitutes for HOT-BOX; it is not a
second simultaneous helper.

Control traffic is an authenticated loopback HTTP API carried through an SSH
local forward. The coordinator exposes each assigned source through an opaque,
attempt-scoped hard-link alias in its private staging folder. Media reads and
candidate uploads therefore use only the configured staging SMB path; the
helper never receives the original filename.

Worker heartbeat phases use one shared allowlist from claim through upload
verification and publishing, so the HTTP dispatcher cannot reject a phase the
coordinator already recognizes. Helper presence is only a scheduling signal:
missing the shorter presence window makes the helper appear offline but does
not fence a still-valid lease. A lease is fenced only when its authoritative
deadline expires or the worker explicitly abandons or reports failure.

## Helper-preferred scheduling and producer validation

Set `scheduling_mode` to `prefer-helper` to give the configured helper/NVENC
lane exclusive encoding preference. In the current site layout that helper is
HOT-BOX. The coordinator waits `helper_startup_grace_seconds` before first
using INSPIRON, then waits `helper_fallback_after_seconds` from the last
authenticated helper contact before fallback. If the configured helper returns
during fallback, new INSPIRON claims stop immediately and the helper must remain
present for `helper_recovery_stable_seconds` before its next claim. An already
leased encode is never preempted, and an unresolved suspect attempt blocks the
other encoder. `balanced` preserves the two-lane size-aware scheduler;
`local-only` disables helper claims.

Set `validation_policy` to `producer-full` to perform the complete video and
every-audio-stream decode on the PC that encoded the candidate. That worker
submits path-free evidence bound to the run, job, worker role, attempt, fencing
epoch, media contract, executable, FFmpeg, FFprobe, candidate hash and size,
and decoded frame/stream results. The coordinator verifies those bindings,
metadata, exact file identity and hashes, atomic publish, and the post-publish
hash without repeating the full decode. `redundant-full` retains the legacy
coordinator re-decode. Missing or mismatched producer evidence fails closed and
leaves the source untouched.

## Optional interactive XPS control and always-live service

HOT-BOX normally runs headless through its site-pinned scheduled task and does
not require the interactive tray. This section applies when the optional
travel/XPS helper is intentionally selected instead.

The helper configuration uses the exact local sibling files
`helper-control.json` and `helper-control-status.json`. Its lowercase 32-64 hex
`control_id` must exactly match the coordinator's `helper_control_id`; the
example pair uses one shared 32-hex UUID. Missing, malformed, redirected, or
linked control state fails closed.

`VideoTranscoderLanTray.exe` gives the signed-in XPS user two explicit choices:
`Pause XPS after current file` and `Resume XPS transcoding`. Pause atomically
persists `pc_in_use` without stopping FFmpeg or cancelling a lease. If a file is
active, the helper finishes that complete fenced transaction, including
producer-full validation, upload, coordinator integrity checks, and submit,
then acknowledges `paused` and accepts no new claim. Pause also asks Task
Scheduler to run the fixed helper task once, so an idle or rebooted XPS can
announce its persisted pause. At that safe boundary the coordinator marks XPS
paused and makes INSPIRON eligible immediately for the next claim. Resume
atomically persists `available` and asks the same manual task to run once
without elevation.

The tray polls the helper-owned acknowledgement file. Green means XPS is
available or working, amber means a pause is draining or a command is awaiting
acknowledgement, blue means XPS is paused and INSPIRON may fall back, red means
the local control channel is blocked, and gray means no trustworthy state is
available. Stale acknowledgements are never presented as current. Exiting the
tray closes only its UI; it does not change the persisted choice or stop the
helper. The tray never opens the bearer token file.

`keep_alive_when_complete: true` keeps the coordinator service, dashboard, and
API online after the current queue has no pending or active jobs and no
failures. It does not rescan the media folder or start a new run; restart the
coordinator when a newly discovered batch is required. The backward-compatible
default is `false`, which exits successfully when the queue completes.

The coordinator media root and work root must be on the same local NTFS
volume so those no-copy aliases and final transactional renames remain atomic.

## Dashboard

On the storage PC, open `http://127.0.0.1:41802/` while the coordinator is
running. The loopback-only dashboard shows:

- the current filename and phase for both PCs;
- conversion progress, speed, and ETA;
- upload bytes, transfer rate, and transfer ETA;
- queue, completion/failure totals, and recent activity.

Filename details are not returned by the helper API or written to the aggregate
status file.

The helper can also maintain a local rotating JSONL event history with
`event_log_file`, `event_log_max_bytes`, and `event_log_backup_count`.
`event_log_file`, when enabled, must be the exact local
`helper-events.jsonl` file beside the helper configuration. Each
record contains only fixed status tokens, size/timing counters, a fixed
operation phase, an allow-listed transport category, numeric Windows error,
retry count, and UTC timestamp. It never records a media name, path, hostname,
user name, exception message, SSH
destination, or token. Repeated idle/waiting states are deduplicated; completed
attempts and terminal states are retained. The log path must be local and must
not be a reparse point or hard link.

Keep the helper runtime folder outside a directory mirrored by a background
sync client, or pause/exclude that directory while the helper runs. Some sync
clients create temporary hard links while uploading a changed log; the helper
intentionally rejects that identity instead of writing through the second
name.

## Resume and failure behavior

Completed output identities are recorded in a durable ledger and skipped after
a restart. An interrupted encode restarts that file from the beginning; FFmpeg
hardware HEVC output cannot safely resume in the middle of a file. Transaction
journals are recovered by verified rollback or roll-forward. Ambiguous identity
or hash state remains fail-closed. After the old run marker proves no worker is
still attached, restart cleanup removes only strict, attempt-shaped orphan
candidate names; unrelated files are never glob-deleted.

An isolated media failure leaves that source untouched and advances the queue.
The coordinator stops after three consecutive terminal failures.

## Build

Install the GUI, portable-build, and test dependencies before building. The
tray executable requires both Pillow and pystray, so a base-only installation
is not sufficient:

```powershell
python -m pip install -e ".[gui,portable,dev]"
powershell -NoProfile -File .\scripts\build_lan_assist.ps1
```

The result contains `VideoTranscoderLanAssist.exe`, the separate windowed
`VideoTranscoderLanTray.exe`, the fail-safe coordinator launcher, the
travel-safe helper launcher, and the interactive share connector. The build
runs a read-only packaged tray self-test and records independent SHA-256 hashes
for both executables. Start from the coordinator or helper example JSON in
`packaging\portable`, then create one shared token without printing it:

```powershell
.\VideoTranscoderLanAssist.exe --config .\VideoTranscoderLanAssist.json --create-token
```

Copy that token file securely to the other PC and reference the local copy from
each configuration. Token creation is allowed only on local storage. On
Windows, the app removes ACL inheritance, grants full control only to the
current user, verifies that exact protected DACL, and deletes the new token if
hardening or verification fails. UNC paths and mapped network drives are
rejected; copy the token into an owner-only local path on each PC rather than
using one token directly from a share. OpenSSH key or agent authentication must
already work in batch mode.

## Jellyfin-safe coordinator startup

Run the coordinator through `Start-Coordinator.ps1`. It validates the
coordinator configuration before launch. If and only if startup terminates
with exit code 1 and the exact terminal tuple `ServiceStopped`, `Failed`, and
`ResumeSourceBusy`, the launcher waits 60 seconds and tries again. The default
limit is 10 retries (11 total attempts).

The launcher does not stop or restart Jellyfin, skip a busy source, alter a
media file, or retry any other failure category. `SourceChanged`, active resume
workers, malformed output, and all other failures remain fail-closed. When the
busy retry limit is exhausted, the launcher writes a path-free
`coordinator-startup-status.json` beside the configuration and exits with the
coordinator's failure code.

The coordinator scheduled task must not also have a generic
`RestartOnFailure` policy. The launcher owns this one narrowly classified
retry; Task Scheduler retries could repeat unrelated failures or multiply the
bounded busy retry window.

Deploy a reviewed coordinator configuration to the fixed `codex-remote`
production target with the guarded coordinator deployer. It stages and
validates the complete release, preserves the token, verifies the exact media
and work-root identities, normalizes the scheduled task to no automatic
restart and no execution-time limit, and leaves the task stopped for an
intentional start:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\Deploy-LanCoordinatorSafely.ps1 `
  -CoordinatorConfigPath .\path\to\reviewed-coordinator.json `
  -ExpectedWorkRoot 'D:\Development\VideoTranscoderToolchain\jobs\stuff-distributed'
```

## Optional travel/XPS helper startup

Deploy this bundle only when intentionally substituting the tray-equipped XPS
for HOT-BOX. Use the same control identity configured on INSPIRON:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\Deploy-TravelSafeLanHelper.ps1 `
  -ControlId b37609d869b84f8b8e05744ee428adb2
```

The deployer verifies both release hashes, preserves the token and existing
control/status files, and does not start either executable. It keeps
`VideoTranscoder LAN Helper` manual-only with no triggers, no restart policy,
`StartWhenAvailable` disabled, no execution-time limit, and one exact limited
same-user interactive action. It separately creates
`VideoTranscoder LAN Tray` as one limited, same-user interactive at-logon task.
The tray is UI only; its `Exit tray` item does not stop the hidden helper or
alter the persisted control state.

When the tray starts, it asks the fixed helper task to start once for either
persisted choice. With `available`, the helper may claim work. With `pc_in_use`,
it announces the pause and idles without claiming. There is no automatic task
retry. If the non-persistent SMB session is not ready yet, establish it from an
interactive PowerShell window and choose `Retry helper now` or resume again:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Connect-LanHelperShare.ps1
```

The connector first proves the SSH host is reachable using public-key-only
authentication. It then uses the Windows secure credential prompt once, passes
the password to `net use` through standard input, and never stores or prints the
password. Enter the account password, not the Windows Hello PIN. The connection
uses `/persistent:no` and is validated against the exact configured staging
path. A failed attempt is cleaned up and is never retried automatically.

After the connector reports `Ready`, choose `Resume XPS transcoding` (or
`Retry helper now` when already available). The tray uses shell-free
`schtasks.exe /Run` for only `VideoTranscoder LAN Helper`; it does not request
elevation. The task's `Start-LanAssist.ps1` action validates configuration
offline, performs one public-key-only SSH probe, requires the already
established exact SMB session, and probes staging once before launching the
helper. It never asks for credentials or creates an SMB mapping.

Keep coordinator state and staging outside every bidirectional sync root. A
sync engine opening a newly uploaded candidate can conflict with the helper's
exclusive identity/flush proof; syncing mutable coordinator state can also
create conflict copies that are not valid coordination records.

Use `reconnect_initial_seconds`, `reconnect_max_seconds`, and
`reconnect_multiplier` to control reconnects. The helper example uses 30
seconds, a 600-second cap, and a multiplier of 4. Successful coordinator work
resets the delay. Network loss backs off; SMB access or HTTP token
authentication failure stops that run immediately.

Terminal helper and launcher block states return a nonzero process result so
Task Scheduler cannot report a blocked run as successful. This is monitoring
evidence only: the task remains manual-only with no automatic restart policy.

For schema-version-1 compatibility, `reconnect_seconds` is still accepted when
the new initial-delay key is absent. It is interpreted as the initial delay,
not as a fixed retry interval.
